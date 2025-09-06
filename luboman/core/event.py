import atexit
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from queue import Queue
from threading import Thread
from typing import Generator

logger = logging.getLogger('luboman')


class EventManager(Thread):
    def __init__(self):
        super().__init__(name='Synchronous', daemon=True)

        # 事件队列，设置最大大小防止内存泄漏
        self.__queue = Queue(maxsize=1000)

        # 事件引擎开关
        self.__active = True

        self.__handlers = {}
        self.__pool_blocks = []
        
        # 尝试使用全局线程池
        self.__use_global_pool = True
        self.__thread_pool = None
        
        try:
            from .thread_pool import thread_pool_manager
            self._global_manager = thread_pool_manager
            if not thread_pool_manager.get_pool('NORMAL'):
                raise Exception("全局线程池不可用")
            logger.info("EventManager 使用全局线程池")
        except Exception as e:
            logger.warning(f"全局线程池不可用，创建本地线程池: {e}")
            self.__use_global_pool = False
            self._global_manager = None
            self.__thread_pool = {
                'NORMAL': ThreadPoolExecutor(2, thread_name_prefix='Local-NORMAL'),
                'SLOW': ThreadPoolExecutor(3, thread_name_prefix='Local-SLOW'),
            }
            # 注册退出时的清理函数
            atexit.register(self._cleanup_local_pools)

    def _cleanup_local_pools(self):
        """清理本地线程池"""
        if self.__thread_pool:
            for pool in self.__thread_pool.values():
                try:
                    pool.shutdown(wait=True)
                except Exception as e:
                    logger.error(f"清理本地线程池时出错: {e}")

    def stop(self):
        """停止事件管理器"""
        logger.debug(f"停止EventManager: {self.name}")
        self.__active = False
        
        # 清理事件处理器，防止循环引用
        self.__handlers.clear()
        self.__pool_blocks.clear()
        
        # 如果使用本地线程池，需要关闭
        if not self.__use_global_pool and self.__thread_pool:
            for pool_name, pool in self.__thread_pool.items():
                try:
                    logger.debug(f"关闭本地线程池: {pool_name}")
                    pool.shutdown(wait=True)
                except Exception as e:
                    logger.error(f"关闭本地线程池 {pool_name} 失败: {e}")
        
        # 清空队列中剩余的事件
        try:
            while not self.__queue.empty():
                self.__queue.get_nowait()
        except Exception:
            pass

    def run(self):
        while self.__active:
            try:
                event = self.__queue.get(block=True, timeout=1)
                self.__event_process(event)
            except Exception as e:
                pass

    def __event_process(self, event):
        if not self.__active:
            return
            
        if event and event.type_ in self.__handlers:
            for handler in self.__handlers[event.type_]:
                if handler.__qualname__ in self.__pool_blocks:
                    # 使用全局线程池或本地线程池
                    if self.__use_global_pool and self._global_manager:
                        future = self._global_manager.submit_task(
                            handler.thread_pool, handler, event
                        )
                        if future is None:
                            logger.error(f"提交任务到全局线程池失败: {handler.__qualname__}")
                    else:
                        pool = self.__thread_pool.get(handler.thread_pool) if self.__thread_pool else None
                        if pool:
                            try:
                                pool.submit(handler, event)
                            except Exception as e:
                                logger.error(f"提交任务到本地线程池失败: {e}")
                        else:
                            logger.error(f"无法获取线程池: {handler.thread_pool}")
                else:
                    try:
                        handler(event)
                    except Exception as e:
                        logger.error(f"处理事件时出错: {e}")

    def add_event_handler(self, type_, handler):
        if type_ not in self.__handlers:
            self.__handlers[type_] = []
        self.__handlers[type_].append(handler)

    def remove_event_handler(self, type_, handler):
        if type_ in self.__handlers:
            self.__handlers[type_].remove(handler)

    def fire(self, type_, *args, **kwargs):
        if type_ in self.__handlers:
            for handler in self.__handlers[type_]:
                handler(*args, **kwargs)

    def send(self, event):
        """发送事件，向事件队列中存入事件"""
        if not self.__active:
            return
            
        try:
            # 使用非阻塞方式，避免队列满时卡死
            self.__queue.put_nowait(event)
        except Exception:
            # 队列满时，记录警告并丢弃最旧的事件
            logger.warning("事件队列已满，丢弃最旧事件")
            try:
                self.__queue.get_nowait()  # 移除最旧事件
                self.__queue.put_nowait(event)  # 添加新事件
            except Exception:
                logger.error("无法处理队列满的情况")

    def register(self, type_, block="NORMAL"):
        def callback(result):
            if not result:
                pass
            elif isinstance(result, (tuple, Generator)):
                for event in result:
                    self.send(event)
            else:
                self.send(result)

        def block_append(fc, blk):
            if blk:
                self.__pool_blocks.append(fc.__qualname__)

        def decorator(func):
            block_append(func, block)

            @functools.wraps(func)
            def wrapper(event):
                _event = func(*event.args)
                callback(_event)
                return _event

            wrapper.thread_pool = block
            self.add_event_handler(type_, wrapper)
            return wrapper

        return decorator


class EventType:
    EVENT_CHECK_STATUS = "check-status"
    EVENT_DOWNLOAD_ASSET = "download-asset"
    EVENT_UPDATE_DB_ROOM_DATA = "update-db-room-data"
    EVENT_REFRESH_ROOM_INFO = "refresh-room-info"
    EVENT_PRE_RECORD = "pre-record"
    EVENT_RECORD = "record"
    EVENT_RECORD_COMPLETED = "record-completed"
    EVENT_NOTIFY = "notify-event"
    EVENT_UPLOAD_BILI = "upload-bili"
    EVENT_UPLOAD_BILI_COMPLETED = "upload-bili-completed"
    EVENT_UPLOAD = "upload"
    EVENT_UPLOAD_COMPLETED = "upload-completed"


@dataclass
class Event:
    """事件对象"""
    type_: str  # 事件类型
    args: tuple = ()
    data: dict = field(default_factory=dict)  # 字典用于保存具体的事件数据


if __name__ == '__main__':
    import inspect

    print(inspect.getouterframes(inspect.currentframe())[1][3])
