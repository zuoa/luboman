import asyncio
import logging
import os
import pathlib
from importlib.resources import files

import aiohttp_cors
from aiohttp import web
from playhouse.shortcuts import model_to_dict

from luboman.core.live import start_room
from luboman.database.db import DB
from luboman.database.models import LiveRoom

logger = logging.getLogger('luboman')
routes = web.RouteTableDef()


def success(data):
    wrapper_data = {
        "code": 0,
        "data": data,
        "message": "success"
    }
    return web.json_response(wrapper_data)


def error(code, message):
    wrapper_data = {
        "code": code,
        "message": message
    }
    return web.json_response(wrapper_data)


@routes.get("/")
async def root_handler(request):
    return web.HTTPFound('/index.html')


@routes.get('/ping')
async def hello(request):
    return web.Response(text="pong")


@routes.post("/v1/room/listAll")
async def list_room(request):
    res = []
    for ls in LiveRoom.select():
        temp = model_to_dict(ls)
        res.append(temp)
    return success(res)


@routes.post("/v1/room/add")
async def add_room(request):
    res = []
    json_data = await request.json()
    try:
        new_room_id = LiveRoom.add(**json_data)
        start_room(json_data.get('room_name'), json_data.get('room_url'), **{'room_db_row_id': new_room_id})
        return success(new_room_id)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


@routes.post("/v1/room/update")
async def add_room(request):
    data = await request.json()
    if not data.get('id'):
        return error(1, "id is required")
    try:
        row = DB.update_live_room(data)
        return success(row)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


app = web.Application()
app.add_routes(routes)


async def serve(host='127.0.0.1', port=5001):
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
    site = None
    if os.path.exists('/.dockerenv'):
        site = web.TCPSite(runner, port=port)
    else:
        site = web.TCPSite(runner, host=host, port=port)
    await site.start()

    logger.info(f"HttpServer started at http://{host}:{port}")
