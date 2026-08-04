import time
from typing import Optional, Dict

import requests

from luboman.config import config
from luboman.core.decorators import PluginTool
from luboman.core.live import LiveBase
from luboman.core.utils import match1, NamedLock
from luboman.plugins import logger


# AfreecaTV 已于 2024 年更名为 SOOP；接口路径仍保留 afreeca 前缀。
# 同时兼容新旧三种域名（sooplive.com / sooplive.co.kr / afreecatv.com，旧域名会 301 到新域名）。
_URL_DOMAIN = r"(?:sooplive\.(?:com|co\.kr)|afreecatv\.com)"


@PluginTool.live(regexp=rf'https?://(.*?)\.{_URL_DOMAIN}/(?P<username>\w+)(?:/\d+)?')
class AfreecaTV(LiveBase):
    VALID_URL_BASE = rf"https?://.*?\.{_URL_DOMAIN}/(?P<username>\w+)(?:/\d+)?"
    CHANNEL_API_URL = "https://live.sooplive.com/afreeca/player_live_api.php"
    QUALITY = "original"
    # broad_stream_assign 的 return_type 不能直接透传 CDN 字段：
    # gs_cdn/lg_cdn 必须映射为 *_pc_web，否则接口返回 404 空响应（对齐 streamlink soop 插件）
    CDN_TYPE_MAPPING = {
        "gs_cdn": "gs_cdn_pc_web",
        "lg_cdn": "lg_cdn_pc_web",
    }
    # 对齐 biliup：固定现代 UA + referer，播放器接口与 CDN 都会校验
    USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    REFERER = "https://play.sooplive.com/"

    def __init__(self, room_name, room_url, suffix='flv'):
        super().__init__(room_name, room_url, suffix)
        self.fake_headers['user-agent'] = self.USER_AGENT
        self.fake_headers['referer'] = self.REFERER
        cookie = AfreecaTVUtils.get_cookie()
        if cookie:
            self.fake_headers['cookie'] = ';'.join([f"{name}={value}" for name, value in cookie.items()])

    def check_live(self, is_check_status=False):
        # 注意不能用 self.VALID_URL_BASE 提取：其第 1 捕获组是子域名（装饰器 regexp），
        # match1 返回 group(1) 会拿到 "play"/"www" 而非主播 ID（此前一直取错，导致平台永不判定开播）
        username = match1(self.room_url, rf"https?://(?:.*?)\.{_URL_DOMAIN}/(\w+)(?:/\d+)?")
        if not username:
            logger.warning(f"{AfreecaTV.__name__}: {self.room_url}: 直播间地址错误")
            return False

        try:
            channel_info = self._channel_api(username, bno="", api_type="live")
        except Exception as e:
            logger.warning(f"{AfreecaTV.__name__}: {self.room_url}: 获取直播间信息失败: {e}")
            return False

        channel = channel_info.get("CHANNEL", {})
        result = channel.get("RESULT")
        if result == -6:
            logger.warning(f"{AfreecaTV.__name__}: {self.room_url}: 检测失败(需要登录或成人认证),请检查账号密码设置")
            return False
        if result != 1:
            # 未开播或房间不存在
            return False

        bno = channel.get('BNO', '')
        self.room_data.update({
            'room_id': username,
            'room_platform': self.__class__.__name__,
            'room_title': channel.get('TITLE', ''),
            'room_owner': channel.get('BJNICK', ''),
            'room_owner_id': channel.get('BJID', ''),
            'room_cover_url': f"https://liveimg.sooplive.co.kr/h/{bno}.webp",
            'room_cover_frame_url': f"https://liveimg.sooplive.co.kr/h/{bno}.webp",
            'live_state': 1
        })

        if is_check_status:
            return True

        try:
            aid_resp = self._channel_api(username, bno=bno, api_type="aid").get("CHANNEL", {})
            aid = aid_resp.get("AID", "")
            if not aid:
                logger.warning(f"{AfreecaTV.__name__}: {self.room_url}: AID 为空(RESULT={aid_resp.get('RESULT')})")
                return False
        except Exception as e:
            logger.warning(f"{AfreecaTV.__name__}: {self.room_url}: 获取 AID 失败: {e}")
            return False

        resp = None
        try:
            return_type = next((v for k, v in self.CDN_TYPE_MAPPING.items() if k in channel["CDN"]), channel["CDN"])
            resp = requests.get(f'{channel["RMD"]}/broad_stream_assign.html', params={
                "return_type": return_type,
                "broad_key": f'{bno}-common-{self.QUALITY}-hls'
            }, headers=self.fake_headers, timeout=5)
            view_info = resp.json()

            view_url = view_info.get("view_url", "")
            if not view_url:
                logger.warning(f"{AfreecaTV.__name__}: {self.room_url}: 播放地址为空: {view_info}")
                return False
            self.raw_stream_url = view_url + "?aid=" + aid
        except Exception as e:
            body = resp.text[:200] if resp is not None else ''
            req_url = resp.url if resp is not None else ''
            logger.warning(f"{AfreecaTV.__name__}: {self.room_url}: 获取流地址失败: {e}, 请求: {req_url}, 响应: {body}")
            return False

        return True

    def _channel_api(self, username, bno, api_type):
        return requests.post(self.CHANNEL_API_URL, data={
            "bid": username,
            "bno": bno,
            "type": api_type,
            "pwd": "",
            "player_type": "html5",
            "stream_type": "common",
            "quality": self.QUALITY,
            "mode": "landing",
            "from_api": 0,
        }, headers=self.fake_headers, timeout=5).json()


