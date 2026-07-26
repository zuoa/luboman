import asyncio
import datetime
import functools
import json
import logging
import os
from urllib.parse import quote

import aiohttp_cors
from aiohttp import web
from playhouse.shortcuts import model_to_dict

from luboman.config import config
from luboman.core import bili_account_health
from luboman.core.async_utils import run_blocking
from luboman.core.async_upload import UploadPriority, schedule_bili_submission
from luboman.core.dance_clip import clip_scheduler
from luboman.core.biliup_login import biliup_login_manager
from luboman.core.runtime import (
    collect_runtime_stats,
    reconcile_room_runtime,
    start_room_runtime,
    stop_room_runtime,
)
from luboman.core.upload import BiliBili, Data
from luboman.core.utils import get_video_dir
from luboman.database.db import (
    DB,
    RECORD_FILE_STATUS_COMPLETED,
    RECORD_FILE_STATUS_RECORDING,
    SUBMISSION_TASK_SOURCE_FILE_MANAGER,
)
from luboman.database.models import (
    BiliAccount,
    BiliUploadTemplate,
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
    return room_data


def _prepare_bili_publish(data):
    """同步校验并组装手动发布的上传上下文，返回 (room_data, file_list)。"""
    file_ids = data.get('file_ids')
    videos = data.get('videos')
    bili_upload_template_id = data.get('bili_upload_template_id')
    live_room_id = _as_int(data.get('live_room_id'))
    room_data_override = data.get('room_data') or {}

    if not bili_upload_template_id:
        raise ValueError('bili_upload_template_id is required')
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

    room_data = _build_bili_publish_room_data(bili_upload_template_id, live_room_id, room_data_override)
    return room_data, file_list


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

        await start_room_runtime(new_room_data)
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
        room_data, file_list = await run_db(_prepare_bili_publish, data)

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
        return success(result)
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


app = web.Application(logger=logger, middlewares=[error_middleware])
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
