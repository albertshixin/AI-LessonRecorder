# -*- coding: utf-8 -*-
"""实时转写调度：消费音频块 → VAD 静音分段 → 调用 ASR 引擎 → 发射 TranscriptEvent

分段策略（v1.2 修正 P0-1）：
- 原实现把 `_tail_silence` **直接赋值为** silence_sec（`self._tail_silence =
  self.silence_sec`），等价于"一个 1024 samples 音频块（64ms）静音即出段"，
  与需求 FR-02「检测到句尾静音 ≥0.8 秒即提交」严重不符。2 小时课程会退化为
  约 9000 次 ASR 调用 → CPU 打满、段落碎裂、字幕闪烁。
- 现改为**累积连续静音时长**（`_tail_silence += chunk_sec`），并配合三项增强：
  1) **pre-roll**：语音起始前的音频留在缓冲，确认有语音后才并入段首，避免切掉首字；
  2) **尾部静音裁剪**：送 ASR 前剪掉多余静音（保留 0.15s 过渡），省算力、减幻听；
  3) **背压自适应**：队列积压时自动放宽出段间隔（用更少更大的调用追平实时）。
"""
from __future__ import annotations

import threading
import time

import numpy as np

from src.core.audio.ring_buffer import RingBuffer, pcm_to_float
from src.core.timeline import TranscriptEvent
from src.utils.logger import logger

SAMPLE_RATE = 16000
SILENCE_RMS = 0.006                # 静音判定阈值（RMS）
TAIL_WINDOW_SEC = 0.03             # 判定"尾部是否静音"的分析窗口
KEEP_SILENCE_TAIL_SEC = 0.15      # 裁剪尾部静音时保留的过渡，避免切掉字尾
MIN_USEFUL_SEC = 0.2               # 裁剪后短于此长度视为无有效语音


