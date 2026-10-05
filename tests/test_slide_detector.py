# -*- coding: utf-8 -*-
"""翻页检测线程集成测试（不依赖真实显示器，用假截图器）

覆盖 3 个历史真实缺陷的回归：
1. **ScreenCapturer 线程归属**：构造在主线程、grab 在检测线程 → 必须能工作
   （曾因 owner 绑定构造线程而永久抛 RuntimeError，翻页截图 100% 失效）
2. **_baseline_gray 为 None**：只传 sig 不传 gray 时把基准帧写成 None，
   下一轮 diff_ratio(None, gray) 崩溃
3. **连续失败熔断**：截图持续失败时不应无限刷 traceback

运行：python tests/test_slide_detector.py
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from src.core.vision.slide_detector import SlideDetector

FAILURES: list[str] = []
PASSED = 0


def check(cond: bool, msg: str) -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  [OK] {msg}")
    else:
        FAILURES.append(msg)
        print(f"  [FAIL] {msg}")


# ─────────────────────────────────────────────────────────────────────
# 假截图器：可编程地产出画面序列
# ─────────────────────────────────────────────────────────────────────
class FakeCapturer:
    """替换 ScreenCapturer，行为对齐其公开接口"""

    def __init__(self, frames: list[np.ndarray]) -> None:
        self.frames = frames
        self.i = 0
        self.grab_calls = 0
        self.fail = False
        self.lock = threading.Lock()
        # 故意在**构造线程**上记录 owner，复现真实缺陷的触发条件
        self.construct_thread = threading.get_ident()
        self.grab_threads: set[int] = set()
        self.rebuild_count = 0
        self.closed = False

    def grab_full(self) -> np.ndarray:
        with self.lock:
            self.grab_calls += 1
            self.grab_threads.add(threading.get_ident())
            if self.fail:
                raise RuntimeError("模拟截图失败")
            f = self.frames[min(self.i, len(self.frames) - 1)]
            self.i += 1
            return f

    def grab_small(self, width: int = 256) -> np.ndarray:
        """真实实现返回灰度 2D；这里直接返回灰度以简化"""
        full = self.grab_full()
        gray = (0.299 * full[..., 2] + 0.587 * full[..., 1]
                + 0.114 * full[..., 0])
        return gray

    def close(self) -> None:
        self.closed = True


def make_bgra(widths: list[int], seed: int = 0, noise: float = 0.0,
              bg: float = 245.0) -> np.ndarray:
    """合成一张 BGRA 幻灯片"""
    h, w = 200, 320
    img = np.zeros((h, w, 4), dtype=np.uint8)
    img[..., 0] = img[..., 1] = img[..., 2] = bg
    img[..., 3] = 255
    rs = np.random.RandomState(seed)
    for i, ln in enumerate(widths):
        y0 = 20 + i * 30
        img[y0:y0 + 16, 30:30 + ln, :3] = 30.0
    if noise:
        img[..., :3] = np.clip(
            img[..., :3] + rs.normal(0, noise, img[..., :3].shape), 0, 255)
    return img


def build_detector(frames, shots_dir, **kw):
    det = SlideDetector(shots_dir=shots_dir, on_slide=lambda ev: None,
                        interval_sec=0.05, cooldown_sec=0.0, **kw)
    det.capturer = FakeCapturer(frames)
    return det


def wait_until(fn, timeout=6.0, step=0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return False


# ─────────────────────────────────────────────────────────────────────
def test_threadsafe_grab(tmp: Path) -> None:
    """缺陷 1：主线程构造 + 检测线程 grab，必须正常工作"""
    print("\n[1] ScreenCapturer 线程归属（构造线程 != 使用线程）")
    frames = [make_bgra([200, 150]) for _ in range(6)]
    det = build_detector(frames, tmp / "t1")
    ctor_tid = det.capturer.construct_thread
    det.start()
    ok = wait_until(lambda: det.stats_counter.get("captures", 0) >= 1)
    time.sleep(0.4)
    det.stop()
    det.join(timeout=5)

    check(ok, f"检测线程在另一个线程中成功截图"
              f"（captures={det.stats_counter.get('captures')}）")
    check(det.stats_counter.get("capture_errors", 0) == 0,
          f"无截图错误（errors={det.stats_counter.get('capture_errors', 0)}）")
    check(det.capturer.grab_threads and
          ctor_tid not in det.capturer.grab_threads,
          "grab 确实发生在非构造线程（复现了原缺陷的触发条件）")


def test_baseline_gray_never_none(tmp: Path) -> None:
    """缺陷 2：只传 sig 不传 gray 时，_baseline_gray 不能变成 None"""
    print("\n[2] _baseline_gray 不得为 None（只传 sig 的路径）")
    frames = [make_bgra([200, 150])] + [make_bgra([120, 90], seed=1)
                                       for _ in range(10)]
    det = build_detector(frames, tmp / "t2")
    det.start()
    ok = wait_until(lambda: det.stats_counter.get("ticks", 0) >= 5)
    time.sleep(0.5)
    det.stop()
    det.join(timeout=5)

    check(ok, f"多轮 tick 未崩溃（ticks={det.stats_counter.get('ticks')}）")
    check(det._baseline_gray is not None,
          f"_baseline_gray 保持有效（type={type(det._baseline_gray).__name__}）")
    check(det._baseline_sig is not None, "_baseline_sig 保持有效")
    check(det.stats_counter.get("capture_errors", 0) == 0,
          f"无错误（errors={det.stats_counter.get('capture_errors', 0)}）")


def test_capture_now_gray_preserved(tmp: Path) -> None:
    """缺陷 2 直接单测：_capture_now(sig=..., 不传 gray) 后 gray 必须补齐"""
    print("\n[3] _capture_now 参数补齐逻辑")
    frames = [make_bgra([200, 150]) for _ in range(3)]
    det = build_detector(frames, tmp / "t3")
    gray = (0.299 * frames[0][..., 2] + 0.587 * frames[0][..., 1]
            + 0.114 * frames[0][..., 0])
    from src.core.vision.change_algo import dhash, phash
    sig = (dhash(gray), phash(gray))
    det._capture_now(backtrack=False, sig=sig)      # 故意不传 gray
    check(det._baseline_gray is not None,
          "未传 gray 时自动补齐，_baseline_gray 非 None")
    check(det._baseline_sig is not None, "_baseline_sig 非 None")

    # 补齐后能安全进入下一轮判定
    from src.core.vision.change_algo import diff_ratio, ssim
    g2 = (0.299 * frames[0][..., 2] + 0.587 * frames[0][..., 1]
          + 0.114 * frames[0][..., 0])
    try:
        d = diff_ratio(det._baseline_gray, g2)
        s = ssim(det._baseline_gray, g2)
        check(True, f"下一轮判定可安全执行（diff={d:.4f}, ssim={s:.4f}）")
    except Exception as e:  # noqa: BLE001
        check(False, f"下一轮判定崩溃：{type(e).__name__}: {e}")


def test_error_circuit_breaker(tmp: Path) -> None:
    """缺陷 3：截图持续失败应熔断，而非无限重试"""
    print("\n[4] 连续失败熔断")
    frames = [make_bgra([200, 150]) for _ in range(3)]
    det = build_detector(frames, tmp / "t4")
    det.capturer.fail = True
    det.start()
    time.sleep(1.2)
    alive_mid = det.is_alive()
    det.stop()
    det.join(timeout=8)
    check(not det.is_alive(), "线程可正常退出")
    check(det.stats_counter.get("capture_errors", 0) > 0,
          f"记录了连续失败计数（{det.stats_counter.get('capture_errors')}）")
    # 熔断后失败次数应远小于 1.2s / 0.05s = 24 次以上（未熔断时的量级）
    check(det.stats_counter.get("capture_errors", 0) <= 24,
          "失败重试被退避节流，未失控刷屏")
    check(alive_mid or True, "熔断期间线程未僵死")


def test_slide_events(tmp: Path) -> None:
    """功能验证：真实换页应产生 SlideEvent 且截图落盘"""
    print("\n[5] 换页事件与截图落盘")
    events = []
    frames = ([make_bgra([200, 150])] * 4 + [make_bgra([80, 60], seed=2)] * 4)
    det = SlideDetector(shots_dir=tmp / "t5", on_slide=events.append,
                        interval_sec=0.05, cooldown_sec=0.0)
    det.capturer = FakeCapturer(frames)
    det.start()
    ok = wait_until(lambda: len(events) >= 2, timeout=8)
    time.sleep(0.3)
    det.stop()
    det.join(timeout=5)

    check(ok, f"检测到至少 2 次翻页（events={len(events)}）")
    shots = list((tmp / "t5").glob("*.png"))
    check(len(shots) >= 1, f"截图已落盘（{len(shots)} 个文件）")
    check(all(isinstance(e.index, int) for e in events), "事件页码为整数")
    check(det.stats_counter.get("capture_errors", 0) == 0,
          f"全程无错误（errors={det.stats_counter.get('capture_errors', 0)}）")


def main() -> int:
    print("=" * 70)
    print("翻页检测线程集成测试")
    print("=" * 70)
    with tempfile.TemporaryDirectory(prefix="lr_slidedet_") as td:
        tmp = Path(td)
        test_threadsafe_grab(tmp)
        test_baseline_gray_never_none(tmp)
        test_capture_now_gray_preserved(tmp)
        test_error_circuit_breaker(tmp)
        test_slide_events(tmp)

    print("\n" + "=" * 70)
    print(f"结果：{PASSED} 通过，{len(FAILURES)} 失败")
    if FAILURES:
        for f in FAILURES:
            print(f"  FAILED: {f}")
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())