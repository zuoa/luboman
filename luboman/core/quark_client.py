"""夸克网盘 API 客户端。

上传流程对齐 2026 年官方 PC Web（drive-pc.quark.cn）：
pre（parallel_upload）→ 分片 PUT（OSS + X-Oss-Hash-Ctx）→ hash 确认会话 → commit → finish。

仅依赖 requests，不依赖 luboman 运行时（config/DB），可独立单测。
"""

import base64
import email.utils
import hashlib
import html
import itertools
import json
import logging
import mimetypes
import os
import struct
import time

import requests

logger = logging.getLogger('luboman')

# PC Web 走 drive-pc；旧 drive.quark.cn 仍能建目录，但预上传/分片会失败
API = 'https://drive-pc.quark.cn/1/clouddrive'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36')
REFERER = 'https://pan.quark.cn'
ORIGIN = 'https://pan.quark.cn'
# 签名串（auth_meta）与 OSS 请求头共用的固定 UA，逐字符敏感，勿改
OSS_UA = 'aliyun-sdk-js/1.0.0 Chrome 145.0.0.0 on Windows 10 64-bit'
ROOT_FID = '0'
# urllib3 会把 timeout 元组的 connect 套到 socket.sendall 上；
# (10, 300) 会让大分片在写到一半时被掐断。OSS 直传用单一长超时。
OSS_TIMEOUT = 1800

# 响应 Set-Cookie 里需要合并回 cookie 的续期字段
_COOKIE_REFRESH_KEYS = ('__puus', '__pus')
# 判定 cookie 失效的 message 关键词
_AUTH_ERR_HINTS = ('登录', 'login', 'token')


class QuarkApiError(Exception):
    """夸克 API 业务/网络错误。"""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class QuarkAuthError(QuarkApiError):
    """Cookie 失效（HTTP 412 或服务端提示重新登录）。"""


def _set_cookie_kv(cookie_str, name, value):
    """在 'k=v; k=v' 形式的 cookie 字符串里覆盖一个键。"""
    pairs = [p.strip() for p in cookie_str.split(';') if p.strip()]
    kv = dict(p.split('=', 1) for p in pairs if '=' in p)
    kv[name] = value
    return '; '.join(f'{k}={v}' for k, v in kv.items())


def _oss_date():
    """OSS V1 签名用的 GMT 日期，固定英文月份（locale 无关）。"""
    return email.utils.formatdate(time.time(), usegmt=True)


class _Sha1Running:
    """可导出内部中间态的 SHA-1，供 X-Oss-Hash-Ctx 使用。

    OSS 要的是处理完完整 64 字节块之后的 h0-h4，不是 finalized digest。
    """

    _H0 = (0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0)

    def __init__(self):
        self.h = list(self._H0)
        self.buf = b''
        self.nbytes = 0

    def update(self, data):
        if not data:
            return
        self.nbytes += len(data)
        data = self.buf + data
        offset = 0
        end = len(data) - (len(data) % 64)
        while offset < end:
            self._block(data[offset:offset + 64])
            offset += 64
        self.buf = data[offset:]

    def _block(self, block):
        w = list(struct.unpack('>16I', block))
        for i in range(16, 80):
            w.append(_rol32(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1))
        a, b, c, d, e = self.h
        for i in range(80):
            if i < 20:
                f = (b & c) | ((~b) & d)
                k = 0x5A827999
            elif i < 40:
                f = b ^ c ^ d
                k = 0x6ED9EBA1
            elif i < 60:
                f = (b & c) | (b & d) | (c & d)
                k = 0x8F1BBCDC
            else:
                f = b ^ c ^ d
                k = 0xCA62C1D6
            temp = (_rol32(a, 5) + f + e + k + w[i]) & 0xFFFFFFFF
            e, d, c, b, a = d, c, _rol32(b, 30), a, temp
        self.h = [
            (self.h[0] + a) & 0xFFFFFFFF,
            (self.h[1] + b) & 0xFFFFFFFF,
            (self.h[2] + c) & 0xFFFFFFFF,
            (self.h[3] + d) & 0xFFFFFFFF,
            (self.h[4] + e) & 0xFFFFFFFF,
        ]

    def snapshot(self):
        """当前完整块之后的 Hash-Ctx（忽略未满 64 字节的尾巴，对齐官方实现）。"""
        total_bits = self.nbytes * 8
        return {
            'hash_type': 'sha1',
            'h0': str(self.h[0]),
            'h1': str(self.h[1]),
            'h2': str(self.h[2]),
            'h3': str(self.h[3]),
            'h4': str(self.h[4]),
            'Nl': str(total_bits & 0xFFFFFFFF),
            'Nh': str(total_bits >> 32),
            'data': '',
            'num': '0',
        }

    def hexdigest(self):
        """标准 SHA-1 终态，仅用于单测对照 hashlib。"""
        clone = _Sha1Running()
        clone.h = list(self.h)
        clone.buf = self.buf
        clone.nbytes = self.nbytes
        bit_len = clone.nbytes * 8
        clone.update(b'\x80')
        while (clone.nbytes % 64) != 56:
            clone.update(b'\x00')
        clone.update(struct.pack('>Q', bit_len))
        return ''.join(f'{w:08x}' for w in clone.h)


