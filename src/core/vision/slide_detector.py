# -*- coding: utf-8 -*-
"""PPT 翻页检测：定时抓屏 → 哈希比对 → 翻页截图 + 回退识别 + 冷却防抖"""
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

from src.core.vision.change_algo import bgra_to_gray, dhash, phash, hamming
from src.core.timeline import SlideEvent
from src.utils.logger import logger
from src.utils.timeutil import fmt_ts_compact


class SlideDetector(threading.Thread):
    """翻页检测线程

    on_slide(event: SlideEvent)：检测到翻页时回调（截图已保存）
    """

    def __init__(
        self,
        shots_dir: Path,
        on_slide,
        interval_sec: float = 1.5,
        phash_threshold: float = 0.18,
        backtrack_tol: float = 0.08,
        cooldown_sec: float = 3.0,
        capture: str = "primary",
        region=None,
        monitor: int = 1,
        get_time=None,   #Callable[[], float]：返回当前录制相对时间（与音频时间轴对齐）
    ) -> None:
        super().__init__(daemon=True, name="SlideDetector")
        from src.core.vision.screen_capturer import ScreenCapturer
        self.capturer = ScreenCapturer(capture, region, monitor=monitor)
        self.shots_dir = Path(shots_dir)
        self.on_slide = on_slide
        self.interval = interval_sec
        self.phash_threshold = phash_threshold   # 哈希距离超过此值判定为变化
        self.backtrack_tol = backtrack_tol       # 与历史帧距离小于此值判定为翻回
        self.cooldown = cooldown_sec
        self.get_time = get_time or (lambda: time.time())

        self._stop_evt = threading.Event()
        self._pause_evt = threading.Event()

        self._baseline: tuple[np.ndarray, np.ndarray] | None = None  # 当前页基准哈希
        self._history: list[tuple[np.ndarray, np.ndarray, int, str]] = []  # (d, p, idx, img)
        self._slide_count = 0
        self._last_trigger_t = -1e9          # 冷却计时（录制时间）
        self._armed = False                  # 首帧只建立基准不触发
        self._last_page_idx = -1             # 最近一次截图的页码（用于同页去重）
        self._last_page_ts = -1e9            # 最近一次截图时间
        self.same_page_dedup_sec = 30.0      # 同页重复触发去重窗口（秒）

    # ---------- 外部控制 ----------
    def stop(self) -> None:
        self._stop_evt.set()

    def pause(self) -> None:
        self._pause_evt.set()

    def resume(self) -> None:
        self._pause_evt.clear()

    @property
    def slide_count(self) -> int:
        return self._slide_count

    def force_capture(self) -> None:
        """手动强制截图（漏检补救，在检测线程外调用需持有 GIL，安全）"""
        self._capture_now(backtrack=False)

    # ---------- 主循环 ----------
    def run(self) -> None:
        logger.info("翻页检测线程启动")
        while not self._stop_evt.is_set():
            try:
                if not self._pause_evt.is_set():
                    self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("翻页检测异常")
            self._stop_evt.wait(self.interval)
        logger.info("翻页检测线程已退出")

    def _tick(self) -> None:
        small = self.capturer.grab_small(256)
        gray = bgra_to_gray(small)
        sig = (dhash(gray), phash(gray))
        rec_ts = self.get_time()

        if not self._armed:
            self._baseline = sig
            self._armed = True
            # 首页也截图（第 1 页）
            self._capture_now(backtrack=False, sig=sig)
            return

        d, p = sig
        bd, bp = self._baseline
        # 双哈希任一显著变化才判定可能翻页（粗筛+复核）
        if hamming(d, bd) >= self.phash_threshold or hamming(p, bp) >= self.phash_threshold:
            if rec_ts - self._last_trigger_t < self.cooldown:
                return  # 冷却期内忽略（过渡动画）
            # 是否翻回历史页
            hit = self._is_backtrack(sig)
            if hit:
                idx, _img = hit
                # 同页翻回去重：画面中的动态内容（讲师摄像头/动画）会导致
                # 基准哈希频繁失配，若短时间内反复"翻回"同一页，判定为误判
                if idx == self._last_page_idx \
                        and rec_ts - self._last_page_ts < self.same_page_dedup_sec:
                    self._baseline = sig   # 以当前画面为新基准，停止连续误触发
                    self._last_trigger_t = rec_ts
                    return
                self._capture_now(backtrack=True, sig=sig)
            else:
                # 与上一张截图内容几乎相同 → 动态画面噪声，不新增页
                if self._history:
                    ld, lp, _li, _im = self._history[-1]
                    if hamming(d, ld) <= self.backtrack_tol \
                            and hamming(p, lp) <= self.backtrack_tol:
                        self._baseline = sig
                        self._last_trigger_t = rec_ts
                        return
                self._capture_now(backtrack=False, sig=sig)

    def _is_backtrack(self, sig) -> tuple[int, str] | None:
        """匹配历史帧：返回 (idx, img) 或 None"""
        d, p = sig
        for hd, hp, idx, img in reversed(self._history):
            if hamming(d, hd) <= self.backtrack_tol and hamming(p, hp) <= self.backtrack_tol:
                return idx, img
        return None

    def _capture_now(self, backtrack: bool, sig=None) -> None:
        rec_ts = self.get_time()
        if backtrack:
            hit = self._is_backtrack(sig) if sig else self._is_backtrack(self._current_sig())
            if hit:
                idx, img = hit
                # 关键：以当前画面为新基准，否则动态内容会导致连续误触发
                self._baseline = sig if sig else self._current_sig()
                self._last_trigger_t = rec_ts
                self._last_page_idx = idx
                self._last_page_ts = rec_ts
                ev = SlideEvent(ts=rec_ts, image=img, index=idx, is_backtrack=True)
                logger.info(f"翻回第 {idx} 页 @{fmt_ts_compact(rec_ts)}")
                if self.on_slide:
                    self.on_slide(ev)
                return
            # 回退匹配失败 → 当作新页处理
            backtrack = False

        # 新页：原始分辨率截图
        full = self.capturer.grab_full()
        self._slide_count += 1
        idx = self._slide_count
        fname = f"{fmt_ts_compact(rec_ts)}_slide_{idx:03d}.png"
        fpath = self.shots_dir / fname
        rgba = full[..., [2, 1, 0, 3]]  # BGRA → RGBA
        Image.fromarray(rgba, mode="RGBA").save(fpath, "PNG")

        if sig is None:
            sig = self._current_sig()
        self._baseline = sig
        self._history.append((*sig, idx, f"screenshots/{fname}"))
        self._last_trigger_t = rec_ts
        self._last_page_idx = idx
        self._last_page_ts = rec_ts
        ev = SlideEvent(ts=rec_ts, image=f"screenshots/{fname}", index=idx)
        logger.info(f"翻页 → 第 {idx} 页 @{fmt_ts_compact(rec_ts)}: {fname}")
        if self.on_slide:
            self.on_slide(ev)

    # 辅助：现场重新计算当前画面哈希
    def _current_sig(self):
        small = self.capturer.grab_small(256)
        gray = bgra_to_gray(small)
        return dhash(gray), phash(gray)
