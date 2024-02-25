import asyncio
from aiohttp import web

routes = web.RouteTableDef()


@routes.get('/')
async def hello(request):
    return web.Response(text="Hello, world")


app = web.Application()
app.add_routes(routes)


# 手动启动事件循环
async def setup():
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 5001)

    return site
