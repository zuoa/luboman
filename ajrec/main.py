import argparse
import asyncio
import logging.config

from ajrec.core.daemon import Daemon
from ajrec.core.timer import Timer
from ajrec import __version__, LOG_CONF
from ajrec.plugins.bilibili import Bilibili
from ajrec.plugins.douyu import Douyu


def arg_parser():
    daemon = Daemon('watch_process.pid', lambda: main(args))
    parser = argparse.ArgumentParser(description='Stream download and upload, not only for bilibili.')
    parser.add_argument('--version', action='version', version=f"v{__version__}")
    parser.add_argument('-H', help='web api host [default: 0.0.0.0]', dest='host')
    parser.add_argument('-P', help='web api port [default: 19159]', default=19159, dest='port')
    parser.add_argument('--no-http', action='store_true', help='disable web api')
    parser.add_argument('--static-dir', help='web static files directory for custom ui')
    parser.add_argument('--password', help='web ui password ,default username is biliup', dest='password')
    parser.add_argument('-v', '--verbose', action="store_const", const=logging.DEBUG, help="Increase output verbosity")
    parser.add_argument('--config', type=argparse.FileType(mode='rb'),
                        help='Location of the configuration file (default "./config.yaml")')
    subparsers = parser.add_subparsers(help='Windows does not support this sub-command.')
    # create the parser for the "start" command
    parser_start = subparsers.add_parser('start', help='Run as a daemon process.')
    parser_start.set_defaults(func=daemon.start)
    parser_stop = subparsers.add_parser('stop', help='Stop daemon according to "watch_process.pid".')
    parser_stop.set_defaults(func=daemon.stop)
    parser_restart = subparsers.add_parser('restart')
    parser_restart.set_defaults(func=daemon.restart)
    parser.set_defaults(func=lambda: asyncio.run(main(args)))
    args = parser.parse_args()

    if args.verbose:
        LOG_CONF['loggers']['ajrec']['level'] = args.verbose
        LOG_CONF['root']['level'] = args.verbose
    logging.config.dictConfig(LOG_CONF)
    args.func()


async def check_func():
    print("!")


async def main(args):
    Douyu("谢彬DD", "https://www.douyu.com/110").start()
    Douyu("Azheng", "https://www.douyu.com/73965").start()

    Bilibili("测试2", "https://live.bilibili.com/21727410").start()

    checker = Timer(check_func, interval=3)
    await asyncio.gather(checker.async_start())
    #
    # while True:
    #     await asyncio.gather(detector.astart(), site.start())


if __name__ == '__main__':
    arg_parser()


