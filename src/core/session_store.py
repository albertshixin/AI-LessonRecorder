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
        """写会话元数据

        stats 为运行时指标（转写积压/丢块、ASR 实时率、翻页判据命中分布），
        用于事后排查"为什么这节课转写质量差"——没有这些数字只能靠猜。
        """
        meta = {
            "name": self.name,
            "created_at": self.created_at,
            "finished": kwargs.get("finished", False),
            "duration": kwargs.get("duration", 0.0),
            "transcript_count": kwargs.get("transcript_count", 0),
            "slide_count": kwargs.get("slide_count", 0),
            "config": kwargs.get("config", {}),
            "stats": kwargs.get("stats", {}),
        }
        # 原子写：先写临时文件再替换，避免断电产生半截 JSON
        tmp = self.meta_file.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        tmp.replace(self.meta_file)

    def read_meta(self) -> dict:
        try:
            with open(self.meta_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}


class SessionStore:
    """事件流落盘（线程安全，增量 jsonl）

    v1.2：改用**长驻文件句柄 + 每事件 flush**。
    原实现每个事件 open/append/close 一次——2 小时课程约 5000 次文件开关，
    既慢又增加断电时缓冲区丢失的风险。改为常驻句柄后：
    - 性能：消除重复 open/close；
    - 可靠性：每条事件 flush 到 OS，崩溃最多丢最后 1 条。
    """

    def __init__(self, paths: SessionPaths) -> None:
        self.paths = paths
        self._io_lock = threading.Lock()
        self._fh = None

    def _ensure_open(self):
        if self._fh is None or self._fh.closed:
            self._fh = open(self.paths.events_file, "a", encoding="utf-8",
                            buffering=1)
        return self._fh

    def append_event(self, event: TimelineEvent) -> None:
        if isinstance(event, TranscriptEvent):
            row = {"t": round(event.ts, 3), "type": "transcript",
                   "text": event.text, "conf": round(event.conf, 3)}
        elif isinstance(event, SlideEvent):
            row = {"t": round(event.ts, 3), "type": "slide",
                   "img": event.image, "idx": event.index,
                   "backtrack": event.is_backtrack}
        else:
            return
        with self._io_lock:
            try:
                fh = self._ensure_open()
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()          # 立即刷到 OS：崩溃时最多丢最后一条
            except Exception:  # noqa: BLE001
                logger.exception("事件落盘失败")
                # 落盘失败不能拖垮录制：关闭句柄下次重建，并上报警告
                self.close()
                raise

    def close(self) -> None:
        """关闭文件句柄（会话收尾时调用）"""
        with self._io_lock:
            if self._fh and not self._fh.closed:
                try:
                    self._fh.flush()
                    self._fh.close()
                except Exception:  # noqa: BLE001
                    logger.warning("关闭事件流失败", exc_info=True)
            self._fh = None

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


def _existing_session_names(output_root: Path) -> set[str]:
    """输出目录里已有会话的课程名集合（去掉时间戳前缀）"""
    if not output_root.exists():
        return set()
    return {session_name_of(d) for d in output_root.iterdir() if d.is_dir()}


def _next_free_name(existing: set[str], base: str) -> str:
    """同名课程顺延序号：首次用原名，再次录制为 名称-1、名称-2 …

    场景：录了一半停止，再点开始且没改课程名 —— 上一次的 audio.wav /
    transcript.md 已经存在，必须另起一份并编号，否则两份内容混在一起、
    也无法区分哪份是最新的。
    """
    if base not in existing:
        return base
    n = 1
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def create_session(output_root: Path, raw_name: str) -> SessionPaths:
    """创建新的会话目录：output/{时间}_{课程名}/（同名自动加 -1/-2 序号）"""
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    base = safe_name(raw_name)
    name = _next_free_name(_existing_session_names(output_root), base)
    root = output_root / f"{stamp}_{name}"
    # 极端情况：同一秒内同名启动（目录恰好同名）→ 继续顺延，绝不覆盖上一次的成果
    n = 1
    while root.exists():
        name = f"{base}-{n}"
        root = output_root / f"{stamp}_{name}"
        n += 1
    paths = SessionPaths(root=root, name=name)
    paths.ensure_dirs()
    if name != base:
        logger.info(f"课程名「{base}」已存在历史会话，本次记为「{name}」")
    logger.info(f"创建会话目录: {root}")
    return paths


def session_name_of(root: Path) -> str:
    """由会话目录名反推课程名（去掉时间戳前缀）"""
    return root.name.split("_", 2)[-1] if "_" in root.name else root.name


def scan_sessions(output_root: Path) -> list[SessionPaths]:
    """扫描历史会话（按创建时间倒序）"""
    result: list[SessionPaths] = []
    if not output_root.exists():
        return result
    for d in sorted(output_root.iterdir(), reverse=True):
        if d.is_dir() and (d / "events.jsonl").exists():
            result.append(SessionPaths(root=d, name=session_name_of(d)))
    return result
