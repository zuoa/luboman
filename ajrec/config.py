import json
import pathlib
import shutil
from collections import UserDict

from ajrec.database.models import GlobalConfig


class Config(UserDict):
    def load_cookies(self, file='cookies.json'):
        self.data["user"] = {"cookies": {}}
        with open(file, encoding='utf-8') as stream:
            s = json.load(stream)
            for i in s["cookie_info"]["cookies"]:
                name = i["name"]
                self.data["user"]["cookies"][name] = i["value"]
            self.data["user"]["access_token"] = s["token_info"]["access_token"]

    def load_from_db(self):
        context = {
            'url_upload_count': self.data.get('url_upload_count', {}),
            'upload_filename': self.data.get('upload_filename', []),
            'PluginInfo': self.data.get('PluginInfo')
        }
        for cfg in GlobalConfig.select().where(GlobalConfig.key == 'global_config'):
            self.data = json.loads(cfg.value)

        self.data.update(context)

        self['streamers'] = {}
        for ls in LiveStreamers.select():
            self['streamers'][ls.remark] = {k: v for k, v in model_to_dict(ls).items() if v and (k != 'upload_streamers')}
            # self['streamers'][ls.remark].pop('upload_streamers')
            if ls.upload_streamers:
                self['streamers'][ls.remark].update({k: v for k, v in model_to_dict(ls.upload_streamers).items() if v})
            if self['streamers'][ls.remark].get('tags'):
                self['streamers'][ls.remark]['tags'] = self['streamers'][ls.remark]['tags']
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
