# -*- coding: utf-8 -*-
"""会话管理器：录制会话的完整生命周期编排（核心调度）"""
import threading
from pathlib import Path

from PySide6.QtCore import QObject

from src.app.event_bus import EventBus
from src.ai.asr.asr_engine import create_asr_engine
from src.ai.asr.live_transcriber import LiveTranscriber
from src.ai.llm.course_writer import CourseWriter
from src.core.audio.loopback_recorder import LoopbackRecorder
from src.core.audio.ring_buffer import RingBuffer
from src.core.session_store import SessionPaths, SessionStore, create_session
from src.core.timeline import Timeline, TimelineEvent, TranscriptEvent, SlideEvent
from src.core.vision.slide_detector import SlideDetector
from src.export.md_builder import build_transcript_md
from src.export.word_exporter import export_transcript_docx
from src.export.course_exporter import export_course_docx
from src.utils.config import AppConfig
from src.utils.logger import logger


class SessionManager(QObject):
    """状态机：idle → recording ⇄ paused → finalizing → idle"""

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self.bus = EventBus()

        self.state = "idle"
        self.paths: SessionPaths | None = None
        self.timeline = Timeline()
        self.store: SessionStore | None = None

        self._recorder: LoopbackRecorder | None = None
        self._transcriber: LiveTranscriber | None = None
        self._detector: SlideDetector | None = None
        self._finalize_lock = threading.Lock()

    # ================= 录制控制 =================
    def start(self, session_name: str) -> bool:
        if self.state != "idle":
            self.bus.status.emit("当前已在录制中")
            return False

        cfg = self.cfg
        # 创建会话
        self.paths = create_session(cfg.output_root, session_name)
        self.store = SessionStore(self.paths)
        self.timeline = Timeline()

        # 1) 音频：环回录音 + 环形缓冲
        ring = RingBuffer()
        self._recorder = LoopbackRecorder(
            wav_path=self.paths.audio_file,
            on_chunk=lambda ts, pcm: ring.put(ts, pcm),
        )

        # 2) ASR：实时转写
        try:
            engine = create_asr_engine(cfg.as_dict()["asr"])
        except Exception as e:  # noqa: BLE001
            self.bus.error.emit(f"ASR 引擎初始化失败: {e}")
            return False
        asr_cfg = cfg.as_dict()["asr"]
        self._transcriber = LiveTranscriber(
            ring=ring, engine=engine,
            on_transcript=self._on_transcript,
            silence_sec=float(asr_cfg.get("silence_sec", 0.8)),
            min_segment_sec=float(asr_cfg.get("min_segment_sec", 1.5)),
            max_segment_sec=float(asr_cfg.get("max_segment_sec", 30.0)),
            language=asr_cfg.get("language", "zh"),
        )

        # 3) 视觉：翻页检测
        v = cfg.as_dict()["vision"]
        region = (v["region"]["x"], v["region"]["y"], v["region"]["w"], v["region"]["h"]) \
            if v.get("capture") == "region" else None
        self._detector = SlideDetector(
            shots_dir=self.paths.screenshots_dir,
            on_slide=self._on_slide,
            interval_sec=float(v.get("interval_sec", 1.5)),
            phash_threshold=float(v.get("phash_threshold", 0.18)),
            backtrack_tol=float(v.get("backtrack_tol", 0.08)),
            cooldown_sec=float(v.get("cooldown_sec", 3.0)),
            capture=v.get("capture", "primary"),
            region=region,
            get_time=lambda: self._recorder.elapsed if self._recorder else 0.0,
        )

        # 启动线程
        self._recorder.start()
        self._transcriber.start()
        self._detector.start()

        # 监听录音错误
        def _watch_recorder():
            self._recorder.join()
            if self._recorder.error and self.state == "recording":
                self.bus.error.emit(self._recorder.error)
                self.stop()
        threading.Thread(target=_watch_recorder, daemon=True,
                         name="RecorderWatch").start()

        self.state = "recording"
        self.paths.write_meta(finished=False, config=cfg.as_dict())
        self.bus.status.emit(f"开始录制：{self.paths.name}")
        logger.info(f"会话开始: {self.paths.root}")
        return True

    def pause(self) -> None:
        if self.state == "recording":
            self._recorder.pause()
            self._detector.pause()
            self.state = "paused"
            self.bus.status.emit("已暂停")

    def resume(self) -> None:
        if self.state == "paused":
            self._recorder.resume()
            self._detector.resume()
            self.state = "recording"
            self.bus.status.emit("继续录制")

    def stop(self) -> None:
        with self._finalize_lock:
            if self.state in ("idle", "finalizing"):
                return
            self.state = "finalizing"
        self.bus.status.emit("正在停止并生成逐字稿 ...")

        for t in (self._recorder, self._detector):
            if t:
                t.stop()
        if self._transcriber:
            self._transcriber.stop()
        for t in (self._recorder, self._detector, self._transcriber):
            if t:
                t.join(timeout=30)

        try:
            self._build_transcript()
            t_count, s_count = self.timeline.counts()
            duration = max(self._recorder.elapsed if self._recorder else 0.0,
                           self.timeline.duration())
            self.paths.write_meta(finished=True, duration=round(duration, 1),
                                  transcript_count=t_count,
                                  slide_count=s_count,
                                  config=self.cfg.as_dict())
        except Exception:  # noqa: BLE001
            logger.exception("生成逐字稿失败")

        self.state = "idle"
        self.bus.finished.emit(str(self.paths.root))
        self.bus.status.emit("录制完成")
        logger.info(f"会话结束: {self.paths.root}")

    # ================= 事件回调（工作线程） =================
    def _on_transcript(self, ev: TranscriptEvent) -> None:
        self.timeline.add(ev)
        self.store.append_event(ev)
        self.bus.transcript_added.emit(f"[{int(ev.ts // 60):02d}:{int(ev.ts % 60):02d}]",
                                       ev.text)

    def _on_slide(self, ev: SlideEvent) -> None:
        self.timeline.add(ev)
        self.store.append_event(ev)
        self.bus.slide_added.emit(str(self.paths.root / ev.image), ev.index)

    # ================= 导出动作 =================
    def _build_transcript(self) -> Path:
        events = self.timeline.events_sorted()
        md = build_transcript_md(self.paths.name, events)
        self.paths.transcript_md.write_text(md, encoding="utf-8")
        return self.paths.transcript_md

    # ---------- 对历史会话的操作 ----------
    @staticmethod
    def rebuild_transcript(session_root: str) -> Path:
        """根据 events.jsonl 重建逐字稿 MD（崩溃恢复/重新导出）"""
        root = Path(session_root)
        paths = SessionPaths(root=root, name=_session_name(root))
        store = SessionStore(paths)
        events = store.load_events()
        md = build_transcript_md(paths.name, events)
        paths.transcript_md.write_text(md, encoding="utf-8")
        return paths.transcript_md

    @staticmethod
    def export_word(session_root: str, include_images: bool = True) -> Path:
        """导出逐字稿 Word"""
        root = Path(session_root)
        paths = SessionPaths(root=root, name=_session_name(root))
        events = SessionStore(paths).load_events()
        return export_transcript_docx(root, paths.name, events, include_images)

    @staticmethod
    def generate_course(session_root: str, llm_cfg: dict,
                        on_progress=None) -> tuple[Path, Path]:
        """生成 AI 教程文档（MD + Word）"""
        root = Path(session_root)
        paths = SessionPaths(root=root, name=_session_name(root))
        events = SessionStore(paths).load_events()
        slides = [e for e in events if isinstance(e, SlideEvent)]

        writer = CourseWriter(llm_cfg)
        md_text = writer.generate(events, paths.name, on_progress)
        # 清理占位符外的多余空白行
        md_text = "\n".join(line for line in md_text.splitlines() if line.strip()) + "\n"
        paths.course_md.write_text(md_text, encoding="utf-8")
        docx = export_course_docx(root, md_text, slides)
        return paths.course_md, docx


def _session_name(root: Path) -> str:
    return root.name.split("_", 2)[-1] if "_" in root.name else root.name
