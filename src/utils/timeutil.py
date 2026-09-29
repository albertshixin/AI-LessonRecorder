# -*- coding: utf-8 -*-
"""时间处理工具"""


def fmt_ts(seconds: float) -> str:
    """录制相对时间 → [HH:MM:SS]"""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_ts_compact(seconds: float) -> str:
    """录制相对时间 → HHMMSS（用于文件名）"""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}{m:02d}{s:02d}"


def fmt_ts_ms(seconds: float) -> str:
    """含毫秒：HH:MM:SS.mmm"""
    ms = int(round((seconds - int(seconds)) * 1000))
    ms = max(0, min(999, ms))
    return f"{fmt_ts(seconds)}.{ms:03d}"
