import logging
import os
import sys

__version__ = "0.4.34"

LOG_CONF = {
    'version': 1,
    'formatters': {
        'verbose': {
            'format': "%(asctime)s %(filename)s[line:%(lineno)d](Pid:%(process)d "
                      "Thread:%(threadName)s) %(levelname)s %(message)s",
            # 'datefmt': "%Y-%m-%d %H:%M:%S"
        },
        'simple': {
            'format': '%(asctime)s %(filename)s-%(lineno)d [%(levelname)s]-%(threadName)s: %(message)s'
        },
    },
    'handlers': {
        'console': {
            'level': logging.DEBUG,
            'class': 'logging.StreamHandler',
            'stream': sys.stdout,
            'formatter': 'simple'
        },
        'file': {
            'level': logging.INFO,
            'class': 'luboman.core.log.SafeRotatingFileHandler',
            'when': 'W0',
            'interval': 1,
            'backupCount': 1,
            'filename': '/data/logs/run.log' if os.path.exists('/.dockerenv') else 'data/logs/run.log',
            'formatter': 'verbose'
        },
        'debug': {
            'level': logging.DEBUG,
            'class': 'luboman.core.log.SafeRotatingFileHandler',
            'when': 'W0',
            'interval': 1,
            'backupCount': 1,
            'filename': '/data/logs/debug.log' if os.path.exists('/.dockerenv') else 'data/logs/debug.log',
            'formatter': 'verbose'
        }
    },
    'root': {
        'handlers': ['console'],
        'level': logging.INFO,
    },
    'loggers': {
        'luboman': {
            'handlers': ['file', 'debug'],
            'level': logging.DEBUG,
        },
    }
}
