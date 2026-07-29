import base64
import hashlib
import html
import random
import time
from enum import Enum
from typing import Any, Dict, List, Union
from urllib.parse import parse_qs, unquote

import requests

from luboman.config import config
from luboman.core.decorators import PluginTool
from luboman.core.live import LiveBase
from luboman.core.utils import match1, json_loads
from luboman.plugins import logger
from luboman.plugins.huya_wup import Wup, DEFAULT_TICKET_NUMBER
from luboman.plugins.huya_wup.packet import HuyaGetCdnTokenReq, HuyaGetCdnTokenRsp

HUYA_WEB_BASE_URL = "https://www.huya.com"
HUYA_MP_BASE_URL = "https://mp.huya.com"
HUYA_WUP_BASE_URL = "https://wup.huya.com"
HUYA_WEB_ROOM_DATA_REGEX = r"var TT_ROOM_DATA = (.*?);"

# 星秀等分区不能使用自定义构建的 anti_code
GID_BLACKLIST = [1663, ]

# 虎牙自家CDN不可用
CDN_BLACKLIST = ['HY', 'HUYA', 'HYZJ']


def _cfg_bool(key, default=False):
    """GlobalConfig 中的值均为字符串，布尔配置需要显式转换"""
    val = config.get(key, default)
    if isinstance(val, str):
        return val.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(val)


