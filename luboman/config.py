import json
import logging
import pathlib
import shutil
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


    def save_to_db(self):
        with db.connection_context():
            for k, v in self['streamers'].items():
                us = UploadStreamers(template_name=k, tags=v.pop('tags', ['biliup']), **v)
                us.save()
                for url in v.pop('url'):
                    LiveStreamers(upload_streamers=us, remark=k, url=url, **v).save()
            del self['streamers']

            GlobalConfig(key='global_config', value=json.dumps(self.data)).save()


config = Config()
