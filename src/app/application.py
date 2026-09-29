# -*- coding: utf-8 -*-
"""应用上下文：全局配置与日志的单例持有者

路径策略：
- 开发环境（源码运行）：项目根目录
- 打包环境（PyInstaller EXE）：EXE 所在目录（config/logs/output 均在 EXE 旁边）
"""
import sys
from pathlib import Path

from src.utils.config import AppConfig
from src.utils.logger import setup_logger, logger


def _app_root() -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller 打包后
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]  # 源码运行：项目根目录


_ROOT = _app_root()


class AppContext:
    _instance: "AppContext | None" = None

    def __init__(self) -> None:
        self.root = _ROOT
        setup_logger(str(_ROOT / "logs"))
        self.config = AppConfig(_ROOT / "config" / "settings.json")
        logger.info(f"应用上下文初始化完成，运行根目录: {_ROOT}"
                    f"（{'打包模式' if getattr(sys, 'frozen', False) else '开发模式'}）")

    @classmethod
    def instance(cls) -> "AppContext":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
