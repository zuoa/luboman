import hashlib
import time
from urllib.parse import parse_qs

import requests

from luboman.core.live import LiveBase
from ..config import config
from ..core.decorators import PluginTool
from ..core.utils import match1
from ..plugins import logger


@PluginTool.live(regexp=r'(?:https?://)?(?:(?:www|m)\.)?douyu\.com')
class Douyu(LiveBase):

    def __init__(self, room_name, room_url, suffix='flv'):
        super().__init__(room_name, room_url, suffix)

    def check_live(self, is_check_status=False):
        if len(self.room_url.split("douyu.com/")) < 2:
            logger.warning(f"{Douyu.__name__}: {self.room_url}: 直播间地址错误")
            return False

        try:
            if 'm.douyu.com' in self.room_url:
                room_id = self.room_url.split('m.douyu.com/')[1].split('/')[0].split('?')[0]
            else:
                html = requests.get(self.room_url, headers=self.fake_headers, timeout=5).text
                room_id = match1(html, r'\$ROOM\.room_id\s*=\s*(\d+)', r'apm_room_id\s*=\s*(\d+)')[0]
            if not room_id:
                logger.warning(f"{Douyu.__name__}: {self.room_url}: 直播间不存在或已关闭")
                return False
        except:
            logger.warning(f"{Douyu.__name__}: {self.room_url}: 获取房间号错误")
            return False

        try:
            room_info = requests.get(f"https://www.douyu.com/betard/{room_id}", headers=self.fake_headers, timeout=5).json()['room']
            if room_info:
                new_room_data = {
                    'room_id': room_id,
                    'room_platform': self.__class__.__name__,
                    'room_title': room_info.get('room_name', ''),
                    'room_cover_url': room_info.get('room_pic', ''),
                    'room_cover_frame_url': room_info.get('room_pic', ''),
                    'room_owner': room_info.get('owner_name', ''),
                    'room_owner_id': room_info.get('owner_uid', ''),
                    'room_owner_avatar': room_info.get('owner_avatar', ''),
                    'room_owner_title': room_info.get('officialAnchor', {}).get('od', ''),
                    'live_state': 1 if room_info.get('show_status', 0) == 1 else 0
                }
                self.room_data.update(new_room_data)

            if room_info['show_status'] != 1:
                logger.debug(f"{Douyu.__name__}: {self.room_url}: 未开播")
                return False

            if room_info['videoLoop'] != 0:
                logger.debug(f"{Douyu.__name__}: {self.room_url}: 正在放录播")
                return False
        except Exception as e:
            logger.warning(f"{Douyu.__name__}: {self.room_url}: 获取直播间信息错误:{e}")
            return False

        if is_check_status:
            return True

        try:
            import jsengine
            ctx = jsengine.jsengine()
            js_enc = requests.get(f'https://www.douyu.com/swf_api/homeH5Enc?rids={room_id}',
                                  headers=self.fake_headers,
                                  timeout=5).json()['data'][f'room{room_id}']
            js_enc = js_enc.replace('return eval', 'return [strc, vdwdae325w_64we];')

            sign_fun, sign_v = ctx.eval(f'{js_enc};ub98484234();')

            tt = str(int(time.time()))
            did = hashlib.md5(tt.encode('utf-8')).hexdigest()
            rb = hashlib.md5(f"{room_id}{did}{tt}{sign_v}".encode('utf-8')).hexdigest()
            sign_fun = sign_fun.rstrip(';').replace("CryptoJS.MD5(cb).toString()", f'"{rb}"')
            sign_fun += f'("{room_id}","{did}","{tt}");'

            params = parse_qs(ctx.eval(sign_fun))
        except TypeError:
            logger.error(f"{Douyu.__name__}: {self.room_url}: 请安装至少一个 Javascript 解释器，如 pip install quickjs")
            return False
        except Exception as e:
            logger.warning(f"{Douyu.__name__}: {self.room_url}: 获取签名参数异常({e})")
            return False

        params['cdn'] = config.get('douyucdn', 'tct-h5')
        params['rate'] = config.get('douyu_rate', 0)

        try:
            live_data = self.get_play_info(room_id, params)
            if type(live_data) is not dict:
                return False
        except:
            logger.warning(f"{Douyu.__name__}: {self.room_url}: 获取下载信息错误")
            return False

        self.raw_stream_url = f"{live_data.get('rtmp_url')}/{live_data.get('rtmp_live')}"
        return True

    def get_play_info(self, room_id, params):
        live_data = requests.post(f'https://www.douyu.com/lapi/live/getH5Play/{room_id}', headers=self.fake_headers,
                                  params=params, timeout=5).json().get('data')
        if type(live_data) is dict:
            # 禁用斗鱼主线路
            if not live_data.get('rtmp_cdn', '').endswith('h5') or 'akm' in live_data.get('rtmp_cdn', ''):
                params['cdn'] = 'tct-h5'
                return self.get_play_info(room_id, params)
            return live_data

        return None