class AfreecaTVUtils:
    _cookie: Optional[Dict[str, str]] = None
    _cookie_expires = 0
    # 缓存对应的凭据，配置变更后旧缓存立即失效（对齐 biliup 的 CachedLogin 行为）
    _cookie_credentials: Optional[tuple] = None

    LOGIN_URL = "https://login.sooplive.com/app/LoginAction.php"
    COOKIE_KEYS = ("RDB", "PdboxBbs", "PdboxTicket", "PdboxSaveTicket")

    @staticmethod
    def get_cookie() -> Optional[Dict[str, str]]:
        username = config.get('afreecatv_username', '')
        password = config.get('afreecatv_password', '')
        if not username or not password:
            return None

        with NamedLock("AfreecaTV_cookie_get"):
            credentials = (username, password)
            if (AfreecaTVUtils._cookie
                    and AfreecaTVUtils._cookie_expires > time.time()
                    and AfreecaTVUtils._cookie_credentials == credentials):
                return AfreecaTVUtils._cookie

            try:
                response = requests.post(AfreecaTVUtils.LOGIN_URL, data={
                    "szUid": username,
                    "szPassword": password,
                    "szWork": "login",
                    "szType": "json",
                    "isSaveId": "true",
                    "isSavePw": "true",
                    "isSaveJoin": "true",
                    "isLoginRetain": "Y",
                }, timeout=10)
                if response.json().get("RESULT") != 1:
                    logger.warning(f"{AfreecaTVUtils.__name__}: 登录失败，请检查账号密码")
                    AfreecaTVUtils._cookie = None
                    AfreecaTVUtils._cookie_credentials = None
                    return None

                cookie_dict = response.cookies.get_dict()
                cookie = {k: cookie_dict[k] for k in AfreecaTVUtils.COOKIE_KEYS if k in cookie_dict}
                if not cookie:
                    logger.warning(f"{AfreecaTVUtils.__name__}: 登录响应缺少有效 Cookie")
                    return None

                AfreecaTVUtils._cookie = cookie
                AfreecaTVUtils._cookie_credentials = credentials
                AfreecaTVUtils._cookie_expires = time.time() + (7 * 24 * 60 * 60)
            except Exception as e:
                logger.warning(f"{AfreecaTVUtils.__name__}: 登录请求失败: {e}")
                return None

            return AfreecaTVUtils._cookie


if __name__ == '__main__':
    print(match1('https://play.sooplive.com/tildaaa/263094720', AfreecaTV.VALID_URL_BASE))
