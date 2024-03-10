import argparse
import asyncio
import datetime
import functools
import logging.config
import os
import re

from playhouse.shortcuts import model_to_dict

import luboman.web
from luboman.config import config
from luboman.core.daemon import Daemon
from luboman.core.decorators import PluginTool
from luboman.core.live import start_room
from luboman.core.timer import Timer
from luboman import __version__, LOG_CONF
from luboman.core.utils import remove_file, get_video_dir
from luboman.database.models import LiveRoom
from luboman.plugins.bilibili import Bilibili
from luboman.plugins.douyu import Douyu
from luboman.database.db import DB
from luboman.plugins.huya import Huya

from luboman import plugins


async def start_all_record():
    for room in LiveRoom.select():
        start_room(model_to_dict(room), **{})


def check_runtime_state():
    local_video_file_remain_days = config.get("local_video_file_remain_days", 3)

    video_dir = get_video_dir()

    for platform_dir in os.listdir(video_dir):
        if not os.path.isdir(os.path.join(video_dir, platform_dir)):
            continue

        for room_dir in os.listdir(os.path.join(video_dir, platform_dir)):
            if not os.path.isdir(os.path.join(video_dir, platform_dir, room_dir)):
                continue

            for day_dir in os.listdir(os.path.join(video_dir, platform_dir, room_dir)):
                if not os.path.isdir(os.path.join(video_dir, platform_dir, room_dir, day_dir)):
                    continue
                day_time = datetime.datetime.strptime(day_dir, "%Y-%m-%d")
                if (day_time + datetime.timedelta(days=local_video_file_remain_days)) < datetime.datetime.now():
                    remove_file(os.path.join(video_dir, platform_dir, room_dir, day_dir))


def do_exit(lp):
    lp.stop()


if __name__ == '__main__':
    logging.config.dictConfig(LOG_CONF)
    DB.init()
    config.load_from_db()
    PluginTool(plugins)

    Timer(func=check_runtime_state, interval=60).start()

    loop = asyncio.get_event_loop()
    future = asyncio.gather(start_all_record(), luboman.web.serve())
    future.add_done_callback(functools.partial(do_exit, loop))
    loop.run_forever()
    loop.close()
