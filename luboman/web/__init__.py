import datetime
import functools
import json
import logging
import os
import re

import aiohttp_cors
from aiohttp import web
from playhouse.shortcuts import model_to_dict

from luboman.config import config
from luboman.core.async_utils import run_blocking
from luboman.core.async_upload import UploadPriority, async_upload_scheduler
from luboman.core.runtime import (
    collect_runtime_stats,
    reconcile_room_runtime,
    start_room_runtime,
    stop_room_runtime,
)
from luboman.core.upload import BiliBili, Data, resolve_bili_uploader
from luboman.core.utils import get_video_dir
from luboman.database.db import DB
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
        with BiliBili(Data()) as bili:
            bili.login(payload.get('bili_cookies_filepath'), {})
            account_info = bili.myinfo()
            if account_info and account_info.get('code') == 0:
                payload['account_name'] = account_info['data']['name']
                payload['account_avatar'] = account_info['data']['face']
            payload['bili_cookies'] = _cookies_to_string(bili.cookies)
    elif payload.get('bili_cookies'):
        cookies = _parse_cookie_string(payload.get('bili_cookies'))
        if not cookies:
            raise ValueError(f"bili_cookies format error:{payload.get('bili_cookies')}")

        with BiliBili(Data()) as bili:
            bili.login_by_cookie(cookies)
            account_info = bili.myinfo()
            if account_info and account_info.get('code') == 0:
                payload['account_name'] = account_info['data']['name']
                payload['account_avatar'] = account_info['data']['face']
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
RECORD_FILE_VIDEO_EXTENSIONS = ('.flv', '.mp4', '.mkv', '.webm', '.ts')
RECORD_FILE_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
# 手动发布时允许通过 room_data 覆盖的标题模板相关字段
BILI_PUBLISH_ROOM_DATA_FIELDS = ('room_name', 'room_title', 'room_url', 'room_owner', 'room_platform')


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_video_rel_path(rel_path):
    """从相对 video_dir 的路径中尽力解析平台/房间名/日期。

    录像目录结构为 {video_dir}/{platform}/{room_id}-{room_name}/{day}/{file}，
    解析失败时返回空字典，磁盘补齐条目相应字段留空。
    """
    info = {}
    parts = [part for part in rel_path.split(os.sep) if part]
    if len(parts) >= 4:
        info['platform'] = parts[0]
        if RECORD_FILE_DATE_RE.match(parts[-2]):
            info['date'] = parts[-2]
        room_folder = parts[1]
        if '-' in room_folder:
            info['room_name'] = room_folder.split('-', 1)[1]
    return info


def _scan_local_video_files(video_dir):
    """扫描本地录像目录，返回 {规范化绝对路径: 磁盘条目}。"""
    result = {}
    for root, _dirs, files in os.walk(video_dir):
        for name in files:
            if not name.lower().endswith(RECORD_FILE_VIDEO_EXTENSIONS):
                continue
            full = os.path.realpath(os.path.join(root, name))
            try:
                stat = os.stat(full)
            except OSError:
                continue
            parsed = _parse_video_rel_path(os.path.relpath(full, video_dir))
            result[full] = {
                'id': None,
                'video': full,
                'filename': name,
                'size': stat.st_size,
                'mtime': stat.st_mtime,
                'exists': True,
                'source': 'disk',
                'live_room_id': None,
                'room_name': parsed.get('room_name'),
                'room_platform': parsed.get('platform'),
                'begin_time': None,
                'end_time': None,
                'series_code': None,
                'upload_info': None,
            }
    return result


def _datetime_to_epoch(value):
    if isinstance(value, datetime.datetime):
        try:
            return value.timestamp()
        except (OSError, OverflowError, ValueError):
            return 0
    return 0


def _entry_matches_date(entry, date_str):
    if date_str in (entry.get('video') or ''):
        return True
    begin_time = entry.get('begin_time')
    if isinstance(begin_time, datetime.datetime) and begin_time.strftime('%Y-%m-%d') == date_str:
        return True
    return False