@PluginTool.live(regexp=r'(?:https?://)?(?:(?:www|m)\.)?huya\.com')
class Huya(LiveBase):
    def __init__(self, room_name, room_url, suffix='flv'):
        super().__init__(room_name, room_url, suffix)
        self.fake_headers['referer'] = room_url
        self.fake_headers['cookie'] = config.get('huya_cookie', '')
        self.huya_max_ratio = int(config.get('huya_max_ratio', 0) or 0)
        # huyacdn 为旧配置键，将于后续版本移除
        self.huya_cdn = (config.get('huya_cdn', config.get('huyacdn', '')) or '').upper()
        self.huya_protocol = 'Hls' if config.get('huya_protocol') == 'Hls' else 'Flv'
        self.huya_imgplus = _cfg_bool('huya_imgplus', True)
        self.huya_cdn_fallback = _cfg_bool('huya_cdn_fallback', False)
        self.huya_mobile_api = _cfg_bool('huya_mobile_api', False)
        self.huya_codec = config.get('huya_codec', '264')
        self.huya_use_wup = _cfg_bool('huya_use_wup', True)

    def check_live(self, is_check_status=False):
        try:
            room_id = self.room_url.split('huya.com/')[1].split('/')[0].split('?')[0]
            if not room_id:
                raise Exception('直播间地址错误')
            if not room_id.isdigit():
                room_id = self._get_real_rid()

            room_profile = self.get_room_profile(room_id, self.huya_mobile_api)
        except Exception as e:
            logger.warning(f"{self.log_prefix}: {e}")
            return False

        if not room_profile['live']:
            '''
            ON: 直播
            REPLAY: 重播
            OFF: 未开播
            '''
            logger.debug(f"{Huya.__name__} - {self.room_url}: {room_profile['message']}")
            self.raw_stream_url = None
            return False

        # 虎牙回放
        if room_profile['room_title'].startswith('【回放】'):
            logger.debug(f"{self.log_prefix}: {room_profile['room_title']}")
            return False

        self.room_data.update({
            'room_id': room_id,
            'room_platform': self.__class__.__name__,
            'room_title': room_profile['room_title'],
            'room_cover_url': room_profile['room_cover'],
            'room_cover_frame_url': room_profile['room_cover'],
            'room_owner': room_profile['artist'],
            'room_owner_id': room_profile['uid'],
            'room_owner_avatar': room_profile['artist_img'],
            'live_state': 1
        })

        if is_check_status:
            return True

        skip_query_build = room_profile['gid'] in GID_BLACKLIST
        try:
            stream_urls = self.build_stream_urls(room_profile['streams_info'], skip_query_build)
        except Exception as e:
            logger.exception(f"{self.log_prefix}: 没有可用的链接:{e}")
            return False

        cdn_list = list(stream_urls.keys())
        if not cdn_list:
            logger.error(f"{self.log_prefix}: 没有可用的CDN")
            return False

        perf_cdn = self.huya_cdn
        if not perf_cdn or perf_cdn not in cdn_list:
            logger.warning(f"{Huya.__name__}: {self.room_url}: 使用 {cdn_list[0]}")
            perf_cdn = cdn_list[0]

        self.raw_stream_url = self.add_ratio(
            stream_urls[perf_cdn],
            room_profile['bitrate_info'],
            room_profile['max_bitrate']
        )

        # HTTPS的直播流只允许连接一次
        if self.huya_cdn_fallback:
            if not self._check_url_healthy(self.raw_stream_url):
                logger.info(f"{self.log_prefix}: cdn_fallback 顺序尝试 {cdn_list}")
                for cdn in cdn_list:
                    if cdn == perf_cdn:
                        continue
                    logger.info(f"{self.log_prefix}: cdn_fallback-{cdn}")
                    if not self._check_url_healthy(stream_urls[cdn]):
                        continue
                    perf_cdn = cdn
                    logger.info(f"{self.log_prefix}: cdn_fallback 回退到 {perf_cdn}")
                    break
                else:
                    logger.error(f"{self.log_prefix}: cdn_fallback 所有链接无法使用")
                    return False
                # 健康检查消耗了唯一连接，重新获取流地址
                try:
                    room_profile = self.get_room_profile(room_id, self.huya_mobile_api)
                except Exception as e:
                    logger.warning(f"{self.log_prefix}: {e}")
                    return False
                if not room_profile['live']:
                    logger.debug(f"{self.log_prefix}: {room_profile['message']}")
                    return False
                stream_urls = self.build_stream_urls(room_profile['streams_info'], skip_query_build)
                self.raw_stream_url = self.add_ratio(
                    stream_urls[perf_cdn],
                    room_profile['bitrate_info'],
                    room_profile['max_bitrate']
                )
        return True

    def add_ratio(self, url: str, bitrate_info: List[Dict[str, Any]], max_bitrate: int) -> str:
        '''
        添加码率
        :param url: 流地址
        :param bitrate_info: 可选择的码率信息
        :param max_bitrate: 最大码率(不含hdr)
        :return: 添加码率后的流地址
        '''
        if self.huya_max_ratio and "&ratio" not in url:
            def __get_ratio(info: Dict[str, Any]) -> int:
                return info.get('iBitRate', 0) or max_bitrate
            try:
                selected_ratio = 0
                # 符合条件的码率
                allowed_ratio_list = [
                    __get_ratio(x)
                    for x in bitrate_info
                    if __get_ratio(x) <= self.huya_max_ratio
                ]
                # 录制码率
                if allowed_ratio_list:
                    selected_ratio = max(allowed_ratio_list)
                if selected_ratio:
                    return f"{url}&ratio={selected_ratio}"
            except (KeyError, TypeError) as e:
                logger.error(f"{self.log_prefix}: 在确定码率时发生错误 {e}")
        return url

    def get_stream_name(self, stream_name: str) -> str:
        if self.huya_imgplus:
            return stream_name
        return stream_name.replace('-imgplus', '')

    def build_stream_urls(self, streams_info: List[Dict[str, Any]], skip_query_build: bool) -> Dict[str, str]:
        '''
        构建流地址
        :param streams_info: 流信息
        :param skip_query_build: 跳过构建anti_code
        :return: 按CDN优先级排序的流地址
        '''
        proto = self.huya_protocol
        streams = {}
        weights = {}  # https://cdnweb.huya.com/getUidsDomainList?anchor_uid={anchor_uid}
        for stream in streams_info:
            # 优先级<0代表不可用
            priority = stream.get('iWebPriorityRate', 0)
            if priority < 0:
                continue
            stream_name = self.get_stream_name(stream['sStreamName'])
            cdn = stream['sCdnType']
            suffix = stream[f's{proto}UrlSuffix']
            # 默认不修改 anticode
            anti_code = stream[f's{proto}AntiCode']
            presenter_uid = self.get_uid(stream.get('lPresenterUid'))
            if (
                # 禁用 imgplus
                not self.huya_imgplus
                or
                # 禁用 wup，流信息不来自移动端，不在分区黑名单中
                not (self.huya_use_wup or self.huya_mobile_api or skip_query_build)
            ):
                logger.debug(f"{self.log_prefix}: 构建 anticode")
                anti_code = self.build_query(stream_name, anti_code, presenter_uid)
            # 启用 imgplus、wup 且非 mobile api
            elif self.huya_use_wup and not self.huya_mobile_api:
                # 使用 Wup 获取的 anti_code，必须使用 Wup UA 进行连接
                anti_code = self.get_true_anticode(cdn, stream_name, presenter_uid, proto)
            anti_code = f"{anti_code}&codec={self.huya_codec}"
            base_url = stream[f's{proto}Url'].replace('http://', 'https://')  # 强制https
            streams[cdn] = f"{base_url}/{stream_name}.{suffix}?{anti_code}"
            weights[cdn] = priority
        return self.__weight_sorting(streams, weights)

    def extract_room_profile(self, data: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
        '''
        ON: 直播
        REPLAY: 重播
        OFF: 未开播
        '''
        # PC web
        if isinstance(data, str):
            room_data = json_loads(match1(data, HUYA_WEB_ROOM_DATA_REGEX) or '')
            s = data.split('stream: ')[1].split('};')[0]
            s_json = json_loads(s)
            bitrate_info = s_json.get('vMultiStreamInfo')
            if not room_data or room_data.get('state') != 'ON' or not bitrate_info:
                return {
                    'live': False,
                    'message': '未开播' if not room_data or room_data.get('state') != 'ON' else '未推流',
                }
            live_info = s_json['data'][0]['gameLiveInfo']
            streams_info = s_json['data'][0]['gameStreamInfoList']
        # Mobile API（微信小程序）
        elif isinstance(data, dict):
            data = data['data']
            if data['liveStatus'] != 'ON' or not data.get('liveData', {}).get('bitRateInfo'):
                return {
                    'live': False,
                    'message': '未开播' if data['liveStatus'] != 'ON' else '未推流',
                }
            live_info = data['liveData']
            bitrate_info = json_loads(live_info['bitRateInfo'])
            streams_info = data['stream']['baseSteamInfoList']
        return {
            'artist': live_info['nick'],
            'artist_img': live_info['avatar180'].replace('http://', 'https://'),
            'bitrate_info': bitrate_info,
            'gid': live_info['gid'],
            'live': True,
            'live_start_time': live_info['startTime'],
            'max_bitrate': live_info['bitRate'],
            'room_cover': live_info['screenshot'].replace('http://', 'https://'),
            'room_title': live_info['introduction'],
            'streams_info': streams_info,
            'uid': live_info.get('uid', ''),
        }

    def get_room_profile(self, room_id, use_api=False) -> dict:
        '''
        获取房间信息
        :param use_api: 是否使用API
        :return: 房间信息
        '''
        # 使用Session避免连接泄漏
        with requests.Session() as session:
            session.headers.update(self.fake_headers)
            if use_api:
                params = {
                    'm': 'Live',
                    'do': 'profileRoom',
                    'roomid': room_id,
                    'showSecret': 1,
                }
                resp = session.get(f"{HUYA_MP_BASE_URL}/cache.php", params=params, timeout=10)
                resp.raise_for_status()
                resp_json = json_loads(html.unescape(resp.text))
                if resp_json.get('status') != 200:
                    raise Exception(f"{resp_json.get('message')}")
                return self.extract_room_profile(resp_json)
            else:
                resp = session.get(f"{HUYA_WEB_BASE_URL}/{room_id}", timeout=10)
                resp.raise_for_status()
                text = html.unescape(resp.text)
                _raise_for_room_block(text)
                return self.extract_room_profile(text)

    def get_true_anticode(self, cdn: str, stream_name: str, presenter_uid: int, proto: str) -> str:
        '''
        获取 wup anti_code
        :param cdn: cdn类型
        :param stream_name: 流名称
        :param presenter_uid: 主播uid
        :param proto: 协议类型
        :return: wup anti_code
        '''
        proto = "hls" if proto == "Hls" else "flv"
        headers = dict(self.fake_headers)
        if self.huya_use_wup:
            headers['user-agent'] = UAGenerator.build_user_agent(UAType.HYSDK, Platform.WINDOWS)
            headers['origin'] = HUYA_WEB_BASE_URL
        wup_req = Wup()
        wup_req.requestid = abs(DEFAULT_TICKET_NUMBER)
        wup_req.servant = "liveui"
        wup_req.func = "getCdnTokenInfo"
        token_info_req = HuyaGetCdnTokenReq()
        token_info_req.cdnType = cdn
        token_info_req.streamName = stream_name
        token_info_req.presenterUid = presenter_uid
        wup_req.put(HuyaGetCdnTokenReq, "tReq", token_info_req)
        data = wup_req.encode_v3()
        rsp = requests.post(HUYA_WUP_BASE_URL, data=data, headers=headers, timeout=10)
        rsp.raise_for_status()
        wup_rsp = Wup()
        wup_rsp.decode_v3(rsp.content)
        token_info_rsp = wup_rsp.get(HuyaGetCdnTokenRsp, "tRsp")
        token_info = token_info_rsp.as_dict()
        logger.debug(f"{self.log_prefix}: wup token_info {token_info}")
        return token_info[f'{proto}AntiCode']

    @staticmethod
    def build_query(stream_name: str, anti_code: str, uid: int) -> str:
        '''
        构建anti_code
        :param stream_name: 流名称
        :param anti_code: 原始anti_code
        :param uid: 主播uid
        :return: 构建后的anti_code
        '''
        url_query = parse_qs(anti_code)
        platform_id = url_query.get('t', [100])[0]
        ws_time = url_query['wsTime'][0]
        convert_uid = (uid << 8 | uid >> (32 - 8)) & 0xFFFFFFFF
        seq_id = uid + int(time.time() * 1000)
        ctype = url_query['ctype'][0]
        fm = unquote(url_query['fm'][0])
        ct = int((int(ws_time, 16) + random.random()) * 1000)
        ws_secret_prefix = base64.b64decode(fm.encode()).decode().split('_')[0]
        ws_secret_hash = hashlib.md5(f"{seq_id}|{ctype}|{platform_id}".encode()).hexdigest()
        secret_str = f'{ws_secret_prefix}_{convert_uid}_{stream_name}_{ws_secret_hash}_{ws_time}'
        ws_secret = hashlib.md5(secret_str.encode()).hexdigest()

        # &codec=av1
        # &codec=264
        # &codec=265
        # dMod: wcs-25 / mesh-0 DecodeMod-SupportMod
        # sdkPcdn: 1_1 第一个1连接次数 第二个1是因为什么连接
        # t: 平台信息 100 web(ctype=huya_live/huya_webh5) 102 小程序(ctype=tars_mp)
        # PLATFORM_TYPE = {'adr': 2, 'huya_liveshareh5': 104, 'ios': 3, 'mini_app': 102, 'wap': 103, 'web': 100}
        # sv: 2401090219 版本
        # sdk_sid:  _sessionId sdkInRoomTs 当前毫秒时间
        anti_code = {
            "wsSecret": ws_secret,
            "wsTime": ws_time,
            "seqid": str(seq_id),
            "ctype": ctype,
            "ver": "1",
            "fs": url_query['fs'][0],
            "t": platform_id,
            "u": convert_uid,
            "uuid": str(int((ct % 1e10 + random.random()) * 1e3 % 0xffffffff)),
            "sdk_sid": str(int(time.time() * 1000)),
        }
        return '&'.join([f"{k}={v}" for k, v in anti_code.items()])

    def _check_url_healthy(self, url: str) -> bool:
        try:
            with requests.get(url, headers=self.fake_headers, stream=True, timeout=5) as resp:
                return resp.status_code == 200
        except Exception:
            return False

    def _get_real_rid(self) -> str:
        with requests.Session() as session:
            session.headers.update(self.fake_headers)
            resp = session.get(self.room_url, timeout=10)
            resp.raise_for_status()
            _raise_for_room_block(resp.text)
            room_data = match1(resp.text, HUYA_WEB_ROOM_DATA_REGEX)
            room_data = json_loads(room_data or '')
            if not room_data.get('profileRoom'):
                raise Exception("找不到这个主播")
            return str(room_data['profileRoom'])

    @staticmethod
    def __weight_sorting(data: Dict[str, Any], weights: Dict[str, Any]) -> Dict[str, Any]:
        if data:
            data = {k: v for k, v in data.items() if k not in CDN_BLACKLIST}
            return dict(sorted(data.items(), key=lambda x: weights[x[0]], reverse=True))
        return {}

    @staticmethod
    def get_uid(uid: Union[str, int, None] = None) -> int:
        try:
            if isinstance(uid, str):
                uid = int(uid)
        except ValueError:
            pass
        return uid if isinstance(uid, int) and uid else random.randint(1400000000000, 1499999999999)


class UAType(Enum):
    MEDIA_PLAYER = 'media_player'
    HYSDK = 'hysdk'


class Platform(Enum):
    ANDROID = 'adr'
    HUYA_NFTV = 'huya_nftv'
    WEBSOCKET = 'webh5'
    WINDOWS = 'pc_exe'


class UAGenerator:
    # 配置字典
    HYAPP_CONFIGS = {
        Platform.ANDROID: {
            'platform': Platform.ANDROID,
            'version': '0.0.0',  # LocalVersion or "0.0.0" + hotfix_version
            'channel': 'live'
        },
        Platform.HUYA_NFTV: {
            'platform': Platform.HUYA_NFTV,
            'version': '2.5.1.3141',
            'channel': 'official'
        },
        Platform.WINDOWS: {
            'platform': Platform.WINDOWS,
            'version': '6100301',
            'channel': 'official'
        },
        Platform.WEBSOCKET: {  # UnUsed
            'platform': Platform.WEBSOCKET,
            'version': '2505091506',
            'channel': 'websocket'
        }
    }

    HYSDK_CONFIGS = {
        Platform.ANDROID: {
            'platform': 'Android',
            'version': '30000002'
        },
        Platform.WINDOWS: {
            'platform': 'Windows',
            'version': '30000002'
        }
    }

    TRANS_MOD_CONFIGS = {
        Platform.HUYA_NFTV: {
            'name': 'trans',
            'version': '1.24.99-rel-tv'
        },
        Platform.ANDROID: {
            'name': 'trans',
            'version': '2.22.13-rel'
        },
        Platform.WINDOWS: {
            'name': 'trans',
            'version': '2.24.0.5157'
        }
    }

    @staticmethod
    def get_hyapp_ua(platform: Platform = Platform.WINDOWS) -> str:
        '''生成 hyapp 用户代理字符串'''
        cfg = UAGenerator.HYAPP_CONFIGS.get(platform)
        if not cfg:
            raise ValueError(f"不支持的平台: {platform}")

        ua = f"{cfg['platform']}&{cfg['version']}&{cfg['channel']}"
        # windows 和 websocket 不需要添加 android_api_level
        if platform not in {Platform.WINDOWS, Platform.WEBSOCKET}:
            android_api_level = random.randint(28, 35)
            ua = f"{ua}&{android_api_level}"

        return ua

    @staticmethod
    def get_hysdk_ua(platform: Platform = Platform.WINDOWS) -> str:
        '''生成 hysdk 用户代理字符串'''
        cfg = UAGenerator.HYSDK_CONFIGS.get(platform)
        if not cfg:
            raise ValueError(f"HYSDK 不支持的平台: {platform}")
        return f"HYSDK({cfg['platform']}, {cfg['version']})"

    @staticmethod
    def get_hy_media_player_ua(platform: Platform = Platform.WINDOWS) -> str:
        '''生成 hy_media_player 用户代理字符串（目前只支持 android 平台）'''
        return f"android, 20000313"

    @staticmethod
    def get_hy_trans_mod_ua(platform: Platform = Platform.WINDOWS) -> str:
        '''生成 hy_trans_mod 用户代理字符串'''
        cfg = UAGenerator.TRANS_MOD_CONFIGS.get(platform)
        if not cfg:
            raise ValueError(f"Trans mod 不支持的平台: {platform}")
        return f"{cfg['name']}&{cfg['version']}"

    @staticmethod
    def build_user_agent(ua_type: UAType = UAType.HYSDK, platform: Platform = Platform.WINDOWS) -> str:
        '''构建完整的用户代理字符串'''
        hyapp_ua = UAGenerator.get_hyapp_ua(platform)
        trans_mod_ua = UAGenerator.get_hy_trans_mod_ua(platform)

        if ua_type == UAType.MEDIA_PLAYER:
            media_player_ua = UAGenerator.get_hy_media_player_ua(platform)
            return f"{media_player_ua}_APP({hyapp_ua})_SDK({trans_mod_ua})"
        elif ua_type == UAType.HYSDK:
            sdk_platform = platform if platform in {Platform.ANDROID, Platform.HUYA_NFTV} else Platform.WINDOWS
            hysdk_ua = UAGenerator.get_hysdk_ua(sdk_platform)
            return f"{hysdk_ua}_APP({hyapp_ua})_SDK({trans_mod_ua})"
        else:
            raise ValueError(f"不支持的 UA 类型: {ua_type}")


def _raise_for_room_block(text: str):
    for err_key in ("找不到这个主播", "该主播涉嫌违规，正在整改中"):
        if err_key in text:
            raise Exception(err_key)


if __name__ == '__main__':
    h = Huya('test', 'https://www.huya.com/52399')
    print(h.check_live())
    print(h.raw_stream_url)
