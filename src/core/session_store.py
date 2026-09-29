# -*- coding: utf-8 -*-
"""会话持久化：会话目录结构管理、events.jsonl 增量落盘、元数据、崩溃恢复扫描"""
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.core.timeline import TimelineEvent, TranscriptEvent, SlideEvent
from src.utils.fsutil import safe_name
from src.utils.logger import logger


@dataclass
class SessionPaths:
    """一次录制会话的目录结构"""
    root: Path
    name: str
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # ---------- 子路径 ----------
    @property
    def events_file(self) -> Path: return self.root / "events.jsonl"

    @property
    def meta_file(self) -> Path: return self.root / "session.json"

    @property
    def screenshots_dir(self) -> Path: return self.root / "screenshots"

    @property
    def audio_file(self) -> Path: return self.root / "audio.wav"

    @property
    def transcript_md(self) -> Path: return self.root / "transcript.md"

    @property
    def transcript_docx(self) -> Path: return self.root / "transcript.docx"

    @property
    def course_md(self) -> Path: return self.root / "course_notes.md"

    @property
    def course_docx(self) -> Path: return self.root / "course_notes.docx"

    def ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 元数据 ----------
    def write_meta(self, **kwargs) -> None:
        meta = {
            "name": self.name,
            "created_at": self.created_at,
            "finished": kwargs.get("finished", False),
            "duration": kwargs.get("duration", 0.0),
            "transcript_count": kwargs.get("transcript_count", 0),
            "slide_count": kwargs.get("slide_count", 0),
            "config": kwargs.get("config", {}),
        }
        with open(self.meta_file, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    def read_meta(self) -> dict:
        try:
            with open(self.meta_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}


class SessionStore:
    """事件流落盘（线程安全，增量 jsonl）"""

    def __init__(self, paths: SessionPaths) -> None:
        self.paths = paths
        self._io_lock = threading.Lock()

    def append_event(self, event: TimelineEvent) -> None:
        with self._io_lock:
            with open(self.paths.events_file, "a", encoding="utf-8") as f:
                if isinstance(event, TranscriptEvent):
                    row = {"t": round(event.ts, 3), "type": "transcript",
                           "text": event.text, "conf": round(event.conf, 3)}
                elif isinstance(event, SlideEvent):
                    row = {"t": round(event.ts, 3), "type": "slide",
                           "img": event.image, "idx": event.index,
                           "backtrack": event.is_backtrack}
                else:
                    return
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def load_events(self) -> list[TimelineEvent]:
        """从 events.jsonl 重建时间轴（崩溃恢复）"""
        events: list[TimelineEvent] = []
        p = self.paths.events_file
        if not p.exists():
            return events
        with open(p, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(f"events.jsonl 第 {line_no} 行解析失败，已跳过")
                    continue
                if row.get("type") == "transcript":
                    events.append(TranscriptEvent(ts=row["t"], text=row.get("text", ""),
                                                  conf=row.get("conf", 1.0)))
                elif row.get("type") == "slide":
                    events.append(SlideEvent(ts=row["t"], image=row.get("img", ""),
                                             index=row.get("idx", 0),
                                             is_backtrack=row.get("backtrack", False)))
        return events


def create_session(output_root: Path, raw_name: str) -> SessionPaths:
    """创建新的会话目录：output/{时间}_{课程名}/"""
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    root = output_root / f"{stamp}_{safe_name(raw_name)}"
    paths = SessionPaths(root=root, name=safe_name(raw_name))
    paths.ensure_dirs()
    logger.info(f"创建会话目录: {root}")
    return paths


def scan_sessions(output_root: Path) -> list[SessionPaths]:
    """扫描历史会话（按创建时间倒序）"""
    result: list[SessionPaths] = []
    if not output_root.exists():
        return result
    for d in sorted(output_root.iterdir(), reverse=True):
        if d.is_dir() and (d / "events.jsonl").exists():
            name = d.name.split("_", 2)[-1] if "_" in d.name else d.name
            result.append(SessionPaths(root=d, name=name))
    return result
