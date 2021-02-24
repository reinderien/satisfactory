import logging

logger = logging.getLogger('satisfactory')


def setup_logs():
    logger.level = logging.INFO
    logger.addHandler(logging.StreamHandler())
