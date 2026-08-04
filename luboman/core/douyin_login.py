"""抖音创作者服务平台（creator.douyin.com）扫码登录与 cookie 管理。

登录态用 patchright 驱动 Chrome 获取，保存为 Playwright storage_state JSON。
与 biliup_login.py 的会话模式对称：后台线程内 ``asyncio.run`` 自建事件循环跑
浏览器（上传插件在 run_blocking 线程里同理），与 luboman web 进程的 asyncio
主循环完全隔离，避免第三方库 runtime 冲突（见 biliup_login.py 模块 docstring）。

cookie 文件统一放 ``douyin_cookie_base_dir()``（Docker 内 /data/douyin-cookies，
本地 ./data/douyin-cookies），随 /data 卷持久化。
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Callable, Optional

from luboman.config import config

logger = logging.getLogger('luboman')

# 抖音二维码有效期较短且支持手动刷新；会话最长存活时间留足缓冲，超时由 manager 清理。
_SESSION_MAX_AGE = 300

# 登录完成后的落地页（扫码成功会跳到创作者后台首页）
_LOGIN_SUCCESS_URL_PREFIX = 'https://creator.douyin.com/creator-micro/'
# 登录页 URL（未登录访问创作者平台会停留/跳转到这里）
_LOGIN_URL_KEYWORD = 'creator.douyin.com'


def douyin_cookie_base_dir() -> str:
    base_dir = config.get('douyin_cookie_dir')
    if not base_dir:
        base_dir = (
            '/data/douyin-cookies'
            if os.path.exists('/.dockerenv')
            else os.path.join(os.getcwd(), 'data', 'douyin-cookies')
        )
    return os.path.realpath(os.path.abspath(os.path.expanduser(base_dir)))


def _safe_filename_part(value: Optional[str]) -> str:
    value = (value or '').strip()
    if not value:
        return 'douyin'
    value = re.sub(r'[^A-Za-z0-9_.-]+', '-', value)
    return value.strip('.-') or 'douyin'


def default_douyin_cookie_path(
    label: Optional[str] = None,
    unique_id: Optional[str] = None,
) -> str:
    base_dir = douyin_cookie_base_dir()
    suffix = (unique_id or uuid.uuid4().hex)[:8]
    filename = (
        f"{_safe_filename_part(label)}-"
        f"{time.strftime('%Y%m%d-%H%M%S')}-{suffix}.json"
    )
    return os.path.join(base_dir, filename)


def resolve_douyin_cookie_path(
    cookie_path: Optional[str] = None,
    label: Optional[str] = None,
    unique_id: Optional[str] = None,
) -> str:
    """解析并校验 cookie 文件目标路径（防目录穿越，与 biliup resolve_cookie_path 同款）。"""
    base_dir = douyin_cookie_base_dir()
    if not cookie_path:
        return default_douyin_cookie_path(label, unique_id)

    expanded_path = os.path.expanduser(cookie_path.strip())
    if not expanded_path:
        return default_douyin_cookie_path(label, unique_id)

    if not os.path.isabs(expanded_path):
        expanded_path = os.path.join(base_dir, expanded_path)

    candidate = os.path.realpath(os.path.abspath(expanded_path))
    try:
        in_base_dir = os.path.commonpath([base_dir, candidate]) == base_dir
    except ValueError:
        in_base_dir = False

    if not in_base_dir:
        raise ValueError(f'douyin cookies path must be under {base_dir}')

    if os.path.basename(candidate) in {'', '.', '..'}:
        raise ValueError('douyin cookies path must be a file path')

    return candidate


def resolve_account_cookie_path(account: dict) -> Optional[str]:
    """从 DouyinAccount 字典解析可用的 cookie 文件路径。

    优先用 douyin_cookies_filepath（文件须存在）；否则把 DB 里冗余存储的
    storage_state JSON 物化到 cookie 目录（account_{id}.json）后返回。
    都没有返回 None（调用方应提示先扫码登录）。
    """
    account = account or {}
    filepath = (account.get('douyin_cookies_filepath') or '').strip()
    if filepath and os.path.exists(filepath):
        return filepath

    cookies_text = (account.get('douyin_cookies') or '').strip()
    if not cookies_text:
        return None
    try:
        parsed = json.loads(cookies_text)
        if not isinstance(parsed, dict) or 'cookies' not in parsed:
            raise ValueError('not a storage_state json')
    except (TypeError, ValueError):
        logger.warning('抖音账号 %s 的 douyin_cookies 不是合法 storage_state JSON，已忽略', account.get('id'))
        return None

    account_id = account.get('id') or uuid.uuid4().hex[:8]
    path = os.path.join(douyin_cookie_base_dir(), f'account_{account_id}.json')
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fp:
            json.dump(parsed, fp, ensure_ascii=False)
    except OSError:
        logger.exception('物化抖音 cookie 文件失败: %s', path)
        return None
    return path


def _browser_launch_kwargs(headless: bool) -> dict:
    """容器内以 root 运行必须 --no-sandbox；/dev/shm 过小会导致渲染进程崩溃。"""
    return {
        'headless': headless,
        'args': [
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--disable-blink-features=AutomationControlled',
        ],
    }


async def _extract_qrcode_data_url(page, timeout: float = 30.0) -> Optional[str]:
    """从登录页提取二维码图片（data URL）。前端改版时只需调整这里的 selector。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        # 优先走「扫码登录」标签页（部分版本默认是手机号登录）
        try:
            scan_tab = page.get_by_text('扫码登录', exact=True).first
            if await scan_tab.count():
                await scan_tab.click(timeout=2000)
        except Exception:
            pass
        for selector in (
            'img[aria-label="二维码"]',
            'img[class*="qrcode"]',
            'img[src^="data:image"]',
        ):
            try:
                img = page.locator(selector).first
                if await img.count():
                    src = await img.get_attribute('src')
                    if src and src.startswith('data:image'):
                        return src
            except Exception:
                continue
        await asyncio.sleep(1)
    return None


