"""
日志模块：统一日志输出到控制台和文件。
"""

import logging
import sys
from pathlib import Path


def setup_logger(
    name: str = "limbus_agent",
    level: str = "INFO",
    log_file: str | None = "logs/agent.log",
) -> logging.Logger:
    """初始化日志器（同时配置 root logger，确保所有子模块日志可见）"""
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 避免重复添加 handler
    if root_logger.handlers:
        return logging.getLogger(name)

    # 简化日志格式（级别 + 模块名 + 消息）
    fmt = logging.Formatter(
        "[%(levelname)-7s] %(name)-20s %(message)s",
        datefmt="%H:%M:%S",
    )

    # 控制台输出
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    root_logger.addHandler(console_handler)

    # 文件输出（保留完整时间戳）
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_fmt = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(file_fmt)
        root_logger.addHandler(file_handler)

    return logging.getLogger(name)
