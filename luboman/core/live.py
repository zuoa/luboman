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
import tempfile
import threading
import time
from collections import deque
from urllib.parse import urlparse

import requests
from playhouse.shortcuts import model_to_dict

from luboman.config import config
from luboman.core.decorators import PluginTool
from luboman.core.event import EventManager, Event, EventType
from luboman.core.upload import resolve_bili_uploader, upload
from luboman.core.utils import random_user_agent, get_valid_filename, get_video_dir, rename, get_public_dir, download_file
from luboman.database.db import DB, resolve_room_bili_template_ids, should_auto_upload_full_bili
from luboman.database.models import BiliUploadTemplate, BiliAccount

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
        # 秒断判定阈值(秒)：单次录制时长小于此值视为流被对端秒断，
        # 删除该小文件并转入失败重试，避免 record_func 成功分支立刻秒级重启。
        self.min_record_seconds = 10
        self.event_manager = self.create_event_manager()
        self.record_thread = self.start_record_thread()
        self._status_task = None

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
        last_memory_check = 0
        while self._active:
            self.send_event(Event(EventType.EVENT_CHECK_STATUS, (1,)))

            # 每10次更新一次数据库
            if seq % 10 == 0:
                self.send_event(Event(EventType.EVENT_UPDATE_DB_ROOM_DATA))

            # 每100次检查（50分钟）进行内存监控和垃圾回收
            if seq % 100 == 0:
                try:
                    import psutil
                    import gc
                    
                    # 获取当前进程内存使用情况
                    process = psutil.Process()
                    memory_info = process.memory_info()
                    memory_mb = memory_info.rss / 1024 / 1024
                    
                    # 如果内存使用增长过快，记录警告
                    if last_memory_check > 0:
                        memory_growth = memory_mb - last_memory_check
                        if memory_growth > 50:  # 增长超过50MB
                            logger.warning(f'{self.log_prefix} : 内存使用增长: {memory_growth:.1f}MB，当前: {memory_mb:.1f}MB')
                            
                            # 强制垃圾回收
                            collected = gc.collect()
                            logger.info(f'{self.log_prefix} : 执行垃圾回收，回收对象数: {collected}')
                            
                            # 重新检查内存
                            new_memory_mb = process.memory_info().rss / 1024 / 1024
                            if new_memory_mb < memory_mb:
                                logger.info(f'{self.log_prefix} : 垃圾回收释放内存: {memory_mb - new_memory_mb:.1f}MB')
                            
                    last_memory_check = memory_mb
                    
                    # 每500次（约4小时）详细报告内存状态
                    if seq % 500 == 0:
                        gc_stats = gc.get_stats()
                        logger.info(f'{self.log_prefix} : 内存状态报告 - 使用: {memory_mb:.1f}MB, GC统计: {gc_stats}')
                        
                except ImportError:
                    # psutil未安装时跳过内存监控
                    if seq == 100:  # 只在第一次时警告
                        logger.warning(f'{self.log_prefix} : psutil未安装，跳过内存监控')
                except Exception as e:
                    logger.error(f'{self.log_prefix} : 内存监控失败: {e}')

            seq += 1
            await asyncio.sleep(config.get_live_check_interval())  # 检测间隔可配置

    def start(self):
        logger.info(f'{self.log_prefix} : 开启直播间')
        logger.info(self.room_data)
        if self._status_task and not self._status_task.done():
            logger.warning(f'{self.log_prefix} :  状态检查任务已存在，跳过重复启动')
            return
        self._status_task = asyncio.create_task(self.async_check_status())

    def stop(self):
        logger.warning(f'{self.log_prefix} :  停止直播间')
        self._active = False
        
        try:
            if self._status_task and not self._status_task.done():
                self._status_task.cancel()
                self._status_task = None
            # 首先停止事件管理器，防止新事件产生
            if hasattr(self, 'event_manager') and self.event_manager:
                logger.debug(f'{self.log_prefix} :  停止事件管理器...')
                self.event_manager.stop()
            
            # 等待录制线程结束，设置超时
            if hasattr(self, 'record_thread') and self.record_thread and self.record_thread.is_alive():
                logger.debug(f'{self.log_prefix} :  等待录制线程结束...')
                self.record_thread.join(timeout=10)  # 10秒超时
                if self.record_thread.is_alive():
                    logger.warning(f'{self.log_prefix} :  录制线程未能在10秒内结束，可能存在死锁')
                    # 强制清理线程引用，让垃圾回收器处理
                    self.record_thread = None
                else:
                    logger.debug(f'{self.log_prefix} :  录制线程已正常结束')
            
            # 清理所有可能的循环引用
            if hasattr(self, 'room_data'):
                self.room_data.clear()
            
            # 清理HTTP会话相关资源
            if hasattr(self, 'fake_headers'):
                self.fake_headers.clear()
            
            # 清理流地址
            self.raw_stream_url = None
            
            # 强制垃圾回收
            import gc
            gc.collect()
            
            logger.info(f'{self.log_prefix} :  直播间已停止，资源已清理')
            
        except Exception as e:
            logger.error(f'{self.log_prefix} :  停止直播间时发生错误: {e}')
            # 即使发生错误也要确保基本的清理
            self._active = False
            if hasattr(self, 'record_thread'):
                self.record_thread = None

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

        # 是否已输出过"等待中"日志，避免在轮询循环里重复刷屏
        logged_waiting = False
        while self._active:
            # 未启动录制
            if not self.is_recording:
                if not logged_waiting:
                    logger.debug(f'{self.log_prefix} :  录制线程等待中: is_recording={self.is_recording}, is_living={self.is_living}')
                    logged_waiting = True
                time.sleep(3)
                continue

            logged_waiting = False
            logger.info(f'{self.log_prefix} :  录制线程开始执行录制: is_recording={self.is_recording}')

            recording_context = {
                "begin_time": datetime.datetime.now(),
            }

            ret = False
            filepath = None

            try:
                # 阻塞下载，流没中断，会一直录制
                ret, filepath = self.record(recording_context)
            except Exception as e:
                logger.exception(f'{self.log_prefix} :  Uncaught exception:{e}')
            finally:
                self.stopped()

            # 秒断检测：录制时长过短视为流被对端秒断。虎牙 CDN 一个签名仅允许
            # 一个连接，且对高频重连的 IP 渐进式 403 封禁（本机同出口 IP 实测复现），
            # 因此连续秒断时指数退避（5s→…→300s 封顶）压住重连频率，避免越重连越封；
            # 删除小文件，不产生秒级垃圾文件。不计入失败重试次数——主播在播只是
            # 连接被封，不应误判下播。
            if ret and recording_context.get('begin_time'):
                _duration = (datetime.datetime.now() - recording_context['begin_time']).total_seconds()
                if _duration < self.min_record_seconds:
                    self._consecutive_short = getattr(self, '_consecutive_short', 0) + 1
                    _backoff = min(300, 5 * (2 ** (self._consecutive_short - 1)))
                    logger.warning(
                        f'{self.log_prefix} : 录制仅 {_duration:.1f}s，疑似流秒断'
                        f'（连续第 {self._consecutive_short} 次），删除小文件 {filepath}，'
                        f'退避 {_backoff}s 后重试')
                    try:
                        if filepath and os.path.exists(filepath):
                            os.remove(filepath)
                    except Exception as _e:
                        logger.warning(f'{self.log_prefix} : 删除秒断小文件失败: {_e}')
                    # 秒断的 DB 记录即时标记完成（文件已删，由死记录清理脚本收敛）
                    _rid = recording_context.get('id')
                    if _rid:
                        try:
                            recording_context['end_time'] = datetime.datetime.now()
                            DB.complete_record_file(_rid, recording_context)
                        except Exception:
                            pass
                    time.sleep(_backoff)
                    continue

            if ret:
                # 成功下载重置重试次数与秒断计数
                retry_count = 0
                retry_count_delay = 0
                is_offline = False
                self._consecutive_short = 0

                recording_context["end_time"] = datetime.datetime.now()
                recording_context["video"] = filepath
                recording_context['live_room_id'] = self.room_data.get('id')
                record_file_list.append(recording_context)

                # 数据库记录：录制开始时已创建，这里只更新状态、结束时间和时长。
                try:
                    logger.info(recording_context)
                    record_id = recording_context.get('id')
                    if record_id:
                        DB.complete_record_file(record_id, recording_context)
                    else:
                        created = DB.create_record_file_started(recording_context)
                        DB.complete_record_file(created.get('id'), recording_context)
                except Exception as e:
                    logger.exception(f'{self.log_prefix} :  | Uncaught exception:{e}')

                self.send_event(Event(EventType.EVENT_UPLOAD, ([recording_context],)))
                # 单分段录制完成事件：供自动舞蹈切片等按分段触发的逻辑使用
                self.send_event(Event(EventType.EVENT_RECORD_SEGMENT, ([recording_context],)))

                try:
                    ## 录像最后一个时间减去第一个起始时间大于24小时
                    first_file = record_file_list[0]
                    if (recording_context["end_time"] - first_file["begin_time"]).seconds > 86400:
                        self.send_event(Event(EventType.EVENT_RECORD_COMPLETED, (record_file_list,)))
                        record_file_list = []
                except Exception as ex:
                    logger.exception(f'{self.log_prefix} :  Uncaught exception:{ex}')

            else:
                record_id = recording_context.get('id')
                if record_id:
                    try:
                        recording_context["end_time"] = datetime.datetime.now()
                        DB.complete_record_file(record_id, recording_context)
                    except Exception as e:
                        logger.exception(f'{self.log_prefix} :  更新失败录制记录失败:{e}')

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

    def record(self, recording_context=None):
        logger.debug(f'{self.log_prefix} :  进入record()方法，检查直播状态')
        
        if not self.check_live():
            logger.warning(f'{self.log_prefix} :  录制中检查状态失败，直播可能已结束')
            return False, None

        logger.info(f'{self.log_prefix} :  开始新的录制:{self.raw_stream_url}')
        
        if not self.raw_stream_url:
            logger.error(f'{self.log_prefix} :  录制失败，未获取到流地址')
            return False, None
            
        filepath = self.get_filepath()
        logger.debug(f'{self.log_prefix} :  录制文件路径: {filepath}')

        if recording_context is not None:
            recording_context['live_room_id'] = self.room_data.get('id')
            recording_context['video'] = filepath
            try:
                created = DB.create_record_file_started(recording_context)
                recording_context['id'] = created.get('id')
                logger.info(f'{self.log_prefix} :  录制文件记录已创建: {recording_context["id"]}')
            except Exception as e:
                logger.exception(f'{self.log_prefix} :  创建录制文件记录失败:{e}')
        
        use_stream_gears = self._use_stream_gears()
        try:
            if use_stream_gears:
                ok = self.stream_gears_download(filepath)
            else:
                ok = self.ffmpeg_download(filepath)
            if not ok:
                logger.error(f'{self.log_prefix} :  录制失败: {filepath}')
                return False, filepath
            if not use_stream_gears:
                # ffmpeg 输出 {filepath}.part，需手动改名；stream_gears 自行 rename
                rename(filepath)
            logger.info(f'{self.log_prefix} :  片段录制结束: {filepath}')
            return True, filepath
        except Exception as e:
            logger.error(f'{self.log_prefix} :  录制过程出错: {e}')
            return False, None

    def raw_download(self, filepath):
        try:
            with requests.get(self.raw_stream_url, stream=True, headers=self.fake_headers, timeout=60) as resp:
                resp.raise_for_status()  # Raise an exception for bad status codes
                with open(filepath + ".part", "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024):
                        if chunk:
                            f.write(chunk)
        except requests.exceptions.RequestException as e:
            logger.error(f"{self.log_prefix} | Raw download failed: {e}")

    def ffmpeg_download(self, filepath):
        ffmpeg_path = config.get('ffmpeg_path', 'ffmpeg')
        default_input_args = ['-headers', ''.join('%s: %s\r\n' % x for x in self._record_headers().items()),
                              '-rw_timeout', '20000000']
        # 虎牙 CDN 的 FLV 直链一个签名仅允许一个连接、且对高频重连的 IP 渐进式 403
        # 封禁（已实测），-reconnect 会以同一签名重连必 403、加速封禁 → 虎牙 FLV 禁用；
        # 但 HLS 不受此限——浏览器看直播就是拿同一签名 URL 高频 reload 播放列表，
        # 是 CDN 预期的访问模式。虎牙 HLS 禁用 reconnect 会导致 CDN 一抖动 ffmpeg
        # 就退出、外层换新签名重开（约 5 分钟一个分段文件），故 HLS 恢复自动重连。
        is_hls = '.m3u8' in urlparse(self.raw_stream_url).path
        if self.__class__.__name__.lower() != 'huya' or is_hls:
            default_input_args += ['-reconnect', '1', '-reconnect_streamed', '1',
                                   '-reconnect_delay_max', '5']
        if is_hls:
            default_input_args += ['-max_reload', '1000']
        command_args = [ffmpeg_path, '-y', '-hide_banner', '-nostats', '-loglevel', 'warning',
                        *default_input_args,
                        '-i', self.raw_stream_url, *self.ffmpeg_opt_args,
                        '-c', 'copy', '-f', self.suffix]

        command_args += [f'{filepath}.part']

        try:
            # Use the context manager for robust cleanup
            with subprocess.Popen(
                    command_args,
                    stdin=subprocess.DEVNULL,  # Use DEVNULL if you don't need stdin
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT
            ) as proc:
                output_tail = deque(maxlen=20)
                with proc.stdout as stdout:
                    for line in iter(stdout.readline, b''):
                        decode_line = line.decode(errors='ignore').rstrip()
                        output_tail.append(decode_line)
                        logger.debug(decode_line)

                # The context manager's __exit__ will call proc.wait()
                # but we can get the return code to check for success.
                retval = proc.wait()
                if retval != 0:
                    logger.error(f"{self.log_prefix} :  ffmpeg 退出码 {retval}, 输出尾部: "
                                 f"{' | '.join(output_tail)}")
                return retval == 0

        except FileNotFoundError:
            logger.error(f"ffmpeg not found at path: {ffmpeg_path}. Please check your configuration.")
            return False
        except Exception as e:
            logger.error(f"{self.log_prefix} | ffmpeg_download encountered an error: {e}")
            return False

    def _record_headers(self):
        """录制/连流用的请求头。虎牙 wup 模式下插件会设置独立 stream_headers
        （HYSDK UA + origin，与 wup 请求端一致）；其余平台用 fake_headers。"""
        return getattr(self, 'stream_headers', None) or self.fake_headers

    def stream_gears_download(self, filepath):
        """用 stream_gears（biliup 下载核心）录制 FLV 流，单文件模式录到下播。

        stream_gears 自写 {prefix}.flv.part 并 rename 成 {prefix}.flv，最终落盘路径
        == get_filepath() 的返回，与 DB 记录一致。time/size 均不设 → 不切片单文件。

        网络失败/非 2xx 会抛 pyo3 PanicException（MRO: PanicException→BaseException，
        不继承 Exception），用 except BaseException 兜住返回 False，交由 record_func 重试。
        """
        import stream_gears

        # file_name 前缀：get_filepath() 已 strftime，结果为纯字面串（无 %），
        # chrono 原样保留；扩展名 .flv 由 stream_gears(Rust) 追加。
        suffix_dot = '.' + self.suffix
        file_name_prefix = filepath[:-len(suffix_dot)] if filepath.endswith(suffix_dot) else filepath

        # time/size 均不设 → 整场录制为单文件（录到下播）
        segment = stream_gears.PySegment()

        # header_map：录制专用头（虎牙 wup 模式为 HYSDK UA + origin），去掉
        # accept-encoding（FLV 二进制流不需要，避免 reqwest 解压干扰原始字节流）
        headers = {k: v for k, v in self._record_headers().items()
                   if k.lower() != 'accept-encoding'}

        try:
            stream_gears.download(
                self.raw_stream_url, headers, file_name_prefix, segment,
                proxy=config.get('stream_gears_proxy') or None,
            )
            return True
        except BaseException as e:  # 兜 pyo3 PanicException
            logger.error(f'{self.log_prefix} | stream_gears 下载失败: {e}')
            return False

    def _use_stream_gears(self):
        """是否用 stream_gears 录制：仅虎牙 + FLV 直链（非 m3u8）。

        可由 GlobalConfig.huya_use_stream_gears 关闭以回退 ffmpeg。
        """
        val = config.get('huya_use_stream_gears', True)
        if isinstance(val, str) and val.strip().lower() in ('0', 'false', 'no', 'off'):
            return False
        if self.__class__.__name__.lower() != 'huya':
            return False
        if self.suffix != 'flv':
            return False
        if self.raw_stream_url and '.m3u8' in urlparse(self.raw_stream_url).path:
            return False
        return True


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
            # 优化内存使用：减少不必要的拷贝操作
            # 仅在绝对必要时才进行浅拷贝，减少内存分配
            needs_copy = False
            for arg in event.args:
                # 只对大型可变对象或深度嵌套对象进行检查
                if isinstance(arg, (list, dict)):
                    try:
                        if len(arg) > 1000:
                            needs_copy = True
                            break
                    except Exception:
                        pass
            
            if needs_copy:
                # 只有当确实需要时才进行拷贝
                new_args = []
                for arg in event.args:
                    if isinstance(arg, (list, dict)) and len(str(arg)) > 1000:
                        # 对大型对象进行浅拷贝
                        new_args.append(copy.copy(arg))
                    else:
                        # 小对象直接引用，减少内存分配
                        new_args.append(arg)
                event.args = tuple(new_args)

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

            if self.__class__.__name__.lower() == 'huya':
                # FFMPEG 合并文件
                if file_list and len(file_list) > 1:
                    try:
                        logger.info(f'{self.log_prefix} :  开始合并录制文件: {file_list}')
                        file_list_prepare = sorted(file_list, key=lambda x: x['begin_time'])
                        file_list_prepare = [f['video'] for f in file_list_prepare]
                        output_file = self.get_filepath() + ".merged." + self.suffix
                        ffmpeg_path = config.get('ffmpeg_path', 'ffmpeg')

                        # 创建临时文件列表文件
                        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False,
                                                         encoding='utf-8') as temp_file:
                            for file in file_list_prepare:
                                temp_file.write(f"file '{file}'\n")
                            temp_file_path = temp_file.name

                        try:
                            command_args = [ffmpeg_path, '-y', '-f', 'concat', '-safe', '0', '-i', temp_file_path, '-c',
                                            'copy', output_file]

                            with subprocess.Popen(command_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
                                stdout, stderr = proc.communicate()

                                if proc.returncode != 0:
                                    logger.error(f'{self.log_prefix} :  合并录制文件失败: {stderr.decode()}')
                                else:
                                    logger.info(f'{self.log_prefix} :  合并录制文件成功: {output_file}')

                                    # 删除原始文件
                                    for file in file_list:
                                        if os.path.exists(file['video']):
                                            try:
                                                os.remove(file['video'])
                                                logger.info(f'{self.log_prefix} :  删除原始录制文件: {file["video"]}')
                                            except Exception as e:
                                                logger.error(f'{self.log_prefix} :  删除原始录制文件失败: {e}')

                                    file_list = [{'video': output_file}]

                                    self.send_event(Event(EventType.EVENT_UPLOAD, (file_list,)))
                        finally:
                            # 清理临时文件
                            if os.path.exists(temp_file_path):
                                os.remove(temp_file_path)

                    except Exception as e:
                        logger.error(f'{self.log_prefix} :  合并录制文件失败: {e}')

            if file_list and should_auto_upload_full_bili(self.room_data):
                self.send_event(Event(EventType.EVENT_UPLOAD_BILI, (file_list,)))
            elif file_list and resolve_room_bili_template_ids(self.room_data):
                logger.info(f'{self.log_prefix} 已开启只投稿切片，跳过整场录像的 B 站自动投稿')

        @event_manager.register(EventType.EVENT_NOTIFY)
        def process_notify(title, content):
            from luboman.core.notify import notify_message
            notify_message(title, content)

        @event_manager.register(EventType.EVENT_DOWNLOAD_ASSET)
        def download_living_asset():
            # cover 下载
            cover_url = self.room_data.get('room_cover_frame_url') if self.room_data.get('room_cover_frame_url') else self.room_data.get('room_cover_url')
            if cover_url:
                cover_file = f'{get_public_dir()}/cover/{self.room_data.get("room_platform")}-{self.room_data.get("room_id")}.jpg'
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
                avatar_file = f'{get_public_dir()}/avatar/{self.room_data.get("room_platform")}-{self.room_data.get("room_id")}.jpg'
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
            template_ids = resolve_room_bili_template_ids(self.room_data)
            if not template_ids:
                logger.error(f"{self.log_prefix} | 未配置B站投稿模板")
                return

            # 过滤文件（只过滤一次，各模板复用）
            prepare_upload_file_list = []
            filtering_threshold_file_size = config.get("filtering_threshold_file_size", 5)
            filtering_threshold_file_size = int(filtering_threshold_file_size) * 1024 * 1024
            for file in file_list:
                if os.path.exists(file['video']) and os.path.getsize(file['video']) >= filtering_threshold_file_size:
                    prepare_upload_file_list.append(file)

            if not prepare_upload_file_list:
                return

            # 循环投稿到每个模板（账号绑定在模板上，多模板即多账号），单个失败不影响其他
            for template_id in template_ids:
                try:
                    template_info = BiliUploadTemplate.get_by_id_(template_id)
                    if not template_info:
                        logger.error(f"{self.log_prefix} :  bili_upload_template_id: {template_id} not found")
                        continue

                    if template_info.bili_account_id is None:
                        logger.error(f"{self.log_prefix} :  bili_upload_template_id: {template_id} bili_account_id is None")
                        continue

                    bili_account = BiliAccount.get_by_id_(template_info.bili_account_id)
                    if not bili_account:
                        logger.error(f"{self.log_prefix} :  bili_account_id: {template_info.bili_account_id} not found")
                        continue

                    template_info = model_to_dict(template_info)
                    template_info['bili_account'] = model_to_dict(bili_account)

                    room_data = self.room_data.copy()
                    room_data['bili_upload_template'] = template_info
                    room_data['bili_upload_template_id'] = template_info['id']
                    upload_info = {
                        'room_data': room_data
                    }

                    bili_uploader = resolve_bili_uploader(room_data)
                    ret = upload(bili_uploader, prepare_upload_file_list, **upload_info)
                    logger.info(f'{self.log_prefix} :  Bili上传完成: 模板={template_info.get("template_name")}, {ret}')
                except Exception as e:
                    logger.error(f'{self.log_prefix} :  Bili上传失败: template_id={template_id}, 错误={e}')

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
        logger.info(f'{self.log_prefix} :  设置录制状态为True')
        self.is_recording = True
        logger.debug(f'{self.log_prefix} :  录制状态已设置: is_recording={self.is_recording}')

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
    """停止指定直播间"""
    room_id_str = str(room_row_id)
    pg = PluginTool.running_plugins.pop(room_id_str, None)
    if pg:
        try:
            logger.info(f"停止直播间: {room_id_str}")
            pg.stop()
        except Exception as e:
            logger.error(f"停止直播间 {room_id_str} 时出错: {e}")
        finally:
            # 确保对象被删除
            try:
                del pg
            except:
                pass
    else:
        logger.warning(f"直播间 {room_id_str} 未在运行中")
