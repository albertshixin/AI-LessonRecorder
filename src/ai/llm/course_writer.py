# -*- coding: utf-8 -*-
"""AI 教程文档生成：Map-Reduce 分治 + 截图占位标记"""
from typing import Callable

from src.ai.llm.llm_client import LLMClient
from src.ai.llm.prompts import SYSTEM_PROMPT, MAP_PROMPT, REDUCE_PROMPT
from src.core.timeline import TimelineEvent, TranscriptEvent, SlideEvent
from src.utils.logger import logger
from src.utils.timeutil import fmt_ts


class CourseWriter:
    def __init__(self, cfg: dict) -> None:
        self.client = LLMClient(cfg)
        self.max_chunk_chars = int(cfg.get("max_chunk_chars", 3000))

    # ---------- 主入口 ----------
    def generate(self, events: list[TimelineEvent], course_name: str,
                 on_progress: Callable[[str, float], None] | None = None) -> str:
        """生成教程文档 Markdown，返回文本（含 {{slide:N}} 占位符）"""
        if not self.client.configured:
            raise RuntimeError("LLM 未配置：请在设置中填写 Base URL 与 API Key")

        sections = self._build_sections(events)
        if not sections:
            raise RuntimeError("没有可用的逐字稿内容")

        # Map：分段提炼
        chunks = self._chunk_sections(sections)
        mapped: list[str] = []
        for i, chunk in enumerate(chunks):
            if on_progress:
                on_progress(f"AI 梳理分段 {i + 1}/{len(chunks)} ...", i / max(1, len(chunks)) * 0.7)
            logger.info(f"Map 阶段 {i + 1}/{len(chunks)}")
            mapped.append(self.client.chat(
                MAP_PROMPT.format(segment=chunk), system=SYSTEM_PROMPT))

        # Reduce：全文编排
        if on_progress:
            on_progress("AI 正在编排完整教程文档 ...", 0.8)
        slide_list = self._slide_list(events)
        final = self.client.chat(REDUCE_PROMPT.format(
            course_name=course_name,
            mapped_notes="\n\n---\n\n".join(mapped),
            slide_list=slide_list or "（无截图）",
        ), system=SYSTEM_PROMPT)
        if on_progress:
            on_progress("完成", 1.0)
        return final

    # ---------- 内部 ----------
    def _build_sections(self, events: list[TimelineEvent]) -> list[dict]:
        """按翻页事件切分：[{slide, ts, text}]"""
        sections: list[dict] = []
        cur: dict = {"slide": 0, "ts": 0.0, "parts": []}
        for ev in sorted(events, key=lambda e: e.ts):
            if isinstance(ev, SlideEvent):
                if cur["parts"]:
                    sections.append(cur)
                cur = {"slide": ev.index, "ts": ev.ts, "parts": []}
            elif isinstance(ev, TranscriptEvent) and ev.text.strip():
                cur["parts"].append(ev.text.strip())
        if cur["parts"]:
            sections.append(cur)
        return sections

    def _chunk_sections(self, sections: list[dict]) -> list[str]:
        """把分节文本拼成 ≤ max_chunk_chars 的块，边界不跨越分节"""
        chunks: list[str] = []
        buf: list[str] = []
        size = 0
        for sec in sections:
            header = f"【第{sec['slide']}页】（{fmt_ts(sec['ts'])}）\n"
            body = header + "\n".join(sec["parts"]) + "\n\n"
            if size + len(body) > self.max_chunk_chars and buf:
                chunks.append("".join(buf))
                buf, size = [], 0
            buf.append(body)
            size += len(body)
        if buf:
            chunks.append("".join(buf))
        return chunks

    @staticmethod
    def _slide_list(events: list[TimelineEvent]) -> str:
        rows = [f"- 第 {e.index} 页 → {fmt_ts(e.ts)}"
                for e in events if isinstance(e, SlideEvent)]
        return "\n".join(rows)
