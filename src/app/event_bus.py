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
    # 录制状态变化："idle" | "recording" | "paused" | "finalizing"
    state_changed = Signal(str)
    # 录音错误（致命）
    error = Signal(str)
    # 录制完成：会话根目录
    finished = Signal(str)
    # 音量电平 0~1
    level = Signal(float)
    # 模型下载请求：(模型名) —— UI 应弹出进度对话框并启动下载
    model_download_required = Signal(str, int)
    # 模型下载进度：(描述, 已下载文件数, 总文件数)
    model_download_progress = Signal(str, int, int)
    # 模型下载结束：(模型名, 本地路径)
    model_download_finished = Signal(str, str)
    # 模型下载失败：(模型名, 错误信息)
    model_download_error = Signal(str, str)
