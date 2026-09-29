# -*- coding: utf-8 -*-
"""时间轴数据模型：统一汇聚转写事件与翻页截图事件"""
import threading
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class TranscriptEvent:
    """一段转写文本"""
    ts: float                 # 录制相对时间（秒）
    text: str
    conf: float = 1.0
    type: str = field(default="transcript", init=False)


@dataclass
class SlideEvent:
    """一次 PPT 翻页截图"""
    ts: float                 # 录制相对时间（秒）
    image: str                # 相对会话目录的路径，如 screenshots/000012_slide_001.png
    index: int                # 第几页（从 1 开始）
    is_backtrack: bool = False   # 是否为"翻回上一页"
    type: str = field(default="slide", init=False)


TimelineEvent = TranscriptEvent | SlideEvent


class Timeline:
    """线程安全的时间轴事件容器（内存 + 排序访问）"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: list[TimelineEvent] = []

    def add(self, event: TimelineEvent) -> None:
        with self._lock:
            self._events.append(event)

    def extend(self, events: Iterable[TimelineEvent]) -> None:
        with self._lock:
            self._events.extend(events)

    def events_sorted(self) -> list[TimelineEvent]:
        with self._lock:
            return sorted(self._events, key=lambda e: e.ts)

    def counts(self) -> tuple[int, int]:
        """返回 (转写段数, 截图数)"""
        with self._lock:
            t = sum(1 for e in self._events if isinstance(e, TranscriptEvent))
            s = sum(1 for e in self._events if isinstance(e, SlideEvent))
            return t, s

    def duration(self) -> float:
        with self._lock:
            return max((e.ts for e in self._events), default=0.0)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
