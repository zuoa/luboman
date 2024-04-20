import time
from typing import Optional, Dict

import requests

from luboman.config import config
from luboman.core.decorators import PluginTool
from luboman.core.live import LiveBase
from luboman.core.utils import match1, NamedLock
from luboman.plugins import logger


@PluginTool.live(regexp=r'https?://(.*?)\.afreecatv\.com/(?P<username>\w+)(?:/\d+)?')
class AfreecaTV(LiveBase):
    VALID_URL_BASE = r"https?://.*?\.afreecatv\.com/(?P<username>\w+)(?:/\d+)?"
    CHANNEL_API_URL = "https://live.afreecatv.com/afreeca/player_live_api.php"
    QUALITIES = ["original", "hd4k", "hd", "sd"]

    def __init__(self, room_name, room_url, suffix='flv'):
        super().__init__(room_name, room_url, suffix)
        if AfreecaTVUtils.get_cookie():
            self.fake_headers['cookie'] = ';'.join([f"{name}={value}" for name, value in AfreecaTVUtils.get_cookie().items()])

    def check_live(self, is_check_status=False):
        try:
            username = match1(self.room_url, self.VALID_URL_BASE)
            if not username:
                logger.warning(f"{AfreecaTV.__name__}: {self.room_url}: 直播间地址错误")
                return False

            channel_info = requests.post(self.CHANNEL_API_URL, data={
                "bid": username,
                "bno": "",
                "type": "live",
                "pwd": "",
                "player_type": "html5",
                "stream_type": "common",
                "quality": self.QUALITIES[0],
                "mode": "landing",
                "from_api": 0,
            }, headers=self.fake_headers, timeout=5).json()

            if channel_info["CHANNEL"]["RESULT"] == -6:
                logger.warning(f"{AfreecaTV.__name__}: {self.room_url}: 检测失败,请检查账号密码设置")
                return False

            if channel_info["CHANNEL"]["RESULT"] != 1:
                return False

            new_room_data = {
                'room_id': channel_info.get('CHANNEL', {}).get('BNO', ''),
                'room_platform': self.__class__.__name__,
                'room_title': channel_info.get('CHANNEL', {}).get('TITLE', ''),
                'room_owner': channel_info.get('CHANNEL', {}).get('BJNICK', ''),
                'room_owner_id': channel_info.get('CHANNEL', {}).get('BJID', ''),
                'room_cover_url': f"https://liveimg.afreecatv.com/h/{channel_info.get('CHANNEL', {}).get('BNO', '')}.webp",
                'room_cover_frame_url': f"https://liveimg.afreecatv.com/h/{channel_info.get('CHANNEL', {}).get('BNO', '')}.webp",
                'live_state': 1
            }
            self.room_data.update(new_room_data)

            if is_check_status:
                return True

            aid_info = requests.post(self.CHANNEL_API_URL, data={
                "bid": username,
                "bno": channel_info["CHANNEL"]["BNO"],
                "type": "aid",
                "pwd": "",
                "player_type": "html5",
                "stream_type": "common",
                "quality": self.QUALITIES[0],
                "mode": "landing",
                "from_api": 0,
            }, headers=self.fake_headers, timeout=5).json()

            view_info = requests.get(f'{channel_info["CHANNEL"]["RMD"]}/broad_stream_assign.html', params={
                "return_type": channel_info["CHANNEL"]["CDN"],
                "broad_key": f'{channel_info["CHANNEL"]["BNO"]}-common-{self.QUALITIES[0]}-hls'
            }, headers=self.fake_headers, timeout=5).json()

            self.raw_stream_url = view_info["view_url"] + "?aid=" + aid_info["CHANNEL"]["AID"]
        except:
            logger.warning(f"{AfreecaTV.__name__}: {self.room_url}: 获取错误，本次跳过")
            return False

        return True


class AfreecaTVUtils:
    _cookie: Optional[Dict[str, str]] = None
    _cookie_expires = None

    @staticmethod
    def get_cookie() -> Optional[Dict[str, str]]:
        with NamedLock("AfreecaTV_cookie_get"):
            if not AfreecaTVUtils._cookie or AfreecaTVUtils._cookie_expires <= time.time():
                username = config.get('afreecatv_username', '')
                password = config.get('afreecatv_password', '')
                if not username or not password:
                    return {}
                response = requests.post("https://login.afreecatv.com/app/LoginAction.php", data={
                    "szUid": username,
                    "szPassword": password,
                    "szWork": "login",
                    "szType": "json",
                    "isSaveId": "true",
                    "isSavePw": "true",
                    "isSaveJoin": "true",
                    "isLoginRetain": "Y",
                })
                if response.json()["RESULT"] != 1:
                    return None

                cookie_dict = response.cookies.get_dict()
                AfreecaTVUtils._cookie = {
                    "RDB": cookie_dict["RDB"],
                    "PdboxBbs": cookie_dict["PdboxBbs"],
                    "PdboxTicket": cookie_dict["PdboxTicket"],
                    "PdboxSaveTicket": cookie_dict["PdboxSaveTicket"],
                }
                AfreecaTVUtils._cookie_expires = time.time() + (7 * 24 * 60 * 60)

            return AfreecaTVUtils._cookie


if __name__ == '__main__':
    print(match1('https://play.afreecatv.com/tildaaa/263094720', r"https?://.*?\.afreecatv\.com/(?P<username>\w+)(?:/\d+)?"))
