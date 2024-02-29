import logging

import requests

from luboman.config import config

logger = logging.getLogger(__name__)


def notify_message(title, content):
    notify_platform = config.get('notify_platform', 'pushplus')
    notify_token = config.get('notify_token', '01083154c7854191a14ca66dfbf0592c')
    if notify_platform == 'pushplus':
        if notify_token:
            resp = requests.post('http://www.pushplus.plus/send', json={
                'token': notify_token,
                'template': 'markdown',
                'title': title,
                'content': content,
            })

            logger.info(resp.text)
