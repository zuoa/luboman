"""biliup 扫码登录会话管理(PTY 跑 ``biliup login`` 子进程)。

为什么不用 stream_gears 库:stream_gears 的 ``get_qrcode`` 在 luboman web 进程
(跑着大量 asyncio 任务)里会挂起——其内部 tokio runtime 与 Python asyncio
同进程冲突,手动新进程能跑、web 进程内卡死。故改回跑 ``biliup login`` 子进程,
子进程独立于 luboman 进程,不受 tokio 冲突影响。

为什么用 PTY:``biliup login`` 第一步是 ``dialoguer::Select`` 交互菜单,强制要
TTY,直接 subprocess 会报 ``not a terminal``。用 ``pty.openpty()`` 分配伪终端,
让菜单可用,父进程通过 master fd 读输出并自动发键选中「浏览器登录」——该方式
(biliup-rs ``login_by_browser``)会在 stdout 打印二维码 URL,便于正则提取后
返回给前端用 QRCode 渲染;随后子进程阻塞轮询扫码状态,扫码完成即写 cookie 文件。
"""

import logging
import os
import pty
import re
import select
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Optional

from luboman.config import config

logger = logging.getLogger('luboman')

# B 站二维码有效期约 180s;会话最长存活时间留足缓冲,超时由 manager 清理。
_SESSION_MAX_AGE = 240

# 终端 ANSI 转义序列(颜色/光标),用于剥离后再做关键词匹配
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]')
# 「浏览器登录」打印的二维码 URL(biliup-rs 把 https 替换为 http 以缩短二维码)
_QRCODE_URL_RE = re.compile(r'https?://passport\.bilibili\.com/\S*auth_code=\S+')


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


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
        self._stop_flag = False
        self._tmpdir = None

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

        self._worker = threading.Thread(
            target=self._run,
            name=f'biliup-login-{self.session_id}',
            daemon=True,
        )
        self._worker.start()

    def wait_for_qrcode(self, timeout: float = 30.0):
        """阻塞等待二维码就绪(status 离开 created),供 start_session 同步返回。

        start() 起后台线程跑 PTY 子进程;此处轮询直到后台线程把 status 推进到
        waiting(已拿到 url)或 failed,再让调用方返回给前端——因为前端 useEffect
        仅在 status=waiting 时轮询,若立即返回 created 前端就不会继续拉取。
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
                self.error_message = '获取二维码超时(biliup login 未输出二维码 URL)'
                self.updated_at = time.time()

    def _run(self):
        master_fd = None
        slave_fd = None
        proc = None
        try:
            # 独立 cwd,避免子进程在服务目录写入意外文件
            self._tmpdir = tempfile.mkdtemp(prefix='biliup-login-')
            master_fd, slave_fd = pty.openpty()
            biliup_path = config.get('biliup_path', 'biliup')
            try:
                proc = subprocess.Popen(
                    [biliup_path, '-u', self.cookie_path, 'login'],
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    cwd=self._tmpdir,
                    close_fds=True,
                )
            except FileNotFoundError:
                self._fail(f'未找到 biliup 命令: {biliup_path}')
                return
            finally:
                # 父进程关闭 slave 端,子进程持有副本作其 TTY
                os.close(slave_fd)
                slave_fd = None

            self._monitor(master_fd, proc)
        except Exception as exc:
            logger.exception('biliup 登录子进程异常')
            self._fail_if_active(f'登录子进程异常: {exc}')
        finally:
            if slave_fd is not None:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            self._cleanup_tmpdir()

    def _monitor(self, master_fd, proc):
        # biliup login 菜单:0 账号密码 / 1 短信登录(default) / 2 扫码登录 /
        # 3 浏览器登录 / 4 网页Cookie1 / 5 网页Cookie2
        # 选「浏览器登录」(index 3):从 default(1) 按 2 次↓ + 回车。
        # 浏览器登录在 stdout 打印二维码 URL,便于提取;随后同样轮询扫码完成。
        menu_selected = False
        buffer = ''
        deadline = time.time() + _SESSION_MAX_AGE

        while True:
            if self._stop_flag:
                self._terminate(proc)
                return
            if time.time() > deadline:
                self._fail_if_active('扫码登录超时(二维码可能已过期)')
                self._terminate(proc)
                return

            try:
                readable, _, _ = select.select([master_fd], [], [], 1.0)
            except (OSError, ValueError):
                break

            if master_fd in readable:
                try:
                    data = os.read(master_fd, 8192)
                except OSError:
                    break
                if not data:
                    break
                buffer += data.decode('utf-8', errors='replace')

                plain = _strip_ansi(buffer)
                if not menu_selected and '登录方式' in plain:
                    os.write(master_fd, b'\x1b[B\x1b[B\r')  # ↓ ↓ Enter
                    menu_selected = True

                if self.qrcode_url is None:
                    match = _QRCODE_URL_RE.search(plain)
                    if match:
                        with self._lock:
                            self.qrcode_url = match.group(0)
                            if self.status == 'created':
                                self.status = 'waiting'
                                self.updated_at = time.time()

            if proc.poll() is not None:
                break

        # 子进程退出后,PTY 读端会以 EIO/EOF 结束——读循环在 OSError/空读处
        # break,跳过了循环末尾的 proc.poll(),导致此处的 proc.returncode 可能
        # 仍是 None(子进程已退出但尚未被 reap)。显式 wait() 拿到真实退出码,
        # 仅用于失败时的错误信息。
        if proc.returncode is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        return_code = proc.returncode

        with self._lock:
            if self.status in ('stopped', 'expired', 'failed'):
                return

        # biliup-rs 仅在登录成功时才写 cookie 文件,故「文件存在且非空」是成功的
        # 权威信号——比依赖退出码更可靠(退出码会因上述 reap 时序偶发为 None)。
        cookie_ok = (
            os.path.isfile(self.cookie_path)
            and os.path.getsize(self.cookie_path) > 0
        )
        if cookie_ok:
            with self._lock:
                if self.status in ('created', 'waiting'):
                    self.status = 'success'
                    self.updated_at = time.time()
            logger.info('biliup 扫码登录成功,cookie -> %s', self.cookie_path)
        else:
            tail = _strip_ansi(buffer)[-200:].strip()
            self._fail_if_active(f'biliup login 未成功(退出码 {return_code}): {tail}')

    @staticmethod
    def _terminate(proc):
        try:
            proc.terminate()
        except Exception:
            pass

    def _cleanup_tmpdir(self):
        if not self._tmpdir or not os.path.isdir(self._tmpdir):
            return
        try:
            for name in os.listdir(self._tmpdir):
                path = os.path.join(self._tmpdir, name)
                try:
                    os.remove(path)
                except OSError:
                    pass
            os.rmdir(self._tmpdir)
        except Exception:
            logger.debug('清理 biliup login 临时目录失败: %s', self._tmpdir, exc_info=True)
        finally:
            self._tmpdir = None

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
        # start() 起后台线程跑 PTY;同步等二维码就绪再返回,前端即可直接拿到
        # status=waiting + qrcode_url(前端 useEffect 仅在 waiting 时轮询)。
        session.wait_for_qrcode(timeout=30)
        self._schedule_cleanup()
        return session.snapshot()

    def get_session(self, session_id: str) -> BiliupLoginSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            raise ValueError('biliup login session not found')
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


biliup_login_manager = BiliupLoginManager()
