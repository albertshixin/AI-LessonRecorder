# -*- coding: utf-8 -*-
"""文件系统工具"""
import os
import re
from pathlib import Path

_SAFE_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_name(name: str, fallback: str = "课程") -> str:
    """将任意字符串转为安全的文件/目录名"""
    name = _SAFE_RE.sub("_", name).strip().strip("_")
    return name[:60] if name else fallback


def open_in_explorer(path: str) -> None:
    """在资源管理器中打开目录"""
    path = str(Path(path).resolve())
    if os.path.isdir(path):
        os.startfile(path)  # noqa: S606 (Windows only)
    elif os.path.isfile(path):
        os.startfile(os.path.dirname(path))  # noqa: S606
