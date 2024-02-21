from dataclasses import dataclass, field
from queue import Queue
from threading import Thread


class EventManager(Thread):
    def __init__(self):
        super().__init__(name='Synchronous', daemon=True)

        # 事件队列
        self.__queue = Queue()

        # 事件引擎开关
        self.__active = True

        self.__handlers = {}

    def stop(self):
        """停止"""
        self.__active = False
        # for pool in self._pool.values():
        #     pool.shutdown()

    def run(self):
        while self.__active:
            try:
                event = self.__queue.get(block=True, timeout=1)
                self.__event_process(event)
            except:
                pass

    def __event_process(self, event):
        if self.__active:
            if event and event.type_ in self.__handlers:
                for handler in self.__handlers[event.type_]:
                    handler(event)

    def add_event_handler(self, event, handler):
        if event.type_ not in self.__handlers:
            self.__handlers[event.type_] = []
        self.__handlers[event.type_].append(handler)

    def remove_event_handler(self, event, handler):
        if event.type_ in self.__handlers:
            self.__handlers[event.type_].remove(handler)

    def fire(self, event, *args, **kwargs):
        if event.type_ in self.__handlers:
            for handler in self.__handlers[event.type_]:
                handler(*args, **kwargs)


@dataclass
class Event:
    """事件对象"""
    type_: str  # 事件类型
    args: tuple = ()
    data: dict = field(default_factory=dict)  # 字典用于保存具体的事件数据


if __name__ == '__main__':
    import inspect

    print(inspect.getouterframes(inspect.currentframe())[1][3])
