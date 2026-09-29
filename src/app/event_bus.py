# -*- coding: utf-8 -*-
"""时间轴事件总线：工作线程 → UI 的线程安全信号通道"""
from PySide6.QtCore import QObject, Signal


class EventBus(QObject):
    """所有跨线程 UI 更新经由此对象发射 Qt 信号（emit 线程安全）"""

    # 转写事件：(时间文本, 文本)
    transcript_added = Signal(str, str)
    # 翻页截图事件：(图片绝对路径, 页码)
    slide_added = Signal(str, int)
    # 状态消息
    status = Signal(str)
    # 录音错误（致命）
    error = Signal(str)
    # 录制完成：会话根目录
    finished = Signal(str)
    # 音量电平 0~1
    level = Signal(float)
