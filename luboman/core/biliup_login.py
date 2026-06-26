"""biliup-rs login session management for Web UI.

The CLI login flow is intentionally kept as a narrow command runner:
only ``biliup -u <cookies.json> login`` is spawned, while stdout and stdin are
bridged to the page so QR-code and prompt based flows both remain usable.
"""

import logging
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from collections import deque
from typing import Deque, Dict, List, Optional

from luboman.config import config

logger = logging.getLogger('luboman')


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
        self.exit_code = None
        self.error_message = None
        self.process = None
        self._lock = threading.Lock()
        self._output: Deque[Dict[str, object]] = deque(maxlen=500)
        self._next_output_index = 0
        self._reader = None

        biliup_path = config.get('biliup_path', 'biliup')
        self.command: List[str] = [biliup_path, '-u', self.cookie_path, 'login']

    def start(self):
        with self._lock:
            if self.status != 'created':
                return
            self.status = 'running'
            self.updated_at = time.time()

        try:
            os.makedirs(os.path.dirname(self.cookie_path), exist_ok=True)
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
            )
        except FileNotFoundError:
            self._fail(
                '未找到 biliup 命令，请安装 biliup 或通过 biliup_path 配置二进制路径'
            )
            return
        except Exception as exc:
            logger.exception('启动 biliup login 失败')
            self._fail(f'启动 biliup login 失败: {exc}')
            return

        self._append_output(f"$ {shlex.join(self.command)}")
        self._reader = threading.Thread(
            target=self._read_output,
            name=f'biliup-login-{self.session_id}',
            daemon=True,
        )
        self._reader.start()

    def _append_output(self, line: str):
        with self._lock:
            self._output.append({'index': self._next_output_index, 'line': line})
            self._next_output_index += 1
            self.updated_at = time.time()

    def _fail(self, message: str):
        logger.error(message)
        with self._lock:
            self.status = 'failed'
            self.error_message = message
            self.updated_at = time.time()
            self._output.append({'index': self._next_output_index, 'line': message})
            self._next_output_index += 1

    def _read_output(self):
        try:
            if self.process and self.process.stdout:
                for line in self.process.stdout:
                    clean_line = line.rstrip('\n')
                    self._append_output(clean_line)
                    logger.info('[biliup login] %s', clean_line)

            exit_code = self.process.wait() if self.process else -1
            with self._lock:
                self.exit_code = exit_code
                if self.status == 'running':
                    if exit_code == 0 and os.path.isfile(self.cookie_path):
                        self.status = 'success'
                    elif exit_code == 0:
                        self.status = 'failed'
                        self.error_message = 'biliup login 已退出，但未生成 cookies 文件'
                    else:
                        self.status = 'failed'
                        self.error_message = f'biliup login 退出码: {exit_code}'
                    self.updated_at = time.time()
        except Exception as exc:
            logger.exception('读取 biliup login 输出失败')
            self._fail(f'读取 biliup login 输出失败: {exc}')

    def send_input(self, text: str):
        with self._lock:
            if self.status != 'running' or not self.process or not self.process.stdin:
                raise RuntimeError('biliup login 会话未运行')
            stdin = self.process.stdin

        stdin.write(f'{text}\n')
        stdin.flush()
        self._append_output(f"> {text}")

    def stop(self, status: str = 'stopped', message: Optional[str] = None):
        with self._lock:
            process = self.process
            if self.status not in ('running', 'created'):
                return
            self.status = status
            if message:
                self.error_message = message
                self._output.append({
                    'index': self._next_output_index,
                    'line': message,
                })
                self._next_output_index += 1
            self.updated_at = time.time()

        if process and process.poll() is None:
            process.terminate()

    def snapshot(self, since: Optional[int] = None) -> Dict[str, object]:
        with self._lock:
            output = list(self._output)
            if since is not None:
                output = [item for item in output if int(item['index']) >= since]
            return {
                'session_id': self.session_id,
                'cookie_path': self.cookie_path,
                'command': shlex.join(self.command),
                'status': self.status,
                'exit_code': self.exit_code,
                'error_message': self.error_message,
                'output': [item['line'] for item in output],
                'output_offset': self._next_output_index,
                'created_at': self.created_at,
                'updated_at': self.updated_at,
            }


class BiliupLoginManager:
    def __init__(self, max_age_seconds: int = 3600):
        self._sessions: Dict[str, BiliupLoginSession] = {}
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
        return self.get_session(session_id).snapshot(since=since)

    def send_input(self, session_id: str, text: str):
        session = self.get_session(session_id)
        session.send_input(text)
        return session.snapshot()

    def stop_session(self, session_id: str):
        session = self.get_session(session_id)
        session.stop()
        return session.snapshot()

    def cleanup(self, max_age_seconds: Optional[int] = None):
        max_age_seconds = max_age_seconds or self.max_age_seconds
        now = time.time()
        sessions_to_expire = []
        with self._lock:
            for session_id, session in list(self._sessions.items()):
                if session.status in ('created', 'running'):
                    if now - session.created_at > max_age_seconds:
                        sessions_to_expire.append(session)
                    continue

                if now - session.updated_at > max_age_seconds:
                    self._sessions.pop(session_id, None)

        for session in sessions_to_expire:
            session.stop(
                status='expired',
                message='biliup login 会话已超时，已停止',
            )


biliup_login_manager = BiliupLoginManager()
