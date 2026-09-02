"""B站充电专属投稿：档位解析与账号档位列表拉取。"""
import logging

import requests

from luboman.core import bili_account_health

logger = logging.getLogger('luboman')

_REQUEST_TIMEOUT = 10
_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
# 6 元档（privilege_type=10）不能发充电专属视频
_NO_EXCLUSIVE_PRIVILEGE_TYPES = {10}
_PRIVILEGE_PRICE_YUAN = {
    10: 6,
    20: 30,
    30: 50,
    40: 88,
    50: 128,
    60: 288,
    70: 588,
    80: 998,
    100: 18,
    110: 238,
    130: 68,
}


def resolve_bili_upower(room_data, template_info=None):
    """房间开了充电投稿且有档位时返回投稿字段，否则返回 None。

    档位优先用投稿账号 upower_level_id，没有再回退房间旧字段 bili_upower_level_id。
    开关为 1 则开；开关为 0 但房间还留着旧档位 ID，视为从未用新 UI 保存过的旧数据，仍开。
    用户在新 UI 关掉开关时，update_live_room 会清空旧档位 ID。
    """
    room_data = room_data or {}
    template_info = template_info or {}
    account = template_info.get('bili_account') or {}
    legacy_level = room_data.get('bili_upower_level_id')
    enabled_raw = room_data.get('bili_upower_enabled')

    if enabled_raw in (1, '1', True):
        enabled = True
    elif enabled_raw in (0, '0', False) and not legacy_level:
        enabled = False
    else:
        enabled = bool(legacy_level)

    if not enabled:
        return None

    level_id = account.get('upower_level_id') or legacy_level
    if not level_id:
        return None
    return {'charging_pay': 1, 'upower_level_id': str(level_id)}


def _mid_from_cookie_str(cookie_str):
    cookies = bili_account_health._parse_cookie_string(cookie_str)
    mid = cookies.get('DedeUserID')
    if mid and str(mid).isdigit():
        return int(mid)
    return None


def _price_yuan(item):
    privilege_type = item.get('privilege_type')
    try:
        privilege_type = int(privilege_type) if privilege_type is not None else None
    except (TypeError, ValueError):
        privilege_type = None
    if privilege_type in _PRIVILEGE_PRICE_YUAN:
        return _PRIVILEGE_PRICE_YUAN[privilege_type]
    raw = item.get('price')
    try:
        raw = int(raw)
    except (TypeError, ValueError):
        return None
    if raw >= 100:
        return raw // 100
    return raw


def _item_id(item):
    for key in ('upower_level_id', 'privilege_id', 'id', 'level_id', 'item_id'):
        value = item.get(key)
        if value is None or value == '':
            continue
        text = str(value)
        # 投稿要的是 snowflake，过滤掉 privilege_type 这种个位数/两位数
        if text.isdigit() and len(text) >= 8:
            return text
    return None


def _walk_level_dicts(payload, acc=None):
    if acc is None:
        acc = []
    if isinstance(payload, dict):
        if _item_id(payload) and (
            'privilege_type' in payload
            or 'price' in payload
            or payload.get('name')
            or payload.get('title')
            or payload.get('level_name')
        ):
            acc.append(payload)
        for value in payload.values():
            _walk_level_dicts(value, acc)
    elif isinstance(payload, list):
        for value in payload:
            _walk_level_dicts(value, acc)
    return acc


def _normalize_levels(raw_items):
    seen = set()
    levels = []
    for item in raw_items:
        level_id = _item_id(item)
        if not level_id or level_id in seen:
            continue
        seen.add(level_id)
        try:
            privilege_type = int(item.get('privilege_type')) if item.get('privilege_type') is not None else None
        except (TypeError, ValueError):
            privilege_type = None
        name = item.get('name') or item.get('title') or item.get('level_name') or ''
        price = _price_yuan(item)
        exclusive_ok = privilege_type not in _NO_EXCLUSIVE_PRIVILEGE_TYPES
        if price == 6:
            exclusive_ok = False
        label_parts = []
        if name:
            label_parts.append(str(name))
        if price:
            label_parts.append(f'{price}元档')
        levels.append({
            'id': level_id,
            'name': name,
            'price': price,
            'privilege_type': privilege_type,
            'exclusive_ok': exclusive_ok,
            'label': ' '.join(label_parts) or level_id,
        })
    return levels


def _get_json(url, cookies, params=None):
    resp = requests.get(
        url,
        params=params or {},
        cookies=cookies,
        headers={
            'user-agent': _UA,
            'referer': 'https://member.bilibili.com/',
        },
        timeout=_REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_upower_levels(account):
    """用投稿账号 cookie 拉取该 UP 的充电档位列表。

    返回 {levels, selected_id, mid}。cookie 缺失或登录失效抛 ValueError。
    """
    cookie_str = bili_account_health.resolve_cookie_str(account)
    if not cookie_str:
        raise ValueError('未配置可用 cookie')

    ok, nav = bili_account_health.is_login_valid(cookie_str)
    if not ok:
        message = None
        if isinstance(nav, dict):
            message = nav.get('message')
        raise ValueError(message or '登录态失效，请重新登录')

    cookies = bili_account_health._parse_cookie_string(cookie_str)
    mid = _mid_from_cookie_str(cookie_str)
    nav_data = (nav or {}).get('data') if isinstance(nav, dict) else None
    if not mid and isinstance(nav_data, dict):
        mid = nav_data.get('mid')

    endpoints = [
        ('https://api.bilibili.com/x/upower/v2/charge/privilege/up/list', {}),
        ('https://api.bilibili.com/x/upower/v2/charge/privilege/item/list', {'up_mid': mid} if mid else {}),
        ('https://api.bilibili.com/x/upower/item/list', {'up_mid': mid} if mid else {}),
        ('https://api.bilibili.com/x/upower/up/level/list', {'up_mid': mid} if mid else {}),
        ('https://member.bilibili.com/x/vupre/web/archive/pre', {}),
    ]

    last_error = None
    for url, params in endpoints:
        if 'up_mid' in params and not params.get('up_mid'):
            continue
        try:
            payload = _get_json(url, cookies, params)
        except Exception as e:
            last_error = e
            logger.debug('拉取充电档位失败 %s: %s', url, e)
            continue
        if not isinstance(payload, dict):
            continue
        code = payload.get('code')
        if code not in (0, None):
            last_error = payload.get('message') or f'code={code}'
            logger.debug('充电档位接口业务失败 %s: %s', url, last_error)
            continue
        levels = _normalize_levels(_walk_level_dicts(payload.get('data', payload)))
        if levels:
            logger.info('充电档位来自 %s，共 %s 个', url, len(levels))
            return {
                'levels': levels,
                'selected_id': account.get('upower_level_id') or None,
                'mid': mid,
            }

    # 接口通了但没有 snowflake 档位：当成未开通，不要把网络错误伪装成空列表
    if last_error and not isinstance(last_error, str):
        raise ValueError(f'拉取充电档位失败: {last_error}')
    return {
        'levels': [],
        'selected_id': account.get('upower_level_id') or None,
        'mid': mid,
        'message': '未开通充电计划，或账号下没有可投稿的充电档位',
    }
