import argparse
import asyncio
import functools
import logging.config
import re

import luboman.web
from luboman.core.daemon import Daemon
from luboman.core.decorators import PluginTool
from luboman.core.timer import Timer
from luboman import __version__, LOG_CONF
from luboman.database.models import LiveRoom
from luboman.plugins.bilibili import Bilibili
from luboman.plugins.douyu import Douyu
from luboman.database.db import DB
from luboman.plugins.huya import Huya

from luboman import plugins


def start_room(room_name, url, **kwargs):
    pg = None

    for plugin in PluginTool.live_plugins:
        if re.match(plugin.VALID_URL_BASE, url):
            pg = plugin(room_name, url)
            for k in pg.__dict__:
                if kwargs.get(k):
                    pg.__dict__[k] = kwargs.get(k)
            break

    if pg:
        return pg.start()


async def start_all_record():
    for room in LiveRoom.select().where(LiveRoom.active_state == 1):
        start_room(room.room_name, room.room_url)


def do_exit(lp):
    lp.stop()


if __name__ == '__main__':
    DB.init()
    PluginTool(plugins)

    # arg_parser()
    logging.config.dictConfig(LOG_CONF)
    loop = asyncio.get_event_loop()
    future = asyncio.gather(start_all_record(), luboman.web.serve())
    future.add_done_callback(functools.partial(do_exit, loop))
    loop.run_forever()
    loop.close()
