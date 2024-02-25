import asyncio

from aiohttp import web

routes = web.RouteTableDef()


@routes.get('/')
async def hello(request):
    return web.Response(text="Hello, world")


app = web.Application()
app.add_routes(routes)


async def serve():
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 5001)
    await site.start()
