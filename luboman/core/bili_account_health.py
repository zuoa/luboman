"""B站投稿账号登录态巡检。

判断登录态是否有效的标准做法是打 nav 接口，读 data.isLogin：
    GET https://api.bilibili.com/x/web-interface/nav  →  data.isLogin == True
plugins/bilibili.py 的 do_login 与 core/upload.py 的 login_by_cookies 用的就是它，
这里只是把它抽出来，供后台周期任务对所有投稿账号批量探测。
"""
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

from luboman.database.db import DB

logger = logging.getLogger('luboman')

# nav 接口：data.isLogin == True 表示 cookie 仍有效
_NAV_URL = 'https://api.bilibili.com/x/web-interface/nav'
_REQUEST_TIMEOUT = 8
_MAX_CHECK_WORKERS = 8


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


def cookies_from_biliup_file(filepath):
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


def resolve_cookie_str(account):
    """根据账号配置取到用于探测登录态的 cookie 字符串，取不到返回 None。

    与 biliweb.py 的登录分支一致：优先用 bili_cookies_filepath 指向的 cookies.json
    （biliup login 产物），否则回退到 bili_cookies 内联字符串。
    """
    filepath = account.get('bili_cookies_filepath')
    if filepath and os.path.isfile(filepath):
        cookie_str = cookies_from_biliup_file(filepath)
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


def profile_from_cookie_str(cookie_str):
    """验证 cookie 并从 nav 响应提取账号资料。"""
    ok, data = is_login_valid(cookie_str)
    if not ok:
        return None, data

    profile = data.get('data') or {}
    return {
        'account_name': profile.get('uname') or profile.get('name'),
        'account_avatar': profile.get('face'),
    }, data


def check_account(account):
    """检查单个账号，返回适合 Web/API 展示的结构化状态。"""
    result = {
        'id': account.get('id'),
        'account_name': account.get('account_name'),
        'account_avatar': account.get('account_avatar'),
        'state_active': account.get('state_active', 1),
        'login_valid': None,
        'status': 'unknown',
        'message': '未检测',
        'checked_at': datetime.now().isoformat(timespec='seconds'),
    }

    if not account.get('state_active', 1):
        result.update({
            'status': 'disabled',
            'message': '账号已停用',
        })
        return result

    cookie_str = resolve_cookie_str(account)
    if not cookie_str:
        result.update({
            'login_valid': False,
            'status': 'missing_credentials',
            'message': '未配置可用 cookie',
        })
        return result

    ok, data = is_login_valid(cookie_str)
    if ok:
        profile = data.get('data') or {}
        result.update({
            'login_valid': True,
            'status': 'valid',
            'message': '登录态有效',
            'account_name': profile.get('uname') or result.get('account_name'),
            'account_avatar': profile.get('face') or result.get('account_avatar'),
        })
        return result

    message = '登录态失效'
    if isinstance(data, dict):
        message = data.get('message') or message
    result.update({
        'login_valid': False,
        'status': 'invalid',
        'message': message,
    })
    return result


def _check_error_result(account, exc):
    logger.exception('B站账号登录态检测失败')
    return {
        'id': account.get('id'),
        'account_name': account.get('account_name'),
        'account_avatar': account.get('account_avatar'),
        'state_active': account.get('state_active', 1),
        'login_valid': False,
        'status': 'error',
        'message': str(exc),
        'checked_at': datetime.now().isoformat(timespec='seconds'),
    }


def _check_account_list(accounts):
    if not accounts:
        return []
    if len(accounts) == 1:
        try:
            return [check_account(accounts[0])]
        except Exception as exc:
            return [_check_error_result(accounts[0], exc)]

    results = [None] * len(accounts)
    worker_count = min(_MAX_CHECK_WORKERS, len(accounts))
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix='bili-account-check',
    ) as executor:
        future_to_index = {
            executor.submit(check_account, account): index
            for index, account in enumerate(accounts)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                results[index] = _check_error_result(accounts[index], exc)

    return [result for result in results if result is not None]


def check_accounts(account_id=None):
    """检查账号登录态；传 account_id 时只检查指定账号。"""
    accounts = DB.list_bili_account()
    selected = [
        account
        for account in accounts
        if account_id is None or str(account.get('id')) == str(account_id)
    ]
    return _check_account_list(selected)


def check_active_accounts():
    """遍历所有启用中的 B站投稿账号，返回 (启用账号数, 失效账号列表)。"""
    accounts = DB.list_bili_account()
    active_accounts = [
        account
        for account in accounts
        if account.get('state_active', 1)
    ]
    accounts_by_id = {str(account.get('id')): account for account in active_accounts}
    invalid = []
    results = _check_account_list(active_accounts)

    for result in results:
        acc = accounts_by_id.get(str(result.get('id')), result)
        name = acc.get('account_name') or f"id={acc.get('id')}"

        if result.get('status') == 'missing_credentials':
            invalid.append(acc)
            logger.warning(f'B站账号「{name}」未配置可用 cookie，视为登录态失效')
            continue

        if result.get('login_valid') is not True:
            invalid.append(acc)
            logger.warning(f'B站账号「{name}」登录态失效')

    return len(active_accounts), invalid


# 兼容旧的私有函数名，避免已有导入断裂。
_cookies_from_biliup_file = cookies_from_biliup_file
_resolve_cookie_str = resolve_cookie_str
