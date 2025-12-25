import hashlib
import time
import random

import requests

from luboman.core.live import LiveBase
from luboman.config import config
from luboman.core.decorators import PluginTool
from luboman.core.utils import match1
from luboman.plugins import logger


@PluginTool.live(regexp=r'(?:https?://)?(?:(?:www|m)\.)?douyu\.com')
class Douyu(LiveBase):

    def __init__(self, room_name, room_url, suffix='flv'):
        super().__init__(room_name, room_url, suffix)

    def extract_room_id(self, html):
        """从 HTML 中提取 roomInfo JSON 并获取 room_id"""
        import json
        import re

        try:
            # 查找 "roomInfo" 字符串
            room_info_pattern = r'\\"roomInfo\\"'
            match = re.search(room_info_pattern, html)
            if not match:
                # 尝试旧的正则匹配方式作为备用
                result = match1(html, r'\$ROOM\.room_id\s*=\s*(\d+)', r'apm_room_id\s*=\s*(\d+)')
                return result[0] if result else None

            # 从匹配位置开始查找 JSON 对象
            start_pos = match.end()
            # 查找第一个 {
            brace_start = html.find('{', start_pos)
            if brace_start == -1:
                return None

            # 使用栈匹配找到完整的 JSON 对象
            brace_count = 0
            end_pos = brace_start
            for i in range(brace_start, len(html)):
                if html[i] == '{':
                    brace_count += 1
                elif html[i] == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        end_pos = i
                        break

            json_str = html[brace_start:end_pos + 1]

            # 清理转义字符
            json_str = json_str.replace('\\"', '"')
            json_str = json_str.replace('\\"', '"')  # 可能需要两次

            # 解析 JSON
            room_data = json.loads(json_str)
            room_id = room_data.get('room', {}).get('room_id')

            return str(room_id) if room_id else None

        except Exception as e:
            logger.debug(f"{self.log_prefix} : JSON 提取失败，尝试正则: {e}")
            # 备用方案：使用正则表达式
            result = match1(html, r'\$ROOM\.room_id\s*=\s*(\d+)', r'apm_room_id\s*=\s*(\d+)')
            return result[0] if result else None

    def get_did(self):
        """生成 did 参数"""
        time_val = int(time.time())
        random.seed(time_val)
        rand_val = str(random.random())
        return hashlib.md5(rand_val.encode('utf-8')).hexdigest()

    def get_encryption(self, did, session):
        """获取新的加密信息"""
        try:
            url = f"https://www.douyu.com/wgapi/livenc/liveweb/websec/getEncryption?did={did}"
            resp = session.get(url, timeout=5).json()

            if resp.get('error') != 0:
                logger.warning(f"{self.log_prefix} : 获取加密信息失败")
                return None

            data = resp.get('data', {})
            return {
                'key': data.get('key'),
                'rand_str': data.get('rand_str'),
                'enc_time': data.get('enc_time'),
                'expire_at': data.get('expire_at'),
                'enc_data': data.get('enc_data'),
                'is_special': data.get('is_special')
            }
        except Exception as e:
            logger.warning(f"{self.log_prefix} : 获取加密信息异常: {e}")
            return None

    def get_auth(self, encryption, room_id, ts):
        """计算 auth 参数"""
        u = encryption['rand_str']
        key = encryption['key']
        enc_time = encryption['enc_time']
        is_special = encryption['is_special']

        # 多次 MD5
        for _ in range(enc_time):
            u = hashlib.md5((u + key).encode('utf-8')).hexdigest()

        # 根据 is_special 决定是否添加 room_id 和 ts
        o = "" if is_special == 1 else f"{room_id}{ts}"

        return hashlib.md5((u + key + o).encode('utf-8')).hexdigest()

    def check_live(self, is_check_status=False):
        if len(self.room_url.split("douyu.com/")) < 2:
            logger.warning(f"{self.log_prefix} : 直播间地址错误")
            return False

        with requests.Session() as session:
            session.headers.update(self.fake_headers)

            try:
                if 'm.douyu.com' in self.room_url:
                    room_id = self.room_url.split('m.douyu.com/')[1].split('/')[0].split('?')[0]
                else:
                    html = session.get(self.room_url, timeout=5).text
                    room_id = self.extract_room_id(html)

                if not room_id:
                    logger.warning(f"{self.log_prefix} : 直播间不存在或已关闭")
                    return False
            except Exception as e:
                logger.warning(f"{self.log_prefix} : 获取房间号错误: {e}")
                return False

            try:
                room_info = session.get(f"https://www.douyu.com/betard/{room_id}", timeout=5).json()['room']
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
                    logger.debug(f"{self.log_prefix} : 未开播")
                    return False

                if room_info.get('videoLoop', 0) != 0:
                    logger.debug(f"{self.log_prefix} : 正在放录播")
                    return False
            except Exception as e:
                logger.warning(f"{self.log_prefix} : 获取直播间信息错误: {e}")
                return False

            if is_check_status:
                return True

            try:
                # 使用新的加密方式
                ts = int(time.time())
                did = self.get_did()

                encryption = self.get_encryption(did, session)
                if not encryption:
                    return False

                auth = self.get_auth(encryption, room_id, ts)

                # 构建请求参数
                params = {
                    'enc_data': encryption['enc_data'],
                    'tt': str(ts),
                    'did': did,
                    'auth': auth,
                    'cdn': config.get('douyucdn', ''),
                    'rate': str(config.get('douyu_rate', 0)),
                    'hevc': '1',
                    'fa': '0',
                    'ive': '0'
                }

            except Exception as e:
                logger.warning(f"{self.log_prefix} : 获取签名参数异常: {e}")
                return False

            try:
                live_data = self.get_play_info(room_id, params, session)
                if type(live_data) is not dict:
                    return False
            except Exception as e:
                logger.warning(f"{self.log_prefix} : 获取下载信息错误: {e}")
                return False

            self.raw_stream_url = f"{live_data.get('rtmp_url')}/{live_data.get('rtmp_live')}"
        return True

    def get_play_info(self, room_id, params, session):
        """获取播放信息"""
        try:
            url = f'https://www.douyu.com/lapi/live/getH5PlayV1/{room_id}'
            resp = session.post(url, data=params, timeout=5).json()

            live_data = resp.get('data')
            if type(live_data) is dict:
                return live_data

            return None
        except Exception as e:
            logger.warning(f"{self.log_prefix} : 请求播放信息失败: {e}")
            return None


if __name__ == '__main__':
    print(Douyu('test', 'https://douyu.com/67554').check_live())