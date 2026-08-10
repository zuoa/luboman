import asyncio
import datetime
import functools
import json
import logging
import os
import uuid
from urllib.parse import quote

import aiohttp_cors
from aiohttp import web
from playhouse.shortcuts import model_to_dict

from luboman.config import config
from luboman.core import bili_account_health
from luboman.core.async_utils import run_blocking
from luboman.core.async_upload import UploadPriority, schedule_bili_submission, schedule_douyin_submission
from luboman.core.dance_clip import clip_scheduler, ensure_douyin_clip, CLIP_SERIES_CODE_PREFIX
from luboman.core.biliup_login import biliup_login_manager
from luboman.core.douyin_login import douyin_login_manager
from luboman.core.runtime import (
    collect_runtime_stats,
    reconcile_room_runtime,
    start_room_runtime,
    stop_room_runtime,
)
from luboman.core.upload import BiliBili, Data
from luboman.core.utils import get_public_dir, get_video_dir
from luboman.database.db import (
    DB,
    RECORD_FILE_STATUS_COMPLETED,
    RECORD_FILE_STATUS_RECORDING,
    SUBMISSION_TASK_SOURCE_FILE_MANAGER,
    resolve_room_bili_template_ids,
    resolve_room_douyin_template_ids,
)
from luboman.web import auth
from luboman.database.models import (
    BiliAccount,
    BiliUploadTemplate,
    DouyinAccount,
    DouyinUploadTemplate,
    GlobalConfig,
    LiveRoom,
    RecordFile,
    db,
)

logger = logging.getLogger('luboman')


def default_json(obj):
    if isinstance(obj, datetime.datetime):
        return str(obj)
    raise TypeError('Unable to serialize {!r}'.format(obj))


json_dumps = functools.partial(json.dumps, default=default_json)
json_response = functools.partial(web.json_response, dumps=json_dumps)

routes = web.RouteTableDef()


async def run_db(func, *args, **kwargs):
    """Run a synchronous database/business operation off the event loop."""
    return await run_blocking(func, *args, **kwargs)


def _parse_cookie_string(cookies_str, strict=True):
    cookies = {}
    for item in cookies_str.split(';'):
        item = item.strip()
        if not item:
            continue
        if '=' not in item:
            if strict:
                raise ValueError(f"invalid cookie item: {item}")
            continue
        key, value = item.split('=', 1)
        cookies[key] = value
    return cookies


def _cookies_to_string(cookies):
    return ''.join(f"{key}={value};" for key, value in cookies.items() if value is not None)


def _database_health_check():
    with db.connection_context():
        db.execute_sql("SELECT 1")
    return True


def _set_config_values(config_data):
    with db.connection_context():
        with db.atomic():
            for key, value in config_data.items():
                try:
                    cfg = GlobalConfig.get(GlobalConfig.key == key)
                    cfg.value = value
                    cfg.save()
                except GlobalConfig.DoesNotExist:
                    GlobalConfig.create(key=key, value=value)

    config.data.update(DB.load_config())


def _pre_archive_data():
    one_account = DB.get_first_bili_account()
    if one_account is None:
        raise ValueError("no account found")

    cookies = _parse_cookie_string(one_account.bili_cookies or '', strict=False)
    with BiliBili(Data()) as bili:
        return bili.tid_archive(cookies)


def _create_live_room(data):
    return DB.create_live_room(data)


def _get_live_room_data(row_id):
    return DB.get_live_room_data(row_id)


def _update_live_room_data(data):
    row = DB.update_live_room(data)
    return row, _get_live_room_data(data["id"])


def _delete_live_room(row_id):
    return DB.delete_live_room(row_id)


def _probe_live_room(room_url):
    """按 URL 匹配直播插件并抓取直播间信息（不启动录制）。

    插件实例化会连带启动事件管理器与录制线程，探测完必须立即停用，
    避免后台空转。未开播时多数平台拿不到房间信息，仅返回平台识别结果。
    """
    import re as _re

    from luboman.core.decorators import PluginTool

    plugin = None
    platform = None
    for pg_cls in PluginTool.live_plugins:
        if _re.match(pg_cls.VALID_URL_BASE, room_url):
            platform = pg_cls.__name__
            plugin = pg_cls('', room_url)
            break

    if not plugin:
        raise ValueError("未识别的直播间链接，暂不支持该平台")

    try:
        is_living = plugin.check_live(is_check_status=True)
    except Exception as e:
        logger.warning(f"探测直播间失败: {room_url}: {e}")
        is_living = False
    finally:
        # 立即停用后台线程（录制线程最多再空转一个 3 秒轮询周期后自行退出）
        try:
            plugin._active = False
            plugin.event_manager.stop()
        except Exception:
            pass

    room_data = getattr(plugin, 'room_data', {}) or {}
    return {
        'room_platform': room_data.get('room_platform') or platform,
        'live_state': 1 if is_living else 0,
        # 房间名优先取主播昵称，退回直播标题
        'room_name': room_data.get('room_owner') or room_data.get('room_title') or '',
        'room_title': room_data.get('room_title', ''),
        'room_owner': room_data.get('room_owner', ''),
        'room_cover_url': room_data.get('room_cover_url', ''),
        'room_owner_avatar': room_data.get('room_owner_avatar', ''),
    }


def _prepare_bili_account_payload(data, require_credentials):
    payload = dict(data)
    if payload.get('bili_cookies_filepath'):
        filepath = os.path.abspath(os.path.expanduser(payload.get('bili_cookies_filepath')))
        cookie_str = bili_account_health.cookies_from_biliup_file(filepath)
        if not cookie_str:
            raise ValueError(f"bili_cookies_filepath is not a valid biliup cookies file: {filepath}")
        payload['bili_cookies_filepath'] = filepath
        payload['bili_cookies'] = cookie_str
        profile, nav_data = bili_account_health.profile_from_cookie_str(cookie_str)
        if not profile:
            message = nav_data.get('message') if isinstance(nav_data, dict) else 'login check failed'
            raise ValueError(f"bili account login check failed: {message}")
        payload.update({key: value for key, value in profile.items() if value})
    elif payload.get('bili_cookies'):
        cookies = _parse_cookie_string(payload.get('bili_cookies'))
        if not cookies:
            raise ValueError(f"bili_cookies format error:{payload.get('bili_cookies')}")
        cookie_str = _cookies_to_string(cookies)
        profile, nav_data = bili_account_health.profile_from_cookie_str(cookie_str)
        if not profile:
            message = nav_data.get('message') if isinstance(nav_data, dict) else 'login check failed'
            raise ValueError(f"bili account login check failed: {message}")
        payload['bili_cookies'] = cookie_str
        payload.update({key: value for key, value in profile.items() if value})
    elif require_credentials:
        raise ValueError("bili_cookies or bili_cookies_filepath is required")

    return payload


