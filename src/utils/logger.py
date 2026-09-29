# -*- coding: utf-8 -*-
"""日志工具：loguru 封装，写文件 + 控制台

注意：PyInstaller 打包的 GUI 程序（console=False）中 sys.stderr 为 None，
必须判空，否则启动即崩溃。
"""
import sys
from pathlib import Path

from loguru import logger

_initialized = False


def setup_logger(log_dir: str = "logs") -> None:
    """初始化日志系统（应用启动时调用一次）"""
    global _initialized
    if _initialized:
        return
    _initialized = True

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger.remove()
    # GUI 打包模式下 sys.stderr 可能为 None，此时跳过控制台输出
    if sys.stderr is not None:
        logger.add(sys.stderr, level="INFO",
                   format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}")
    logger.add(Path(log_dir) / "app_{time:YYYY-MM-DD}.log",
               rotation="1 day", retention="14 days",
               encoding="utf-8", level="DEBUG",
               format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | {name}:{function}:{line} | {message}")


__all__ = ["logger", "setup_logger"]
