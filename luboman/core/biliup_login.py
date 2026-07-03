"""biliup 扫码登录会话管理(基于 stream_gears 库调用,无 TTY / 子进程依赖)。

不再 spawn ``biliup login`` 子进程——它的交互菜单强制要 TTY,在后台服务里
必然报 ``not a terminal``。改为直接调用 biliup 底层的 ``stream_gears``:
``get_qrcode`` 取二维码 URL,后台线程 ``login_by_qrcode`` 阻塞轮询扫码状态,
成功后把返回的登录信息 JSON 原样写入 cookie 文件(biliup cookies.json 格式,
含 cookie_info + token_info,与 plugins/biliup_cli.py 上传、
bili_account_health.py 巡检所读取的格式一致)。
"""

import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Optional

from luboman.config import config

logger = logging.getLogger('luboman')

# B 站二维码有效期约 180s;会话最长存活时间留足缓冲,超时由 manager 清理。
_SESSION_MAX_AGE = 240


def _safe_filename_part(value: Optional[str]) -> str:
    value = (value or '').strip()
    if not value:
        return 'bili'
    value = re.sub(r'[^A-Za-z0-9_.-]+', '-', value)
    return value.strip('.-') or 'bili'


def cookie_base_dir() -> str:
    base_dir = config.get('biliup_cookie_dir')
    if not base_dir:
        base_dir = (
            '/data/biliup-cookies'
            if os.path.exists('/.dockerenv')
            else os.path.join(os.getcwd(), 'data', 'biliup-cookies')
        )
    return os.path.realpath(os.path.abspath(os.path.expanduser(base_dir)))


def default_cookie_path(
    label: Optional[str] = None,
    unique_id: Optional[str] = None,
) -> str:
    base_dir = cookie_base_dir()
    suffix = (unique_id or uuid.uuid4().hex)[:8]
    filename = (
        f"{_safe_filename_part(label)}-"
        f"{time.strftime('%Y%m%d-%H%M%S')}-{suffix}.json"
    )
    return os.path.join(base_dir, filename)


def resolve_cookie_path(
    cookie_path: Optional[str] = None,
    label: Optional[str] = None,
    unique_id: Optional[str] = None,
) -> str:
    base_dir = cookie_base_dir()
    if not cookie_path:
        return default_cookie_path(label, unique_id)

    expanded_path = os.path.expanduser(cookie_path.strip())
    if not expanded_path:
        return default_cookie_path(label, unique_id)

    if not os.path.isabs(expanded_path):
        expanded_path = os.path.join(base_dir, expanded_path)

    candidate = os.path.realpath(os.path.abspath(expanded_path))
    try:
        in_base_dir = os.path.commonpath([base_dir, candidate]) == base_dir
    except ValueError:
        in_base_dir = False

    if not in_base_dir:
        raise ValueError(f'biliup cookies path must be under {base_dir}')

    if os.path.basename(candidate) in {'', '.', '..'}:
        raise ValueError('biliup cookies path must be a file path')

    return candidate


def _biliup_proxy() -> Optional[str]:
    """stream_gears 的 proxy 参数无默认值,需显式传 None 或 URL。"""
    return config.get('biliup_proxy') or None


