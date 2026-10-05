# -*- coding: utf-8 -*-
"""离线重转写：用 audio.wav 重新转写整个课程，产出高精度逐字稿

**为什么必须有这个模块？**
需求 NFR-02 写的是"medium 模型中文字准确率 ≥90%"，但同时 NFR-01 要求
"2 小时课程全程录制不丢帧、不死锁"。这两条在纯实时架构下**不可同时满足**：
- faster-whisper medium int8 在 CPU 上实时率约 0.3~0.5x，
  即2 小时课程要跑4~6 小时，实时字幕不可能跟得上；
- 实时模式为保实时性必须用 small/甚至 base，��准确率达不到 90%。

因此 v1.2 改为**双档策略**（与需求文档 §7 的修订一致）：
- **录制时（实时档）**：small/base + 分段静音出字→ 保证"不丢帧、字幕跟得上"，
  用于**边听边看**的即时回顾；
- **课后（精度档）**：用large-v3/medium 对 audio.wav 离线重转写，
  可用 GPU、可用 beam_size=5+ 更大窗口 → 准确率显著提升，用于**最终交付的逐字稿**。

工程要点：
- **不覆盖原文件**：原 events.jsonl 备份为 events.jsonl.bak，
  新逐字稿写入 events.jsonl，失败可一键回滚。
- **保留截图事件**：只替换 transcript 事件，slide 事件原样保留（截图不重录）。
- **可中断**：长课程耗时数小时，支持回调式进度与取消。
- **复用同一 ASR 引擎抽象**：本地/云端都支持，不为离线单独写一套。
"""
from __future__ import annotations

import json
import shutil
import threading
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from src.ai.asr.asr_engine import create_asr_engine
from src.core.session_store import SessionPaths, SessionStore
from src.core.timeline import SlideEvent, TranscriptEvent
from src.utils.logger import logger

SAMPLE_RATE = 16000
ProgressCB = Callable[[str, float], None]


