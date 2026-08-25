"""B 站稿件 BV 号提取与公开状态探测。

投稿成功后先记为审核中；8 小时内每 10 分钟打一次公开页 view 接口，
能读到稿件则改为已发布并停止探测。超时仍不可见的保持审核中。
"""
import logging
import re
from typing import Any, Optional

import requests

logger = logging.getLogger('luboman')

BV_RE = re.compile(r'BV[0-9A-Za-z]{10}')
_VIEW_URL = 'https://api.bilibili.com/x/web-interface/view'
_REQUEST_TIMEOUT = 8
_VIEW_HEADERS = {
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'referer': 'https://www.bilibili.com/',
}

# view 接口：0 且带 data 视为已对公开展示
_PUBLISHED_CODE = 0


def extract_bvid(value: Any) -> Optional[str]:
    """从投稿返回值 / 日志文本里抽出第一个 BV 号。"""
    seen = set()

    def walk(item):
        if item is None or id(item) in seen:
            return None
        if isinstance(item, dict):
            seen.add(id(item))
            for key in ('bvid', 'BVID', 'bVid', 'avid_bvid'):
                found = walk(item.get(key))
                if found:
                    return found
            for nested in item.values():
                found = walk(nested)
                if found:
                    return found
            return None
        if isinstance(item, (list, tuple)):
            seen.add(id(item))
            for nested in item:
                found = walk(nested)
                if found:
                    return found
            return None
        if isinstance(item, str):
            match = BV_RE.search(item)
            return match.group(0) if match else None
        return None

    return walk(value)


def check_bvid_published(bvid: str) -> Optional[bool]:
    """探测 BV 是否已对公开展示。

    True：已发布；False：仍不可见（审核中/自见/未过审）；None：网络/接口异常，本轮跳过。
    """
    if not bvid or not BV_RE.fullmatch(bvid):
        return None
    try:
        resp = requests.get(
            _VIEW_URL,
            params={'bvid': bvid},
            headers=_VIEW_HEADERS,
            timeout=_REQUEST_TIMEOUT,
        )
        payload = resp.json()
    except Exception as exc:
        logger.warning('探测稿件公开状态失败: %s (%s)', bvid, exc)
        return None

    code = payload.get('code')
    data = payload.get('data') or {}
    if code == _PUBLISHED_CODE and (data.get('bvid') or data.get('aid')):
        return True
    return False


def watch_pending_publications() -> dict:
    """扫描窗口内审核中的 B 站稿件：补 BV、探测公开页、已发布则停。"""
    from luboman.database.db import DB

    tasks = DB.list_bili_publish_watch_tasks()
    stats = {'checked': 0, 'published': 0, 'skipped': 0, 'errors': 0}
    for task in tasks:
        task_id = task.get('task_id')
        bvid = (task.get('bvid') or '').strip() or extract_bvid(task.get('result'))
        if bvid and bvid != (task.get('bvid') or '').strip():
            DB.save_submission_task_bvid(task_id, bvid)
        if not bvid:
            stats['skipped'] += 1
            continue

        published = check_bvid_published(bvid)
        stats['checked'] += 1
        if published is True:
            DB.mark_submission_task_published(task_id, bvid=bvid)
            stats['published'] += 1
            logger.info('稿件已发布: task_id=%s bvid=%s', task_id, bvid)
        elif published is False:
            DB.mark_submission_task_publish_checked(task_id)
        else:
            stats['errors'] += 1
    return stats
