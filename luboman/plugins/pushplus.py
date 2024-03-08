import requests

from luboman.core.decorators import PluginTool
from luboman.core.notify import BaseNotifier


@PluginTool.notify('pushplus')
class PushPlusNotifier(BaseNotifier):
    def __init__(self, token):
        super().__init__('pushplus', token)

    def do_notify(self, title, content):
        resp = requests.post('http://www.pushplus.plus/send', json={
            'token': self.token,
            'template': 'markdown',
            'title': title,
            'content': content,
        })

        return resp.text
