import logging.config
from pathlib import Path
import tempfile
from typing import Dict


def configure_logging() -> None:
    logging.config.dictConfig(LOGGING)
    log = logging.getLogger(__name__)
    log.debug('Logging is configured.')


INSTANCE: Dict = {}  # Set from YAML config file.
PACKAGE_NAME = Path(__file__).parent.stem

BITLY_SHORTENER_MAX_CACHE_SIZE = 4096
DB_FILENAME = 'posts.v1.db'
DEDUP_STRATEGY_DEFAULT = 'channel'
FREQ_HOURS_DEFAULT = 1
MAX_CHANNEL_QUEUE_SIZE = 5
MAX_POSTS_OF_NEW_FEED = 3
MESSAGE_TEMPLATE = '⧙{feed}⧘ {title} → {url}'
MIN_CHANNEL_IDLE_TIME = 15 * 60
REQUEST_TIMEOUT = 90
TEMPDIR = Path(tempfile.gettempdir())
USER_AGENT = 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:65.0) Gecko/20100101 Firefox/65.0'

LOGGING = {  # Ref: https://docs.python.org/3/howto/logging.html#configuring-logging
    'version': 1,
    'formatters': {
        'detailed': {
            'format': '%(asctime)s %(thread)x-%(threadName)s:%(name)s:%(lineno)d:%(funcName)s:%(levelname)s: %(message)s',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'level': 'DEBUG',
            'formatter': 'detailed',
            'stream': 'ext://sys.stdout',
        },
    },
    'loggers': {
        PACKAGE_NAME: {
            'level': 'DEBUG',
            'handlers': ['console'],
            'propagate': False,
        },
        'bitlyshortener': {
            'level': 'INFO',
            'handlers': ['console'],
            'propagate': False,
        },
        'peewee': {
            'level': 'DEBUG',
            'handlers': ['console'],
            'propagate': False,
        },
        '': {
            'level': 'DEBUG',
            'handlers': ['console'],
        },
    },
}

configure_logging()