async def _is_logged_in(page) -> bool:
    if page.url.startswith(_LOGIN_SUCCESS_URL_PREFIX) and 'login' not in page.url:
        # 登录标记不可见才算数（页面可能部分渲染）
        for marker_text in ('扫码登录', '手机号登录', '二维码失效'):
            try:
                marker = page.get_by_text(marker_text, exact=True).first
                if await marker.count() and await marker.is_visible():
                    return False
            except Exception:
                continue
        return True
    return False


async def douyin_cookie_gen(
    cookie_path: str,
    qrcode_callback: Optional[Callable[[str], None]] = None,
    headless: bool = False,
    timeout: float = 240.0,
    poll_interval: float = 2.0,
) -> dict:
    """打开创作者平台扫码登录，成功后把 storage_state 写入 cookie_path。

    qrcode_callback 在拿到二维码 data URL 时同步回调（供 CLI 打印 / 会话暴露给前端）。
    返回 {'success': bool, 'message': str, 'cookie_path': str}。
    """
    from patchright.async_api import async_playwright

    os.makedirs(os.path.dirname(cookie_path), exist_ok=True)
    deadline = time.time() + timeout
    result = {'success': False, 'message': '抖音扫码登录失败', 'cookie_path': cookie_path}

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**_browser_launch_kwargs(headless))
        context = await browser.new_context(viewport={'width': 1600, 'height': 900})
        try:
            page = await context.new_page()
            await page.goto('https://creator.douyin.com/')

            qrcode_src = await _extract_qrcode_data_url(page)
            if not qrcode_src:
                result['message'] = '未获取到抖音登录二维码（页面结构可能已变更）'
                return result
            if qrcode_callback:
                qrcode_callback(qrcode_src)

            last_qrcode = qrcode_src
            while time.time() < deadline:
                if await _is_logged_in(page):
                    await asyncio.sleep(2)  # 等 cookie 写稳
                    await context.storage_state(path=cookie_path)
                    result['success'] = True
                    result['message'] = '抖音扫码登录成功'
                    logger.info('抖音扫码登录成功, cookie -> %s', cookie_path)
                    return result

                # 二维码过期：页面出现「二维码失效」时点击刷新并重新提取
                try:
                    expired = page.get_by_text('二维码失效', exact=True).first
                    if await expired.count() and await expired.is_visible():
                        await expired.click(timeout=2000)
                        await asyncio.sleep(1)
                        refreshed = await _extract_qrcode_data_url(page, timeout=10)
                        if refreshed and refreshed != last_qrcode:
                            last_qrcode = refreshed
                            if qrcode_callback:
                                qrcode_callback(refreshed)
                except Exception:
                    pass

                await asyncio.sleep(poll_interval)

            result['message'] = '等待抖音扫码登录超时'
            return result
        finally:
            await context.close()
            await browser.close()


