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

        # 事件队列
        self.__queue = Queue()

        # 事件引擎开关
        self.__active = True

        self.__handlers = {}

        self.__pool_blocks = []

        self.__thread_pool = {
            'NORMAL': ThreadPoolExecutor(2, thread_name_prefix='NORMAL'),
            'SLOW': ThreadPoolExecutor(3, thread_name_prefix='SLOW'),
        }

    def stop(self):
        """停止"""
        self.__active = False
        for pool in self.__thread_pool.values():
            pool.shutdown()

    def run(self):
        while self.__active:
            try:
                event = self.__queue.get(block=True, timeout=1)
                self.__event_process(event)
            except Exception as e:
                logger.debug(e)
                # pass

    def __event_process(self, event):
        if self.__active:
            if event and event.type_ in self.__handlers:
                for handler in self.__handlers[event.type_]:
                    if handler.__qualname__ in self.__pool_blocks:
                        self.__thread_pool.get(handler.thread_pool).submit(handler, event)
                    else:
                        handler(event)

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
        self.__queue.put(event)

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
