import functools
import importlib
import logging
import pkgutil
import re

logger = logging.getLogger('luboman')


class PluginTool:
    live_plugins = []
    upload_plugins = {}

    running_plugins = {}

    def __init__(self, pkg):
        self.load_plugins(pkg)

    @staticmethod
    def live(regexp):
        def decorator(cls):
            @functools.wraps(cls)
            def wrapper(*args, **kw):
                return cls(*args, **kw)

            wrapper.VALID_URL_BASE = regexp
            PluginTool.live_plugins.append(wrapper)
            return wrapper

        return decorator

    @staticmethod
    def upload(platform):
        def decorator(cls):
            PluginTool.upload_plugins[platform] = cls
            return cls

        return decorator

    def load_plugins(self, pkg):
        """Attempt to load plugins from the path specified.
        engine.plugins.__path__[0]: full path to a directory where to look for plugins
        """

        plugins = []

        logger.info(f"Loading plugins from {pkg}")
        for loader, name, is_pkg in pkgutil.iter_modules([pkg.__path__[0]]):
            # set the full plugin module name
            module_name = f"{pkg.__name__}.{name}"
            logger.info(f"Loading plugin: {module_name}")
            module = importlib.import_module(module_name)
            if is_pkg:
                self.load_plugins(module)
                continue
            if module in plugins:
                continue
            plugins.append(module)
        return plugins