# ─────────────────────────────────────────────────────────────────────
#  音频读取与分段
# ─────────────────────────────────────────────────────────────────────
def read_wav_mono16k(path: Path) -> np.ndarray:
    """读取 16k mono int16 WAV → float32 [-1,1]"""
    with wave.open(str(path), "rb") as wf:
        if wf.getframerate() != SAMPLE_RATE or wf.getnchannels() != 1:
            raise ValueError(
                f"音频格式不符：期望 16kHz/mono，实际 {wf.getframerate()}Hz/"
                f"{wf.getnchannels()}ch（audio.wav 应由本程序录制，一般无需转换）")
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def split_by_silence(audio: np.ndarray, max_sec: float = 30.0,
                     silence_sec: float = 0.5,
                     min_sec: float = 1.0) -> list[tuple[float, np.ndarray]]:
    """按静音切分为若干 (起始秒, 音频) 段；超长段按 max_sec 硬切

    离线分段比实时宽松：max_sec 默认 30s（Whisper 在 30s 窗口内上下文最准），
    这样既减少调用次数，又比实时档得到更好的准确率。

    静音阈值采用 **噪声底 + 峰值区间**（而非单纯百分位）：
    单纯取 energy 的某个百分位作阈值，在稳态音源（如长纯音、匀速朗读）下会失效
    ——因为能量分布过于集中，百分位会落在语音能量本身，导致整段被判为静音。
    """
    win = int(0.02 * SAMPLE_RATE)              # 20ms 帧
    n_frames = len(audio) // win
    if n_frames == 0:
        return [(0.0, audio)] if len(audio) else []

    frames = audio[: n_frames * win].reshape(n_frames, win)
    energy = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)

    # 噪声底与峰值：噪声底用 10 百分位（稳健，不受少量爆音影响）
    noise_floor = float(np.percentile(energy, 10))
    peak = float(np.percentile(energy, 95))
    # 阈值 = 噪声底 + 区间30%，并设绝对下限防止全静音时误判为语音
    thresh = max(0.004, noise_floor + 0.30 * max(0.0, peak - noise_floor))
    voiced = energy > thresh

    # 全程无声（有效语音占比过低）→ 视为无语音，不产出段
    if voiced.mean() < 0.02:
        return []

    min_gap = max(1, int(silence_sec / 0.02))  # 连续静音帧数
    segments: list[tuple[float, np.ndarray]] = []
    seg_start_frame = 0
    i = 0
    while i < n_frames:
        if voiced[i]:
            i += 1
            continue
        # 找到一段静音的结束位置
        j = i
        while j < n_frames and not voiced[j]:
            j += 1
        gap = j - i
        cur_len_sec = (i - seg_start_frame) * 0.02
        # 静音足够长且当前段已达最小长度 → 切段
        if gap >= min_gap and cur_len_sec >= min_sec:
            end = int(i * win)
            if end > int(seg_start_frame * win):
                segments.append(((seg_start_frame * win) / SAMPLE_RATE,
                                 audio[int(seg_start_frame * win):end]))
            seg_start_frame = j
        i = j if j > i else i + 1

    # 收尾
    tail_start = int(seg_start_frame * win)
    if tail_start < len(audio):
        segments.append((tail_start / SAMPLE_RATE, audio[tail_start:]))

    # 超长段硬切（Whisper 单窗上限 30s，留 0.5s 余量）
    max_samples = int((max_sec - 0.5) * SAMPLE_RATE)
    final: list[tuple[float, np.ndarray]] = []
    for ts, seg in segments:
        if len(seg) <= max_samples:
            final.append((ts, seg))
            continue
        n_chunks = int(np.ceil(len(seg) / max_samples))
        per = len(seg) // n_chunks
        for k in range(n_chunks):
            s = k * per
            e = len(seg) if k == n_chunks - 1 else s + per
            final.append((ts + s / SAMPLE_RATE, seg[s:e]))
    # 丢弃几乎不含语音的段（切分边界容易产生这种残余）
    return [(ts, s) for ts, s in final
            if len(s) > int(0.3 * SAMPLE_RATE) and _voiced_ratio(s, win) > 0.05]


def _voiced_ratio(seg: np.ndarray, win: int) -> float:
    """段内有效语音帧占比（用于剔除纯静音残余段）"""
    n = len(seg) // win
    if n == 0:
        return 1.0
    energy = np.sqrt((seg[: n * win].reshape(n, win) ** 2).mean(axis=1) + 1e-12)
    peak = float(np.percentile(energy, 95))
    thresh = max(0.004, peak * 0.3)
    return float((energy > thresh).mean())


