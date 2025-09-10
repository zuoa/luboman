import requests

from luboman.core.decorators import PluginTool
from luboman.core.notify import BaseNotifier


@PluginTool.notify('bark')
class BarkNotifier(BaseNotifier):
    def __init__(self, token):
        super().__init__('bark', token)

    def do_notify(self, title, content):
        if self.token:
            requests.post(f'https://bark.aproxy.cn/{self.token}/', json={
                'title': title,
                'body': content,
            })
