"""夸克网盘扫码登录会话管理（纯 requests 轮询，无子进程/浏览器）。

流程逆向自 pan.quark.cn 官网前端（quark-cloud-drive-static-page JS）：
1. GET uop.quark.cn/cas/ajax/getTokenForQrcodeLogin
      ?client_id=532&v=1.2&request_id=<uuid> -> data.members.token
2. 二维码内容 = https://su.quark.cn/4_eMHBJ?token=...&client_id=532&ssb=weblogin&...
   前端渲染成二维码,用户用夸克 App 扫码并确认
3. 每 2s 轮询 getServiceTicketByQrcodeToken
      ?client_id=532&v=1.2&token=...&request_id=<同一 uuid>;
   status==2000000 时拿到 data.members.service_ticket（未扫码为 50004001）
4. GET pan.quark.cn/account/info?st={service_ticket}&lw=scan,响应 Set-Cookie 下发
   登录态（__puus/__pus 等）,拼成 cookie 串校验后写回全局配置 quark_cookie

token 与 request_id 成对绑定：取码和轮询缺 client_id / request_id 时，
App 扫码后会显示「二维码过期」。

与 biliup/douyin 扫码不同,夸克不需要 PTY 子进程或 Playwright,直接 HTTP 即可。
"""

import logging
import threading
import time
import uuid
from typing import Optional

import requests

from luboman.config import config
from luboman.core.quark_client import QuarkApiError, QuarkClient

logger = logging.getLogger('luboman')

# 会话最长存活时间（秒）,超时由 manager 置 expired
_SESSION_MAX_AGE = 300
# 扫码状态轮询间隔,对齐官网前端的 2s
_POLL_INTERVAL = 2.0

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
_TOKEN_URL = 'https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin'
_POLL_URL = 'https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken'
_EXCHANGE_URL = 'https://pan.quark.cn/account/info'
# 官网前端的扫码落地页,二维码内容即此 URL 拼参数
_SCAN_PAGE = 'https://su.quark.cn/4_eMHBJ'
_CLIENT_ID = '532'
_API_VERSION = '1.2'
_STATUS_OK = 2000000
# 50004001=未扫码, 50004002=已扫待确认
_STATUS_PENDING = {50004001, 50004002}


