import json
import logging
import pathlib
import shutil
from collections import UserDict

from luboman.database.models import GlobalConfig

logger = logging.getLogger("luboman")


class Config(UserDict):
    def load_from_db(self):
        context = {
            'url_upload_count': self.data.get('url_upload_count', {}),
            'upload_filename': self.data.get('upload_filename', []),
            'PluginInfo': self.data.get('PluginInfo')
        }
        for cfg in GlobalConfig.select():
            self.data[cfg.key] = cfg.value

        self.data.update(context)

        logger.info(f"Config: {self.data}")
        #
        # self['streamers'] = {}
        # for ls in LiveStreamers.select():
        #     self['streamers'][ls.remark] = {k: v for k, v in model_to_dict(ls).items() if v and (k != 'upload_streamers')}
        #     # self['streamers'][ls.remark].pop('upload_streamers')
        #     if ls.upload_streamers:
        #         self['streamers'][ls.remark].update({k: v for k, v in model_to_dict(ls.upload_streamers).items() if v})
        #     if self['streamers'][ls.remark].get('tags'):
        #         self['streamers'][ls.remark]['tags'] = self['streamers'][ls.remark]['tags']
        # for us in UploadStreamers.select():
        #     config.data[con.key] = con.value

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
