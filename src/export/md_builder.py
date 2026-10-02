# -*- coding: utf-8 -*-
"""逐字稿 MD 合成：时间轴事件 → 图文混排 Markdown（段落式）"""
from src.core.timeline import TimelineEvent, SlideEvent
from src.export.paragraph import iter_grouped
from src.utils.timeutil import fmt_ts


def build_transcript_md(course_name: str, events: list[TimelineEvent]) -> str:
    """按时间轴合并文本与截图，生成段落式 transcript.md 内容

    连续转写合并为段落，段首只标一个时间（段落式，参考飞书会议纪要）。
    """
    lines: list[str] = []
    lines.append(f"# 课程逐字稿 - {course_name}")
    lines.append("")

    has_content = False

    for kind, ev in iter_grouped(events):
        if kind == "slide":
            title = f"第 {ev.index} 页"
            if ev.is_backtrack:
                title += "（翻回）"
            lines.append(f"## [{fmt_ts(ev.ts)}] {title}")
            lines.append("")
            lines.append(f"![PPT {fmt_ts(ev.ts)}]({ev.image})")
            lines.append("")
            has_content = True
        else:  # para
            if not has_content:
                lines.append("## [00:00:00] 开场")
                lines.append("")
                has_content = True
            lines.append(f"**[{fmt_ts(ev.start_ts)}]** {ev.text}")
            lines.append("")

    if not has_content:
        lines.append("（本次录制没有产生转写内容或截图）")
        lines.append("")
    return "\n".join(lines)