class QuarkLoginSession:
    # status: created → waiting → success / failed / stopped / expired
    def __init__(self):
        self.session_id = uuid.uuid4().hex
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.status = 'created'
        self.qrcode_url: Optional[str] = None
        self.error_message: Optional[str] = None
        self._lock = threading.Lock()
        self._worker = None
        self._stop_flag = False

    def start(self):
        with self._lock:
            if self.status != 'created':
                return
            self.updated_at = time.time()
        logger.info('启动夸克扫码登录会话 %s', self.session_id)
        self._worker = threading.Thread(
            target=self._run,
            name=f'quark-login-{self.session_id}',
            daemon=True,
        )
        self._worker.start()

    def wait_for_qrcode(self, timeout: float = 30.0):
        """阻塞等待二维码就绪（status 离开 created）,供 start_session 同步返回。

        前端仅在 status=waiting 时轮询,若立即返回 created 前端就不会继续拉取。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self.status != 'created':
                    return
            time.sleep(0.2)
        with self._lock:
            if self.status == 'created':
                self.status = 'failed'
                self.error_message = '获取二维码超时'
                self.updated_at = time.time()

    def _run(self):
        http = requests.Session()
        http.headers.update({
            'User-Agent': _UA,
            'Referer': 'https://pan.quark.cn/',
            'Origin': 'https://pan.quark.cn',
        })
        try:
            token, request_id = self._fetch_token(http)
            qr_url = self._build_qr_url(token)
            with self._lock:
                self.qrcode_url = qr_url
                self.status = 'waiting'
                self.updated_at = time.time()

            service_ticket = self._poll(http, token, request_id)
            if service_ticket is None:
                return  # stopped / expired

            cookie = self._exchange(http, service_ticket)
            self._verify_and_persist(cookie)
        except Exception as exc:
            logger.exception('夸克扫码登录异常')
            self._fail_if_active(f'扫码登录异常: {exc}')

    @staticmethod
    def _cas_params(request_id: str, token: Optional[str] = None) -> dict:
        """CAS 扫码接口的公共查询参数。token 与 request_id 必须成对出现。"""
        params = {
            'client_id': _CLIENT_ID,
            'v': _API_VERSION,
            'request_id': request_id,
        }
        if token:
            params['token'] = token
        return params

    def _fetch_token(self, http: requests.Session) -> tuple:
        request_id = uuid.uuid4().hex
        resp = http.get(
            _TOKEN_URL,
            params=self._cas_params(request_id),
            timeout=(10, 30),
        )
        data = resp.json()
        token = ((data.get('data') or {}).get('members') or {}).get('token')
        if resp.status_code != 200 or data.get('status') != _STATUS_OK or not token:
            raise QuarkApiError(f'获取扫码 token 失败: {data.get("message") or resp.status_code}')
        return token, request_id

    @staticmethod
    def _build_qr_url(token: str) -> str:
        # 参数与官网前端逐字一致（client_id=532 是夸克网盘 web 端固定值）
        query = {
            'token': token,
            'client_id': _CLIENT_ID,
            'ssb': 'weblogin',
            'uc_param_str': '',
            'uc_biz_str': 'S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0',
        }
        return f'{_SCAN_PAGE}?{requests.compat.urlencode(query)}'

    def _poll(self, http: requests.Session, token: str, request_id: str) -> Optional[str]:
        """轮询扫码状态,确认后返回 service_ticket;被停止/超时返回 None。"""
        deadline = time.time() + _SESSION_MAX_AGE
        while time.time() < deadline:
            if self._stop_flag:
                return None
            try:
                resp = http.get(
                    _POLL_URL,
                    params=self._cas_params(request_id, token=token),
                    timeout=(10, 30),
                )
                data = resp.json()
            except (requests.RequestException, ValueError) as e:
                logger.warning('夸克扫码状态轮询失败（%s）,%ss 后重试', e, _POLL_INTERVAL)
                time.sleep(_POLL_INTERVAL)
                continue
            status = data.get('status')
            if status == _STATUS_OK:
                ticket = ((data.get('data') or {}).get('members') or {}).get('service_ticket')
                if ticket:
                    return ticket
                self._fail_if_active('扫码成功但未返回 service_ticket')
                return None
            if status not in _STATUS_PENDING:
                logger.info(
                    '夸克扫码轮询状态 %s: %s',
                    status, data.get('message') or data.get('msg'),
                )
            time.sleep(_POLL_INTERVAL)
        self._fail_if_active('扫码登录超时（二维码可能已过期）')
        return None

    def _exchange(self, http: requests.Session, service_ticket: str) -> str:
        """service_ticket 换登录态:account/info 响应 Set-Cookie,拼成 cookie 串。"""
        resp = http.get(
            _EXCHANGE_URL,
            params={'st': service_ticket, 'lw': 'scan'},
            timeout=(10, 30),
        )
        try:
            data = resp.json()
        except ValueError:
            raise QuarkApiError(f'登录态换取响应非 JSON（HTTP {resp.status_code}）')
        if resp.status_code != 200 or not data.get('success'):
            raise QuarkApiError(f'登录态换取失败: {data.get("msg") or resp.status_code}')
        cookies = requests.utils.dict_from_cookiejar(http.cookies)
        if not cookies:
            raise QuarkApiError('登录态换取成功但未捕获到 Cookie')
        return '; '.join(f'{k}={v}' for k, v in cookies.items())

    def _verify_and_persist(self, cookie: str):
        """用 drive API 探活确认 cookie 可用（顺带合并续期字段）,再写回配置。"""
        client = QuarkClient(cookie)
        client.check_cookie()  # 失效/网络问题抛 QuarkApiError
        config.set_persistent('quark_cookie', client.cookie)
        with self._lock:
            if self.status == 'waiting':
                self.status = 'success'
                self.updated_at = time.time()
        logger.info('夸克扫码登录成功,cookie 已写回配置 quark_cookie')

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
                'status': self.status,
                'qrcode_url': self.qrcode_url,
                'error_message': self.error_message,
                'created_at': self.created_at,
                'updated_at': self.updated_at,
            }


class QuarkLoginManager:
    def __init__(self, max_age_seconds: int = _SESSION_MAX_AGE):
        self._sessions: dict = {}
        self._lock = threading.Lock()
        self.max_age_seconds = max_age_seconds

    def _schedule_cleanup(self):
        timer = threading.Timer(self.max_age_seconds, self.cleanup)
        timer.daemon = True
        timer.start()

    def start_session(self):
        self.cleanup()
        session = QuarkLoginSession()
        with self._lock:
            self._sessions[session.session_id] = session
        session.start()
        # start() 起后台线程取二维码;同步等二维码就绪再返回,前端即可直接拿到
        # status=waiting + qrcode_url(前端 useEffect 仅在 waiting 时轮询)。
        session.wait_for_qrcode(timeout=30)
        self._schedule_cleanup()
        return session.snapshot()

    def get_session(self, session_id: str) -> QuarkLoginSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            raise ValueError('quark login session not found')
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


quark_login_manager = QuarkLoginManager()
