"""抖音创作者服务平台（creator.douyin.com）扫码登录与 cookie 管理。

登录态用 patchright 驱动 Chrome 获取，保存为 Playwright storage_state JSON。
与 biliup_login.py 的会话模式对称：后台线程内 ``asyncio.run`` 自建事件循环跑
浏览器（上传插件在 run_blocking 线程里同理），与 luboman web 进程的 asyncio
主循环完全隔离，避免第三方库 runtime 冲突（见 biliup_login.py 模块 docstring）。

cookie 文件统一放 ``douyin_cookie_base_dir()``（Docker 内 /data/douyin-cookies，
本地 ./data/douyin-cookies），随 /data 卷持久化。
"""

import asyncio
import base64
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


# 二维码候选元素的尺寸约束（像素）：登录二维码一般在 150~300px，
# 小于 80 的是 loading 图标/装饰图，大于 600 的是背景大图，都不可能是二维码。
_QR_MIN_SIDE = 80
_QR_MAX_SIDE = 600
# data URL 最小长度：真实二维码 PNG 的 base64 通常数 KB，几百字节的是占位图
_QR_MIN_DATA_URL_LEN = 1000
# 元素截图最小字节数：空白/透明 canvas 截出来只有几百字节，不可能是二维码
_QR_MIN_SCREENSHOT_BYTES = 400


def _looks_like_qrcode(image_bytes: bytes) -> bool:
    """二维码内容特征：几乎纯黑白（低彩色度）且有一定比例的黑色模块。

    登录页的彩色装饰插画（如蓝色「+」图标）尺寸同样落在二维码区间，
    单靠元素尺寸/位置无法区分，曾被误当二维码返回（前端显示为「破图」）。
    """
    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(image_bytes)).convert('RGB')
        img.thumbnail((64, 64))  # 降采样加速判定
        pixels = list(img.getdata())
        if not pixels:
            return False
        total = len(pixels)
        gray = sum(1 for r, g, b in pixels if max(r, g, b) - min(r, g, b) <= 30)
        dark = sum(1 for r, g, b in pixels if (r + g + b) / 3 < 80)
        return gray / total >= 0.85 and dark / total >= 0.05
    except Exception:
        return False


# 二维码候选 selector（按优先级）：登录页结构多次变更，二维码可能是
# img（data:/blob: src）或 canvas，也可能渲染在 iframe 里
_QR_SELECTORS = (
    'img[aria-label="二维码"]',
    'img[class*="qrcode"]',
    'canvas[class*="qrcode"]',
    'img[src^="data:image"]',
    'canvas',
)


async def _iter_qr_candidates(frame):
    """按优先级产出 frame 里可能是二维码的元素。"""
    for selector in _QR_SELECTORS:
        try:
            elements = await frame.locator(selector).all()
        except Exception:
            continue
        for element in elements:
            yield element


async def _dump_qr_candidates(page, limit: int = 30):
    """提取失败时输出候选元素诊断日志：selector / 尺寸 / src 前缀 / 加载态 / 内容判定。"""
    for frame in page.frames:
        dumped = 0
        for selector in _QR_SELECTORS:
            try:
                elements = await frame.locator(selector).all()
            except Exception:
                continue
            for element in elements:
                if dumped >= limit:
                    return
                try:
                    box = await element.bounding_box()
                    if not box:
                        continue
                    width, height = box['width'], box['height']
                    src = (await element.get_attribute('src')) or ''
                    info = f'{selector} {width:.0f}x{height:.0f} src={src[:60]!r}'
                    tag = await element.evaluate('(el) => el.tagName')
                    info += f' tag={tag}'
                    if tag == 'IMG':
                        loaded = await element.evaluate(
                            '(el) => el.complete && el.naturalWidth > 0')
                        info += f' loaded={loaded}'
                    logger.warning('二维码候选诊断[%s]: %s', frame.url[:60], info)
                    dumped += 1
                except Exception:
                    continue


