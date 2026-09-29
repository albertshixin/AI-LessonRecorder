# -*- coding: utf-8 -*-
"""逐字稿 MD 合成：时间轴事件 → 图文混排 Markdown"""
from src.core.timeline import TimelineEvent, TranscriptEvent, SlideEvent
from src.utils.timeutil import fmt_ts


def build_transcript_md(course_name: str, events: list[TimelineEvent]) -> str:
    """按时间轴合并文本与截图，生成 transcript.md 内容"""
    lines: list[str] = []
    lines.append(f"# 课程逐字稿 - {course_name}")
    lines.append("")

    sorted_events = sorted(events, key=lambda e: e.ts)
    slide_no = 0
    has_content = False

    for ev in sorted_events:
        if isinstance(ev, SlideEvent):
            slide_no += 1  # 展示序号（含翻回）
            title = f"第 {ev.index} 页"
            if ev.is_backtrack:
                title += "（翻回）"
            lines.append(f"## [{fmt_ts(ev.ts)}] {title}")
            lines.append("")
            lines.append(f"![PPT {fmt_ts(ev.ts)}]({ev.image})")
            lines.append("")
            has_content = True
        elif isinstance(ev, TranscriptEvent) and ev.text.strip():
            if not has_content:
                # 首个翻页事件之前的文字
                lines.append(f"## [00:00:00] 开场")
                lines.append("")
                has_content = True
            lines.append(f"**[{fmt_ts(ev.ts)}]** {ev.text.strip()}")
            lines.append("")

    if not has_content:
        lines.append("（本次录制没有产生转写内容或截图）")
        lines.append("")
    return "\n".join(lines)