def _create_bili_account(data):
    payload = _prepare_bili_account_payload(data, require_credentials=True)
    return DB.create_bili_account(payload)


def _update_bili_account(data):
    if not data.get('id'):
        raise ValueError("id is required")

    payload = _prepare_bili_account_payload(data, require_credentials=False)
    return DB.update_bili_account(payload)


def _disable_bili_account(bili_account_id):
    if not bili_account_id:
        raise ValueError("id is required")

    return DB.update_bili_account({"id": bili_account_id, "state_active": 0})


def _create_bili_upload_template(data):
    return DB.create_bili_upload_template(data)


def _delete_bili_upload_template(template_id):
    return DB.delete_bili_upload_template(template_id)


def _prepare_douyin_account_payload(data, require_credentials):
    """校验抖音账号入参：cookie 为创作者平台扫码登录保存的 storage_state JSON。

    文件路径存在时把内容冗余读进 douyin_cookies（备份/还原用，仿 B 站账号保存逻辑
    :131-146 的思路）；不做远程登录态校验——有效性在扫码会话与上传时自然暴露。
    """
    payload = dict(data)
    filepath = (payload.get('douyin_cookies_filepath') or '').strip()
    if filepath:
        filepath = os.path.abspath(os.path.expanduser(filepath))
        if not os.path.isfile(filepath):
            raise ValueError(f"douyin_cookies_filepath 文件不存在: {filepath}")
        try:
            with open(filepath, 'r', encoding='utf-8') as fp:
                cookies_text = fp.read()
            parsed = json.loads(cookies_text)
            if not isinstance(parsed, dict) or 'cookies' not in parsed:
                raise ValueError('not a storage_state json')
        except (OSError, ValueError) as exc:
            raise ValueError(f"douyin_cookies_filepath 不是有效的 storage_state 文件: {filepath} ({exc})")
        payload['douyin_cookies_filepath'] = filepath
        payload['douyin_cookies'] = cookies_text
    elif payload.get('douyin_cookies'):
        try:
            parsed = json.loads(payload['douyin_cookies'])
            if not isinstance(parsed, dict) or 'cookies' not in parsed:
                raise ValueError('not a storage_state json')
        except (TypeError, ValueError):
            raise ValueError("douyin_cookies 不是合法的 storage_state JSON")
    elif require_credentials:
        raise ValueError("douyin_cookies_filepath or douyin_cookies is required（请先扫码登录）")

    return payload


def _create_douyin_account(data):
    payload = _prepare_douyin_account_payload(data, require_credentials=True)
    return DB.create_douyin_account(payload)


def _update_douyin_account(data):
    if not data.get('id'):
        raise ValueError("id is required")
    payload = _prepare_douyin_account_payload(data, require_credentials=False)
    return DB.update_douyin_account(payload)


def _disable_douyin_account(douyin_account_id):
    if not douyin_account_id:
        raise ValueError("id is required")
    return DB.update_douyin_account({"id": douyin_account_id, "state_active": 0})


def _create_douyin_upload_template(data):
    return DB.create_douyin_upload_template(data)


def _delete_douyin_upload_template(template_id):
    return DB.delete_douyin_upload_template(template_id)


def _build_douyin_publish_room_data(douyin_upload_template_id, live_room_id, room_data_override):
    """组装抖音上传插件需要的 room_data.douyin_upload_template 上下文。"""
    try:
        template = DouyinUploadTemplate.get_by_id_(douyin_upload_template_id)
    except DouyinUploadTemplate.DoesNotExist:
        raise ValueError(f'douyin_upload_template not found: {douyin_upload_template_id}')
    if template.douyin_account_id is None:
        raise ValueError('douyin_upload_template has no douyin_account_id')
    try:
        account = DouyinAccount.get_by_id_(template.douyin_account_id)
    except DouyinAccount.DoesNotExist:
        raise ValueError(f'douyin_account not found: {template.douyin_account_id}')

    template_info = model_to_dict(template)
    template_info['douyin_account'] = model_to_dict(account)

    room_data = {}
    if live_room_id:
        try:
            room = LiveRoom.get_by_id_(live_room_id)
            room_data = model_to_dict(room)
        except LiveRoom.DoesNotExist:
            raise ValueError(f'live_room not found: {live_room_id}')

    # 仅允许覆盖少量标题模板相关字段，避免误改模板/账号上下文
    for field in BILI_PUBLISH_ROOM_DATA_FIELDS:
        if field in room_data_override:
            room_data[field] = room_data_override[field]

    room_data['douyin_upload_template'] = template_info
    room_data['douyin_upload_template_id'] = template_info['id']
    return room_data


def _prepare_douyin_publish(data):
    """同步校验并组装手动发布到抖音的上下文，返回 (template_ids, live_room_id, room_data_override, file_list)。"""
    file_ids = data.get('file_ids')
    videos = data.get('videos')
    template_ids = resolve_room_douyin_template_ids(data)
    live_room_id = _as_int(data.get('live_room_id'))
    room_data_override = data.get('room_data') or {}

    if not template_ids:
        raise ValueError('douyin_upload_template_ids is required')
    if not file_ids and not videos:
        raise ValueError('file_ids or videos is required')

    video_dir = os.path.realpath(get_video_dir())
    min_size = int(config.get('filtering_threshold_file_size', 5)) * 1024 * 1024

    if file_ids:
        raw_items = _resolve_publish_record_files_from_ids(file_ids)
    else:
        raw_items = [{'video': video} for video in videos]

    file_list = []
    for raw_item in raw_items:
        raw_path = raw_item.get('video')
        real = _validate_publish_video_path(raw_path, video_dir, min_size)
        file_info = {'video': real}
        if raw_item.get('id') is not None:
            file_info['id'] = raw_item.get('id')
        file_list.append(file_info)

    if not file_list:
        raise ValueError('no files to publish')

    return template_ids, live_room_id, room_data_override, file_list


