"""统一日志工具。"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

_DEFAULT_FORMAT = "[%(asctime)s] %(levelname)-7s %(name)s | %(message)s"
_DEFAULT_DATEFMT = "%H:%M:%S"
_configured = False


def setup_logging(
    level: int = logging.INFO,
    log_file: Optional[str] = None,
    force: bool = False,
) -> None:
    """配置根 logger：控制台 +（可选）文件。重复调用默认幂等。"""
    global _configured
    root = logging.getLogger("uiagent")
    if _configured and not force:
        root.setLevel(level)
        return

    root.setLevel(level)
    root.handlers.clear()
    root.propagate = False

    # 日志走 stderr，保证 stdout 只承载结构化结果（JSON / 报告摘要）
    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(logging.Formatter(_DEFAULT_FORMAT, _DEFAULT_DATEFMT))
    root.addHandler(console)

    if log_file:
        directory = os.path.dirname(os.path.abspath(log_file))
        if directory:
            os.makedirs(directory, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
        )
        root.addHandler(file_handler)

    _configured = True


def get_logger(name: str = "uiagent") -> logging.Logger:
    """获取 uiagent 命名空间下的 logger。"""
    if not _configured:
        setup_logging()
    if name.startswith("uiagent"):
        return logging.getLogger(name)
    return logging.getLogger(f"uiagent.{name}")
