from ajrec.config import config
from ajrec.core.event import EventManager


def create_event_manager():
    pool1_size = config.get('pool1_size', 3)
    pool2_size = config.get('pool2_size', 3)
    # 初始化事件管理器
    manager = EventManager()
    return manager


event_manager = create_event_manager()


@event_manager.serve
class LiveManager:
    def __init__(self):
        self.event_manager = EventManager()
