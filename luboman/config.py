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


config = Config()
