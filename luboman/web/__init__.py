import asyncio
import logging

from aiohttp import web
from playhouse.shortcuts import model_to_dict

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


@routes.get('/')
async def hello(request):
    return web.Response(text="Hello, world")


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
        new_room = LiveRoom.add(**json_data)
        return success(new_room)
    except Exception as e:
        logger.error(e)
        return error(1, str(e))


app = web.Application()
app.add_routes(routes)


async def serve():
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 5001)
    await site.start()
