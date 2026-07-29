import random
from typing import Optional
from urllib.parse import unquote, urlparse, parse_qs, urlencode, urlunparse

import requests

from luboman.config import config
from luboman.core.abogus import ABogus
from luboman.core.decorators import PluginTool
from luboman.core.live import LiveBase
from luboman.core.utils import match1, random_user_agent, json_loads
from luboman.plugins import logger


def _cfg_bool(key, default=False):
    """GlobalConfig 中的值均为字符串，布尔配置需要显式转换"""
    val = config.get(key, default)
    if isinstance(val, str):
        return val.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(val)


@PluginTool.live(regexp=r'(?:https?://)?(?:(?:www|m|live|v)\.)?douyin\.com')
class Douyin(LiveBase):

    def __init__(self, room_name, room_url, suffix='flv'):
        super().__init__(room_name, room_url, suffix)
        self.fake_headers['user-agent'] = DouyinUtils.DOUYIN_USER_AGENT
        self.fake_headers['referer'] = "https://live.douyin.com/"
        self.fake_headers['cookie'] = config.get('douyin_cookies', '')
        self.douyin_quality = config.get('douyin_quality', 'origin')
        self.douyin_protocol = 'hls' if config.get('douyin_protocol') == 'hls' else 'flv'
        self.douyin_true_origin = _cfg_bool('douyin_true_origin', False)
        self.douyin_double_screen = _cfg_bool('douyin_double_screen', False)
        self.__web_rid = None  # 网页端房间号 或 抖音号
        self.__room_id = None  # 单场直播的直播房间
        self.__sec_uid = None

    def check_live(self, is_check_status=False):

        if self.fake_headers['cookie'] != "" and not self.fake_headers['cookie'].endswith(';'):
            self.fake_headers['cookie'] += ";"
        if "ttwid" not in self.fake_headers['cookie']:
            self.fake_headers['cookie'] += f'ttwid={DouyinUtils.get_ttwid()};'
        if 'odin_ttid=' not in self.fake_headers['cookie']:
            self.fake_headers['cookie'] += f"odin_ttid={DouyinUtils.generate_odin_ttid()};"
        if '__ac_nonce=' not in self.fake_headers['cookie']:
            self.fake_headers['cookie'] += f"__ac_nonce={DouyinUtils.generate_nonce()};"

        if "v.douyin" in self.room_url:
            try:
                resp = requests.get(self.room_url, headers=self.fake_headers, timeout=5, allow_redirects=False)
                if resp.status_code not in {301, 302}:
                    raise Exception('不支持的链接')
                next_url = resp.headers['location']
                if "webcast.amemv" in next_url:
                    self.__sec_uid = match1(next_url, r"sec_user_id=(.*?)&")
                    self.__room_id = match1(next_url.split("?")[0], r"(\d+)")
                elif "isedouyin.com/share/user" in next_url:
                    self.__sec_uid = match1(next_url, r"sec_uid=(.*?)&")
                else:
                    raise Exception('不支持的链接')
            except Exception as e:
                logger.warning(f"{self.log_prefix} :  {e}")
                return False
        elif "/user/" in self.room_url:
            sec_uid = self.room_url.split("user/")[1].split("?")[0]
            if len(sec_uid) in {55, 76}:
                self.__sec_uid = sec_uid
            else:
                try:
                    user_page = requests.get(self.room_url, headers=self.fake_headers, timeout=5).text
                    user_page_data = unquote(
                        user_page.split('<script id="RENDER_DATA" type="application/json">')[1].split('</script>')[0])
                    web_rid = match1(user_page_data, r'"web_rid":"([^"]+)"')
                    if not web_rid:
                        logger.debug(f"{self.log_prefix} :  未开播")
                        return False
                    self.__web_rid = web_rid
                except (KeyError, IndexError):
                    logger.warning(f"{self.log_prefix} :  获取房间ID失败,请检查Cookie设置")
                    return False
                except:
                    logger.warning(f"{self.log_prefix} :  获取房间ID失败")
                    return False
        else:
            try:
                web_rid = self.room_url.split('douyin.com/')[1].split('/')[0].split('?')[0]
                if not web_rid:
                    raise
                if web_rid[0] == "+":
                    web_rid = web_rid[1:]
                self.__web_rid = web_rid
            except:
                logger.warning(f"{self.log_prefix} :  直播间地址错误")
                return False

        try:
            _room_info = {}
            if self.__web_rid:
                _room_info = self.get_web_room_info(self.__web_rid)
                if _room_info:
                    if not _room_info['data'].get('user'):
                        if _room_info['data'].get('prompts', '') == '直播已结束':
                            logger.debug(f"{self.log_prefix} :  未开播")
                            return False
                        # 可能是用户被封禁
                        raise Exception(f"{str(_room_info)}")
                    self.__sec_uid = _room_info['data']['user']['sec_uid']
            # PCWeb 端无流 或 没有提供 web_rid
            if not _room_info.get('data', {}).get('data'):
                _room_info = self.get_h5_room_info(self.__sec_uid, self.__room_id)
                if _room_info['data'].get('room', {}).get('owner'):
                    self.__web_rid = _room_info['data']['room']['owner']['web_rid']
            try:
                room_info = _room_info['data']['data'][0]
            except (KeyError, IndexError):
                # 移动网页端也没有数据，当做未开播处理
                room_info = _room_info['data'].get('room', {})

            if room_info.get('status') != 2:
                logger.debug(f"{self.log_prefix} :  未开播")
                return False

            self.__room_id = room_info['id_str']
            self.room_id = self.__room_id
            owner = room_info.get('owner', {})
            self.room_data.update({
                'room_id': self.__room_id,
                'room_platform': self.__class__.__name__,
                'room_title': room_info.get('title', ''),
                'room_cover_url': room_info.get('cover', {}).get('url_list', [''])[0],
                'room_cover_frame_url': room_info.get('cover', {}).get('url_list', [''])[0],
                'room_owner': owner.get('nickname', ''),
                'room_owner_id': owner.get('id_str', '') or owner.get('sec_uid', ''),
                'room_owner_avatar': owner.get('avatar_thumb', {}).get('url_list', [''])[0],
                'live_state': 1
            })
        except Exception as e:
            logger.warning(f"{self.log_prefix} :  获取直播间信息失败 {e}")
            return False

        if is_check_status:
            return True

        # 清理上一次获取的直播流
        self.raw_stream_url = None

        try:
            pull_data = room_info['stream_url']['live_core_sdk_data']['pull_data']
            if room_info['stream_url'].get('pull_datas') and self.douyin_double_screen:
                pull_data = next(iter(room_info['stream_url']['pull_datas'].values()))
            stream_data = json_loads(pull_data['stream_data'])['data']
        except:
            logger.warning(f"{self.log_prefix} :  加载直播流失败")
            logger.debug(f"{self.log_prefix} :  room_info {room_info}")
            return False

        # 抖音FLV真原画
        if (
            self.douyin_true_origin  # 开启真原画
            and
            self.douyin_quality == 'origin'  # 请求原画
            and
            self.douyin_protocol == 'flv'  # 请求FLV
        ):
            try:
                self.raw_stream_url = stream_data['ao']['main']['flv'].replace('&only_audio=1', '')
            except KeyError:
                logger.debug(f"{self.log_prefix} :  未找到 ao 流 {stream_data}")

        if not self.raw_stream_url:
            # 原画origin 蓝光uhd 超清hd 高清sd 标清ld 流畅md 仅音频ao
            quality_items = ['origin', 'uhd', 'hd', 'sd', 'ld', 'md']
            quality = self.douyin_quality
            if quality not in quality_items:
                quality = quality_items[0]
            try:
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

                self.raw_stream_url = stream_data[quality]['main'][self.douyin_protocol]
            except:
                logger.warning(f"{self.log_prefix} :  寻找清晰度失败")
                return False

        self.raw_stream_url = self.raw_stream_url.replace('http://', 'https://')
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
        resp_web_info = requests.get(target_url, headers=self.fake_headers, timeout=10)
        try:
            web_info = resp_web_info.json()
        except:
            web_info = json_loads(unquote(resp_web_info.text))
        return web_info

    def get_h5_room_info(self, sec_user_id: str, room_id: str) -> dict:
        '''
        Mobile web 的 API 信息，海外可能不允许使用
        '''
        if not sec_user_id:
            raise ValueError("sec_user_id is None")
        query = {
            'type_id': 0,
            'live_id': 1,
            'version_code': '99.99.99',
            'app_id': 1128,
            'room_id': room_id if room_id else 2,  # 必要但不校验
            'sec_user_id': sec_user_id
        }
        abogus = ABogus(user_agent=DouyinUtils.DOUYIN_USER_AGENT)
        query_str, _, _, _ = abogus.generate_abogus(params=urlencode(query, doseq=True), body="")
        logger.debug(f"{self.log_prefix}: get_h5_room_info {query_str}")
        resp = requests.get(
            f"https://webcast.amemv.com/webcast/room/reflow/info/?{query_str}",
            headers=self.fake_headers, timeout=10
        )
        return json_loads(resp.text)


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
    d = Douyin('test', 'https://live.douyin.com/81482202')
    print(d.check_live())
    print(d.raw_stream_url)
