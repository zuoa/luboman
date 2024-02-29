import asyncio
import logging
import pathlib
from importlib.resources import files

import aiohttp_cors
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


@routes.get("/")
async def root_handler(request):
    return web.HTTPFound('/index.html')


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


async def serve(host='localhost', port=5001):
    logger.info(f"Server started at http://{host}:{port}")
    app.add_routes(routes)
    res = []
    for dir in pathlib.Path(files('luboman.web').joinpath('public')).glob('*.html'):
        file_name = dir.relative_to(files('luboman.web').joinpath('public'))

        def _copy(file_name):
            async def static_view(request):
                return web.FileResponse(files('luboman.web').joinpath('public/' + str(file_name)))

            return static_view

        res.append(web.get('/' + str(file_name.with_suffix('')), _copy(file_name)))
        # res.append(web.static('/'+fdir.replace('\\', '/'), files('biliup.web').joinpath('public/'+fdir)))

    res.append(web.static('/', files('luboman.web').joinpath('public')))
    app.add_routes(res)

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
    site = web.TCPSite(runner, port=port)
    await site.start()
