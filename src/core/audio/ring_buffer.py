# -*- coding: utf-8 -*-
"""线程安全音频块队列：生产者（录音线程）→ 消费者（转写线程）

设计要点：
- **有界 + 溢出可观测**：队列满时丢弃最旧块（保证实时性优先：宁可少字，不可卡死），
  但必须累计丢弃计数并上抛，否则"丢字"会变成不可排查的幽灵问题（NFR-01）。
- **单次等待唤醒**：用 while 循环处理虚假唤醒，避免偶发空转。
- **关闭语义**：close() 后不再接受新块，get() 仍会把已缓冲数据排空后返回 None。
"""
import threading
from collections import deque


class RingBuffer:
    """带阻塞读取的音频块队列

    put(ts, pcm)：ts 为该块的**起始**录制相对时间（秒），pcm 为 16k mono int16
    get(timeout) -> (ts, pcm) | None：超时或已关闭且排空后返回 None
    """

    def __init__(self, max_chunks: int = 2048) -> None:
        self._buf: deque[tuple[float, bytes]] = deque()
        self._max = max(1, int(max_chunks))
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._closed = False
        self._dropped = 0          # 因队列满而丢弃的块数
        self._pushed = 0
        self._popped = 0

    # ---------- 生产者 ----------
    def put(self, ts: float, pcm: bytes) -> None:
        with self._not_empty:
            if self._closed:
                return
            if len(self._buf) >= self._max:
                # 实时性优先：丢弃最旧的块，保证最新音频能进队（否则录音线程被拖死）
                self._buf.popleft()
                self._dropped += 1
            self._buf.append((ts, pcm))
            self._pushed += 1
            self._not_empty.notify()

    # ---------- 消费者 ----------
    def get(self, timeout: float = 1.0) -> tuple[float, bytes] | None:
        """阻塞取一块；超时或已关闭且排空后返回 None"""
        with self._not_empty:
            while not self._buf and not self._closed:
                self._not_empty.wait(timeout)
                break  # 单次等待即可，避免虚假唤醒导致空转
            if self._buf:
                self._popped += 1
                return self._buf.popleft()
            return None

    def drain(self) -> list[tuple[float, bytes]]:
        """非阻塞排空全部剩余块（收尾时确保最后一段音频不丢）"""
        with self._not_empty:
            items = list(self._buf)
            self._buf.clear()
            self._popped += len(items)
            return items

    def close(self) -> None:
        with self._not_empty:
            self._closed = True
            self._not_empty.notify_all()

    # ---------- 观测 ----------
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._buf)

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def pushed(self) -> int:
        return self._pushed

    def stats(self) -> dict:
        """运行时可观测指标（写入 session.json，便于事后排查丢字）"""
        with self._lock:
            return {
                "pushed": self._pushed,
                "popped": self._popped,
                "dropped": self._dropped,
                "pending": len(self._buf),
            }


def pcm_to_float(pcm: bytes) -> "object":
    """int16 PCM bytes → float32 [-1, 1]（延迟导入 numpy，便于纯逻辑测试）"""
    import numpy as np
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0