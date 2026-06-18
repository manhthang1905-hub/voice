"""
Logging system cho ElevenLabsTTS.
Ghi log ra console va file, de debug va theo doi hoat dong.
"""

import logging
import os
from datetime import datetime


def setup_logger(name: str = "ElevenLabsTTS", log_dir: str = None) -> logging.Logger:
    """Tao logger voi console + file handler.

    Args:
        name: Ten logger
        log_dir: Thu muc chua file log. Mac dinh: project/logs/

    Returns:
        Logger da config
    """
    logger = logging.getLogger(name)

    # Tranh tao duplicate handlers neu goi nhieu lan
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # Format chung
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler - chi hien INFO tro len
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # File handler - ghi tat ca tu DEBUG
    if log_dir is None:
        log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, f"{datetime.now().strftime('%Y-%m-%d')}.log")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


# Global logger instance
log = setup_logger()