class DouyinLoginSession:
    """web 端扫码登录会话：状态机与 BiliupLoginSession 对称。

    status: created → waiting → success / failed / stopped / expired
    差异：二维码是页面 img 的 data URL（qrcode_img），前端直接 <img> 渲染。
    """

    def __init__(
        self,
        cookie_path: Optional[str] = None,
        label: Optional[str] = None,
    ):
        self.session_id = uuid.uuid4().hex
        self.cookie_path = resolve_douyin_cookie_path(cookie_path, label, self.session_id)
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.status = 'created'
        self.qrcode_img: Optional[str] = None
        self.error_message: Optional[str] = None
        self._lock = threading.Lock()
        self._worker = None
        self._stop_flag = False

    def start(self):
        with self._lock:
            if self.status != 'created':
                return
            self.updated_at = time.time()
        logger.info('启动抖音扫码登录会话 %s, cookie -> %s', self.session_id, self.cookie_path)

        try:
            os.makedirs(os.path.dirname(self.cookie_path), exist_ok=True)
        except Exception as exc:
            self._fail(f'创建 cookie 目录失败: {exc}')
            return

        self._worker = threading.Thread(
            target=self._run,
            name=f'douyin-login-{self.session_id}',
            daemon=True,
        )
        self._worker.start()

    def wait_for_qrcode(self, timeout: float = 40.0):
        """阻塞等待二维码就绪（status 离开 created），供 start_session 同步返回。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self.status != 'created':
                    return
            time.sleep(0.2)
        with self._lock:
            if self.status == 'created':
                self.status = 'failed'
                self.error_message = '获取二维码超时（浏览器未打开登录页或未找到二维码）'
                self.updated_at = time.time()

    def _on_qrcode(self, data_url: str):
        with self._lock:
            self.qrcode_img = data_url
            if self.status == 'created':
                self.status = 'waiting'
            self.updated_at = time.time()

    def _run(self):
        try:
            result = asyncio.run(
                douyin_cookie_gen(
                    self.cookie_path,
                    qrcode_callback=self._on_qrcode,
                    headless=True,
                    timeout=_SESSION_MAX_AGE,
                )
            )
            if self._stop_flag:
                return
            if result.get('success'):
                with self._lock:
                    if self.status in ('created', 'waiting'):
                        self.status = 'success'
                        self.updated_at = time.time()
            else:
                self._fail_if_active(result.get('message') or '扫码登录失败')
        except Exception as exc:
            logger.exception('抖音登录浏览器会话异常')
            self._fail_if_active(f'登录浏览器会话异常: {exc}')

    def _fail(self, message: str):
        logger.error(message)
        with self._lock:
            self.status = 'failed'
            self.error_message = message
            self.updated_at = time.time()

    def _fail_if_active(self, message: str):
        logger.error(message)
        with self._lock:
            if self.status in ('created', 'waiting'):
                self.status = 'failed'
                self.error_message = message
                self.updated_at = time.time()

    def stop(self, status: str = 'stopped'):
        with self._lock:
            if self.status not in ('created', 'waiting'):
                return
            self.status = status
            self.updated_at = time.time()
        self._stop_flag = True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                'session_id': self.session_id,
                'cookie_path': self.cookie_path,
                'status': self.status,
                'qrcode_img': self.qrcode_img,
                'error_message': self.error_message,
                'created_at': self.created_at,
                'updated_at': self.updated_at,
            }


class DouyinLoginManager:
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
        session = DouyinLoginSession(cookie_path, label)
        with self._lock:
            self._sessions[session.session_id] = session
        session.start()
        # 同步等二维码就绪再返回，前端即可直接拿到 status=waiting + qrcode_img
        session.wait_for_qrcode(timeout=40)
        self._schedule_cleanup()
        return session.snapshot()

    def get_session(self, session_id: str) -> DouyinLoginSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            raise ValueError('douyin login session not found')
        return session

    def snapshot(self, session_id: str, since: Optional[int] = None):
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


douyin_login_manager = DouyinLoginManager()


def main():
    """CLI：人工扫码生成 cookie（本机调试用，headed 模式）。

    用法: python -m luboman.core.douyin_login [--output 路径] [--label 名称]
    """
    import argparse

    parser = argparse.ArgumentParser(description='抖音创作者平台扫码登录，生成 storage_state cookie')
    parser.add_argument('--output', default=None, help='cookie 输出路径（默认放 douyin cookie 目录）')
    parser.add_argument('--label', default=None, help='文件名标识（如账号名）')
    args = parser.parse_args()

    cookie_path = resolve_douyin_cookie_path(args.output, args.label)

    def _print_qrcode(data_url: str):
        # data URL 转存临时 png，提示用户打开扫码（终端无法直接渲染图片）
        try:
            import base64
            import tempfile

            header, _, encoded = data_url.partition(',')
            suffix = '.png' if 'png' in header else '.img'
            fd, qr_path = tempfile.mkstemp(prefix='douyin-qrcode-', suffix=suffix)
            with os.fdopen(fd, 'wb') as fp:
                fp.write(base64.b64decode(encoded))
            print(f'二维码已保存: {qr_path} ，请用抖音APP扫码登录')
        except Exception:
            logger.exception('保存二维码图片失败')

    print(f'cookie 将写入: {cookie_path}')
    result = asyncio.run(douyin_cookie_gen(cookie_path, qrcode_callback=_print_qrcode, headless=False))
    print(result['message'])
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
