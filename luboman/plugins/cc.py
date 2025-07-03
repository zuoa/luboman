import requests

from luboman.core.decorators import PluginTool
from luboman.core.live import LiveBase
from luboman.core.utils import random_user_agent, match1
from luboman.plugins import logger

@PluginTool.live(regexp=r'https?://cc\.163\.com')
class CC(LiveBase):
    def __init__(self, room_name, room_url, suffix='flv'):
        super().__init__(room_name, room_url, suffix)
        self.fake_headers['referer'] = room_url

    def check_live(self, is_check_status=False):
        room_info = {}
        try:
            room_id = match1(self.room_url, r"(\d{4,})")
            if not room_id:
                raise

            room_info = requests.get(f"https://api.cc.163.com/v1/activitylives/anchor/lives?anchor_ccid={room_id}", timeout=10, headers=self.fake_headers).json().get('data', {}).get(room_id, {})

            channel_id = room_info["channel_id"]
            channel_info = (requests.get(
                f"https://cc.163.com/live/channel/?channelids={channel_id}",
                timeout=5,
                headers=self.fake_headers
            )).json()["data"][0]

        except:
            logger.debug(f"{self.log_prefix}: 获取直播间信息错误，未开播")
            return False

        if not room_info:
            logger.debug(f"{self.log_prefix}: 未开播")
            return False

        new_room_data = {
            'room_id': room_id,
            'room_platform': self.__class__.__name__,
            'room_title': channel_info.get('title', ''),
            'room_cover_url': channel_info.get('cover', ''),
            'room_cover_frame_url': channel_info.get('cover', ''),
            'room_owner': room_info.get('nickname', ''),
            'room_owner_id': room_info.get('ccid', ''),
            'room_owner_avatar': channel_info.get('purl', ''),
            'live_state': 1
        }

        self.room_data.update(new_room_data)

        if is_check_status:
            return True

        self.raw_stream_url = channel_info["sharefile"]

        return True



if __name__ == '__main__':
    CC('test', 'https://cc.163.com/700700/').check_live()
