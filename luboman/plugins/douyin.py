import json
import random
import time
from typing import Optional
from urllib.parse import urlencode, parse_qs, urlparse, urlunparse, unquote

import requests

from luboman.core.abogus import ABogus
from luboman.core.utils import match1, NamedLock, random_user_agent, json_loads
from luboman.config import config
from luboman.core.decorators import PluginTool
from luboman.core.live import LiveBase
from luboman.plugins import logger


@PluginTool.live(regexp=r'(?:https?://)?(?:(?:www|m|live)\.)?douyin\.com')
class Douyin(LiveBase):

    def __init__(self, room_name, room_url, suffix='flv'):
        super().__init__(room_name, room_url, suffix)
        self.fake_headers['user-agent'] = DouyinUtils.DOUYIN_USER_AGENT
        self.fake_headers['referer'] = "https://live.douyin.com/"
        self.fake_headers['cookie'] = config.get('douyin_cookies', '')
        if self.fake_headers['cookie'] != "" and not self.fake_headers['cookie'].endswith(';'):
            self.fake_headers['cookie'] += ";"
        if "ttwid" not in self.fake_headers['cookie']:
            self.fake_headers['cookie'] += f'ttwid={DouyinUtils.get_ttwid()};'
        if 'odin_ttid=' not in self.fake_headers['cookie']:
            self.fake_headers['cookie'] += f"odin_ttid={DouyinUtils.generate_odin_ttid()};"
        if '__ac_nonce=' not in self.fake_headers['cookie']:
            self.fake_headers['cookie'] += f"__ac_nonce={DouyinUtils.generate_nonce()};"

    def check_live(self, is_check_status=False):

        if "/user/" in self.room_url:
            try:
                room_id = None
                user_page = requests.get(self.room_url, headers=self.fake_headers, timeout=5).text
                if "web_rid" in user_page:
                    user_page_data = user_page[user_page.index("web_rid"):user_page.index("web_rid") + 50].replace('\\"', '"')
                    room_id = match1(user_page_data, r'web_rid":"([^"]+)"')
                if room_id is None or not room_id:
                    logger.debug(f"{self.log_prefix} :  未开播")
                    return False
            except (KeyError, IndexError):
                logger.warning(f"{self.log_prefix} :  获取房间ID失败,请检查Cookie设置")
                return False
            except:
                logger.warning(f"{self.log_prefix} :  获取房间ID失败")
                return False
        else:
            try:
                room_id = self.room_url.split('douyin.com/')[1].split('/')[0].split('?')[0]
                if not room_id:
                    raise
            except:
                logger.warning(f"{self.log_prefix} :  直播间地址错误")
                return False

        if room_id[0] == "+":
            room_id = room_id[1:]

        self.room_id = room_id

        try:
            if "ttwid" not in self.fake_headers['cookie']:
                self.fake_headers['cookie'] = f'ttwid={DouyinUtils.get_ttwid()};{self.fake_headers["cookie"]}'
            web_info = self.get_web_room_info(room_id)

            room_info = web_info.get('data', {}).get('data', [])
            if len(room_info) > 0:
                room_info = room_info[0]
                new_room_data = {
                    'room_id': room_id,
                    'room_platform': self.__class__.__name__,
                    'room_title': room_info.get('title', ''),
                    'room_cover_url': room_info.get('cover', {}).get('url_list', [''])[0],
                    'room_cover_frame_url': room_info.get('cover', {}).get('url_list', [''])[0],
                    'room_owner': room_info.get('owner', {}).get('nickname', ''),
                    'room_owner_id': room_info.get('owner', {}).get('id_str', ''),
                    'room_owner_avatar': room_info.get('owner', {}).get('avatar_thumb', {}).get('url_list', [''])[0],
                    'live_state': 1
                }
                self.room_data.update(new_room_data)

            else:
                room_info = {}
        except Exception as e:
            logger.warning(f"{self.log_prefix} :  获取失败{e}")
            return False

        try:
            if room_info.get('status') != 2:
                logger.debug(f"{self.log_prefix} :  未开播")
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
        except:
            logger.warning(f"{self.log_prefix} :  解析错误")
            return False
        return True

    def get_web_room_info(self, web_rid: str) -> dict:
        query = {
            'app_name': 'douyin_web',
            # 'enter_from': random.choice(['link_share', 'web_live']),
            'enter_from': 'web_live',
            'live_id': '1',
            'web_rid': web_rid,
            'is_need_double_stream': "false"
        }
        target_url = DouyinUtils.build_request_url(f"https://live.douyin.com/webcast/room/web/enter/", query)
        logger.debug(f"{self.log_prefix}: get_web_room_info {target_url}")
        resp_web_info = requests.get(target_url, headers=self.fake_headers)
        # logger.debug(f"{self.log_prefix}: get_web_room_info {resp_web_info.text}")
        try:
            web_info = resp_web_info.json()
        except:
            web_info = json_loads(unquote(resp_web_info.text))
        return web_info


