# -*- coding: utf-8 -*-
"""会话管理器：录制会话的完整生命周期编排（核心调度）

v1.2 修复的可靠性缺陷：
- **P0-4 watcher 闭包竞态**：原 `_watch_recorder` 闭包引用 `self._recorder`（可变属性），
  停止后重开新会话时，旧 watcher 会 join **新** recorder 并可能误发 stop。
  现改为**绑定具体实例** + 会话代号校验。
- **启动失败回滚**：原实现启动失败（如 ASR 引擎初始化异常）时，
  已创建的会话目录、已start() 的线程不会被清理 → 残留空目录 + 幽灵线程。
  现改为统一 `abort_start()` 回滚。
- **stop 幂等**：原实现 stop 后 `_do_stop` 若异常，状态可能卡在 finalizing。
  现加超时兜底与最终保证置 idle。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from PySide6.QtCore import QObject

from src.ai.asr.asr_engine import create_asr_engine
from src.ai.asr.live_transcriber import LiveTranscriber
from src.ai.asr.model_manager import MODEL_SIZE_HINT_MB, ModelManager
from src.ai.llm.course_writer import CourseWriter
from src.app.event_bus import EventBus
from src.core.audio.loopback_recorder import LoopbackRecorder
from src.core.audio.ring_buffer import RingBuffer
from src.core.session_store import SessionPaths, SessionStore, create_session
from src.core.timeline import SlideEvent, Timeline, TimelineEvent, TranscriptEvent
from src.core.vision.slide_detector import SlideDetector
from src.export.course_exporter import export_course_docx
from src.export.md_builder import build_transcript_md
from src.export.word_exporter import export_transcript_docx
from src.utils.config import AppConfig
from src.utils.logger import logger

JOIN_TIMEOUT_SEC = 30


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
        self._ring: RingBuffer | None = None
        self._finalize_lock = threading.Lock()
        self._session_seq = 0        # 会话代号：用于 watcher 校验自己是否仍是当前会话
        self._stopping = False

    # ================= 录制控制 =================
    def start(self, session_name: str, model_path: str | None = None,
              monitor: int = 1) -> bool:
        # 自检/自动化探测模式的硬保险：绝不真的去录音和截屏。
        # （曾因 EXE 参数解析落空进入 GUI 分支，真的录了音并污染了输出目录。）
        if os.environ.get("LESSONRECORDER_SELFTEST") == "1":
            logger.warning("自检模式已禁用录制启动（LESSONRECORDER_SELFTEST=1）")
            return False
        if self.state != "idle":
            self.bus.status.emit("当前已在录制中")
            return False

        cfg = self.cfg
        asr_cfg = cfg.as_dict()["asr"]
        self._session_seq += 1
        seq = self._session_seq

        # 0) 模型就绪检查（本地引擎且模型缺失 → 通知 UI 走下载流程，本次不启动）
        if asr_cfg.get("engine", "local") == "local" and not model_path:
            model_name = asr_cfg.get("model", "small")
            if not ModelManager.is_downloaded(model_name):
                self.bus.status.emit(f"首次使用，正在准备语音识别模型 {model_name}...")
                self.bus.model_download_required.emit(model_name,
                                                      MODEL_SIZE_HINT_MB.get(model_name, 500))
                self._session_seq -= 1   # 未真正开始，回退代号
                return False
            model_path = ModelManager.local_path(model_name)

        # 1) 创建会话
        self.paths = create_session(cfg.output_root, session_name)
        self.store = SessionStore(self.paths)
        self.timeline = Timeline()
        self._stopping = False

        try:
            # 2) 音频：环回录音 + 环形缓冲
            ring = RingBuffer()
            self._ring = ring
            self._recorder = LoopbackRecorder(
                wav_path=self.paths.audio_file,
                on_chunk=lambda ts, pcm: ring.put(ts, pcm),
                on_level=lambda lvl: self.bus.level.emit(lvl),
                on_status=lambda msg: self.bus.status.emit(msg),
            )

            # 3) ASR
            engine = create_asr_engine(asr_cfg, model_path=model_path)
            self._transcriber = LiveTranscriber(
                ring=ring, engine=engine,
                on_transcript=self._on_transcript,
                silence_sec=float(asr_cfg.get("silence_sec", 0.8)),
                min_segment_sec=float(asr_cfg.get("min_segment_sec", 1.5)),
                max_segment_sec=float(asr_cfg.get("max_segment_sec", 15.0)),
                language=asr_cfg.get("language", "zh"),
                pre_roll_sec=float(asr_cfg.get("pre_roll_sec", 0.3)),
            )

            # 4) 视觉：翻页检测
            v = cfg.as_dict()["vision"]
            region = None
            if v.get("capture") == "region":
                r = v.get("region", {})
                region = (int(r.get("x", 0)), int(r.get("y", 0)),
                          int(r.get("w", 1920)), int(r.get("h", 1080)))
            roi = v.get("roi") or None
            self._detector = SlideDetector(
                shots_dir=self.paths.screenshots_dir,
                on_slide=self._on_slide,
                interval_sec=float(v.get("interval_sec", 1.5)),
                phash_threshold=float(v.get("phash_threshold", 0.15)),
                backtrack_tol=float(v.get("backtrack_tol", 0.08)),
                cooldown_sec=float(v.get("cooldown_sec", 3.0)),
                min_diff_ratio=float(v.get("min_diff_ratio", 0.02)),
                ssim_threshold=float(v.get("ssim_threshold", 0.95)),
                capture=v.get("capture", "primary"),
                region=region,
                monitor=int(monitor),
                roi=roi,
                get_time=lambda: self._recorder.elapsed if self._recorder else 0.0,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("录制启动失败")
            self.abort_start()
            self.bus.error.emit(f"启动录制失败：{e}")
            return False

        # 5) 启动线程
        self._recorder.start()
        self._transcriber.start()
        self._detector.start()

        # 6) 录音线程守护（**绑定实例 + 会话代号**，避免旧 watcher 误操作新会话）
        recorder = self._recorder

        def _watch_recorder() -> None:
            recorder.join()
            if seq != self._session_seq:
                return  # 已经不是当前会话，忽略
            if recorder.error and self.state == "recording":
                self.bus.error.emit(recorder.error)
                self.stop()

        threading.Thread(target=_watch_recorder, daemon=True,
                         name="RecorderWatch").start()

        self._set_state("recording")
        self.paths.write_meta(finished=False, config=cfg.as_dict())
        self.bus.status.emit(f"开始录制：{self.paths.name}")
        logger.info(f"会话开始: {self.paths.root}")
        return True

    def abort_start(self) -> None:
        """启动失败回滚：停掉半启动的线程，删除空会话目录"""
        for t in (self._detector, self._transcriber, self._recorder):
            try:
                if t:
                    t.stop()
            except Exception:  # noqa: BLE001
                pass
        for t in (self._detector, self._transcriber, self._recorder):
            try:
                if t and t.is_alive():
                    t.join(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        if self._ring:
            self._ring.close()
        if self.store:
            self.store.close()

        root = self.paths.root if self.paths else None
        self._recorder = self._transcriber = self._detector = None
        self._ring = None
        self.store = None
        self.timeline = Timeline()
        self._set_state("idle")
        # 删除没有任何事件的空目录（仅在无事件文件时删，保护已有数据）
        if root and root.exists():
            try:
                if not (root / "events.jsonl").exists():
                    import shutil
                    shutil.rmtree(root, ignore_errors=True)
                    logger.info(f"已清理启动失败产生的空会话目录: {root}")
            except Exception:  # noqa: BLE001
                logger.warning(f"清理空会话目录失败: {root}", exc_info=True)
        self.paths = None

    # ---------- 暂停 / 恢复 ----------
    def pause(self) -> None:
        if self.state == "recording":
            if self._recorder:
                self._recorder.pause()
            if self._detector:
                self._detector.pause()
            self._set_state("paused")
            self.bus.status.emit("已暂停")

    def resume(self) -> None:
        if self.state == "paused":
            if self._recorder:
                self._recorder.resume()
            if self._detector:
                self._detector.resume()
            self._set_state("recording")
            self.bus.status.emit("继续录制")

    def toggle_pause(self) -> None:
        if self.state == "recording":
            self.pause()
        elif self.state == "paused":
            self.resume()

    def stop(self) -> None:
        """停止录制：UI 立刻进入 finalizing；收尾工作后台线程执行"""
        with self._finalize_lock:
            if self.state in ("idle", "finalizing") or self._stopping:
                return
            self._stopping = True
            self._set_state("finalizing")
        self.bus.status.emit("正在停止并生成逐字稿 ...")
        threading.Thread(target=self._do_stop, daemon=True,
                         name="StopWorker").start()

    def _do_stop(self) -> None:
        """收尾：停线程 → join → 写文件 → 发 finished 信号（异常不影响置 idle）"""
        try:
            for t in (self._recorder, self._detector):
                if t:
                    t.stop()
            if self._transcriber:
                self._transcriber.stop()
            for name, t in (("recorder", self._recorder),
                            ("detector", self._detector),
                            ("transcriber", self._transcriber)):
                if t:
                    t.join(timeout=JOIN_TIMEOUT_SEC)
                    if t.is_alive():
                        logger.warning(f"{name} 线程在 {JOIN_TIMEOUT_SEC}s 内未退出（继续收尾）")

            self._build_transcript()
            t_count, s_count = self.timeline.counts()
            duration = max(self._recorder.elapsed if self._recorder else 0.0,
                           self.timeline.duration())
            stats = {
                "asr": self._transcriber.stats if self._transcriber else {},
                "audio_queue": self._ring.stats() if self._ring else {},
                "vision": getattr(self._detector, "stats_counter", {}),
            }
            # 先关闭事件流再写元数据，确保 jsonl 已完整刷盘
            if self.store:
                self.store.close()
            self.paths.write_meta(
                finished=True, duration=round(duration, 1),
                transcript_count=t_count, slide_count=s_count,
                config=self.cfg.as_dict(), stats=stats)
        except Exception:  # noqa: BLE001
            logger.exception("生成逐字稿失败")
        finally:
            # 无论成败必须回到 idle，否则 UI 卡在 finalizing 无法再次录制
            self._stopping = False
            self._set_state("idle")
            self.bus.finished.emit(str(self.paths.root) if self.paths else "")
            self.bus.status.emit("录制完成")
            logger.info(f"会话结束: {self.paths.root if self.paths else '-'}")

    # ================= 只读属性（供 UI 轮询） =================
    @property
    def elapsed(self) -> float:
        """已录制时长（秒）；无录制时为 0"""
        rec = self._recorder
        return rec.elapsed if rec else 0.0

    @property
    def is_busy(self) -> bool:
        return self.state in ("recording", "paused", "finalizing")

    # ================= 状态变更 =================
    def _set_state(self, new_state: str) -> None:
        if self.state == new_state:
            return
        self.state = new_state
        self.bus.state_changed.emit(new_state)

    # ================= 事件回调（工作线程） =================
    def _on_transcript(self, ev: TranscriptEvent) -> None:
        self.timeline.add(ev)
        if self.store:
            self.store.append_event(ev)
        self.bus.transcript_added.emit(f"[{_mmss(ev.ts)}]", ev.text)

    def _on_slide(self, ev: SlideEvent) -> None:
        self.timeline.add(ev)
        if self.store:
            self.store.append_event(ev)
        if self.paths:
            self.bus.slide_added.emit(str(self.paths.root / ev.image), ev.index)

    # ================= 导出动作 =================
    def _build_transcript(self) -> Path:
        events = self.timeline.events_sorted()
        md = build_transcript_md(self.paths.name, events)
        self.paths.transcript_md.write_text(md, encoding="utf-8")
        return self.paths.transcript_md

    def force_capture(self) -> bool:
        """手动截图（线程安全：仅投递命令给检测线程）"""
        if self._detector and self._detector.is_alive():
            self._detector.request_capture()
            return True
        return False

    # ---------- 历史会话操作（静态，供 UI 直接调用）----------
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
        md_text = "\n".join(line for line in md_text.splitlines() if line.strip()) + "\n"
        paths.course_md.write_text(md_text, encoding="utf-8")
        docx = export_course_docx(root, md_text, slides)
        return paths.course_md, docx


def _mmss(ts: float) -> str:
    m, s = divmod(max(0, int(ts)), 60)
    return f"{m:02d}:{s:02d}"


def _session_name(root: Path) -> str:
    return root.name.split("_", 2)[-1] if "_" in root.name else root.name