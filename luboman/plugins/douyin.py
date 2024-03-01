import json
import time
from typing import Optional
from urllib.parse import urlencode, parse_qs, urlparse, urlunparse, unquote

import requests

from luboman.core.live import LiveBase
from luboman.core.utils import match1, NamedLock
from ..config import config
from . import logger
from ..core.decorators import PluginTool


@PluginTool.liv(regexp=r'(?:https?://)?(?:(?:www|m|live)\.)?douyin\.com')
class Douyin(LiveBase):

    def __init__(self, room_name, room_url, suffix='flv'):
        super().__init__(room_name, room_url, suffix)
        self.room_platform = 'douyin'
        self.fake_headers['referer'] = "https://live.douyin.com/"
        self.fake_headers['cookie'] = config.get('user', {}).get('douyin_cookie', '')

    def check_live(self, is_check_status=False):

        if "/user/" in self.room_url:
            try:
                user_page = requests.get(self.room_url, headers=self.fake_headers, timeout=5).text
                user_page_data = unquote(
                    user_page.split('<script id="RENDER_DATA" type="application/json">')[1].split('</script>')[0])
                room_id = match1(user_page_data, r'"web_rid":"([^"]+)"')
                if room_id is None or not room_id:
                    logger.debug(f"{Douyin.__name__}: {self.room_url}: 未开播")
                    return False
            except (KeyError, IndexError):
                logger.warning(f"{Douyin.__name__}: {self.room_url}: 获取房间ID失败,请检查Cookie设置")
                return False
            except:
                logger.warning(f"{Douyin.__name__}: {self.room_url}: 获取房间ID失败")
                return False
        else:
            try:
                room_id = self.room_url.split('douyin.com/')[1].split('/')[0].split('?')[0]
                if not room_id:
                    raise
            except:
                logger.warning(f"{Douyin.__name__}: {self.room_url}: 直播间地址错误")
                return False

        if room_id[0] == "+":
            room_id = room_id[1:]

        try:
            if "ttwid" not in self.fake_headers['cookie']:
                self.fake_headers['cookie'] = f'ttwid={DouyinUtils.get_ttwid()};{self.fake_headers["cookie"]}'
            page = requests.get(
                DouyinUtils.build_request_url(f"https://live.douyin.com/webcast/room/web/enter/?web_rid={room_id}"),
                headers=self.fake_headers, timeout=5).text
            room_info = json.loads(page)['data']['data']
            if len(room_info) > 0:
                room_info = room_info[0]
            else:
                room_info = {}
        except:
            logger.warning(f"{Douyin.__name__}: {self.room_url}: 获取失败")
            return False

        try:
            if room_info.get('status') != 2:
                logger.debug(f"{Douyin.__name__}: {self.room_url}: 未开播")
                return False

            stream_data = json.loads(room_info['stream_url']['live_core_sdk_data']['pull_data']['stream_data'])['data']

            # 原画origin 蓝光uhd 超清hd 高清sd 标清ld 流畅md 仅音频ao
            quality_items = ['origin', 'uhd', 'hd', 'sd', 'ld', 'md']
            quality = config.get('douyin_quality', 'origin')
            if quality not in quality_items:
                quality = quality_items[0]

            # 如果没有这个画质则取相近的 优先低清晰度
            if quality not in stream_data:
                # 可选的清晰度 含自身
                optional_quality_items = [x for x in quality_items if x in stream_data.keys() or x == quality]
                # 自身在可选清晰度的位置
                optional_quality_index = optional_quality_items.index(quality)
                # 自身在所有清晰度的位置
                quality_index = quality_items.index(quality)
                # 高清晰度偏移
                quality_left_offset = None
                # 低清晰度偏移
                quality_right_offset = None

                if optional_quality_index + 1 < len(optional_quality_items):
                    quality_right_offset = quality_items.index(
                        optional_quality_items[optional_quality_index + 1]) - quality_index

                if optional_quality_index - 1 >= 0:
                    quality_left_offset = quality_index - quality_items.index(
                        optional_quality_items[optional_quality_index - 1])

                # 取相邻的清晰度
                if quality_right_offset <= quality_left_offset:
                    quality = optional_quality_items[optional_quality_index + 1]
                else:
                    quality = optional_quality_items[optional_quality_index - 1]

            self.raw_stream_url = stream_data[quality]['main']['flv']
            self.room_title = room_info['title']
        except:
            logger.warning(f"{Douyin.__name__}: {self.room_url}: 解析错误")
            return False
        return True


class DouyinUtils:
    # 抖音ttwid
    _douyin_ttwid: Optional[str] = None

    @staticmethod
    def get_ttwid() -> Optional[str]:
        with NamedLock("douyin_ttwid_get"):
            if not DouyinUtils._douyin_ttwid:
                page = requests.get("https://live.douyin.com/1-2-3-4-5-6-7-8-9-0", timeout=5)
                DouyinUtils._douyin_ttwid = page.cookies.get("ttwid")
            return DouyinUtils._douyin_ttwid

    @staticmethod
    def build_request_url(url: str) -> str:
        parsed_url = urlparse(url)
        existing_params = parse_qs(parsed_url.query)
        existing_params['aid'] = ['6383']
        existing_params['device_platform'] = ['web']
        existing_params['browser_language'] = ['zh-CN']
        existing_params['browser_platform'] = ['Win32']
        existing_params['browser_name'] = ['Chrome']
        existing_params['browser_version'] = ['92.0.4515.159']
        new_query_string = urlencode(existing_params, doseq=True)
        new_url = urlunparse((
            parsed_url.scheme,
            parsed_url.netloc,
            parsed_url.path,
            parsed_url.params,
            new_query_string,
            parsed_url.fragment
        ))
        return new_url