async def _extract_qrcode_data_url(page, timeout: float = 30.0) -> Optional[str]:
    """从登录页提取二维码图片（data URL）。前端改版时只需调整 _iter_qr_candidates 的 selector。

    抖音登录页结构多次变更：二维码可能是 img（data:/blob: src）、canvas，
    也可能渲染在 iframe 里。因此遍历所有 frame，按「尺寸像二维码的可见元素」
    筛选候选；优先取 img 的 data: src（需足够长，排除占位小图），
    取不到就对元素截图转 PNG data URL（覆盖 canvas / blob: 场景）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for frame in page.frames:
            # 优先走「扫码登录」标签页（部分版本默认是手机号登录）
            try:
                scan_tab = frame.get_by_text('扫码登录', exact=True).first
                if await scan_tab.count():
                    await scan_tab.click(timeout=2000)
            except Exception:
                pass
            async for element in _iter_qr_candidates(frame):
                try:
                    box = await element.bounding_box()
                    if not box:
                        continue
                    width, height = box['width'], box['height']
                    if not (_QR_MIN_SIDE <= width <= _QR_MAX_SIDE
                            and _QR_MIN_SIDE <= height <= _QR_MAX_SIDE):
                        continue
                    # 二维码近正方形，排除横幅/竖条装饰图
                    if not (0.6 <= width / height <= 1.6):
                        continue
                    src = await element.get_attribute('src')
                    if src and src.startswith('data:image') and len(src) >= _QR_MIN_DATA_URL_LEN:
                        try:
                            raw = base64.b64decode(src.partition(',')[2])
                        except Exception:
                            continue
                        # 彩色装饰插画的 data URL 也满足长度条件，需按内容甄别
                        if not _looks_like_qrcode(raw):
                            continue
                        logger.info('提取到抖音登录二维码 img data URL（%d 字符）', len(src))
                        return src
                    # img 的 src 由前端异步填充：未加载完成时 bounding_box 已存在，
                    # 此时截图只会得到浏览器渲染的「破图」占位图标，必须跳过等下一轮
                    tag = await element.evaluate('(el) => el.tagName')
                    if tag == 'IMG':
                        loaded = await element.evaluate(
                            '(el) => el.complete && el.naturalWidth > 0')
                        if not loaded:
                            continue
                    # canvas / blob: src / 过短的 data URL：直接对元素截图
                    png = await element.screenshot(type='png')
                    if len(png) < _QR_MIN_SCREENSHOT_BYTES:
                        # 空白/透明 canvas 截图只有几百字节，不是二维码
                        continue
                    if not _looks_like_qrcode(png):
                        continue
                    data_url = 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')
                    logger.info('通过元素截图提取抖音登录二维码（%.0fx%.0f px）', width, height)
                    return data_url
                except Exception:
                    continue
        await asyncio.sleep(1)
    return None


# 登录成功才会种下的会话 cookie（任一非空即视为已登录）
_SESSION_COOKIE_NAMES = ('sessionid', 'sessionid_ss', 'sid_tt', 'sid_ucp_v1')


async def _is_logged_in(context, page) -> bool:
    """扫码确认后判定登录成功。

    首选会话 cookie 判据：不依赖页面跳转行为（抖音扫码后可能停留原页
    SPA 更新，也可能跳转 creator-micro，路径随版本变化）；URL 判定作兜底。
    """
    try:
        for cookie in await context.cookies():
            if cookie.get('name') in _SESSION_COOKIE_NAMES and cookie.get('value'):
                return True
    except Exception:
        pass

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
        try:
            browser = await playwright.chromium.launch(**_browser_launch_kwargs(headless))
        except Exception as exc:
            result['message'] = (
                f'启动浏览器失败: {exc}。当前镜像/环境未安装浏览器——'
                'slim 镜像（main-slim tag）不含 Chrome，请改用完整镜像（main tag），'
                '或执行 patchright install --with-deps chromium 后重试'
            )
            logger.error(result['message'])
            return result
        context = await browser.new_context(viewport={'width': 1600, 'height': 900})
        try:
            page = await context.new_page()
            await page.goto('https://creator.douyin.com/')

            qrcode_src = await _extract_qrcode_data_url(page, timeout=45)
            if not qrcode_src:
                # 留档诊断：整页截图 + 候选元素清单 + 当前 URL，
                # 便于确认是被风控/验证拦截还是页面结构变更
                try:
                    debug_path = os.path.join(
                        os.path.dirname(cookie_path), 'douyin-login-debug.png')
                    await page.screenshot(path=debug_path, full_page=True)
                    logger.warning('未获取到抖音登录二维码，页面 URL: %s，诊断截图: %s',
                                   page.url, debug_path)
                except Exception:
                    logger.warning('未获取到抖音登录二维码，页面 URL: %s（诊断截图失败）', page.url)
                try:
                    await _dump_qr_candidates(page)
                except Exception:
                    logger.exception('候选元素诊断输出失败')
                result['message'] = '未获取到抖音登录二维码（页面结构可能已变更）'
                return result
            if qrcode_callback:
                qrcode_callback(qrcode_src)

            last_qrcode = qrcode_src
            last_url = page.url
            last_probe = 0.0
            while time.time() < deadline:
                if page.url != last_url:
                    # 扫码后页面跳转是最直观的信号，记录便于排查登录检测问题
                    logger.info('抖音登录页跳转: %s -> %s', last_url, page.url)
                    last_url = page.url
                if await _is_logged_in(context, page):
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

                # 定期输出页面状态：扫码后页面是否感知（「扫描成功」提示/跳转/cookie）
                if time.time() - last_probe >= 15:
                    last_probe = time.time()
                    try:
                        markers = {}
                        for text in ('二维码失效', '扫描成功', '扫码成功', '确认登录', '登录成功'):
                            el = page.get_by_text(text, exact=False).first
                            markers[text] = bool(await el.count()) and await el.is_visible()
                        cookie_names = sorted({c.get('name', '') for c in await context.cookies()})
                        logger.info('扫码等待中: url=%s 页面标记=%s cookies=%s',
                                    page.url, markers, cookie_names)
                    except Exception:
                        pass

                await asyncio.sleep(poll_interval)

            # 超时留档：整页截图看扫码后页面实际状态
            try:
                debug_path = os.path.join(
                    os.path.dirname(cookie_path), 'douyin-login-debug.png')
                await page.screenshot(path=debug_path, full_page=True)
                logger.warning('等待抖音扫码登录超时，页面 URL: %s，诊断截图: %s',
                               page.url, debug_path)
            except Exception:
                pass
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

    def wait_for_qrcode(self, timeout: float = 55.0):
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
        # 容器内浏览器启动慢：提取超时 45s，这里再多留 10s 缓冲
        session.wait_for_qrcode(timeout=55)
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