def _douyin_publish_file_list(file_list, template_info, room_data):
    """按模板把文件列表转成抖音可投的 mp4（切片按 vertical_crop 裁竖屏，其余仅转码）。

    竖屏裁剪只对切片记录（series_code=CLIP:*，必为三分屏）生效——整录/普通录像
    裁中栏会切掉主体。无 record id 的裸路径文件原样透传（插件预检会拦截超限文件）。
    """
    vertical_enabled = int(template_info.get('vertical_crop') if template_info.get('vertical_crop') is not None else 1) == 1
    room_name = room_data.get('room_name')
    converted = []
    for file_info in file_list:
        record_id = file_info.get('id')
        if record_id is None:
            converted.append(file_info)
            continue
        try:
            record = DB.get_record_file(record_id)
        except Exception:
            converted.append(file_info)
            continue
        is_clip = str(record.get('series_code') or '').startswith(CLIP_SERIES_CODE_PREFIX)
        try:
            douyin_video = ensure_douyin_clip(record, room_name, vertical=vertical_enabled and is_clip)
        except Exception as exc:
            raise ValueError(f'抖音版转码失败（record {record_id}）: {exc}')
        converted.append({**file_info, 'video': douyin_video})
    return converted


# 录像文件列表/手动发布相关常量与辅助方法


# 手动发布时允许通过 room_data 覆盖的标题模板相关字段
BILI_PUBLISH_ROOM_DATA_FIELDS = ('room_name', 'room_title', 'room_url', 'room_owner', 'room_platform')


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ('false', '0', 'no', 'none', '')
    return bool(value)


def _record_file_disk_info(record):
    path = record.get('_video_real') or os.path.realpath(record.get('video') or '')
    status = record.get('status') or RECORD_FILE_STATUS_COMPLETED
    size = mtime = None
    exists = False
    if path:
        try:
            st = os.stat(path)
            exists, size, mtime = True, st.st_size, st.st_mtime
        except OSError:
            if status == RECORD_FILE_STATUS_RECORDING:
                try:
                    st = os.stat(f'{path}.part')
                    exists, size, mtime = True, st.st_size, st.st_mtime
                except OSError:
                    pass
    return path, exists, size, mtime


def _record_file_entry(record):
    path, exists, size, mtime = _record_file_disk_info(record)
    status = record.get('status') or RECORD_FILE_STATUS_COMPLETED
    return {
        'id': record.get('id'),
        'video': path,
        'stream_url': _build_record_file_static_url(path) if status != RECORD_FILE_STATUS_RECORDING else None,
        'filename': os.path.basename(path) if path else None,
        'size': size,
        'mtime': mtime,
        'exists': exists,
        'source': 'database',
        'live_room_id': record.get('live_room_id'),
        'room_name': record.get('room_name'),
        'room_platform': record.get('room_platform'),
        'begin_time': record.get('begin_time'),
        'end_time': record.get('end_time'),
        'status': status,
        'duration_seconds': record.get('duration_seconds') or 0,
        'series_code': record.get('series_code'),
        'upload_info': record.get('upload_info'),
    }


def _list_record_files_data(params):
    """纯数据库驱动的录像列表：SQL 过滤查询，按 exists_only 口径返回准确分页总数。

    不再全盘 os.walk 合并磁盘文件（曾导致请求挂死）。exists 过滤依赖磁盘，开启时会先按
    DB 条件取出匹配记录，stat 后再分页，确保 total 与实际可见文件一致；关闭时仅 stat 当前页。
    返回 (list, total, page)。
    """
    page = max(1, _as_int(params.get('page')) or 1)
    page_size = _as_int(params.get('page_size')) or 50
    page_size = min(max(1, page_size), 200)

    filters = {
        'live_room_id': _as_int(params.get('live_room_id')),
        'room_name': (params.get('room_name') or '').strip() or None,
        'platform': (params.get('platform') or '').strip() or None,
        'date': (params.get('date') or '').strip() or None,
        'keyword': (params.get('keyword') or '').strip() or None,
    }

    exists_only = _as_bool(params.get('exists_only'), default=True)

    if exists_only:
        records, _ = DB.list_record_file(filters)
        entries = [_record_file_entry(record) for record in records]
        entries = [entry for entry in entries if entry['exists']]
        total = len(entries)
        start = (page - 1) * page_size
        entries = entries[start:start + page_size]
    else:
        records, total = DB.list_record_file(filters, page=page, page_size=page_size)
        entries = [_record_file_entry(record) for record in records]

    return entries, total, page


def _is_later_datetime(value, current):
    if value is None:
        return False
    if current is None:
        return True
    try:
        return value > current
    except TypeError:
        return str(value) > str(current)


def _list_record_file_room_summary_data(params=None):
    exists_only = _as_bool((params or {}).get('exists_only'), default=True)
    summary = DB.list_record_file_room_summary()
    if not exists_only:
        return summary

    summary_map = {}
    for item in summary:
        item['file_count'] = 0
        item['last_begin_time'] = None
        summary_map[item.get('live_room_id')] = item

    records, _ = DB.list_record_file()
    orphaned = []
    for record in records:
        _, exists, _, _ = _record_file_disk_info(record)
        if not exists:
            continue

        room_id = record.get('live_room_id')
        target = summary_map.get(room_id)
        if target is None:
            target = {
                'live_room_id': room_id,
                'room_name': f'未关联直播间 #{room_id}' if room_id is not None else '未关联直播间',
                'room_platform': None,
                'room_owner': None,
                'room_url': None,
                'live_state': None,
                'file_count': 0,
                'last_begin_time': None,
            }
            summary_map[room_id] = target
            orphaned.append(target)

        target['file_count'] += 1
        begin_time = record.get('begin_time')
        if _is_later_datetime(begin_time, target.get('last_begin_time')):
            target['last_begin_time'] = begin_time

    return summary + orphaned