def _record_file_matches(entry, filters, exists_only):
    """合并后的统一过滤条件，对数据库条目与磁盘补齐条目一视同仁。"""
    if exists_only and not entry.get('exists'):
        return False

    live_room_id = filters.get('live_room_id')
    if live_room_id is not None and str(entry.get('live_room_id')) != str(live_room_id):
        return False

    room_name = filters.get('room_name')
    if room_name and room_name not in (entry.get('room_name') or ''):
        return False

    platform = filters.get('platform')
    if platform and entry.get('room_platform') != platform:
        return False

    date_str = filters.get('date')
    if date_str and not _entry_matches_date(entry, date_str):
        return False

    keyword = filters.get('keyword')
    if keyword:
        haystack = ' '.join(
            str(value) for value in (
                entry.get('filename'),
                entry.get('video'),
                entry.get('room_name'),
                entry.get('room_platform'),
            ) if value
        ).lower()
        if keyword.lower() not in haystack:
            return False

    return True


def _list_record_files_data(params):
    """合并数据库录像记录与本地磁盘扫描，返回分页后的 (list, total, page)。"""
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

    exists_only = params.get('exists_only', True)
    if isinstance(exists_only, str):
        exists_only = exists_only.strip().lower() not in ('false', '0', 'no', 'none', '')

    video_dir = os.path.realpath(get_video_dir())
    disk_entries = _scan_local_video_files(video_dir)

    # 仅用 live_room_id 在数据库层缩小范围，其余筛选交给统一过滤层，保证语义一致
    records, _db_total = DB.list_record_file({'live_room_id': filters['live_room_id']})

    merged = dict(disk_entries)
    for record in records:
        path = os.path.realpath(record.get('video') or '')
        if not path:
            continue
        on_disk = path in disk_entries
        disk_entry = disk_entries.get(path, {})
        merged[path] = {
            'id': record.get('id'),
            'video': path,
            'filename': os.path.basename(path),
            'size': disk_entry.get('size') if on_disk else None,
            'mtime': disk_entry.get('mtime') if on_disk else _datetime_to_epoch(record.get('end_time') or record.get('begin_time')),
            'exists': on_disk,
            'source': 'database',
            'live_room_id': record.get('live_room_id'),
            'room_name': record.get('room_name') or disk_entry.get('room_name'),
            'room_platform': record.get('room_platform') or disk_entry.get('room_platform'),
            'begin_time': record.get('begin_time'),
            'end_time': record.get('end_time'),
            'series_code': record.get('series_code'),
            'upload_info': record.get('upload_info'),
        }

    entries = [entry for entry in merged.values() if _record_file_matches(entry, filters, exists_only)]
    entries.sort(key=lambda entry: entry.get('mtime') or 0, reverse=True)

    total = len(entries)
    start = (page - 1) * page_size
    page_entries = entries[start:start + page_size]
    return page_entries, total, page


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


def _validate_publish_video_path(raw_path, video_dir, min_size):
    real = _resolve_publish_video_path(raw_path, video_dir)
    size = os.path.getsize(real)
    if size < min_size:
        raise ValueError(f'file size {size} below threshold {min_size}: {raw_path}')
    return real


def _resolve_publish_video_paths_from_ids(file_ids):
    """按 file_ids 顺序解析数据库录像记录的 video 路径。"""
    paths = []
    with db.connection_context():
        for file_id in file_ids:
            try:
                record = RecordFile.get_by_id_(file_id)
            except RecordFile.DoesNotExist:
                raise ValueError(f'record file not found: {file_id}')
            if not record.video:
                raise ValueError(f'record file has no video path: {file_id}')
            paths.append(record.video)
    return paths


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
        raw_paths = _resolve_publish_video_paths_from_ids(file_ids)
    else:
        raw_paths = list(videos)

    file_list = []
    for raw_path in raw_paths:
        real = _validate_publish_video_path(raw_path, video_dir, min_size)
        file_list.append({'video': real})

    if not file_list:
        raise ValueError('no files to publish')

    room_data = _build_bili_publish_room_data(bili_upload_template_id, live_room_id, room_data_override)
    return room_data, file_list


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


@routes.post('/v1/RecordFile/publishBili')
async def publish_record_file_to_bili(request):
    try:
        data = await request.json()
        room_data, file_list = await run_db(_prepare_bili_publish, data)

        if not async_upload_scheduler.running:
            return error(1, 'async upload scheduler is not running, please start the service via async_main.py')

        uploader = resolve_bili_uploader(room_data)
        task_id = await async_upload_scheduler.schedule_upload_simple(
            platform=uploader,
            file_list=file_list,
            room_data=room_data,
            priority=UploadPriority.HIGH,
        )
        return success({
            'task_id': task_id,
            'file_count': len(file_list),
            'uploader': uploader,
        })
    except ValueError as e:
        return error(1, str(e))
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
