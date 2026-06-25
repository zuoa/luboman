"""B站投稿账号登录态巡检。

判断登录态是否有效的标准做法是打 nav 接口，读 data.isLogin：
    GET https://api.bilibili.com/x/web-interface/nav  →  data.isLogin == True
plugins/bilibili.py 的 do_login 与 core/upload.py 的 login_by_cookies 用的就是它，
这里只是把它抽出来，供后台周期任务对所有投稿账号批量探测。
"""
import json
import logging
import os

import requests

from luboman.database.db import DB

logger = logging.getLogger('luboman')

# nav 接口：data.isLogin == True 表示 cookie 仍有效
_NAV_URL = 'https://api.bilibili.com/x/web-interface/nav'
_REQUEST_TIMEOUT = 8


def _parse_cookie_string(cookie_str):
    """解析 'k1=v1; k2=v2;' 形式的 cookie 字符串为 dict。"""
    cookies = {}
    for item in (cookie_str or '').split(';'):
        item = item.strip()
        if not item or '=' not in item:
            continue
        key, value = item.split('=', 1)
        cookies[key.strip()] = value.strip()
    return cookies


def _cookies_from_biliup_file(filepath):
    """从 biliup login 生成的 cookies.json 中拼出 cookie 字符串。

    与 plugins/bilibili.py 的 load_cookies 同源：文件结构为
    {"cookie_info": {"cookies": [{"name": ..., "value": ...}, ...]}}。
    """
    try:
        with open(filepath, encoding='utf-8') as f:
            data = json.load(f)
        return ''.join(f"{c['name']}={c['value']};" for c in data['cookie_info']['cookies'])
    except Exception as e:
        logger.debug(e)
        logger.error(f'读取 biliup cookie 文件失败: {filepath}')
        return None


def _resolve_cookie_str(account):
    """根据账号配置取到用于探测登录态的 cookie 字符串，取不到返回 None。

    与 biliweb.py 的登录分支一致：优先用 bili_cookies_filepath 指向的 cookies.json
    （biliup login 产物），否则回退到 bili_cookies 内联字符串。
    """
    filepath = account.get('bili_cookies_filepath')
    if filepath and os.path.isfile(filepath):
        cookie_str = _cookies_from_biliup_file(filepath)
        if cookie_str:
            return cookie_str
    return account.get('bili_cookies') or None


def is_login_valid(cookie_str):
    """调用 nav 接口判断登录态是否有效，返回 (是否有效, nav 原始响应)。"""
    try:
        resp = requests.get(
            _NAV_URL,
            headers={
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'referer': 'https://www.bilibili.com/',
            },
            cookies=_parse_cookie_string(cookie_str),
            timeout=_REQUEST_TIMEOUT,
        )
        data = resp.json()
        return bool(data.get('data', {}).get('isLogin', False)), data
    except Exception as e:
        logger.debug(e)
        logger.error('调用 nav 接口验证登录态失败')
        return False, None


def check_active_accounts():
    """遍历所有启用中的 B站投稿账号，返回 (启用账号数, 失效账号列表)。"""
    accounts = DB.list_bili_account()
    invalid = []
    active_count = 0
    for acc in accounts:
        if not acc.get('state_active', 1):
            continue
        active_count += 1
        name = acc.get('account_name') or f"id={acc.get('id')}"

        cookie_str = _resolve_cookie_str(acc)
        if not cookie_str:
            invalid.append(acc)
            logger.warning(f'B站账号「{name}」未配置可用 cookie，视为登录态失效')
            continue

        ok, _ = is_login_valid(cookie_str)
        if not ok:
            invalid.append(acc)
            logger.warning(f'B站账号「{name}」登录态失效')

    return active_count, invalid