def _build_record_file_static_url(path):
    """构造 nginx 静态播放 URL。仅允许 video 目录下的路径。"""
    if not path:
        return None
    video_dir = os.path.realpath(get_video_dir())
    real = os.path.realpath(path)
    if real != video_dir and not real.startswith(video_dir + os.sep):
        return None
    rel_path = os.path.relpath(real, video_dir)
    return '/video/' + '/'.join(quote(part) for part in rel_path.split(os.sep))


def _resolve_publish_video_path(raw_path, video_dir):
    """规范化并校验路径必须位于 video 目录下，返回规范化绝对路径。"""
    if not raw_path or not isinstance(raw_path, str):
        raise ValueError('invalid video path')
    path = raw_path
    if not os.path.isabs(path):
        path = os.path.join(video_dir, path)
    real = os.path.realpath(path)
    # realpath 会解析 .. 等穿越片段，再用前缀校验拒绝越界路径
    if real != video_dir and not real.startswith(video_dir + os.sep):
        raise ValueError(f'file is not under the video directory: {raw_path}')
    if not os.path.isfile(real):
        raise ValueError(f'file not found or not a regular file: {raw_path}')
    return real


def _resolve_record_file_stream_path(file_id):
    """按录像记录 ID 解析可在线播放的文件路径。"""
    if not file_id:
        raise ValueError('id is required')

    try:
        record = RecordFile.get_by_id_(file_id)
    except RecordFile.DoesNotExist:
        raise ValueError(f'record file not found: {file_id}')

    if record.status and record.status != RECORD_FILE_STATUS_COMPLETED:
        raise ValueError(f'record file is still recording: {file_id}')
    if not record.video:
        raise ValueError(f'record file has no video path: {file_id}')

    video_dir = os.path.realpath(get_video_dir())
    return _resolve_publish_video_path(record.video, video_dir)


def _validate_publish_video_path(raw_path, video_dir, min_size):
    real = _resolve_publish_video_path(raw_path, video_dir)
    size = os.path.getsize(real)
    if size < min_size:
        raise ValueError(f'file size {size} below threshold {min_size}: {raw_path}')
    return real


def _resolve_publish_record_files_from_ids(file_ids):
    """按 file_ids 顺序解析数据库录像记录。"""
    records = []
    with db.connection_context():
        for file_id in file_ids:
            try:
                record = RecordFile.get_by_id_(file_id)
            except RecordFile.DoesNotExist:
                raise ValueError(f'record file not found: {file_id}')
            if record.status and record.status != RECORD_FILE_STATUS_COMPLETED:
                raise ValueError(f'record file is still recording: {file_id}')
            if not record.video:
                raise ValueError(f'record file has no video path: {file_id}')
            records.append({'id': record.id, 'video': record.video})
    return records


def _build_bili_publish_room_data(bili_upload_template_id, live_room_id, room_data_override):
    """组装现有上传插件需要的 room_data.bili_upload_template 上下文。"""
    try:
        template = BiliUploadTemplate.get_by_id_(bili_upload_template_id)
    except BiliUploadTemplate.DoesNotExist:
        raise ValueError(f'bili_upload_template not found: {bili_upload_template_id}')
    if template.bili_account_id is None:
        raise ValueError('bili_upload_template has no bili_account_id')
    try:
        account = BiliAccount.get_by_id_(template.bili_account_id)
    except BiliAccount.DoesNotExist:
        raise ValueError(f'bili_account not found: {template.bili_account_id}')

    template_info = model_to_dict(template)
    template_info['bili_account'] = model_to_dict(account)

    room_data = {}
    if live_room_id:
        try:
            room = LiveRoom.get_by_id_(live_room_id)
            room_data = model_to_dict(room)
        except LiveRoom.DoesNotExist:
            raise ValueError(f'live_room not found: {live_room_id}')

    # 仅允许覆盖少量标题模板相关字段，避免误改模板/账号上下文
    for field in BILI_PUBLISH_ROOM_DATA_FIELDS:
        if field in room_data_override:
            room_data[field] = room_data_override[field]

    room_data['bili_upload_template'] = template_info
    # 覆盖为本次实际使用的模板id，避免沿用房间旧单模板列导致 SubmissionTask 记录错模板
    room_data['bili_upload_template_id'] = template_info['id']
    return room_data


def _prepare_bili_publish(data):
    """同步校验并组装手动发布的上传上下文，返回 (template_ids, live_room_id, room_data_override, file_list)。

    支持 bili_upload_template_ids（列表，可多选模板投多个账号）与旧的 bili_upload_template_id（单值）。
    """
    file_ids = data.get('file_ids')
    videos = data.get('videos')
    template_ids = resolve_room_bili_template_ids(data)
    live_room_id = _as_int(data.get('live_room_id'))
    room_data_override = data.get('room_data') or {}

    if not template_ids:
        raise ValueError('bili_upload_template_ids is required')
    if not file_ids and not videos:
        raise ValueError('file_ids or videos is required')

    video_dir = os.path.realpath(get_video_dir())
    min_size = int(config.get('filtering_threshold_file_size', 5)) * 1024 * 1024

    if file_ids:
        raw_items = _resolve_publish_record_files_from_ids(file_ids)
    else:
        raw_items = [{'video': video} for video in videos]

    file_list = []
    for raw_item in raw_items:
        raw_path = raw_item.get('video')
        real = _validate_publish_video_path(raw_path, video_dir, min_size)
        file_info = {'video': real}
        if raw_item.get('id') is not None:
            file_info['id'] = raw_item.get('id')
        file_list.append(file_info)

    if not file_list:
        raise ValueError('no files to publish')

    return template_ids, live_room_id, room_data_override, file_list


def _list_submission_tasks_data(params):
    page = max(1, _as_int(params.get('page')) or 1)
    page_size = _as_int(params.get('page_size')) or 50
    page_size = min(max(1, page_size), 200)
    filters = {
        'status': (params.get('status') or '').strip() or None,
        'source': (params.get('source') or '').strip() or None,
        'platform': (params.get('platform') or '').strip() or None,
        'keyword': (params.get('keyword') or '').strip() or None,
        'live_room_id': _as_int(params.get('live_room_id')),
    }
    records, total = DB.list_submission_task(filters, page=page, page_size=page_size)
    return records, total, page