def _rol32(value, bits):
    return ((value << bits) | (value >> (32 - bits))) & 0xFFFFFFFF


def encode_hash_ctx(ctx):
    """Hash-Ctx JSON → base64，字段顺序与官方客户端一致。"""
    payload = json.dumps(ctx, separators=(',', ':'), ensure_ascii=True)
    return base64.b64encode(payload.encode('ascii')).decode('ascii')


class QuarkClient:
    def __init__(self, cookie):
        self.cookie = (cookie or '').strip()
        self.session = requests.Session()
        self.cookie_changed = False
        self._dir_cache = {}    # 相对路径 -> fid
        self._files_cache = {}  # pdir_fid -> list[file dict]

    # ------------------------------------------------------------------
    # 基础请求
    # ------------------------------------------------------------------

    def _request(self, path, method='GET', params=None, json_body=None):
        """所有夸克 API 调用的唯一入口：注入公共参数、合并续期 cookie、统一报错。"""
        query = {'pr': 'ucpro', 'fr': 'pc'}
        if params:
            query.update(params)
        headers = {
            'Cookie': self.cookie,
            'Accept': 'application/json, text/plain, */*',
            'Referer': REFERER,
            'Origin': ORIGIN,
            'User-Agent': UA,
        }
        try:
            resp = self.session.request(
                method, API + path, params=query, json=json_body,
                headers=headers, timeout=(10, 60),
            )
        except requests.RequestException as e:
            raise QuarkApiError(f'网络请求失败: {e}') from e

        # cookie 续期：服务端在响应里下发新的 __puus/__pus，合并回存储的 cookie
        for name in _COOKIE_REFRESH_KEYS:
            value = resp.cookies.get(name)
            if value:
                self.cookie = _set_cookie_kv(self.cookie, name, value)
                self.cookie_changed = True

        if resp.status_code == 412:
            raise QuarkAuthError('Cookie 已失效（412），请重新获取', code=412)

        try:
            data = resp.json()
        except ValueError:
            raise QuarkApiError(f'响应非 JSON（HTTP {resp.status_code}）: {resp.text[:200]}')

        if resp.status_code >= 400 or data.get('code', 0) != 0:
            message = data.get('message') or f'HTTP {resp.status_code}'
            err_cls = QuarkAuthError if any(h in message.lower() for h in _AUTH_ERR_HINTS) else QuarkApiError
            raise err_cls(message, code=data.get('code'))
        return data

    # ------------------------------------------------------------------
    # 探活 / 目录
    # ------------------------------------------------------------------

    def check_cookie(self):
        """探活：cookie 无效时抛 QuarkAuthError/QuarkApiError。"""
        self._request('/config')

    def list_dir(self, pdir_fid):
        """分页列出目录内容，每项含 fid/file_name/file/size。file_name 已 html.unescape。"""
        if pdir_fid in self._files_cache:
            return self._files_cache[pdir_fid]
        files = []
        page = 1
        while True:
            data = self._request('/file/sort', params={
                'pdir_fid': pdir_fid,
                '_page': page,
                '_size': 100,
                '_fetch_total': '1',
                'fetch_all_file': '1',
                'fetch_risk_file_name': '1',
            })
            items = (data.get('data') or {}).get('list') or []
            for item in items:
                item['file_name'] = html.unescape(item.get('file_name') or '')
            files.extend(items)
            total = ((data.get('metadata') or {}).get('_total')) or 0
            if page * 100 >= total or not items:
                break
            page += 1
        self._files_cache[pdir_fid] = files
        return files

    def mkdir(self, pdir_fid, name):
        """创建目录，返回 fid。夸克写入有延迟一致性，成功 sleep 1s。"""
        data = self._request('/file', method='POST', json_body={
            'dir_init_lock': False,
            'dir_path': '',
            'file_name': name,
            'pdir_fid': pdir_fid,
        })
        self._files_cache.pop(pdir_fid, None)
        time.sleep(1)
        return (data.get('data') or {})['fid']

    def ensure_dir(self, remote_path):
        """逐级 get-or-create 目录，返回末级 fid。remote_path 形如 'video/douyin/房间/2026-08-10'。"""
        remote_path = remote_path.strip('/').replace('\\', '/')
        if not remote_path:
            return ROOT_FID
        if remote_path in self._dir_cache:
            return self._dir_cache[remote_path]
        fid = ROOT_FID
        walked = []
        for seg in remote_path.split('/'):
            if not seg:
                continue
            walked.append(seg)
            key = '/'.join(walked)
            if key in self._dir_cache:
                fid = self._dir_cache[key]
                continue
            hit = next((f for f in self.list_dir(fid)
                        if not f.get('file') and f['file_name'] == seg), None)
            fid = hit['fid'] if hit else self.mkdir(fid, seg)
            self._dir_cache[key] = fid
        return fid

    def find_unique_name(self, pdir_fid, file_name):
        """夸克不支持覆盖上传，同名时改为 'stem (n).ext'。"""
        names = {f['file_name'] for f in self.list_dir(pdir_fid)}
        if file_name not in names:
            return file_name
        stem, ext = os.path.splitext(file_name)
        for n in itertools.count(1):
            candidate = f'{stem} ({n}){ext}'
            if candidate not in names:
                return candidate

    # ------------------------------------------------------------------
    # 上传（5 步流程，底层阿里 OSS multipart）
    # ------------------------------------------------------------------

    def upload_pre(self, file_name, mime, size, pdir_fid):
        """预上传，返回完整响应。后续字段在 data.* 与 metadata.part_size。"""
        now_ms = int(time.time() * 1000)
        return self._request('/file/upload/pre', method='POST', json_body={
            'ccp_hash_update': True,
            'parallel_upload': True,
            'dir_name': '',
            'file_name': file_name,
            'format_type': mime,
            'l_created_at': now_ms,
            'l_updated_at': now_ms,
            'pdir_fid': pdir_fid,
            'size': size,
        })

    def upload_hash(self, md5_hex, sha1_hex, task_id):
        """秒传检测，返回 True 表示服务端已有相同内容，直接完成。"""
        data = self._request('/file/update/hash', method='POST', json_body={
            'md5': md5_hex,
            'sha1': sha1_hex,
            'task_id': task_id,
        })
        return bool((data.get('data') or {}).get('finish'))

    def _upload_auth(self, auth_info, auth_meta, task_id):
        """用 auth_meta（待签名串）换 OSS 请求的 auth_key。"""
        data = self._request('/file/upload/auth', method='POST', json_body={
            'auth_info': auth_info,
            'auth_meta': auth_meta,
            'task_id': task_id,
        })
        return (data.get('data') or {})['auth_key']

    @staticmethod
    def _oss_url(pre_data):
        upload_url = pre_data['upload_url']
        if '://' in upload_url:
            upload_url = upload_url.split('://', 1)[1]
        return f"https://{pre_data['bucket']}.{upload_url}/{pre_data['obj_key']}"

    def upload_part(self, pre, mime, part_number, data, hash_ctx=None):
        """上传一个分片，返回 ETag。签名串逐字符敏感，勿改动格式。

        part_number>=2 时必须带上一分片累计的 X-Oss-Hash-Ctx，否则 OSS 会拒收。
        """
        pre_data = pre['data']
        gmt = _oss_date()
        hash_ctx_b64 = encode_hash_ctx(hash_ctx) if hash_ctx else ''
        hash_ctx_line = f'X-Oss-Hash-Ctx:{hash_ctx_b64}\n' if hash_ctx_b64 else ''
        auth_meta = (
            f"PUT\n"
            f"\n"
            f"{mime}\n"
            f"{gmt}\n"
            f"{hash_ctx_line}"
            f"x-oss-date:{gmt}\n"
            f"x-oss-user-agent:{OSS_UA}\n"
            f"/{pre_data['bucket']}/{pre_data['obj_key']}"
            f"?partNumber={part_number}&uploadId={pre_data['upload_id']}"
        )
        auth_key = self._upload_auth(pre_data['auth_info'], auth_meta, pre_data['task_id'])

        headers = {
            'Authorization': auth_key,
            'Content-Type': mime,
            'Referer': REFERER + '/',
            'x-oss-date': gmt,
            'x-oss-user-agent': OSS_UA,
        }
        if hash_ctx_b64:
            headers['X-Oss-Hash-Ctx'] = hash_ctx_b64

        # OSS 直传不走 self.session，避免夸克 Cookie 泄露到 aliyuncs 域名
        logger.info('夸克分片 %s 开始上传（%s bytes）', part_number, len(data))
        try:
            resp = requests.put(
                self._oss_url(pre_data),
                params={'partNumber': str(part_number), 'uploadId': pre_data['upload_id']},
                data=data,
                headers=headers,
                timeout=OSS_TIMEOUT,
            )
        except requests.RequestException as e:
            raise QuarkApiError(f'分片 {part_number} 网络错误: {e}') from e
        if resp.status_code != 200:
            raise QuarkApiError(
                f'分片 {part_number} 上传失败（HTTP {resp.status_code}）: {resp.text[:300]}'
            )
        etag = resp.headers.get('ETag') or resp.headers.get('Etag')
        if not etag:
            raise QuarkApiError(f'分片 {part_number} 响应缺少 ETag')
        return etag

    def upload_commit(self, pre, etags):
        """提交分片列表（CompleteMultipartUpload XML）。"""
        pre_data = pre['data']
        body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<CompleteMultipartUpload>\n'
                + ''.join(
                    f'<Part>\n<PartNumber>{i}</PartNumber>\n<ETag>{e}</ETag>\n</Part>\n'
                    for i, e in enumerate(etags, start=1))
                + '</CompleteMultipartUpload>')
        content_md5 = base64.b64encode(hashlib.md5(body.encode()).digest()).decode()

        callback = pre_data['callback']
        if isinstance(callback, str):
            cb_json = callback
        else:
            cb_json = json.dumps(callback, separators=(',', ':'), ensure_ascii=True)
        # 对齐 Go encoding/json 的 HTML 字符转义，否则签名不匹配
        cb_json = (cb_json.replace('&', '\\u0026')
                          .replace('<', '\\u003c')
                          .replace('>', '\\u003e'))
        callback_b64 = base64.b64encode(cb_json.encode()).decode()

        gmt = _oss_date()
        auth_meta = (
            f"POST\n"
            f"{content_md5}\n"
            f"application/xml\n"
            f"{gmt}\n"
            f"x-oss-callback:{callback_b64}\n"
            f"x-oss-date:{gmt}\n"
            f"x-oss-user-agent:{OSS_UA}\n"
            f"/{pre_data['bucket']}/{pre_data['obj_key']}?uploadId={pre_data['upload_id']}"
        )
        auth_key = self._upload_auth(pre_data['auth_info'], auth_meta, pre_data['task_id'])

        try:
            resp = requests.post(
                self._oss_url(pre_data),
                params={'uploadId': pre_data['upload_id']},
                data=body.encode(),
                headers={
                    'Authorization': auth_key,
                    'Content-MD5': content_md5,
                    'Content-Type': 'application/xml',
                    'Referer': REFERER + '/',
                    'x-oss-callback': callback_b64,
                    'x-oss-date': gmt,
                    'x-oss-user-agent': OSS_UA,
                },
                timeout=OSS_TIMEOUT,
            )
        except requests.RequestException as e:
            raise QuarkApiError(f'commit 网络错误: {e}') from e
        if resp.status_code != 200:
            raise QuarkApiError(f'commit 失败（HTTP {resp.status_code}）: {resp.text[:200]}')

    def upload_finish(self, pre):
        """通知夸克上传完成。"""
        pre_data = pre['data']
        self._request('/file/upload/finish', method='POST', json_body={
            'obj_key': pre_data['obj_key'],
            'task_id': pre_data['task_id'],
        })
        time.sleep(1)

    def _with_retry(self, func, desc, retries=5):
        """指数退避重试（2/4/8/16s）；QuarkAuthError 不重试直接上抛。"""
        for attempt in range(retries):
            try:
                return func()
            except QuarkAuthError:
                raise
            except QuarkApiError as e:
                if attempt == retries - 1:
                    raise
                wait = min(2 ** (attempt + 1), 30)
                logger.warning(f'{desc}失败（{e}），{wait}s 后重试（{attempt + 1}/{retries}）')
                time.sleep(wait)

    def upload_file(self, local_path, pdir_fid, file_name=None):
        """编排完整上传流程，返回 {'rapid': bool, 'parts': int, ...}。"""
        size = os.path.getsize(local_path)
        file_name = file_name or os.path.basename(local_path)
        mime = mimetypes.guess_type(local_path)[0] or 'application/octet-stream'

        # 1MB 块流式算 md5+sha1，避免大文件吃内存
        md5, sha1 = hashlib.md5(), hashlib.sha1()
        with open(local_path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                md5.update(chunk)
                sha1.update(chunk)
        md5_hex, sha1_hex = md5.hexdigest(), sha1.hexdigest()

        pre = self.upload_pre(file_name, mime, size, pdir_fid)
        pre_data = pre.get('data') or {}
        if pre_data.get('finish'):
            logger.info(f'夸克秒传成功: {file_name}')
            self._files_cache.pop(pdir_fid, None)
            return {'rapid': True, 'parts': 0, 'file_name': file_name}

        task_id = pre_data.get('task_id')
        if not task_id:
            raise QuarkApiError(f'预上传未返回 task_id: {list(pre_data.keys())}')

        if self.upload_hash(md5_hex, sha1_hex, task_id):
            logger.info(f'夸克秒传成功: {file_name}')
            self._files_cache.pop(pdir_fid, None)
            return {'rapid': True, 'parts': 0, 'file_name': file_name}

        part_size = int((pre.get('metadata') or {}).get('part_size') or 0)
        if part_size <= 0:
            raise QuarkApiError('预上传未返回分片大小 part_size')
        logger.info(
            '夸克开始分片上传: %s（%s bytes，分片 %s bytes）',
            file_name, size, part_size,
        )

        etags = []
        running = _Sha1Running()
        prev_ctx = None
        with open(local_path, 'rb') as f:
            part_number = 1
            while True:
                buf = f.read(part_size)
                if not buf:
                    break
                etags.append(self._with_retry(
                    lambda buf=buf, n=part_number, ctx=prev_ctx: self.upload_part(
                        pre, mime, n, buf, hash_ctx=ctx),
                    f'分片 {part_number} 上传'))
                running.update(buf)
                prev_ctx = running.snapshot()
                part_number += 1

        if not etags:
            raise QuarkApiError(f'文件为空，无法上传: {file_name}')

        # 新协议要求 hash 在分片之后再确认一次，否则 commit 可能 NoSuchUpload
        try:
            if self.upload_hash(md5_hex, sha1_hex, task_id):
                logger.info(f'夸克秒传成功: {file_name}')
                self._files_cache.pop(pdir_fid, None)
                return {'rapid': True, 'parts': len(etags), 'file_name': file_name}
        except QuarkApiError as e:
            logger.warning(f'分片后 hash 确认失败（{e}），继续 commit')

        self._with_retry(lambda: self.upload_commit(pre, etags), 'commit')
        self.upload_finish(pre)
        self._files_cache.pop(pdir_fid, None)
        logger.info(f'夸克上传完成: {file_name}（{len(etags)} 个分片）')
        return {'rapid': False, 'parts': len(etags), 'file_name': file_name}
