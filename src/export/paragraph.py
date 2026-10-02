# -*- coding: utf-8 -*-
"""转写文本段落化：把零散的逐句转写合并成段落式结构

参考飞书会议纪要的呈现方式：
- 连续的转写片段合并为一个段落，段首只标一次时间
- 遇到以下情况才开启新段落：
  1. PPT 翻页（换小节）
  2. 说话间隔超过 gap_sec（长停顿/提问切换）
  3. 段落长度超过 max_chars（避免超长段落）
"""
from __future__ import annotations

from dataclasses import dataclass

from src.core.timeline import TimelineEvent, TranscriptEvent, SlideEvent


@dataclass
class TranscriptParagraph:
    """合并后的一个转写段落"""
    start_ts: float   # 段首时间（秒）
    end_ts: float     # 段尾时间（秒）
    text: str


def iter_grouped(events: list[TimelineEvent],
                 gap_sec: float = 6.0,
                 max_chars: int = 400):
    """按时间顺序遍历事件流，产出 ("slide", SlideEvent) / ("para", TranscriptParagraph)

    翻页事件原样透传（用于小节标题/插图），连续转写合并为段落。
    """
    cur: list | None = None  # [start, end, text]

    def finish(c: list) -> TranscriptParagraph:
        return TranscriptParagraph(start_ts=c[0], end_ts=c[1], text=c[2])

    for ev in sorted(events, key=lambda e: e.ts):
        if isinstance(ev, SlideEvent):
            if cur:
                yield "para", finish(cur)
                cur = None
            yield "slide", ev
        elif isinstance(ev, TranscriptEvent):
            t = ev.text.strip()
            if not t:
                continue
            if cur and (ev.ts - cur[1] > gap_sec
                        or len(cur[2]) + len(t) > max_chars):
                yield "para", finish(cur)
                cur = None
            if cur is None:
                cur = [ev.ts, ev.ts, t]
            else:
                cur[1] = ev.ts
                cur[2] += t  # 中文语句直接拼接（转写片段本就是连续语句）

    if cur:
        yield "para", finish(cur)