class BiliupLoginSession:
    # status: created → waiting → success / failed / stopped / expired
    def __init__(
        self,
        cookie_path: Optional[str] = None,
        label: Optional[str] = None,
    ):
        self.session_id = uuid.uuid4().hex
        self.cookie_path = resolve_cookie_path(cookie_path, label, self.session_id)
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.status = 'created'
        self.qrcode_url: Optional[str] = None
        self.error_message: Optional[str] = None
        self._lock = threading.Lock()
        self._worker = None

    def start(self):
        with self._lock:
            if self.status != 'created':
                return
            self.updated_at = time.time()
        logger.info('启动 biliup 扫码登录会话 %s,cookie -> %s', self.session_id, self.cookie_path)

        try:
            os.makedirs(os.path.dirname(self.cookie_path), exist_ok=True)
        except Exception as exc:
            self._fail(f'创建 cookie 目录失败: {exc}')
            return

        try:
            import stream_gears
        except ImportError:
            self._fail('未找到 stream_gears 模块,请确认 biliup 已正确安装')
            return

        # 1. 申请二维码
        try:
            ret = stream_gears.get_qrcode(_biliup_proxy())
            data = json.loads(ret)
            url = (data.get('data') or {}).get('url')
            if not url:
                # data.code != 0 或缺少 url,把原始返回透出便于排查
                self._fail(f'获取二维码失败: {ret}')
                return
        except Exception as exc:
            logger.exception('获取 biliup 二维码失败')
            self._fail(f'获取二维码失败: {exc}')
            return

        with self._lock:
            self.qrcode_url = url
            self.status = 'waiting'
            self.updated_at = time.time()

        # 2. 后台线程阻塞轮询扫码状态(stream_gears.login_by_qrcode 阻塞,
        #    成功返回登录信息 JSON,二维码过期会抛错返回)
        self._worker = threading.Thread(
            target=self._poll,
            args=(stream_gears, ret),
            name=f'biliup-login-{self.session_id}',
            daemon=True,
        )
        self._worker.start()

    def _poll(self, stream_gears, ret: str):
        try:
            res = stream_gears.login_by_qrcode(ret, _biliup_proxy())
        except Exception as exc:
            logger.exception('biliup 扫码登录轮询失败')
            self._fail_if_waiting(f'扫码登录失败: {exc}')
            return

        # 期间可能已被 stop/cleanup,放弃结果
        with self._lock:
            if self.status != 'waiting':
                return

        try:
            with open(self.cookie_path, 'w', encoding='utf-8') as f:
                f.write(res)
        except Exception as exc:
            logger.exception('写入 biliup cookie 文件失败')
            self._fail_if_waiting(f'写入 cookie 文件失败: {exc}')
            return

        with self._lock:
            if self.status == 'waiting':
                self.status = 'success'
                self.updated_at = time.time()
        logger.info('biliup 扫码登录成功,cookie 已写入 %s', self.cookie_path)

    def _fail(self, message: str):
        logger.error(message)
        with self._lock:
            self.status = 'failed'
            self.error_message = message
            self.updated_at = time.time()

    def _fail_if_waiting(self, message: str):
        logger.error(message)
        with self._lock:
            if self.status == 'waiting':
                self.status = 'failed'
                self.error_message = message
                self.updated_at = time.time()

    def stop(self, status: str = 'stopped'):
        """标记会话停止。

        注意:无法强制中断后台 ``login_by_qrcode`` 的阻塞调用,daemon 线程
        会跑到扫码成功或二维码过期(~180s)自然结束;此处只更新对外状态。
        """
        with self._lock:
            if self.status not in ('created', 'waiting'):
                return
            self.status = status
            self.updated_at = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                'session_id': self.session_id,
                'cookie_path': self.cookie_path,
                'status': self.status,
                'qrcode_url': self.qrcode_url,
                'error_message': self.error_message,
                'created_at': self.created_at,
                'updated_at': self.updated_at,
            }


class BiliupLoginManager:
    def __init__(self, max_age_seconds: int = _SESSION_MAX_AGE):
        self._sessions: dict = {}
        self._lock = threading.Lock()
        self.max_age_seconds = max_age_seconds

    def _schedule_cleanup(self):
        timer = threading.Timer(self.max_age_seconds, self.cleanup)
        timer.daemon = True
        timer.start()

    def start_session(
        self,
        cookie_path: Optional[str] = None,
        label: Optional[str] = None,
    ):
        self.cleanup()
        session = BiliupLoginSession(cookie_path, label)
        with self._lock:
            self._sessions[session.session_id] = session
        session.start()
        self._schedule_cleanup()
        return session.snapshot()

    def get_session(self, session_id: str) -> BiliupLoginSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            raise ValueError('biliup login session not found')
        return session

    def snapshot(self, session_id: str, since: Optional[int] = None):
        # since 历史用于终端输出增量,现无输出流,保留参数仅为兼容旧调用方
        return self.get_session(session_id).snapshot()

    def stop_session(self, session_id: str):
        session = self.get_session(session_id)
        session.stop()
        return session.snapshot()

    def cleanup(self, max_age_seconds: Optional[int] = None):
        max_age_seconds = max_age_seconds or self.max_age_seconds
        now = time.time()
        sessions_to_expire = []
        with self._lock:
            for session in list(self._sessions.values()):
                if session.status in ('created', 'waiting'):
                    if now - session.created_at > max_age_seconds:
                        sessions_to_expire.append(session)
                    continue
                if now - session.updated_at > max_age_seconds:
                    self._sessions.pop(session.session_id, None)

        for session in sessions_to_expire:
            session.stop(status='expired')


biliup_login_manager = BiliupLoginManager()
