import abc
import asyncio
import datetime
import logging
import os
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

import requests
from playhouse.shortcuts import model_to_dict

from luboman.config import config
from luboman.core.decorators import PluginTool
from luboman.core.event import EventManager, Event, EventType
from luboman.core.utils import random_user_agent, get_valid_filename
from luboman.core.upload import upload
from luboman.database.db import DB
from luboman.database.models import RecordFile, BiliUploadTemplate, BiliAccount

logger = logging.getLogger('luboman')


class LiveBase(object):
    def __init__(self, room_name, room_url, suffix):

        self.room_name = room_name
        self.room_url = room_url
        self.room_platform = None
        self.room_data = None

        self.raw_stream_url = None
        self.is_living = False
        self.is_recording = False

        self.suffix = suffix.lower()
        self.event_manager = self.create_event_manager()
        self.record_thread = self.start_record_thread()

        self.default_ffmpeg_options = {
            '-bsf:a': 'aac_adtstoasc',
            '-loglevel': 'error'
        }

        self.fake_headers = {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'accept-encoding': 'gzip, deflate',
            'accept-language': 'zh-CN,zh;q=0.8,en-US;q=0.5,en;q=0.3',
            'user-agent': random_user_agent(),
        }

    @property
    def ffmpeg_opt_args(self):
        options = self.default_ffmpeg_options.copy()
        global_options = {}
        if config.get('segment_file_size'):
            global_options['-fs'] = f"{config.get('segment_file_size')}"
        else:
            global_options['-to'] = f"{config.get('segment_duration', '01:00:00')}"
        options.update(global_options)

        if self.room_data.get('ffmpeg_options') and isinstance(self.room_data.get('ffmpeg_options'), dict):
            options.update(self.room_data.get('ffmpeg_options'))

        option_args = []
        for k, v in options.items():
            option_args += (str(k), str(v))
        return option_args

    async def async_check_status(self):
        while True:
            self.send_event(Event(EventType.EVENT_CHECK_STATUS, (1,)))
            await asyncio.sleep(30)

    def start(self):
        logger.info(f'开始直播间任务：{self.__class__.__name__} - {self.room_name}')
        logger.info(self.room_data)
        asyncio.create_task(self.async_check_status())

    def start_record_thread(self):
        record_thread = threading.Thread(target=self.start_record)
        record_thread.start()
        return record_thread

    def start_record(self):
        delay = int(config.get('live_offline_judge_delay', 60))
        # 重试次数
        retry_count = 0
        # delay 重试次数
        retry_count_delay = 0
        # delay 总重试次数 向上取整
        delay_all_retry_count = -(-delay // 60)
        is_offline = False
        record_file_list = []

        logger.info(f'启动录制线程：{self.__class__.__name__} - {self.room_name}')

        while True:
            # 未启动录制
            if not self.is_recording:
                time.sleep(3)
                continue

            recording_context = {
                "begin_time": datetime.datetime.now(),
            }

            logger.info(f'开始录制：{self.__class__.__name__} - {self.room_name}')

            ret = False
            filepath = None

            try:
                # 阻塞下载，流没中断，会一直录制
                ret, filepath = self.record()
            except Exception as e:
                logger.exception(f'Uncaught exception:{e}')
            finally:
                self.stop()

            if ret:
                # 成功下载重置重试次数
                retry_count = 0
                retry_count_delay = 0
                is_offline = False

                recording_context["end_time"] = datetime.datetime.now(),
                recording_context["video"] = filepath
                record_file_list.append(recording_context)
                self.send_event(Event(EventType.EVENT_UPLOAD, ([recording_context],)))

                recording_context['live_room_id'] = self.room_data.get('id')

                # 数据库记录
                try:
                    RecordFile.add(**recording_context)
                except Exception as e:
                    logger.exception(f'Uncaught exception:{e}')

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
                        is_offline = True
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
                    is_offline = True

                if is_offline:
                    self.send_event(Event(EventType.EVENT_RECORD_COMPLETED, (record_file_list,)))
                    record_file_list = []

    def stop(self):
        logger.warning(f'停止录制：{self.__class__.__name__} - {self.room_name}')

    @abc.abstractmethod
    def check_live(self, is_check_status=False):
        raise NotImplementedError()

    def record(self):
        if not self.check_live():
            return False, None
        filepath = self.get_filepath()
        self.ffmpeg_download(filepath)
        self.rename(filepath)

        return True, filepath

    def raw_download(self, filepath):
        resp = requests.get(self.raw_stream_url, stream=True, headers=self.fake_headers, timeout=60)
        with open(filepath + ".part", "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024):
                if chunk:
                    f.write(chunk)
                    f.flush()

    def ffmpeg_download(self, filepath):
        ffmpeg_path = config.get('ffmpeg_path', 'ffmpeg')
        default_input_args = ['-headers', ''.join('%s: %s\r\n' % x for x in self.fake_headers.items()), '-rw_timeout',
                              '20000000']
        parsed_url = urlparse(self.raw_stream_url)
        path = parsed_url.path
        if '.m3u8' in path:
            default_input_args += ['-max_reload', '1000']
        command_args = [ffmpeg_path, '-y', *default_input_args,
                        '-i', self.raw_stream_url, *self.ffmpeg_opt_args,
                        '-c', 'copy', '-f', self.suffix]

        command_args += [f'{filepath}.part']

        proc = subprocess.Popen(command_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            with proc.stdout as stdout:
                for line in iter(stdout.readline, b''):  # b'\n'-separated lines
                    decode_line = line.decode(errors='ignore')
                    logger.debug(decode_line.rstrip())
            retval = proc.wait()
        except KeyboardInterrupt:
            if sys.platform != 'win32':
                proc.communicate(b'q')
            raise
        if retval != 0:
            return False
        return True

    def get_filepath(self):
        video_dir = '/data/video' if os.path.exists('/.dockerenv') else 'data/video'
        file_dir = f'{video_dir}/{self.room_platform}/{self.room_data.get("room_id")}-{self.room_name}'
        if not os.path.exists(file_dir):
            os.makedirs(file_dir)

        filename = f'{self.room_name}.%Y_%m_%d_%H_%M_%S'

        if self.room_data.get('custom_filename'):  # 判断是否存在自定义录播命名设置
            custom_filename = self.room_data.get('custom_filename')
            filename = (custom_filename.format(**self.room_data).encode(
                'unicode-escape').decode()).encode().decode("unicode-escape")
        filename = get_valid_filename(filename)

        filename = time.strftime(filename.encode("unicode-escape").decode()).encode().decode("unicode-escape")

        return f'{file_dir}/{filename}.{self.suffix}'

    def send_event(self, event):
        if event.type_ != EventType.EVENT_CHECK_STATUS:
            logger.info(f'{self.__class__.__name__} - {self.room_name} 发送事件: {event}')

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

        @event_manager.register(EventType.EVENT_CHECK_STATUS, "NORMAL")
        def check_status(event):
            logger.debug(self.room_name + ": Checking status")
            last_living = self.is_living
            self.is_living = self.check_live(is_check_status=True)
            self.room_data['live_state'] = 1 if self.is_living else 0

            # 状态改变记录
            if last_living != self.is_living:
                logger.info(self.room_name + ": living: " + str(self.is_living) + " last_living: " + str(last_living))

                # 开播通知
                if self.is_living:
                    self.send_event(Event(EventType.EVENT_NOTIFY, (f'开播通知:{self.room_name}',
                                                                   f'### {self.room_name}[{self.room_data.get("room_id")}]开播了\n\n{self.room_data.get("room_title")}\n\n{self.room_url}')))

            # 启动录制
            if self.is_living and not self.is_recording:
                self.send_event(Event(EventType.EVENT_PRE_RECORD))

            # 更新数据库信息
            DB.update_live_room_operation_data(self.room_data)

        @event_manager.register(EventType.EVENT_REFRESH_ROOM_INFO)
        def refresh_room_info(room_info):
            self.room_data.update(room_info)
            logger.info(f'Room data updated:{self.room_data}')

        @event_manager.register(EventType.EVENT_PRE_RECORD)
        def process_pre_record():
            self.send_event(Event(EventType.EVENT_RECORD))

        @event_manager.register(EventType.EVENT_RECORD)
        def process_record():
            self.is_recording = True

        @event_manager.register(EventType.EVENT_RECORD_COMPLETED)
        def process_record_completed(file_list):

            # 未开播状态
            self.is_recording = False
            self.is_living = False

            logger.info(file_list)

            self.send_event(Event(EventType.EVENT_UPLOAD_BILI, (file_list,)))

        @event_manager.register(EventType.EVENT_NOTIFY)
        def process_notify(title, content):
            from luboman.notifier import notify_message
            notify_message(title, content)

        @event_manager.register(EventType.EVENT_UPLOAD_BILI, "SLOW")
        def process_upload_bili(file_list):
            bili_upload_template_id = self.room_data.get('bili_upload_template_id')
            if bili_upload_template_id is None:
                logger.error(f"bili_upload_template_id is None")
                return

            template_info = BiliUploadTemplate.get_by_id_(bili_upload_template_id)
            if not template_info:
                logger.error(f"bili_upload_template_id: {bili_upload_template_id} not found")
                return

            if template_info.bili_account_id is None:
                logger.error(f"bili_account_id is None")
                return

            bili_account = BiliAccount.get_by_id(template_info.bili_account_id)
            if not bili_account:
                logger.error(f"bili_account_id: {template_info.bili_account_id} not found")
                return

            template_info = model_to_dict(template_info)
            template_info['bili_account'] = model_to_dict(bili_account)

            room_data = self.room_data.copy()
            room_data['bili_upload_template'] = template_info
            upload_info = {
                'room_data': room_data
            }

            upload('biliweb', file_list, **upload_info)

        @event_manager.register(EventType.EVENT_UPLOAD_BILI_COMPLETED)
        def process_upload_bili_completed(file_list):
            pass

        @event_manager.register(EventType.EVENT_UPLOAD, "SLOW")
        def process_upload(file_list):
            try:
                upload_platform = self.room_data.get('upload_storage_platform', 'bdpan')
                upload(upload_platform, file_list)
            except Exception as e:
                logger.exception(f'{self.__class__.__name__} - {self.room_name} | 上传失败: {e}')

        @event_manager.register(EventType.EVENT_UPLOAD_COMPLETED)
        def process_upload_completed(file_list, platform='alipan'):
            pass

        event_manager.start()
        return event_manager


def start_room(room_data, **kwargs):
    room_name = room_data.get('room_name')
    url = room_data.get('room_url')
    room_id = room_data.get('id')

    kwargs.update({'room_data': room_data})

    pg = None

    for plugin in PluginTool.live_plugins:
        if re.match(plugin.VALID_URL_BASE, url):
            pg = plugin(room_name, url)
            for k in pg.__dict__:
                if kwargs.get(k):
                    pg.__dict__[k] = kwargs.get(k)

            PluginTool.running_plugins[str(room_id)] = pg
            break

    if pg:
        return pg.start()