class LiveTranscriber(threading.Thread):
    """准实时转写线程

    - 从 RingBuffer 消费 16k mono int16 音频块
    - 累积到「连续静音 ≥ silence_sec」或「段长 ≥ max_sec」时提交 ASR
    - 段时间戳取该段**有效语音**的起点（pre-roll 不计入起点）
    """

    def __init__(self, ring: RingBuffer, engine, on_transcript,
                 silence_sec: float = 0.8, min_segment_sec: float = 1.5,
                 max_segment_sec: float = 15.0, language: str = "zh",
                 pre_roll_sec: float = 0.3,
                 max_backlog_sec: float = 30.0) -> None:
        super().__init__(daemon=True, name="LiveTranscriber")
        self.ring = ring
        self.engine = engine
        self.on_transcript = on_transcript
        self.silence_sec = max(0.2, float(silence_sec))
        self.min_sec = max(0.2, float(min_segment_sec))
        self.max_sec = max(self.min_sec + 1.0, float(max_segment_sec))
        self.language = language
        self.pre_roll_sec = max(0.0, float(pre_roll_sec))
        self.max_backlog_sec = max(5.0, float(max_backlog_sec))

        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._buf: list[np.ndarray] = []        # 已确认语音的累积块
        self._buf_samples = 0                  # 段内有效语音样本数（不含 pre-roll）
        self._preroll: list[np.ndarray] = []    # pre-roll 环形缓冲
        self._pre_samples = 0
        self._seg_start_ts: float | None = None
        self._tail_silence = 0.0# **连续**静音时长（秒），累积而非赋值
        self._base_max_sec = self.max_sec

        # 运行时指标
        self._asr_calls = 0
        self._asr_seconds = 0.0
        self._segments_out = 0
        self._speech_sec = 0.0
        self._locked = threading.Lock()

    # ---------- 外部控制 ----------
    def stop(self) -> None:
        self._stop_evt.set()
        self.ring.close()

    @property
    def stats(self) -> dict:
        with self._locked:
            rt = round(self._asr_seconds / self._speech_sec, 3) if self._speech_sec > 1 else 0.0
            return {
                "asr_calls": self._asr_calls,
                "asr_seconds": round(self._asr_seconds, 2),
                "segments_out": self._segments_out,
                "speech_sec": round(self._speech_sec, 1),
                "realtime_factor": rt,
            }

    # ---------- 主循环 ----------
    def run(self) -> None:
        logger.info("实时转写线程启动")
        last_log = time.monotonic()
        while not self._stop_evt.is_set():
            item = self.ring.get(timeout=0.5)
            if item is None:
                if self.ring.closed:
                    break
                continue
            self._feed(item[0], pcm_to_float(item[1]))

            now = time.monotonic()
            if now - last_log >= 30:
                last_log = now
                self._log_progress()

        # 收尾：把 ring 中剩余音频与当前段全部处理掉，确保最后一句不丢
        for ts, pcm in self.ring.drain():
            self._feed(ts, pcm_to_float(pcm))
        self._flush(reason="退出")
        logger.info(f"实时转写线程已退出：{self.stats}")

    def _log_progress(self) -> None:
        st = self.ring.stats()
        if st["dropped"]:
            logger.warning(
                f"转写积压告警：已丢弃 {st['dropped']} 块音频"
                f"（约 {st['dropped'] * 1024 / SAMPLE_RATE:.1f} 秒），"
                f"ASR 速度跟不上录音速度 → 建议换更小的模型档位或改用云端 ASR")
        else:
            logger.info(f"转写运行正常：{self.stats}")

    # ---------- 分段核心 ----------
    def _feed(self, ts: float, audio: np.ndarray) -> None:
        """把一个音频块喂入分段器"""
        n = len(audio)
        if n == 0:
            return
        chunk_sec = n / SAMPLE_RATE

        win = min(n, max(1, int(TAIL_WINDOW_SEC * SAMPLE_RATE)))
        rms = float(np.sqrt(np.mean(audio[-win:] ** 2))) if win else 1.0
        is_speech = rms >= SILENCE_RMS

        with self._lock:
            if self._seg_start_ts is None:
                if is_speech:
                    # 语音起始：并入之前积累的 pre-roll（不计入起点与时长）
                    if self._preroll:
                        self._buf.extend(self._preroll)
                    self._preroll = []
                    self._pre_samples = 0
                    self._seg_start_ts = ts
                else:
                    self._push_preroll(audio)
                    return

            self._buf.append(audio)
            self._buf_samples += n
            self._speech_sec += chunk_sec

            # 关键修复：累积**连续**静音时长（仅在已处于语音段内时累积）
            if is_speech:
                self._tail_silence = 0.0
            else:
                self._tail_silence += chunk_sec

            dur = self._buf_samples / SAMPLE_RATE

            # 背压自适应：ASR 积压时放宽出段间隔（用更少更大的调用追平实时）
            pending_sec = self.ring.pending * 1024 / SAMPLE_RATE
            if pending_sec > self.max_backlog_sec:
                self.max_sec = min(self._base_max_sec * 3, self.max_sec * 1.25)
            elif pending_sec < self.max_backlog_sec / 3 and self.max_sec > self._base_max_sec:
                self.max_sec = max(self._base_max_sec, self.max_sec * 0.9)

            should_flush = (
                (self._tail_silence >= self.silence_sec and dur >= self.min_sec)
                or dur >= self.max_sec
            )

        if should_flush:
            self._flush(reason="静音/超时")

    def _push_preroll(self, audio: np.ndarray) -> None:
        """维护 pre-roll 环形缓冲（上限 pre_roll_sec）"""
        if self.pre_roll_sec <= 0:
            return
        limit = int(self.pre_roll_sec * SAMPLE_RATE)
        self._preroll.append(audio)
        self._pre_samples += len(audio)
        while self._pre_samples > limit and self._preroll:
            drop = self._preroll.pop(0)
            self._pre_samples -= len(drop)

    def _flush(self, reason: str) -> None:
        """切出当前段并提交 ASR 转写"""
        with self._lock:
            if self._buf_samples == 0 or self._seg_start_ts is None:
                self._reset_segment()
                return
            audio = np.concatenate(self._buf) if self._buf else np.zeros(0, dtype=np.float32)
            seg_ts = self._seg_start_ts
            tail_silence = self._tail_silence
            self._reset_segment()

        # 裁掉尾部多余静音（保留 KEEP_SILENCE_TAIL_SEC 过渡）
        trim = int(max(0.0, tail_silence - KEEP_SILENCE_TAIL_SEC) * SAMPLE_RATE)
        if trim > 0:
            if trim < len(audio):
                audio = audio[: len(audio) - trim]
            else:
                return  # 整段几乎都是静音
        if len(audio) < int(MIN_USEFUL_SEC * SAMPLE_RATE):
            return

        t0 = time.monotonic()
        try:
            text, conf = self.engine.transcribe_segment(audio, self.language)
        except Exception:  # noqa: BLE001
            logger.exception("ASR 转写异常")
            return

        with self._locked:
            self._asr_calls += 1
            self._asr_seconds += time.monotonic() - t0

        if not text:
            return
        with self._locked:
            self._segments_out += 1

        ev = TranscriptEvent(ts=seg_ts, text=text, conf=conf)
        logger.debug(f"[{seg_ts:.1f}s] {reason} {text}")
        if self.on_transcript:
            try:
                self.on_transcript(ev)
            except Exception:  # noqa: BLE001
                logger.exception("转写事件回调失败")

    def _reset_segment(self) -> None:
        self._buf = []
        self._buf_samples = 0
        self._seg_start_ts = None
        self._tail_silence = 0.0
        self._preroll = []
        self._pre_samples = 0