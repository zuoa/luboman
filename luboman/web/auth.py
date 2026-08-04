"""WebUI 访问鉴权：密码登录 + 登录限流（防暴力破解）。

通过环境变量 WEBUI_PASSWORD 启用；未设置时 AUTH_ENABLED=False，所有请求放行
（老部署升级后行为不变）。仅依赖标准库。

- 会话：内存 {token: 过期时间戳}，重启失效；cookie HttpOnly + SameSite=Lax，7 天有效。
- 密码比对：hmac.compare_digest 防时序攻击；失败响应统一延迟，避免响应时间泄露。
- 限流：单 IP 10 分钟内失败 5 次锁定 10 分钟；全局 20 次失败/分钟兜底，
  防分布式慢速爆破。锁定/计数都在内存中，重启清零。
"""

import asyncio
import hmac
import logging
import os
import secrets
import time

from aiohttp import web

logger = logging.getLogger('luboman')

COOKIE_NAME = 'luboman_token'
SESSION_TTL = 7 * 24 * 3600      # 会话有效期（秒）
FAIL_WINDOW = 600                # 失败计数窗口（秒）
MAX_FAILS_PER_IP = 5             # 窗口内单 IP 最大失败次数
LOCKOUT_DURATION = 600           # 触发锁定后的锁定时长（秒）
GLOBAL_WINDOW = 60               # 全局限流窗口（秒）
MAX_FAILS_GLOBAL = 20            # 窗口内全局最大失败次数
LOGIN_FAIL_DELAY = 0.5           # 失败响应恒定延迟（秒）

_password = os.environ.get('WEBUI_PASSWORD') or ''
AUTH_ENABLED = bool(_password)

_sessions = {}       # token -> expire_ts
_fail_by_ip = {}     # ip -> [fail_ts, ...]
_locked_until = {}   # ip -> lock_until_ts
_fail_global = []    # [fail_ts, ...]


def client_ip(request) -> str:
    """取客户端 IP：优先 X-Forwarded-For 首段（nginx 反代会补），回退直连地址。"""
    xff = request.headers.get('X-Forwarded-For')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote or 'unknown'


def verify_password(candidate: str) -> bool:
    """恒定时间比对，防时序攻击。"""
    if not AUTH_ENABLED or not isinstance(candidate, str):
        return False
    return hmac.compare_digest(candidate.encode(), _password.encode())


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    # 顺手清理过期会话，避免内存缓慢增长
    now = time.time()
    for t in [t for t, exp in _sessions.items() if exp <= now]:
        _sessions.pop(t, None)
    return token


def check_session(token) -> bool:
    if not token:
        return False
    exp = _sessions.get(token)
    if exp is None:
        return False
    if exp <= time.time():
        _sessions.pop(token, None)
        return False
    return True


def drop_session(token):
    if token:
        _sessions.pop(token, None)


def token_from_request(request):
    return request.cookies.get(COOKIE_NAME)


def is_logged_in(request) -> bool:
    return check_session(token_from_request(request))


def _prune(ts_list, window, now):
    return [t for t in ts_list if t > now - window]


def ip_lock_remaining(ip: str) -> float:
    """返回该 IP 剩余锁定秒数，未锁定为 0。"""
    until = _locked_until.get(ip, 0)
    remaining = until - time.time()
    if remaining <= 0:
        _locked_until.pop(ip, None)
        return 0
    return remaining


def global_limit_hit() -> bool:
    now = time.time()
    _fail_global[:] = _prune(_fail_global, GLOBAL_WINDOW, now)
    return len(_fail_global) >= MAX_FAILS_GLOBAL


def record_failure(ip: str):
    """记录一次失败；若触发单 IP 锁定则设置锁定截止时间。"""
    now = time.time()
    _fail_global.append(now)
    fails = _prune(_fail_by_ip.get(ip, []), FAIL_WINDOW, now)
    fails.append(now)
    _fail_by_ip[ip] = fails
    if len(fails) >= MAX_FAILS_PER_IP:
        _locked_until[ip] = now + LOCKOUT_DURATION
        _fail_by_ip[ip] = []
        logger.warning(f"登录失败次数过多，IP {ip} 已锁定 {LOCKOUT_DURATION // 60} 分钟")


def clear_failures(ip: str):
    _fail_by_ip.pop(ip, None)
    _locked_until.pop(ip, None)


async def constant_delay():
    """失败路径统一延迟，抹平比对耗时差异。"""
    await asyncio.sleep(LOGIN_FAIL_DELAY)


# 鉴权白名单：登录相关端点与探活
_PUBLIC_PATHS = {'/', '/ping', '/v1/Auth/login', '/v1/Auth/status', '/v1/Auth/check'}


@web.middleware
async def auth_middleware(request, handler):
    if not AUTH_ENABLED:
        return await handler(request)
    if request.method == 'OPTIONS':  # CORS 预检放行
        return await handler(request)
    if request.path in _PUBLIC_PATHS:
        return await handler(request)
    if is_logged_in(request):
        return await handler(request)
    return web.json_response(
        {'success': False, 'code': 401, 'message': '未登录或登录已过期'},
        status=401,
    )
