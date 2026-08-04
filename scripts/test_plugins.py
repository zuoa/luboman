"""手动测试脚本：测试三个平台插件的流地址解析（绕过 LiveBase 常驻线程）"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

from luboman.plugins.huya import Huya
from luboman.plugins.douyin import Douyin
from luboman.plugins.afreecatv import AfreecaTV


def test(name, obj):
    try:
        ok = obj.check_live()
        print(f"[{name}] check_live -> {ok}")
        if ok:
            print(f"[{name}] url: {obj.raw_stream_url[:150]}...")
            print(f"[{name}] title: {obj.room_data.get('room_title')} owner: {obj.room_data.get('room_owner')}")
    except Exception as e:
        print(f"[{name}] EXCEPTION: {e}")


which = sys.argv[1] if len(sys.argv) > 1 else 'all'

if which in ('huya', 'all'):
    test('huya', Huya('test', 'https://www.huya.com/52399'))
if which in ('douyin', 'all'):
    test('douyin', Douyin('test', 'https://live.douyin.com/81482202'))
if which in ('afreecatv', 'all'):
    test('afreecatv', AfreecaTV('test', 'https://play.sooplive.com/tildaaa/263094720'))

sys.stdout.flush()
sys.stderr.flush()
os._exit(0)  # LiveBase 的常驻线程不退出，直接结束进程
