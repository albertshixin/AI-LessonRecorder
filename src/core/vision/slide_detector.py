# -*- coding: utf-8 -*-
"""PPT 翻页检测：定时抓屏 → 多判据联合判定 → 翻页截图 + 翻回识别 + 冷却防抖

v1.2 重写判据（基于实测标定，见 tests/test_vision_algo.py）
--------------------------------------------------------------------
原实现：`if hamming(dhash) >= T or hamming(phash) >= T` → 判翻页。存在两个实测问题：

1. **dHash 抗噪性差，会单独触发误检**。实测（400x600 白底 PPT，叠加 σ=3 的噪声）：
   - 同页 + 噪声：dHash 距离 **0.406**（已超默认阈值 0.18）→ 误判翻页
   - 真实换页：  dHash 距离 **0.062**（反而低于阈值）
   即 dHash 对"整页亮度平移"极敏感、对"内容变化"不敏感，**方向恰好是反的**。
   原代码用 `or` 连接，等于把最不可靠的判据当成主判据。

2. 文档声称的"SSIM 复核"从未实现。

v1.2 改为 **pHash 主判据 + diff_ratio 确认 + SSIM 辅助** 的三层结构：
   | 场景                | pHash   | diff_ratio | SSIM    | 判定 |
   |---------------------|---------|------------|---------|------|
   | 同页 + 噪声         | 0.062   | 0.0000     | 0.905   | 不变 |
   | 仅光标移动          | 0.125   | 0.0017     | 0.997   | 不变 |
   | 真实换页            | 0.469   | 0.2773     | 0.675   | 翻页 |
   pHash 负责"像不像另一页"，diff_ratio 负责"像素改了多少"（对光标/水印极稳），
   SSIM 作为结构复核。三者**与**关系，显著降低误检同时不牺牲漏检率。

线程模型：所有截图与状态变更**只在检测线程内执行**。外部（如 UI 热键）
通过 `request_capture()` 投递命令，由主循环消费——mss 实例非线程安全，
跨线程直接调用会花屏甚至崩溃（原实现即为此缺陷）。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image

from src.core.timeline import SlideEvent
from src.core.vision.change_algo import (bgra_to_gray, dhash, diff_ratio, hamming,
                                         phash, ssim)
from src.utils.logger import logger
from src.utils.timeutil import fmt_ts_compact

# 判据默认值（均可配置，见 config DEFAULTS.vision）
DEFAULT_SMALL_WIDTH = 256# 参与判定的参考帧宽度（降采样后）
DEFAULT_CHANGE_THRESHOLD = 0.15   # pHash 距离：超过视为"可能是另一页"
DEFAULT_MIN_DIFF_RATIO = 0.02# 差异像素占比下限：低于此值判为噪声/光标
DEFAULT_SSIM_THRESHOLD = 0.95     # SSIM 下限：高于此值判为"结构未变"
DEFAULT_BACKTRACK_TOL = 0.08# 与历史页判为同一页的 pHash 容差
DEFAULT_COOLDOWN_SEC = 3.0

# 连续截图失败多少次后熔断（防止无显示设备/会话销毁时无限刷 traceback）
_MAX_CONSECUTIVE_ERRORS = 30


class SlideDetector(threading.Thread):
    """翻页检测线程

    on_slide(event: SlideEvent)：检测到翻页时回调（截图已保存）
    """

    def __init__(
        self,
        shots_dir: Path,
        on_slide,
        interval_sec: float = 1.5,
        phash_threshold: float = DEFAULT_CHANGE_THRESHOLD,
        backtrack_tol: float = DEFAULT_BACKTRACK_TOL,
        cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
        min_diff_ratio: float = DEFAULT_MIN_DIFF_RATIO,
        ssim_threshold: float = DEFAULT_SSIM_THRESHOLD,
        capture: str = "primary",
        region=None,
        monitor: int = 1,
        roi=None,
        get_time=None,          # Callable[[], float]：当前录制相对时间（与音频时间轴对齐）
        small_width: int = DEFAULT_SMALL_WIDTH,
    ) -> None:
        super().__init__(daemon=True, name="SlideDetector")
        from src.core.vision.screen_capturer import ScreenCapturer
        self.capturer = ScreenCapturer(capture, region, monitor=monitor)
        self.shots_dir = Path(shots_dir)
        # 目录必须在此处创建：历史缺陷是只假定create_session 已建好目录，
        # 一旦目录缺失（清理过output、手动指定路径），每次截图保存都抛
        # FileNotFoundError 且被 except 吞掉 → 表现为"翻页检测完全失灵"。
        try:
            self.shots_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.error(f"无法创建截图目录 {self.shots_dir}: {e}")
        self.on_slide = on_slide
        self.interval = max(0.2, float(interval_sec))
        self.change_th = float(phash_threshold)
        self.backtrack_tol = float(backtrack_tol)
        self.cooldown = float(cooldown_sec)
        self.min_diff_ratio = float(min_diff_ratio)
        self.ssim_th = float(ssim_threshold)
        self.roi = roi            # (x, y, w, h) 归一化内容区，排除播放器控制栏
        self.get_time = get_time or (lambda: time.time())
        self.small_width = int(small_width)

        self._stop_evt = threading.Event()
        self._pause_evt = threading.Event()
        self._commands: deque = deque()
        self._cmd_lock = threading.Lock()

        self._baseline_sig: tuple[np.ndarray, np.ndarray] | None = None
        self._baseline_gray: np.ndarray | None = None   # 基准帧灰度（供 diff/SSIM 用）
        self._history: list[tuple[np.ndarray, np.ndarray, int, str]] = []
        self._slide_count = 0
        self._last_trigger_t = -1e9
        self._armed = False
        self._last_page_idx = -1
        self._last_page_ts = -1e9
        self.same_page_dedup_sec = 30.0

        # 运行时指标
        self.stats_counter = {"ticks": 0, "captures": 0, "backtracks": 0,
                              "rejected_noise": 0, "rejected_cursor": 0}

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

    def request_capture(self) -> None:
        """请求手动强制截图（线程安全：仅投递命令，由检测线程执行）

        主循环按 50ms 分片休眠，命令到达后最迟 50ms 内响应。
        """
        with self._cmd_lock:
            self._commands.append("capture")

    # ---------- 主循环 ----------
    def run(self) -> None:
        logger.info("翻页检测线程启动")
        consecutive_errors = 0
        while not self._stop_evt.is_set():
            # 1) 优先消费外部命令（手动截图）
            cmd = self._pop_command()
            if cmd == "capture":
                try:
                    if not self._pause_evt.is_set():
                        self._capture_now(backtrack=False, sig=None)
                    consecutive_errors = 0
                except Exception:  # noqa: BLE001
                    logger.exception("手动截图失败")
                self._stop_evt.wait(0.05)
                continue

            # 2) 常规检测
            try:
                if not self._pause_evt.is_set():
                    self._tick()
                consecutive_errors = 0
            except Exception as e:  # noqa: BLE001
                consecutive_errors += 1
                self.stats_counter["capture_errors"] = consecutive_errors
                # 熔断：截图是本线程的主要工作，连续失败（如无显示设备/会话已销毁）
                # 若不熔断，每 interval 刷一条 traceback，日志会被瞬间冲垮且掩盖真因。
                if consecutive_errors == 1:
                    logger.exception("翻页检测异常")
                elif consecutive_errors == 5:
                    logger.error(
                        f"翻页检测连续 {consecutive_errors} 次失败，已熔断自动重试"
                        f"（停止截图功能，录制与转写不受影响）：{e}")
                if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    logger.error("翻页检测线程退出（连续失败达上限）")
                    break
                self._stop_evt.wait(min(2.0, 0.2 * consecutive_errors))
                continue

            # 3) 分段等待：命令到达时立即唤醒，否则按周期休眠
            self._wait_interval()
        logger.info(f"翻页检测线程已退出：{self.stats_counter}")

    def _wait_interval(self) -> None:
        """按 interval 分片休眠，每片检查一次命令（使手动截图响应 <100ms）"""
        deadline = time.monotonic() + self.interval
        while not self._stop_evt.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop_evt.wait(min(0.05, remaining))

    def _pop_command(self):
        with self._cmd_lock:
            return self._commands.popleft() if self._commands else None

    # ---------- 核心判定 ----------
    def _tick(self) -> None:
        small = self.capturer.grab_small(self.small_width)
        gray = self._apply_roi(bgra_to_gray(small))
        sig = (dhash(gray), phash(gray))
        rec_ts = self.get_time()
        self.stats_counter["ticks"] += 1

        if not self._armed:
            self._baseline_sig = sig
            self._baseline_gray = gray
            self._armed = True
            self._capture_now(backtrack=False, sig=sig, gray=gray)   # 首页也截图
            return

        if rec_ts - self._last_trigger_t < self.cooldown:
            return

        # 防御：基准帧灰度缺失（如截图保存失败导致状态被清空）→ 重新武装，
        # 绝不能把 None 传进 diff_ratio/ssim
        if self._baseline_sig is None or self._baseline_gray is None:
            self._baseline_sig, self._baseline_gray = sig, gray
            self._armed = True
            return

        bd, bp = self._baseline_sig
        d_dist, p_dist = hamming(sig[0], bd), hamming(sig[1], bp)

        # 第一层：pHash 主判据（dHash 不再参与"或"判定，抗噪性不足）
        if p_dist < self.change_th and d_dist < self.change_th * 1.5:
            return

        # 第二层：diff_ratio 确认像素变化量（挡掉光标/水印/弹幕/轻微动画）
        diff = diff_ratio(self._baseline_gray, gray)
        if diff < self.min_diff_ratio:
            self.stats_counter["rejected_cursor"] += 1
            self._baseline_sig = sig          # 以当前帧为新基准，避免反复比对同一旧帧
            self._baseline_gray = gray
            return

        # 第三层：SSIM 结构复核（挡掉"整体亮度变化但内容相同"）
        s = ssim(self._baseline_gray, gray)
        if s > self.ssim_th:
            self.stats_counter["rejected_noise"] += 1
            self._baseline_sig = sig
            self._baseline_gray = gray
            return

        logger.debug(f"翻页候选 pHash={p_dist:.3f} diff={diff:.3f} ssim={s:.3f}")

        # 翻回历史页？
        hit = self._is_backtrack(sig)
        if hit:
            idx, img = hit
            if idx == self._last_page_idx and rec_ts - self._last_page_ts < self.same_page_dedup_sec:
                self._baseline_sig, self._baseline_gray = sig, gray
                self._last_trigger_t = rec_ts
                return
            self._capture_now(backtrack=True, sig=sig, gray=gray)
        else:
            self._capture_now(backtrack=False, sig=sig, gray=gray)

    def _apply_roi(self, gray: np.ndarray) -> np.ndarray:
        """裁剪内容区（可选），排除播放器控制栏/弹幕区"""
        if not self.roi:
            return gray
        h, w = gray.shape
        rx, ry, rw, rh = self.roi
        x0, y0 = int(rx * w), int(ry * h)
        x1, y1 = int((rx + rw) * w), int((ry + rh) * h)
        if x1 - x0 < 32 or y1 - y0 < 32:
            return gray
        return gray[y0:y1, x0:x1]

    def _is_backtrack(self, sig) -> tuple[int, str] | None:
        """匹配历史帧：返回 (idx, img) 或 None"""
        p = sig[1]
        for _hd, hp, idx, img in reversed(self._history):
            if hamming(p, hp) <= self.backtrack_tol:
                return idx, img
        return None

    # ---------- 截图落盘 ----------
    def _capture_now(self, backtrack: bool, sig=None, gray=None) -> None:
        rec_ts = self.get_time()

        if backtrack:
            if sig is None:
                small = self.capturer.grab_small(self.small_width)
                gray = self._apply_roi(bgra_to_gray(small))
                sig = (dhash(gray), phash(gray))
            hit = self._is_backtrack(sig)
            if hit:
                idx, img = hit
                self._baseline_sig = sig
                self._baseline_gray = gray
                self._last_trigger_t = rec_ts
                self._last_page_idx = idx
                self._last_page_ts = rec_ts
                self.stats_counter["backtracks"] += 1
                ev = SlideEvent(ts=rec_ts, image=img, index=idx, is_backtrack=True)
                logger.info(f"翻回第 {idx} 页 @{fmt_ts_compact(rec_ts)}")
                if self.on_slide:
                    self.on_slide(ev)
                return
            backtrack = False

        # 新页：原始分辨率截图
        full = self.capturer.grab_full()
        self._slide_count += 1
        idx = self._slide_count
        fname = f"{fmt_ts_compact(rec_ts)}_slide_{idx:03d}.png"
        fpath = self.shots_dir / fname
        try:
            rgba = full[..., [2, 1, 0, 3]]      # BGRA → RGBA
            Image.fromarray(rgba, mode="RGBA").save(fpath, "PNG")
        except Exception:  # noqa: BLE001
            logger.exception(f"截图保存失败: {fpath}")
            self._slide_count -= 1
            return

        # 归一化入参：gray 与 sig 互为推导，缺一就补齐另一个。
        # （历史缺陷：只传 sig 不传 gray 时会把 _baseline_gray 写成 None，
        #   下一轮 diff_ratio(None, gray) 直接崩溃，翻页检测整体失效。）
        if gray is None:
            small = self.capturer.grab_small(self.small_width)
            gray = self._apply_roi(bgra_to_gray(small))
        if sig is None:
            sig = (dhash(gray), phash(gray))
        self._baseline_sig = sig
        self._baseline_gray = gray
        self._history.append((*sig, idx, f"screenshots/{fname}"))
        self._last_trigger_t = rec_ts
        self._last_page_idx = idx
        self._last_page_ts = rec_ts
        self.stats_counter["captures"] += 1

        ev = SlideEvent(ts=rec_ts, image=f"screenshots/{fname}", index=idx)
        logger.info(f"翻页 → 第 {idx} 页 @{fmt_ts_compact(rec_ts)}: {fname}")
        if self.on_slide:
            self.on_slide(ev)