def resp_data(data=None, code=0, message="success"):
    wrapper_data = {
        "success": code == 0,
        "code": code,
        "data": data,
        "message": message
    }
    return json_response(wrapper_data)


def resp_page_list(list_, total, page):
    data = {
        "list": list_,
        "total": total,
        "page": page,
    }

    return resp_data(data)


def success(data=None):
    return resp_data(data)


def error(code, message):
    return resp_data(code=code, message=message)


@routes.get("/")
async def root_handler(request):
    return web.HTTPFound('/index.html')


@routes.get('/ping')
async def hello(request):
    return web.Response(text="pong")


@routes.get("/v1/System/stats")
@routes.post("/v1/System/stats")
async def system_stats(request):
    return success(collect_runtime_stats())


@routes.get("/v1/System/health")
@routes.post("/v1/System/health")
async def system_health(request):
    from luboman.core.thread_pool import thread_pool_manager

    checks = {}
    try:
        checks["thread_pool"] = thread_pool_manager.is_healthy()
    except Exception as e:
        checks["thread_pool"] = False
        checks["thread_pool_error"] = str(e)

    try:
        checks["database"] = await run_db(_database_health_check)
    except Exception as e:
        checks["database"] = False
        checks["database_error"] = str(e)

    checks["runtime"] = collect_runtime_stats()
    return resp_data(
        {"healthy": checks.get("thread_pool", False) and checks.get("database", False), "checks": checks},
        code=0 if checks.get("thread_pool", False) and checks.get("database", False) else 1,
        message="healthy" if checks.get("thread_pool", False) and checks.get("database", False) else "unhealthy"
    )


@routes.post('/bili/archive/pre')
async def pre_archive(request):
    try:
        return web.json_response(await run_db(_pre_archive_data))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/Config/get")
async def get_config(request):
    res = await run_db(DB.load_config)
    return success(res)


@routes.post("/v1/Config/set")
async def set_config(request):
    config_data = await request.json()
    try:
        await run_db(_set_config_values, config_data)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))

    return success()


@routes.post("/v1/LiveRoom/listAll")
async def list_room(request):
    res = await run_db(DB.list_room)
    return success(res)


@routes.post("/v1/LiveRoom/add")
async def add_room(request):
    json_data = await request.json()
    try:
        json_data["room_name"] = json_data.get("room_name", "").strip()
        json_data["room_url"] = json_data.get("room_url", "").strip()
        new_room_data = await run_db(_create_live_room, json_data)

        # 按 active_state 对账：默认激活则启动 worker，未激活则保持停止
        await reconcile_room_runtime(new_room_data)
        return success(new_room_data["id"])
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/LiveRoom/update")
async def update_room(request):
    data = await request.json()
    if not data.get('id'):
        return error(1, "id is required")
    try:
        row, room_data = await run_db(_update_live_room_data, data)

        if room_data:
            # 根据 active_state 对账 worker 运行态：激活/停用/仅刷新配置
            await reconcile_room_runtime(room_data)

        return success(row)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/LiveRoom/del")
async def del_room(request):
    data = await request.json()
    row_id = data.get('id')
    if not row_id:
        return error(1, "id is required")

    try:
        await run_db(_delete_live_room, row_id)
        await stop_room_runtime(row_id)
        return success(row_id)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/LiveRoom/probe")
async def probe_room(request):
    data = await request.json()
    room_url = (data.get('room_url') or '').strip()
    if not room_url:
        return error(1, "room_url is required")

    try:
        return success(await run_db(_probe_live_room, room_url))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/LiveRoom/start")
async def start_live_room(request):
    data = await request.json()
    row_id = data.get('id')
    if not row_id:
        return error(1, "id is required")

    try:
        room_data = await run_db(_get_live_room_data, row_id)
        await start_room_runtime(room_data)
        return success(row_id)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/LiveRoom/stop")
async def stop_live_room(request):
    data = await request.json()
    row_id = data.get('id')
    if not row_id:
        return error(1, "id is required")

    try:
        await stop_room_runtime(row_id)
        return success(row_id)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/BiliAccount/listAll")
async def list_bili_account(request):
    res = await run_db(DB.list_bili_account)
    return success(res)


@routes.post("/v1/BiliAccount/loginCheck")
async def check_bili_account_login(request):
    data = await request.json()
    try:
        results = await run_db(bili_account_health.check_accounts, data.get('id'))
        active_count = len([item for item in results if item.get('state_active', 1)])
        invalid_count = len([item for item in results if item.get('login_valid') is False])
        return success({
            'active_count': active_count,
            'invalid_count': invalid_count,
            'results': results,
        })
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/BiliAccount/biliupLogin/start")
async def start_biliup_login(request):
    data = await request.json()
    try:
        # start_session 内同步调用 stream_gears.get_qrcode(阻塞网络请求),必须放
        # 线程池,否则会卡死 aiohttp event loop 导致请求永不响应(前端 499)。
        snapshot = await run_db(
            biliup_login_manager.start_session,
            data.get('bili_cookies_filepath'),
            data.get('account_name'),
        )
        return success(snapshot)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/BiliAccount/biliupLogin/status")
async def get_biliup_login_status(request):
    data = await request.json()
    try:
        return success(biliup_login_manager.snapshot(
            data.get('session_id'),
            since=_as_int(data.get('since')),
        ))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/BiliAccount/biliupLogin/stop")
async def stop_biliup_login(request):
    data = await request.json()
    try:
        return success(biliup_login_manager.stop_session(data.get('session_id')))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/BiliAccount/add")
async def add_bili_account(request):
    data = await request.json()
    try:
        return success(await run_db(_create_bili_account, data))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/BiliAccount/update")
async def update_bili_account(request):
    data = await request.json()
    try:
        return success(await run_db(_update_bili_account, data))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/BiliAccount/del")
async def del_bili_account(request):
    data = await request.json()
    bili_account_id = data.get('id')
    if not bili_account_id:
        return error(1, "id is required")

    try:
        await run_db(_disable_bili_account, bili_account_id)
        return success(bili_account_id)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/BiliUploadTemplate/listAll")
async def list_bili_upload_template(request):
    res = await run_db(DB.list_bili_upload_template)
    return success(res)


