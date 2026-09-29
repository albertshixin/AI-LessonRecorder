# -*- coding: utf-8 -*-
"""线程安全环形缓冲区：音频生产者(录音线程)与消费者(ASR线程)解耦"""
import threading
from collections import deque
from typing import Callable

import numpy as np


class RingBuffer:
    """带阻塞读取的音频块队列"""

    def __init__(self, max_chunks: int = 2048) -> None:
        self._buf: deque[tuple[float, bytes]] = deque(maxlen=max_chunks)
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._closed = False

    def put(self, ts: float, pcm: bytes) -> None:
        with self._not_empty:
            if self._closed:
                return
            self._buf.append((ts, pcm))
            self._not_empty.notify()

    def get(self, timeout: float = 1.0) -> tuple[float, bytes] | None:
        """阻塞取一块；超时或已关闭返回 None"""
        with self._not_empty:
            if not self._buf and not self._closed:
                self._not_empty.wait(timeout)
            if self._buf:
                return self._buf.popleft()
            return None

    def close(self) -> None:
        with self._not_empty:
            self._closed = True
            self._not_empty.notify_all()

    @property
    def closed(self) -> bool:
        return self._closed


def pcm_to_float(pcm: bytes) -> np.ndarray:
    """int16 PCM bytes → float32 [-1, 1]"""
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
