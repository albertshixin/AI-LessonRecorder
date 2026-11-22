# -*- coding: utf-8 -*-
"""文件系统工具"""
import os
import re
import subprocess
from pathlib import Path

from src.utils.logger import logger

_SAFE_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_name(name: str, fallback: str = "课程") -> str:
    """将任意字符串转为安全的文件/目录名"""
    name = _SAFE_RE.sub("_", name).strip().strip("_")
    return name[:60] if name else fallback


def open_in_explorer(path: str) -> bool:
    """在资源管理器中打开目录（不存在/无权限时返回 False，不抛异常）

    自动打开场景下（录制/导出完成）路径可能尚未落盘、用户中途移动了目录，
    把异常吃掉比让主窗口弹错误更友好；返回 bool 方便调用方记录日志。
    """
    if not path or not str(path).strip():
        logger.warning("open_in_explorer: 路径为空")
        return False
    try:
        p = Path(path).resolve()
    except (OSError, ValueError) as e:
        logger.warning(f"open_in_explorer: 路径无效 {path!r}: {e}")
        return False
    if not p.exists():
        logger.warning(f"open_in_explorer: 目标不存在 {p}")
        return False
    target = p if p.is_dir() else p.parent
    try:
        os.startfile(str(target))  # noqa: S606 (Windows only)
        return True
    except OSError as e:
        # 极少数情况：目录存在但权限不足/无关联程序（不应发生在 .exe 资源管理器上）
        logger.warning(f"open_in_explorer 失败 {target}: {e}")
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning(f"open_in_explorer 异常 {target}: {e}")
        return False


def reveal_in_explorer(path: str) -> bool:
    """在资源管理器中**选中**文件/目录（不打开），仅 Windows"""
    try:
        p = Path(path).resolve()
        if not p.exists():
            return False
        # /select, 参数必须用 Windows 路径分隔符
        subprocess.Popen(["explorer", "/select,", str(p).replace("/", "\\")])
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"reveal_in_explorer 失败 {path}: {e}")
        return False
