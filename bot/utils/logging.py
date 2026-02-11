from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os

from bot.config import Config


def setup_logging(config: Config) -> None:
    level = getattr(logging, config.log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    os.makedirs(config.log_dir, exist_ok=True)
    log_path = os.path.join(config.log_dir, config.log_file)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=config.log_max_bytes,
        backupCount=config.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(fmt)

    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Discord/wavelink log rất nhiều ở DEBUG (heartbeat, gateway payload)
    # nên đặt mức tối thiểu WARNING để tránh spam
    lib_level = max(level, logging.WARNING)
    logging.getLogger("discord").setLevel(lib_level)
    logging.getLogger("wavelink").setLevel(lib_level)
