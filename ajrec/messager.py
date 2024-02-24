import logging

import requests

from ajrec.config import config

logger = logging.getLogger(__name__)


def push(title, content):
    messager = config.get('messager', 'pushplus')
    messager_token = config.get('messager_token', '01083154c7854191a14ca66dfbf0592c')
    if messager == 'pushplus':
        if messager_token:
            resp = requests.post('http://www.pushplus.plus/send', json={
                'token': messager_token,
                'template': 'markdown',
                'title': title,
                'content': content,
            })

            logger.info(resp.text)
