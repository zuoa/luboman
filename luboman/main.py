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


def arg_parser():
    # daemon = Daemon('watch_process.pid', lambda: main(args))
    parser = argparse.ArgumentParser(description='Stream download and upload, not only for bilibili.')
    # parser.add_argument('--version', action='version', version=f"v{__version__}")
    # parser.add_argument('-H', help='web api host [default: 0.0.0.0]', dest='host')
    # parser.add_argument('-P', help='web api port [default: 19159]', default=19159, dest='port')
    # parser.add_argument('--no-http', action='store_true', help='disable web api')
    # parser.add_argument('--static-dir', help='web static files directory for custom ui')
    # parser.add_argument('--password', help='web ui password ,default username is biliup', dest='password')
    parser.add_argument('-v', '--verbose', action="store_const", const=logging.DEBUG, help="Increase output verbosity")
    # parser.add_argument('--config', type=argparse.FileType(mode='rb'),
    #                     help='Location of the configuration file (default "./config.yaml")')
    # subparsers = parser.add_subparsers(help='Windows does not support this sub-command.')
    # # create the parser for the "start" command
    # parser_start = subparsers.add_parser('start', help='Run as a daemon process.')
    # parser_start.set_defaults(func=daemon.start)
    # parser_stop = subparsers.add_parser('stop', help='Stop daemon according to "watch_process.pid".')
    # parser_stop.set_defaults(func=daemon.stop)
    # parser_restart = subparsers.add_parser('restart')
    # parser_restart.set_defaults(func=daemon.restart)
    # parser.set_defaults(func=lambda: asyncio.run(main(args)))
    # args = parser.parse_args()
    #
    # if args.verbose:
    #     LOG_CONF['loggers']['luboman']['level'] = args.verbose
    #     LOG_CONF['root']['level'] = args.verbose
    # logging.config.dictConfig(LOG_CONF)
    # # args.func()
    # asyncio.run(main(args))


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

    # Douyu("谢彬DD", "https://www.douyu.com/110").start()
    # Douyu("Azheng", "https://www.douyu.com/73965").start()

    # Bilibili("舞见", "https://live.bilibili.com/26357031").start()
    # start_room('测试', 'https://www.huya.com/924898')
    # start_room("Azheng", "https://www.douyu.com/73965")


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
