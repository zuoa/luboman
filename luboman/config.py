import logging
from collections import UserDict

from luboman.database.models import GlobalConfig

logger = logging.getLogger("luboman")


class Config(UserDict):
    def load_from_db(self):
        context = {}
        for cfg in GlobalConfig.select():
            self.data[cfg.key] = cfg.value

        self.data.update(context)

        logger.info(f"Config: {self.data}")

    def get_live_check_interval(self, default=10):
        """直播间在线检测间隔（秒）。

        值越小越快发现开播/下播，但会增加对直播平台的请求频率。
        从配置读取失败或非法时回退到 default。
        """
        try:
            return max(1, int(self.data.get('live_check_interval', default)))
        except (TypeError, ValueError):
            return default


config = Config()
