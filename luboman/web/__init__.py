import datetime
import functools
import json
import logging
import os

import aiohttp_cors
from aiohttp import web
from playhouse.shortcuts import model_to_dict

from luboman.config import config
from luboman.core.decorators import PluginTool
from luboman.core.event import Event, EventType
from luboman.core.live import start_room
from luboman.core.upload import BiliBili, Data
from luboman.database.db import DB
from luboman.database.models import LiveRoom, BiliAccount, BiliUploadTemplate, GlobalConfig

logger = logging.getLogger('luboman')


def default_json(obj):
    if isinstance(obj, datetime.datetime):
        return str(obj)
    raise TypeError('Unable to serialize {!r}'.format(obj))


json_dumps = functools.partial(json.dumps, default=default_json)
json_response = functools.partial(web.json_response, dumps=json_dumps)

routes = web.RouteTableDef()


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


@routes.post('/bili/archive/pre')
async def pre_archive(request):
    one_account = BiliAccount.select().first()
    if one_account is None:
        return error(1, "no account found")
    cookies_str = one_account.bili_cookies
    cookies = {}
    for i in cookies_str.split(';'):
        if i:
            k, v = i.split('=')
            cookies[k] = v

    return web.json_response(BiliBili(Data()).tid_archive(cookies))


@routes.post("/v1/Config/get")
async def get_config(request):
    res = {}
    for ls in GlobalConfig.select():
        res[ls.key] = ls.value
    return success(res)


@routes.post("/v1/Config/set")
async def set_config(request):
    config_data = await request.json()
    try:
        for k, v in config_data.items():
            try:
                cfg = GlobalConfig.get(GlobalConfig.key == k)
                cfg.value = v
                cfg.save()
            except GlobalConfig.DoesNotExist:
                GlobalConfig.add(key=k, value=v)
    except:
        logger.exception("1")

    config.load_from_db()
    return success()


@routes.post("/v1/LiveRoom/listAll")
async def list_room(request):
    res = []
    for ls in LiveRoom.select():
        temp = model_to_dict(ls)
        res.append(temp)
    return success(res)


@routes.post("/v1/LiveRoom/add")
async def add_room(request):
    json_data = await request.json()
    try:
        new_room_id = LiveRoom.add(**json_data)

        room = LiveRoom.get_by_id(new_room_id)
        if room:
            start_room(model_to_dict(room), **{})
        return success(new_room_id)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/LiveRoom/update")
async def update_room(request):
    data = await request.json()
    if not data.get('id'):
        return error(1, "id is required")
    try:
        row = DB.update_live_room(data)

        room = LiveRoom.get_by_id(data.get('id'))
        if room:
            running_plugin = PluginTool.running_plugins.get(str(data.get('id')))
            running_plugin.send_event(Event(EventType.EVENT_REFRESH_ROOM_INFO, (model_to_dict(room),)))

        return success(row)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/LiveRoom/del")
async def del_room(request):
    data = await request.json()
    row_id = data.get('id')

    try:
        LiveRoom.delete_by_id(row_id)
        return success(row_id)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/BiliAccount/listAll")
async def list_bili_account(request):
    res = []
    for ls in BiliAccount.select():
        temp = model_to_dict(ls)
        res.append(temp)
    return success(res)


@routes.post("/v1/BiliAccount/add")
async def add_bili_account(request):
    data = await request.json()
    if data.get('bili_cookies_filepath'):
        with BiliBili(Data()) as bili:
            bili.login(data.get('bili_cookies_filepath'), {})
            account_info = bili.myinfo()
            if account_info and account_info.get('code') == 0:
                data['account_name'] = account_info['data']['name']
                data['account_avatar'] = account_info['data']['face']
            cookies = bili.cookies
            cookies_str = ''
            for k, v in cookies.items():
                if v is not None:
                    cookies_str += f"{k}={v};"
            data['bili_cookies'] = cookies_str

    elif data.get('bili_cookies'):
        cookies_str = data.get('bili_cookies')
        cookies = {}
        try:
            for i in cookies_str.split(';'):
                if '=' in i:
                    k, v = i.split('=')
                    cookies[k] = v
        except Exception as e:
            return error(1, f"bili_cookies format error{e}")

        if not cookies:
            return error(1, f"bili_cookies format error:{cookies_str}")
        with BiliBili(Data()) as bili:
            bili.login_by_cookie(cookies)
            account_info = bili.myinfo()
            if account_info and account_info.get('code') == 0:
                data['account_name'] = account_info['data']['name']
                data['account_avatar'] = account_info['data']['face']
    else:
        return error(1, "bili_cookie or bili_cookie_filepath is required")
    try:

        bili_account_id = BiliAccount.add(**data)

        resp_data = BiliAccount.get_by_id(bili_account_id)

        return success(model_to_dict(resp_data))
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
        bili_account = BiliAccount.get_by_id(bili_account_id)
        bili_account.state_active = 0
        bili_account.save()
        return success(bili_account_id)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/BiliUploadTemplate/listAll")
async def list_bili_upload_template(request):
    res = []
    for ls in BiliUploadTemplate.select():
        temp = model_to_dict(ls)
        res.append(temp)
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
        bili_account_id = BiliUploadTemplate.add(**data)
        return success(bili_account_id)
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
        row = DB.update_bili_upload_template(data)
        return success(row)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/BiliUploadTemplate/del")
async def del_bili_upload_template(request):
    data = await request.json()
    template_id = data.get('id')

    try:
        BiliUploadTemplate.delete_by_id(template_id)
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
