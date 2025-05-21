import abc
import asyncio
import copy
import datetime
import gc
import json
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
from luboman.core.upload import upload
from luboman.core.utils import random_user_agent, get_valid_filename, get_video_dir, rename, get_public_dir, download_file
from luboman.database.db import DB
from luboman.database.models import RecordFile, BiliUploadTemplate, BiliAccount

logger = logging.getLogger('luboman')


class LiveBase(object):
    def __init__(self, room_name, room_url, suffix):

        self.room_name = room_name
        self.room_url = room_url
        self.room_data = {}
        self.log_prefix = f"<<<<< {self.__class__.__name__} - {self.room_name} - {self.room_url} >>>>>"
        self.raw_stream_url = None
        self.is_living = False
        self.living_time = 0
        self.is_recording = False
        self._active = True
        self.suffix = suffix.lower()
        self.event_manager = self.create_event_manager()
        self.record_thread = self.start_record_thread()

        self.default_ffmpeg_options = {
            '-bsf:a': 'aac_adtstoasc',
            # '-reconnect': '1',
            # '-reconnect_streamed': '1',
            # '-reconnect_delay_max': '5'
            # '-loglevel': 'error'
        }

        self.fake_headers = {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'accept-encoding': 'gzip, deflate',
            'accept-language': 'zh-CN,zh;q=0.8,en-US;q=0.5,en;q=0.3',
            'user-agent': random_user_agent(),
        }

    def __del__(self):
        logger.warning(f'{self.log_prefix} | 销毁')
        try:
            # Clean up any remaining resources
            if hasattr(self, 'record_thread') and self.record_thread and self.record_thread.is_alive():
                self._active = False
                self.record_thread.join(timeout=5)

            # Force garbage collection
            gc.collect()
        except:
            pass

    @property
    def ffmpeg_opt_args(self):
        options = self.default_ffmpeg_options.copy()
        global_options = {}

        if config.get('segment_file_size'):
            file_size = config.get('segment_file_size')
            file_size = int(file_size) * 1024 * 1024
            global_options['-fs'] = f"{file_size}"
        else:
            global_options['-to'] = f"{config.get('segment_duration', '01:00:00')}"
        options.update(global_options)

        if self.room_data.get('ffmpeg_options'):
            if isinstance(self.room_data.get('ffmpeg_options'), dict):
                options.update(self.room_data.get('ffmpeg_options'))
            elif isinstance(self.room_data.get('ffmpeg_options'), str):
                try:
                    op = json.loads(self.room_data.get('ffmpeg_options'))
                    options.update(op)
                except:
                    pass

        option_args = []
        for k, v in options.items():
            option_args += (str(k), str(v))
        return option_args

    async def async_check_status(self):
        seq = 1
        while self._active:
            self.send_event(Event(EventType.EVENT_CHECK_STATUS, (1,)))

            # 每10次更新一次数据库
            if seq % 10 == 0:
                self.send_event(Event(EventType.EVENT_UPDATE_DB_ROOM_DATA))

            seq += 1
            await asyncio.sleep(30)

    def start(self):
        logger.info(f'{self.log_prefix} : 开启直播间')
        logger.info(self.room_data)
        asyncio.create_task(self.async_check_status())

    def stop(self):
        logger.warning(f'{self.log_prefix} :  停止直播间')
        self._active = False
        self.record_thread.join()
        self.event_manager.stop()

    def stopped(self):
        logger.warning(f'{self.log_prefix} : 直播间已停止')

    def start_record_thread(self):
        record_thread = threading.Thread(target=self.record_func)
        record_thread.start()
        return record_thread

    def record_func(self):
        delay = int(config.get('live_offline_judge_delay', 60))
        # 重试次数
        retry_count = 0
        # delay 重试次数
        retry_count_delay = 0
        # delay 总重试次数 向上取整
        delay_all_retry_count = -(-delay // 60)
        is_offline = False
        record_file_list = []

        logger.info(f'{self.log_prefix} :  启动录制线程')

        while self._active:
            # 未启动录制
            if not self.is_recording:
                time.sleep(3)
                continue

            recording_context = {
                "begin_time": datetime.datetime.now(),
            }

            ret = False
            filepath = None

            try:
                # 阻塞下载，流没中断，会一直录制
                ret, filepath = self.record()
            except Exception as e:
                logger.exception(f'{self.log_prefix} :  Uncaught exception:{e}')
            finally:
                self.stopped()

            if ret:
                # 成功下载重置重试次数
                retry_count = 0
                retry_count_delay = 0
                is_offline = False

                recording_context["end_time"] = datetime.datetime.now()
                recording_context["video"] = filepath
                record_file_list.append(recording_context)
                self.send_event(Event(EventType.EVENT_UPLOAD, ([recording_context],)))

                recording_context['live_room_id'] = self.room_data.get('id')

                # 数据库记录
                try:
                    logger.info(recording_context)
                    RecordFile.create_(**recording_context)
                except Exception as e:
                    logger.exception(f'{self.log_prefix} :  | Uncaught exception:{e}')

                try:
                    ## 录像最后一个时间减去第一个起始时间大于24小时
                    first_file = record_file_list[0]
                    if (recording_context["end_time"] - first_file["begin_time"]).seconds > 86400:
                        self.send_event(Event(EventType.EVENT_RECORD_COMPLETED, (record_file_list,)))
                        record_file_list = []
                except Exception as ex:
                    logger.exception(f'{self.log_prefix} :  Uncaught exception:{ex}')

            else:
                if retry_count < 3:
                    retry_count += 1
                    logger.info(f'{self.log_prefix} :  获取流失败：重试次数 {retry_count} / 3，等待 3 秒')
                    time.sleep(3)
                    continue

                if delay:
                    retry_count_delay += 1
                    if retry_count_delay > delay_all_retry_count:
                        logger.info(f'{self.log_prefix} :  下播延迟检测结束')
                        is_offline = True
                    else:
                        if delay < 60:
                            logger.info(f'{self.log_prefix} :  下播延迟检测，将在 {delay} 秒后检测开播状态')
                            time.sleep(delay)
                        else:
                            if retry_count_delay == 1:
                                # 只有第一次显示
                                logger.info(f'{self.log_prefix} :  下播延迟检测，每隔 60 秒检测开播状态，共检测 {delay_all_retry_count} 次')
                            time.sleep(60)
                        continue
                else:
                    is_offline = True

                if is_offline:
                    self.send_event(Event(EventType.EVENT_RECORD_COMPLETED, (record_file_list,)))
                    record_file_list = []

    @abc.abstractmethod
    def check_live(self, is_check_status=False):
        raise NotImplementedError()

    def record(self):
        if not self.check_live():
            return False, None

        logger.info(f'{self.log_prefix} :  开始新的录制:{self.raw_stream_url}')
        filepath = self.get_filepath()
        self.ffmpeg_download(filepath)
        rename(filepath)

        logger.info(f'{self.log_prefix} :  片段录制结束: {filepath}')
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

        proc = None
        try:
            proc = subprocess.Popen(command_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            with proc.stdout as stdout:
                for line in iter(stdout.readline, b''):  # b'\n'-separated lines
                    decode_line = line.decode(errors='ignore')
                    logger.debug(decode_line.rstrip())
            retval = proc.wait()
            return retval == 0
        except KeyboardInterrupt:
            if sys.platform != 'win32':
                proc.communicate(b'q')
            raise
        finally:
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)  # Wait for termination with timeout
                    proc.kill()  # Force kill if still running
                except:
                    pass

                # Close any open file descriptors
                if proc.stdin:
                    proc.stdin.close()
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()

                # Force garbage collection
            gc.collect()


    def get_filepath(self):
        video_dir = get_video_dir()
        day = time.strftime('%Y-%m-%d', time.localtime(time.time()))

        file_dir = f'{video_dir}/{self.room_data.get("room_platform", "other")}/{self.room_data.get("room_id")}-{self.room_name}/{day}'
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
            logger.debug(f'{self.log_prefix} :  发送事件: {event}')

        if event.args:
            # deepcopy
            event.args = copy.deepcopy(event.args)

        self.event_manager.send(event)

    def create_event_manager(self):
        event_manager = EventManager()

        @event_manager.register(EventType.EVENT_CHECK_STATUS, "NORMAL")
        def check_status(event):
            logger.debug(f'{self.log_prefix} :  检查直播状态')
            last_living = self.is_living
            last_living_time = self.living_time
            self.is_living = self.check_live(is_check_status=True)
            self.room_data['live_state'] = 1 if self.is_living else 0
            if self.is_living:
                self.living_time = int(time.time() * 1000)
                self.room_data['last_living_time'] = datetime.datetime.now()
                self.send_event(Event(EventType.EVENT_DOWNLOAD_ASSET))

            # 状态改变记录
            if last_living != self.is_living:
                logger.info(f'{self.log_prefix} :  living: ' + str(self.is_living) + " last_living: " + str(last_living))

                self.send_event(Event(EventType.EVENT_UPDATE_DB_ROOM_DATA))

                # 开播通知
                if self.is_living and self.living_time - last_living_time > 60000:
                    self.send_event(Event(EventType.EVENT_NOTIFY, (f'开播通知:{self.room_name}',
                                                                   f'### {self.room_name}[{self.room_data.get("room_id")}]开播了\n\n{self.room_data.get("room_title")}\n\n{self.room_url}')))

            # 启动录制
            if self.is_living and not self.is_recording:
                self.send_event(Event(EventType.EVENT_PRE_RECORD))

        @event_manager.register(EventType.EVENT_UPDATE_DB_ROOM_DATA)
        def update_db_room_data():

            try:
                logger.debug(f'{self.log_prefix} :  Try to update room data in db:{self.room_data}')
                # 更新数据库信息
                DB.update_live_room_operation_data(self.room_data)
                logger.debug(f'{self.log_prefix} :  Room data updated')
            except Exception as e:
                logger.error(f'{self.log_prefix} :  更新数据库信息失败: {e}')

        @event_manager.register(EventType.EVENT_REFRESH_ROOM_INFO)
        def refresh_room_info(room_info):
            self.room_data.update(room_info)
            logger.info(f'{self.log_prefix} :  Room data updated:{self.room_data}')

        @event_manager.register(EventType.EVENT_PRE_RECORD)
        def process_pre_record():
            self.send_event(Event(EventType.EVENT_RECORD))

        @event_manager.register(EventType.EVENT_RECORD)
        def process_record():
            self._start_record()

        @event_manager.register(EventType.EVENT_RECORD_COMPLETED)
        def process_record_completed(file_list):

            # 未开播状态
            self.is_recording = False
            self.is_living = False

            if file_list:
                self.send_event(Event(EventType.EVENT_UPLOAD_BILI, (file_list,)))

        @event_manager.register(EventType.EVENT_NOTIFY)
        def process_notify(title, content):
            from luboman.core.notify import notify_message
            notify_message(title, content)

        @event_manager.register(EventType.EVENT_DOWNLOAD_ASSET)
        def download_living_asset():
            # cover 下载
            cover_url = self.room_data.get('room_cover_frame_url') if self.room_data.get('room_cover_frame_url') else self.room_data.get('room_cover_url')
            if cover_url:
                cover_file = f'{get_public_dir()}/cover/{self.room_data.get("room_id")}-{self.room_name}.jpg'
                cover_dir = os.path.dirname(cover_file)
                if not os.path.exists(cover_dir):
                    os.makedirs(cover_dir)
                try:
                    download_file(cover_url, cover_file, headers=self.fake_headers)
                except Exception as e:
                    logger.error(f'{self.log_prefix} :  下载封面失败: {e}')

            # avatar 下载
            avatar_url = self.room_data.get('room_owner_avatar')
            if avatar_url:
                avatar_file = f'{get_public_dir()}/avatar/{self.room_data.get("room_id")}-{self.room_name}.jpg'
                avatar_dir = os.path.dirname(avatar_file)
                if not os.path.exists(avatar_dir):
                    os.makedirs(avatar_dir)
                try:
                    download_file(avatar_url, avatar_file, headers=self.fake_headers)
                except Exception as e:
                    logger.error(f'{self.log_prefix} :  下载头像失败: {e}')

        @event_manager.register(EventType.EVENT_UPLOAD_BILI, "SLOW")
        def process_upload_bili(file_list):
            logger.info(f'{self.log_prefix} | Bili上传开始: {file_list}')
            bili_upload_template_id = self.room_data.get('bili_upload_template_id')
            if bili_upload_template_id is None:
                logger.error(f"{self.log_prefix} | bili_upload_template_id is None")
                return

            template_info = BiliUploadTemplate.get_by_id_(bili_upload_template_id)
            if not template_info:
                logger.error(f"{self.log_prefix} :  bili_upload_template_id: {bili_upload_template_id} not found")
                return

            if template_info.bili_account_id is None:
                logger.error(f"{self.log_prefix} :  bili_account_id is None")
                return

            bili_account = BiliAccount.get_by_id_(template_info.bili_account_id)
            if not bili_account:
                logger.error(f"{self.log_prefix} :  bili_account_id: {template_info.bili_account_id} not found")
                return

            template_info = model_to_dict(template_info)
            template_info['bili_account'] = model_to_dict(bili_account)

            room_data = self.room_data.copy()
            room_data['bili_upload_template'] = template_info
            upload_info = {
                'room_data': room_data
            }

            prepare_upload_file_list = []
            filtering_threshold_file_size = config.get("filtering_threshold_file_size", 5)
            filtering_threshold_file_size = int(filtering_threshold_file_size) * 1024 * 1024
            for file in file_list:
                if os.path.exists(file['video']) and os.path.getsize(file['video']) >= filtering_threshold_file_size:
                    prepare_upload_file_list.append(file)

            ret = upload('biliweb', prepare_upload_file_list, **upload_info)
            logger.info(f'{self.log_prefix} :  Bili上传完成: {ret}')

        @event_manager.register(EventType.EVENT_UPLOAD_BILI_COMPLETED)
        def process_upload_bili_completed(file_list):
            pass

        @event_manager.register(EventType.EVENT_UPLOAD, "SLOW")
        def process_upload(file_list):
            prepare_upload_file_list = []
            filtering_threshold_file_size = config.get("filtering_threshold_file_size", 5)
            filtering_threshold_file_size = int(filtering_threshold_file_size) * 1024 * 1024
            for file in file_list:
                if os.path.exists(file['video']) and os.path.getsize(file['video']) >= filtering_threshold_file_size:
                    prepare_upload_file_list.append(file)

            self._upload_to_storage(prepare_upload_file_list)
            self.send_event(Event(EventType.EVENT_UPLOAD_COMPLETED, (prepare_upload_file_list,)))

        @event_manager.register(EventType.EVENT_UPLOAD_COMPLETED)
        def process_upload_completed(file_list, platform='alipan'):
            pass

        event_manager.start()
        return event_manager

    def _start_record(self):
        self.is_recording = True

    # @BaseNotifier.live_notify("{room_name} 开始上传网盘", "")
    def _upload_to_storage(self, file_list):
        try:
            upload_platform = self.room_data.get('upload_storage_platform')
            if upload_platform:
                upload(upload_platform, file_list)
        except Exception as e:
            logger.exception(f'{self.log_prefix} :  上传失败: {e}')


def start_room(room_data, **kwargs):
    room_name = room_data.get('room_name')
    url = room_data.get('room_url')
    room_row_id = room_data.get('id')

    kwargs.update({'room_data': room_data})

    pg = PluginTool.running_plugins.get(str(room_row_id))
    if not pg:
        for plugin in PluginTool.live_plugins:
            if re.match(plugin.VALID_URL_BASE, url):
                pg = plugin(room_name, url)
                for k in pg.__dict__:
                    if kwargs.get(k):
                        pg.__dict__[k] = kwargs.get(k)

                PluginTool.running_plugins[str(room_row_id)] = pg
                break
    if pg:
        return pg.start()


def stop_room(room_row_id):
    pg = PluginTool.running_plugins.pop(str(room_row_id))
    if pg:
        pg.stop()
        del pg
