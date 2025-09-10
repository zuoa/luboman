import functools
import inspect
import logging

import requests

from luboman.config import config
from luboman.core.decorators import PluginTool

logger = logging.getLogger('luboman')


class BaseNotifier:
    def __init__(self, platform, token):
        self.platform = platform
        self.token = token

    def notify(self, title, content):
        if self.platform and self.token:
            if not content:
                content = title
            self.do_notify(title, content)

    def do_notify(self, title, content):
        raise NotImplementedError

    @staticmethod
    def live_notify(format_title, format_text):
        def decorator(func):
            @functools.wraps(func)
            def wrapper(self, *args, **kwargs):
                data = self.room_data
                notify_message(format_title.format(**data), format_text.format(**data))
                return func(self, *args, **kwargs)

            return wrapper

        return decorator


def notify_message(title, content, **kwargs):
    try:
        platform = config.get('notify_platform', 'pushplus')
        token = config.get('notify_token', '')
        context = {
            'platform': platform,
            'token': token,
        }

        cls = PluginTool.notify_plugins.get(platform)
        if cls is None:
            return logger.error(f"No such notifier: {platform}")
        sig = inspect.signature(cls)
        for k in sig.parameters:
            v = context.get(k)
            if v:
                kwargs[k] = v
        return cls(**kwargs).notify(title, content)
    except:
        logger.exception("Uncaught exception:")
