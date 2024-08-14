import random
import time

import requests

from luboman.config import config
from luboman.core.decorators import PluginTool
from luboman.core.live import LiveBase
from luboman.plugins import logger


@PluginTool.live(regexp=r'(?:https?://)?(?:(?:live|www|v)\.)?(kuaishou)\.com')
class Kuaishou(LiveBase):
    def __init__(self, room_name, room_url, suffix='flv'):
        super().__init__(room_name, room_url, suffix)
        self.fake_headers['Cookie'] = config.get('kuaishou_cookies', '')
        self.logger_prefix = f"{Kuaishou.__name__}: {self.room_url}"

    def check_live(self, is_check_status=False):
        try:
            room_id = self.get_kwaiId()
            if not room_id:
                logger.warning(f"{self.logger_prefix}: 直播间地址错误")
                return False
        except Exception as e:
            logger.error(f"{self.logger_prefix}: {e}")
            return False

        fake_headers = self.fake_headers.copy()
        session = requests.session()
        session.headers.update(fake_headers)
        session.get("https://live.kuaishou.com", timeout=5)

        time.sleep(3 + random.random())

        err_keys = ["错误代码22", "主播尚未开播"]
        html_text = session.get(f"https://live.kuaishou.com/u/{room_id}", timeout=5).text
        for key in err_keys:
            if key in html_text:
                logger.debug(f"{self.logger_prefix}: {key}")
                return False

        room_info = session.get(f"https://live.kuaishou.com/live_api/liveroom/livedetail?principalId={room_id}", timeout=15).json()['data']

        if room_info['result'] == 22:
            logger.error(f"{self.logger_prefix}: 直播间地址错误")
            return False
        if room_info['result'] == 671:
            logger.debug(f"{self.logger_prefix}: 直播间未开播或非直播")
            return False
        if room_info['result'] != 1:
            logger.error(f"{self.logger_prefix}: {room_info}")
            return False

        new_room_data = {
            'room_id': room_id,
            'room_platform': self.__class__.__name__,
            'room_title': room_info.get('author', {}).get('description', room_id),
            'room_owner': room_info.get('author', {}).get('name', ''),
            'room_owner_id': room_info.get('author', {}).get('id', ''),
            'room_owner_avatar': room_info.get('author', {}).get('avatar', ''),
            'room_cover_url': room_info.get('liveStream', {}).get('poster', ''),
            'room_cover_frame_url': room_info.get('liveStream', {}).get('poster', ''),
            'live_state': 1
        }
        self.room_data.update(new_room_data)
        logger.info(self.room_data)
        if is_check_status:
            return True

        self.raw_stream_url = room_info['liveStream']['playUrls'][0]['adaptationSet']['representation'][-1]['url']

        return True

    def get_kwaiId(self):
        url = self.room_url
        split_args = ["/profile/", "/fw/live/", "/u/"]
        for key in split_args:
            if key in url:
                kwaiId = url.split(key)[1]
                return kwaiId


if __name__ == '__main__':
    Kuaishou('test', 'https://live.kuaishou.com/u/tianci666').check_live()