@routes.post("/v1/BiliUploadTemplate/add")
async def add_bili_upload_template(request):
    data = await request.json()
    if not data.get('template_name'):
        return error(1, "template_name is required")

    if not data.get('bili_account_id'):
        return error(1, "bili_account_id is required")

    if not data.get('tags'):
        data['tags'] = ["录播Man"]
    try:
        return success(await run_db(_create_bili_upload_template, data))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/BiliUploadTemplate/update")
async def update_bili_upload_template(request):
    data = await request.json()
    if not data.get('id'):
        return error(1, "id is required")
    if data.get('tags'):
        if '录播Man' not in data['tags']:
            data['tags'].append('录播Man')

    try:
        row = await run_db(DB.update_bili_upload_template, data)
        return success(row)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/BiliUploadTemplate/del")
async def del_bili_upload_template(request):
    data = await request.json()
    template_id = data.get('id')
    if not template_id:
        return error(1, "id is required")

    try:
        await run_db(_delete_bili_upload_template, template_id)
        return success(template_id)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/DouyinAccount/listAll")
async def list_douyin_account(request):
    res = await run_db(DB.list_douyin_account)
    return success(res)


@routes.post("/v1/DouyinAccount/login/start")
async def start_douyin_login(request):
    data = await request.json()
    try:
        # start_session 内同步等浏览器出二维码（阻塞），必须放线程池
        snapshot = await run_db(
            douyin_login_manager.start_session,
            data.get('douyin_cookies_filepath'),
            data.get('account_name'),
        )
        return success(snapshot)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/DouyinAccount/login/status")
async def get_douyin_login_status(request):
    data = await request.json()
    try:
        return success(douyin_login_manager.snapshot(
            data.get('session_id'),
            since=_as_int(data.get('since')),
        ))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/DouyinAccount/login/stop")
async def stop_douyin_login(request):
    data = await request.json()
    try:
        return success(douyin_login_manager.stop_session(data.get('session_id')))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/DouyinAccount/add")
async def add_douyin_account(request):
    data = await request.json()
    try:
        return success(await run_db(_create_douyin_account, data))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/DouyinAccount/update")
async def update_douyin_account(request):
    data = await request.json()
    try:
        return success(await run_db(_update_douyin_account, data))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/DouyinAccount/del")
async def del_douyin_account(request):
    data = await request.json()
    douyin_account_id = data.get('id')
    if not douyin_account_id:
        return error(1, "id is required")

    try:
        await run_db(_disable_douyin_account, douyin_account_id)
        return success(douyin_account_id)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/DouyinUploadTemplate/listAll")
async def list_douyin_upload_template(request):
    res = await run_db(DB.list_douyin_upload_template)
    return success(res)


@routes.post("/v1/DouyinUploadTemplate/add")
async def add_douyin_upload_template(request):
    data = await request.json()
    if not data.get('template_name'):
        return error(1, "template_name is required")

    if not data.get('douyin_account_id'):
        return error(1, "douyin_account_id is required")

    try:
        return success(await run_db(_create_douyin_upload_template, data))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/DouyinUploadTemplate/update")
async def update_douyin_upload_template(request):
    data = await request.json()
    if not data.get('id'):
        return error(1, "id is required")

    try:
        row = await run_db(DB.update_douyin_upload_template, data)
        return success(row)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/DouyinUploadTemplate/del")
async def del_douyin_upload_template(request):
    data = await request.json()
    template_id = data.get('id')
    if not template_id:
        return error(1, "id is required")

    try:
        await run_db(_delete_douyin_upload_template, template_id)
        return success(template_id)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post('/v1/RecordFile/list')
async def list_record_file(request):
    try:
        params = await request.json()
        page_entries, total, page = await run_db(_list_record_files_data, params)
        return resp_page_list(page_entries, total, page)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post('/v1/RecordFile/roomSummary')
async def list_record_file_room_summary(request):
    try:
        try:
            params = await request.json()
        except Exception:
            params = {}
        return success(await run_db(_list_record_file_room_summary_data, params))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.get('/video/{path:.*}')
async def serve_video_static(request):
    """后端直接提供 /video/ 静态文件。FileResponse 支持 Range（flv.js 拖动播放依赖 206），
    使无 nginx /video 反代的部署（单容器、本地 dev 代理）也能在线播放。
    经 /api 前缀代理后与 nginx 静态地址同路径，前端无需区分部署形态。"""
    try:
        video_dir = os.path.realpath(get_video_dir())
        rel_path = request.match_info.get('path') or ''
        # realpath 消解 ../ 等穿越片段，再前缀校验限制在 video 目录内
        real = os.path.realpath(os.path.join(video_dir, rel_path))
        if real != video_dir and not real.startswith(video_dir + os.sep):
            return web.Response(status=403, text='forbidden')
        if not os.path.isfile(real):
            return web.Response(status=404, text=f'file not found: {rel_path}')
        return web.FileResponse(real)
    except web.HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        return web.Response(status=500, text=str(e))


@routes.get('/public/{path:.*}')
async def serve_public_static(request):
    """后端直接提供 /public/ 静态文件（直播封面缓存、自定义封面、片头等），
    使无 nginx 的部署也能预览。与 serve_video_static 同款 realpath+前缀校验。"""
    try:
        public_dir = os.path.realpath(get_public_dir())
        rel_path = request.match_info.get('path') or ''
        real = os.path.realpath(os.path.join(public_dir, rel_path))
        if real != public_dir and not real.startswith(public_dir + os.sep):
            return web.Response(status=403, text='forbidden')
        if not os.path.isfile(real):
            return web.Response(status=404, text=f'file not found: {rel_path}')
        return web.FileResponse(real)
    except web.HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        return web.Response(status=500, text=str(e))


# 上传文件类型/大小限制（大小可用全局配置 upload_cover_max_mb / upload_intro_max_mb 覆盖）
UPLOAD_COVER_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}
UPLOAD_INTRO_EXTS = {'.mp4', '.flv', '.mkv', '.ts', '.mov'}


