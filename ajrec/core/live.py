import abc
import asyncio
import logging
import os
import threading
import time
import requests

from ajrec.core.event import EventManager, Event, EVENT_CHECK_STATUS, EVENT_PRE_RECORD, EVENT_RECORD, \
    EVENT_RECORD_COMPLETED
from ajrec.core.utils import random_user_agent, get_valid_filename

logger = logging.getLogger('ajrec')


class LiveBase(object):
    def __init__(self, room_name, room_url, suffix):

        self.room_name = room_name
        self.room_url = room_url
        self.room_platform = None
        self.room_id = None
        self.room_title = None
        self.room_owner_id = None
        self.room_owner = None
        self.room_owner_avatar = None
        self.room_owner_title = None
        self.room_cover_url = None
        self.room_cover_frame_url = None
        self.raw_stream_url = None
        self.live_state = None

        self.is_recording = False
        self.fake_headers = {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'accept-encoding': 'gzip, deflate',
            'accept-language': 'zh-CN,zh;q=0.8,en-US;q=0.5,en;q=0.3',
            'user-agent': random_user_agent(),
        }

        self.custom_filename = ""

        # 暂时只支持flv
        self.suffix = suffix
        self.event_manager = self.create_event_manager()
        self.record_thread = self.start_record_thread()

    @property
    def live_context(self):
        return {
            "room_name": self.room_name,
            "room_url": self.room_url,
            "room_platform": self.room_platform,
            "room_id": self.room_id,
            "room_title": self.room_title,
            "room_owner_id": self.room_owner_id,
            "room_owner": self.room_owner,
            "room_owner_avatar": self.room_owner_avatar,
            "room_owner_title": self.room_owner_title,
            "room_cover_url": self.room_cover_url,
            "room_cover_frame_url": self.room_cover_frame_url,
            "raw_stream_url": self.raw_stream_url,
            "live_state": self.live_state,
        }

    async def async_check_status(self):
        while True:
            self.send_event(Event(EVENT_CHECK_STATUS, (1,)))
            await asyncio.sleep(30)

    def start(self):
        asyncio.create_task(self.async_check_status())

    def start_record_thread(self):
        record_thread = threading.Thread(target=self.start_record)
        record_thread.start()
        return record_thread

    def start_record(self):
        date = time.localtime()
        end_time = None
        delay = 0  # int(config.get('delay', 0))
        # 重试次数
        retry_count = 0
        # delay 重试次数
        retry_count_delay = 0
        # delay 总重试次数 向上取整
        delay_all_retry_count = -(-delay // 60)

        while True:
            if not self.is_recording:
                time.sleep(2)
                continue

            logger.info(f'开始下载：{self.__class__.__name__} - {self.room_name}')
            # 流没中断，会一直录制
            ret = False
            try:
                ret = self.record()
            except Exception as e:
                logger.exception(f'Uncaught exception:{e}')
            finally:
                self.stop()

            if ret:
                # 成功下载重置重试次数
                retry_count = 0
                retry_count_delay = 0
            else:
                if retry_count < 3:
                    retry_count += 1
                    logger.info(
                        f'获取流失败：{self.__class__.__name__} - {self.room_name}，重试次数 {retry_count} / 3，等待 3 秒')
                    time.sleep(3)
                    continue

                if delay:
                    retry_count_delay += 1
                    if retry_count_delay > delay_all_retry_count:
                        logger.info(f'下播延迟检测结束：{self.__class__.__name__}:{self.room_name}')
                        break
                    else:
                        if delay < 60:
                            logger.info(
                                f'下播延迟检测：{self.__class__.__name__} - {self.room_name}，将在 {delay} 秒后检测开播状态')
                            time.sleep(delay)
                        else:
                            if retry_count_delay == 1:
                                end_time = time.localtime()
                                # 只有第一次显示
                                logger.info(
                                    f'下播延迟检测：{self.__class__.__name__} - {self.room_name}，每隔 60 秒检测开播状态，共检测 {delay_all_retry_count} 次')
                            time.sleep(60)
                        continue
                else:
                    end_time = time.localtime()
                    break

        stream_record_info = {}
        self.send_event(Event(EVENT_RECORD_COMPLETED, (stream_record_info,)))

    def stop(self):
        pass

    @abc.abstractmethod
    def check_live(self, is_check_status=False):
        raise NotImplementedError()

    def record(self):
        if not self.check_live():
            return False
        logger.info(self.live_context)
        filepath = self.get_filepath()
        self.raw_download(filepath)
        self.rename(filepath)
        return True

    def raw_download(self, filepath):
        resp = requests.get(self.raw_stream_url, stream=True, headers=self.fake_headers, timeout=60)
        with open(filepath + ".part", "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024):
                if chunk:
                    f.write(chunk)
                    f.flush()

    def ffmpeg_download(self, filepath):
        pass

    def get_filepath(self):
        file_dir = f'video/{self.room_platform}/{self.room_id}-{self.room_name}'
        if not os.path.exists(file_dir):
            os.makedirs(file_dir)

        filename = f'{self.room_name}.%Y_%m_%d_%H_%M_%S'

        if self.custom_filename:  # 判断是否存在自定义录播命名设置
            filename = (self.custom_filename.format(**self.live_context).encode(
                'unicode-escape').decode()).encode().decode("unicode-escape")
        filename = get_valid_filename(filename)

        filename = time.strftime(filename.encode("unicode-escape").decode()).encode().decode("unicode-escape")

        return f'{file_dir}/{filename}.{self.suffix}'

    def send_event(self, event):
        logger.info(f'Sending event {event}')
        self.event_manager.send(event)

    @staticmethod
    def rename(filepath):
        try:
            os.rename(filepath + '.part', filepath)
            logger.info(f'更名 {filepath + ".part"} 为 {filepath}')
        except FileNotFoundError:
            logger.debug(f'文件不存在: {filepath + ".part"}')
        except FileExistsError:
            os.rename(filepath + '.part', filepath)
            logger.info(f'更名 {filepath + ".part"} 为 {filepath} 失败, {filepath} 已存在')

    def create_event_manager(self):
        event_manager = EventManager()

        @event_manager.register(EVENT_CHECK_STATUS)
        def check_status(event):
            logger.info(self.room_name + "Checking status")

            is_living = self.check_live(is_check_status=True)
            logger.info(is_living)
            if is_living:
                self.send_event(Event(EVENT_PRE_RECORD))

        @event_manager.register(EVENT_PRE_RECORD)
        def process_pre_record():
            self.send_event(Event(EVENT_RECORD))

        @event_manager.register(EVENT_RECORD)
        def record():
            self.is_recording = True

        @event_manager.register(EVENT_RECORD_COMPLETED)
        def process_record_complete(event):
            pass

        event_manager.start()
        return event_manager
