import datetime
import functools
import json
import logging
import os

import aiohttp_cors
from aiohttp import web

from luboman.config import config
from luboman.core.async_utils import run_blocking
from luboman.core.runtime import (
    collect_runtime_stats,
    refresh_room_runtime,
    start_room_runtime,
    stop_room_runtime,
)
from luboman.core.upload import BiliBili, Data
from luboman.database.db import DB
from luboman.database.models import GlobalConfig, db

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
            await refresh_room_runtime(room_data)

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