async def _save_upload_file(request, sub_dir, name_prefix, allowed_exts, max_mb):
    """multipart 流式落盘到 {public}/{sub_dir}/，文件名服务端生成，不信任客户端。
    返回 (绝对路径, /public 相对URL, 大小, 原始文件名)；校验失败抛 ValueError 并清理残件。"""
    max_bytes = max_mb * 1024 * 1024
    reader = await request.multipart()
    field = await reader.next()
    while field is not None and not getattr(field, 'filename', None):
        field = await reader.next()
    if field is None:
        raise ValueError('未找到上传文件')

    origin_filename = os.path.basename(field.filename)
    ext = os.path.splitext(origin_filename)[1].lower()
    if ext not in allowed_exts:
        raise ValueError(f'不支持的文件类型: {ext or "(无扩展名)"}，允许: {",".join(sorted(allowed_exts))}')

    dest_dir = os.path.join(get_public_dir(), sub_dir)
    os.makedirs(dest_dir, exist_ok=True)
    filename = f'{name_prefix}-{uuid.uuid4().hex[:8]}{ext}'
    dest_path = os.path.join(dest_dir, filename)

    size = 0
    try:
        with open(dest_path, 'wb') as f:
            while True:
                chunk = await field.read_chunk(256 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f'文件超过大小限制 {max_mb}MB')
                f.write(chunk)
    except Exception:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        raise

    rel_url = f'/public/{sub_dir}/{filename}'
    return dest_path, rel_url, size, origin_filename


@routes.post('/v1/Upload/cover')
async def upload_cover(request):
    """上传自定义封面图片（按直播间），落盘 {public}/cover/custom/。"""
    try:
        max_mb = int(config.get('upload_cover_max_mb', 10) or 10)
        path, url, size, origin = await _save_upload_file(
            request, os.path.join('cover', 'custom'), 'cover', UPLOAD_COVER_EXTS, max_mb
        )
        # PIL 校验确为可解码图片，防止伪装扩展名
        from PIL import Image
        try:
            with Image.open(path) as img:
                img.verify()
        except Exception:
            os.remove(path)
            return error(1, '文件不是有效的图片')
        return success({'path': path, 'url': url, 'size': size, 'filename': origin})
    except ValueError as e:
        return error(1, str(e))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post('/v1/Upload/intro')
async def upload_intro(request):
    """上传片头视频（按B站账号），落盘 {public}/intro/。"""
    try:
        max_mb = int(config.get('upload_intro_max_mb', 200) or 200)
        path, url, size, origin = await _save_upload_file(
            request, 'intro', 'intro', UPLOAD_INTRO_EXTS, max_mb
        )
        return success({'path': path, 'url': url, 'size': size, 'filename': origin})
    except ValueError as e:
        return error(1, str(e))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.get('/v1/RecordFile/stream/{file_id}')
async def stream_record_file(request):
    try:
        file_id = _as_int(request.match_info.get('file_id'))
        path = await run_db(_resolve_record_file_stream_path, file_id)
        return web.FileResponse(
            path,
            headers={
                'Accept-Ranges': 'bytes',
                'Content-Disposition': f"inline; filename*=UTF-8''{quote(os.path.basename(path))}",
            },
        )
    except ValueError as e:
        return error(1, str(e))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post('/v1/RecordFile/publishBili')
async def publish_record_file_to_bili(request):
    try:
        data = await request.json()
        template_ids, live_room_id, room_data_override, file_list = await run_db(_prepare_bili_publish, data)

        # 每个模板（账号）创建一个投稿任务，单个失败不影响其他模板
        tasks = []
        errors = []
        for template_id in template_ids:
            try:
                room_data = await run_db(
                    _build_bili_publish_room_data, template_id, live_room_id, room_data_override
                )
                result = await schedule_bili_submission(
                    file_list=file_list,
                    room_data=room_data,
                    source=SUBMISSION_TASK_SOURCE_FILE_MANAGER,
                    priority=UploadPriority.HIGH,
                    metadata={
                        'created_from': 'record_file',
                        'file_ids': data.get('file_ids') or [],
                        'videos': data.get('videos') or [],
                    },
                )
                tasks.append(result)
            except Exception as e:
                logger.error(f'手动投稿任务创建失败: template_id={template_id}, 错误={e}')
                errors.append({'bili_upload_template_id': template_id, 'error': str(e)})

        if not tasks:
            return error(1, '; '.join(f"模板 {e['bili_upload_template_id']}: {e['error']}" for e in errors))
        return success({'tasks': tasks, 'errors': errors})
    except ValueError as e:
        return error(1, str(e))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post('/v1/RecordFile/publishDouyin')
async def publish_record_file_to_douyin(request):
    try:
        data = await request.json()
        template_ids, live_room_id, room_data_override, file_list = await run_db(_prepare_douyin_publish, data)

        # 每个模板（账号）创建一个投稿任务，单个失败不影响其他模板
        tasks = []
        errors = []
        for template_id in template_ids:
            try:
                room_data = await run_db(
                    _build_douyin_publish_room_data, template_id, live_room_id, room_data_override
                )
                # 切片按模板配置裁竖屏，整录仅转码 mp4（flv 抖音不接受）
                douyin_file_list = await run_db(
                    _douyin_publish_file_list, file_list, room_data['douyin_upload_template'], room_data
                )
                result = await schedule_douyin_submission(
                    file_list=douyin_file_list,
                    room_data=room_data,
                    source=SUBMISSION_TASK_SOURCE_FILE_MANAGER,
                    priority=UploadPriority.HIGH,
                    metadata={
                        'created_from': 'record_file',
                        'douyin_upload_template_id': template_id,
                        'file_ids': data.get('file_ids') or [],
                        'videos': data.get('videos') or [],
                    },
                )
                tasks.append(result)
            except Exception as e:
                logger.error(f'手动投稿抖音任务创建失败: template_id={template_id}, 错误={e}')
                errors.append({'douyin_upload_template_id': template_id, 'error': str(e)})

        if not tasks:
            return error(1, '; '.join(f"模板 {e['douyin_upload_template_id']}: {e['error']}" for e in errors))
        return success({'tasks': tasks, 'errors': errors})
    except ValueError as e:
        return error(1, str(e))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post('/v1/SubmissionTask/list')
async def list_submission_task(request):
    try:
        params = await request.json()
        page_entries, total, page = await run_db(_list_submission_tasks_data, params)
        return resp_page_list(page_entries, total, page)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post('/v1/SubmissionTask/detail')
