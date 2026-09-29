# -*- coding: utf-8 -*-
"""实时转写调度：消费音频块 → VAD 静音分段 → 调用 ASR 引擎 → 发射 TranscriptEvent"""
import threading

import numpy as np

from src.core.audio.ring_buffer import RingBuffer, pcm_to_float
from src.core.timeline import TranscriptEvent
from src.utils.logger import logger

SAMPLE_RATE = 16000
SILENCE_RMS = 0.008   # 静音判定阈值（RMS）


class LiveTranscriber(threading.Thread):
    """准实时转写线程

    - 从 RingBuffer 消费 16k mono int16 音频块
    - 累积到检测到句尾静音（或超长）时提交 ASR
    - 段时间戳取该段音频的起点
    """

    def __init__(self, ring: RingBuffer, engine, on_transcript,
                 silence_sec: float = 0.8, min_segment_sec: float = 1.5,
                 max_segment_sec: float = 30.0, language: str = "zh") -> None:
        super().__init__(daemon=True, name="LiveTranscriber")
        self.ring = ring
        self.engine = engine
        self.on_transcript = on_transcript
        self.silence_sec = silence_sec
        self.min_sec = min_segment_sec
        self.max_sec = max_segment_sec
        self.language = language

        self._stop_evt = threading.Event()
        self._buf: list[np.ndarray] = []      # float32 块
        self._buf_samples = 0
        self._seg_start_ts: float | None = None  # 段起点录制时间
        self._tail_silence = 0.0

    def stop(self) -> None:
        self._stop_evt.set()
        self.ring.close()

    # ---------- 主循环 ----------
    def run(self) -> None:
        logger.info("实时转写线程启动")
        while not self._stop_evt.is_set():
            item = self.ring.get(timeout=0.5)
            if item is None:
                if self.ring.closed:
                    break
                continue
            ts, pcm = item
            audio = pcm_to_float(pcm)
            n = len(audio)
            if n == 0:
                continue
            if self._seg_start_ts is None:
                self._seg_start_ts = ts

            self._buf.append(audio)
            self._buf_samples += n

            # 尾部静音检测
            tail_n = min(n, int(self.silence_sec * SAMPLE_RATE))
            rms = float(np.sqrt(np.mean(audio[-tail_n:] ** 2))) if tail_n else 1.0
            dur = self._buf_samples / SAMPLE_RATE
            if rms < SILENCE_RMS:
                self._tail_silence = self.silence_sec
            else:
                self._tail_silence = 0.0

            if (self._tail_silence >= self.silence_sec and dur >= self.min_sec) \
                    or dur >= self.max_sec:
                self._flush()

        self._flush()  # 退出前清空剩余
        logger.info("实时转写线程已退出")

    def _flush(self) -> None:
        if self._buf_samples == 0 or self._seg_start_ts is None:
            return
        audio = np.concatenate(self._buf)
        try:
            text, conf = self.engine.transcribe_segment(audio, self.language)
        except Exception:  # noqa: BLE001
            logger.exception("ASR 转写异常")
            text, conf = "", 0.0
        if text:
            ev = TranscriptEvent(ts=self._seg_start_ts, text=text, conf=conf)
            logger.debug(f"[{self._seg_start_ts:.1f}s] {text}")
            if self.on_transcript:
                self.on_transcript(ev)
        self._buf = []
        self._buf_samples = 0
        self._seg_start_ts = None
        self._tail_silence = 0.0