class DouyinUtils:
    # 抖音ttwid
    _douyin_ttwid: Optional[str] = None
    # DOUYIN_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.159 Safari/537.36'
    DOUYIN_USER_AGENT = random_user_agent()
    DOUYIN_HTTP_HEADERS = {
        'user-agent': DOUYIN_USER_AGENT
    }
    CHARSET = "abcdef0123456789"
    LONG_CHATSET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"

    @staticmethod
    def get_ttwid() -> Optional[str]:
            if not DouyinUtils._douyin_ttwid:
                page = requests.get("https://live.douyin.com/1-2-3-4-5-6-7-8-9-0", timeout=15)
                DouyinUtils._douyin_ttwid = page.cookies.get("ttwid")
            return DouyinUtils._douyin_ttwid


    @staticmethod
    def generate_ms_token() -> str:
        '''生成随机 msToken'''
        return ''.join(random.choice(DouyinUtils.LONG_CHATSET) for _ in range(184))


    @staticmethod
    def generate_nonce() -> str:
        """生成 21 位随机十六进制小写 nonce"""
        return ''.join(random.choice(DouyinUtils.CHARSET) for _ in range(21))


    @staticmethod
    def generate_odin_ttid() -> str:
        """生成 160 位随机十六进制小写 odin_ttid"""
        return ''.join(random.choice(DouyinUtils.CHARSET) for _ in range(160))


    @staticmethod
    def build_request_url(url: str, query: Optional[dict] = None) -> str:
        # NOTE: 不能在类级别初始化，否则非首次生成的 abogus 有问题，原因未知
        abogus = ABogus(user_agent=DouyinUtils.DOUYIN_USER_AGENT)
        parsed_url = urlparse(url)
        existing_params = query or parse_qs(parsed_url.query)
        existing_params['aid'] = ['6383']
        existing_params['compress'] = ['gzip']
        existing_params['device_platform'] = ['web']
        existing_params['browser_language'] = ['zh-CN']
        existing_params['browser_platform'] = ['Win32']
        existing_params['browser_name'] = [DouyinUtils.DOUYIN_USER_AGENT.split('/')[0]]
        existing_params['browser_version'] = [DouyinUtils.DOUYIN_USER_AGENT.split(existing_params['browser_name'][0])[-1][1:]]
        if 'msToken' not in existing_params:
            existing_params['msToken'] = [DouyinUtils.generate_ms_token()]
        new_query_string = urlencode(existing_params, doseq=True)
        signed_query_string, _, _, _ = abogus.generate_abogus(params=new_query_string, body="")
        new_url = urlunparse((
            parsed_url.scheme,
            parsed_url.netloc,
            parsed_url.path,
            parsed_url.params,
            signed_query_string,
            parsed_url.fragment
        ))
        return new_url

if __name__ == '__main__':
    print(Douyin('test', 'https://live.douyin.com/81482202').check_live(is_check_status=True))
