"""统一日志配置。"""

from __future__ import annotations

import logging
from typing import Optional

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> None:
    """配置根日志器。

    level 支持 DEBUG/INFO/WARNING/ERROR；log_file 为空时只输出到 stderr。
    """
    numeric = getattr(logging, level.upper(), None)
    if not isinstance(numeric, int):
        raise ValueError(f"无效日志级别: {level}")

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=numeric,
        format=LOG_FORMAT,
        handlers=handlers,
        force=True,
    )