# ─────────────────────────────────────────────────────────────────────
#  离线重转写主流程
# ─────────────────────────────────────────────────────────────────────
class OfflineRetranscriber:
    """离线重转写器（可后台线程运行，支持取消）"""

    def __init__(self, asr_cfg: dict, model_path: str | None = None) -> None:
        self.asr_cfg = asr_cfg
        self.model_path = model_path
        self._cancel = threading.Event()
        self.engine = None

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self, session_root: Path, model_size: str | None = None,
            on_progress: ProgressCB | None = None,
            max_sec: float = 30.0) -> dict:
        """执行离线重转写。

        返回 {"segments": n, "chars": n, "elapsed_sec": x, "cancelled": bool}
        副作用：重写 events.jsonl 与 transcript.md（保留 slide 事件）。
        """
        import time as _time

        root = Path(session_root)
        paths = SessionPaths(root=root, name=_session_name(root))
        wav = paths.audio_file
        if not wav.exists():
            raise FileNotFoundError(f"未找到音频存档：{wav}")

        def prog(msg: str, pct: float) -> None:
            if on_progress:
                try:
                    on_progress(msg, max(0.0, min(1.0, pct)))
                except Exception:  # noqa: BLE001
                    logger.debug("进度回调异常", exc_info=True)

        prog("正在读取音频...", 0.0)
        audio = read_wav_mono16k(wav)
        duration = len(audio) / SAMPLE_RATE
        prog(f"音频 {duration / 60:.1f} 分钟，正在按静音分段...", 0.02)
        segments = split_by_silence(audio, max_sec=max_sec)
        total = len(segments)
        if total == 0:
            raise ValueError("音频为空或过短，无法转写")
        logger.info(f"离线重转写：{duration:.0f}s 音频切成 {total} 段")

        # 构造引擎（优先用指定模型档位）
        cfg = dict(self.asr_cfg)
        cfg["engine"] = "local"
        cfg["model"] = model_size or cfg.get("model", "small")
        prog(f"正在加载模型 {cfg['model']}（首次可能需要数分钟）...", 0.04)
        t_load = _time.monotonic()
        self.engine = create_asr_engine(cfg, model_path=self.model_path)
        logger.info(f"模型加载耗时 {_time.monotonic() - t_load:.1f}s")

        language = cfg.get("language", "zh")
        # 保留原有 slide 事件（截图不重录）
        store = SessionStore(paths)
        old_events = store.load_events()
        store.close()
        slides = [e for e in old_events if isinstance(e, SlideEvent)]

        transcripts: list[TranscriptEvent] = []
        t0 = _time.monotonic()
        chars = 0
        for i, (ts, seg) in enumerate(segments, 1):
            if self._cancel.is_set():
                prog("已取消", 0.0)
                return {"segments": 0, "chars": 0, "elapsed_sec": 0.0,
                        "cancelled": True}
            try:
                text, conf = self.engine.transcribe_segment(seg, language)
            except Exception:  # noqa: BLE001
                logger.exception(f"离线转写第 {i}/{total} 段失败，跳过")
                continue
            if text:
                transcripts.append(TranscriptEvent(ts=ts, text=text, conf=conf))
                chars += len(text)
            # 0.05~0.95 映射到进度区间
            prog(f"转写中 {i}/{total} 段（已得 {chars} 字）",
                 0.05 + 0.9 * i / total)

        elapsed = _time.monotonic() - t0
        if not transcripts:
            raise RuntimeError("离线转写未产出任何文字（请检查模型与音频）")

        # 合并事件：slide 保持原样，transcript 用新结果
        merged = sorted(slides + transcripts, key=lambda e: e.ts)

        # 备份原事件流后重写（失败可回滚）
        events_file = paths.events_file
        if events_file.exists():
            shutil.copy2(events_file, events_file.with_suffix(".jsonl.bak"))
        with open(events_file, "w", encoding="utf-8") as f:
            for ev in merged:
                if isinstance(ev, TranscriptEvent):
                    row = {"t": round(ev.ts, 3), "type": "transcript",
                           "text": ev.text, "conf": round(ev.conf, 3)}
                else:
                    row = {"t": round(ev.ts, 3), "type": "slide",
                           "img": ev.image, "idx": ev.index,
                           "backtrack": ev.is_backtrack}
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

        # 重建逐字稿 MD
        from src.export.md_builder import build_transcript_md
        md = build_transcript_md(paths.name, merged)
        paths.transcript_md.write_text(md, encoding="utf-8")

        prog(f"完成：{len(transcripts)} 段 / {chars} 字，耗时 {elapsed / 60:.1f} 分钟", 1.0)
        logger.info(f"离线重转写完成：{len(transcripts)} 段 {chars} 字，"
                    f"耗时 {elapsed / 60:.1f} 分钟（音频 {duration / 60:.1f} 分钟，"
                    f"速度 {duration / max(1.0, elapsed):.2f}x 实时）")

        return {
            "segments": len(transcripts),
            "chars": chars,
            "elapsed_sec": round(elapsed, 1),
            "cancelled": False,
            "speed_x": round(duration / max(1.0, elapsed), 2),
        }


def _session_name(root: Path) -> str:
    return root.name.split("_", 2)[-1] if "_" in root.name else root.name