async def get_submission_task(request):
    try:
        data = await request.json()
        task = await run_db(
            DB.get_submission_task,
            task_id=data.get('task_id'),
            row_id=_as_int(data.get('id')),
        )
        return success(task)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post('/v1/SubmissionTask/stats')
async def get_submission_task_stats(request):
    try:
        return success(await run_db(DB.get_submission_task_stats))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


def _prepare_dance_clip_detect(data):
    """同步校验探测请求，返回 (file_ids, live_room_id, room_name, params)。"""
    file_ids = data.get('file_ids') or []
    if not file_ids:
        raise ValueError('file_ids is required')

    records = _resolve_publish_record_files_from_ids(file_ids)

    live_room_id = None
    room_name = None
    with db.connection_context():
        first = RecordFile.get_by_id_(records[0]['id'])
        live_room_id = first.live_room_id
        room = LiveRoom.get_or_none(LiveRoom.id == live_room_id)
        room_name = room.room_name if room else None

    params = data.get('params') or {}
    if not isinstance(params, dict):
        raise ValueError('params must be an object')
    return [r['id'] for r in records], live_room_id, room_name, params


def _list_clip_tasks_data(params):
    page = max(1, _as_int(params.get('page')) or 1)
    page_size = _as_int(params.get('page_size')) or 50
    page_size = min(max(1, page_size), 200)
    filters = {
        'status': (params.get('status') or '').strip() or None,
        'keyword': (params.get('keyword') or '').strip() or None,
        'live_room_id': _as_int(params.get('live_room_id')),
    }
    records, total = DB.list_clip_task(filters, page=page, page_size=page_size)
    return records, total, page


@routes.post('/v1/RecordFile/detectDanceClip')
async def detect_dance_clip(request):
    try:
        data = await request.json()
        file_ids, live_room_id, room_name, params = await run_db(_prepare_dance_clip_detect, data)
        result = await clip_scheduler.schedule(
            file_ids=file_ids,
            live_room_id=live_room_id,
            room_name=room_name,
            params=params,
        )
        return success(result)
    except ValueError as e:
        return error(1, str(e))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post('/v1/ClipTask/list')
async def list_clip_task(request):
    try:
        params = await request.json()
        page_entries, total, page = await run_db(_list_clip_tasks_data, params)
        return resp_page_list(page_entries, total, page)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post('/v1/ClipTask/detail')
async def get_clip_task(request):
    try:
        data = await request.json()
        task = await run_db(
            DB.get_clip_task,
            task_id=data.get('task_id'),
            row_id=_as_int(data.get('id')),
        )
        return success(task)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post('/v1/ClipTask/stats')
async def get_clip_task_stats(request):
    try:
        return success(await run_db(DB.get_clip_task_stats))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post('/v1/ClipTask/retry')
async def retry_clip_task(request):
    try:
        data = await request.json()
        task_id = (data.get('task_id') or '').strip()
        if not task_id:
            return error(1, 'task_id is required')
        result = await clip_scheduler.retry(task_id)
        return success(result)
    except ValueError as e:
        return error(1, str(e))
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post('/v1/Auth/login')
async def auth_login(request):
    if not auth.AUTH_ENABLED:
        return success({'enabled': False})

    ip = auth.client_ip(request)
    lock_remaining = auth.ip_lock_remaining(ip)
    if lock_remaining > 0:
        await auth.constant_delay()
        return json_response(
            {'success': False, 'code': 429,
             'message': f'尝试次数过多，请 {int(lock_remaining // 60) + 1} 分钟后再试'},
            status=429)
    if auth.global_limit_hit():
        await auth.constant_delay()
        return json_response(
            {'success': False, 'code': 429, 'message': '尝试次数过多，请稍后再试'},
            status=429)

    try:
        data = await request.json()
    except Exception:
        data = {}
    if auth.verify_password(data.get('password')):
        auth.clear_failures(ip)
        token = auth.create_session()
        resp = success()
        resp.set_cookie(auth.COOKIE_NAME, token, max_age=auth.SESSION_TTL,
                        httponly=True, samesite='Lax', path='/')
        return resp

    auth.record_failure(ip)
    lock_remaining = auth.ip_lock_remaining(ip)
    await auth.constant_delay()
    if lock_remaining > 0:
        return json_response(
            {'success': False, 'code': 429,
             'message': f'尝试次数过多，请 {int(lock_remaining // 60) + 1} 分钟后再试'},
            status=429)
    return json_response({'success': False, 'code': 401, 'message': '密码错误'},
                         status=401)


@routes.post('/v1/Auth/logout')
async def auth_logout(request):
    auth.drop_session(auth.token_from_request(request))
    resp = success()
    resp.del_cookie(auth.COOKIE_NAME, path='/')
    return resp


@routes.get('/v1/Auth/status')
@routes.post('/v1/Auth/status')
async def auth_status(request):
    return success({'enabled': auth.AUTH_ENABLED, 'logged_in': auth.is_logged_in(request)})


@routes.get('/v1/Auth/check')
async def auth_check(request):
    """nginx auth_request 子请求专用：已登录 200，未登录 401，空 body。"""
    if not auth.AUTH_ENABLED or auth.is_logged_in(request):
        return web.Response(status=200)
    return web.Response(status=401)


@web.middleware
async def error_middleware(request, handler):
    try:
        response = await handler(request)
        if response.status != 404:
            return response
        message = response.message
    except web.HTTPException as ex:
        if ex.status != 404:
            raise
        message = ex.reason
    return web.json_response({'error': message})


app = web.Application(logger=logger, middlewares=[auth.auth_middleware, error_middleware])
app.add_routes(routes)


async def serve(host='127.0.0.1', port=5005):
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            allow_methods="*",
            expose_headers="*",
            allow_headers="*"
        )
    })

    for route in list(app.router.routes()):
        cors.add(route)

    runner = web.AppRunner(app)
    await runner.setup()
    if os.path.exists('/.dockerenv'):
        site = web.TCPSite(runner, port=port)
    else:
        site = web.TCPSite(runner, host=host, port=port)
    await site.start()

    logger.info(f"HttpServer started at http://{host}:{port}")
