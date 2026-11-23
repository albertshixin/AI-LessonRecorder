# -*- coding: utf-8 -*-
"""自动/手动截图模式回归测试

需求（v1.3.1）：
1. 默认**不**自动截图，全靠手动（F9 / 界面按钮）
2. 设置里勾选"自动截图"才开启
3. 自动截图开启时，手动截图依然随时可用（互不影响）
4. 自动截图的触发条件：**页面变化率**超过阈值（默认 10%）才截
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from src.core.vision.change_algo import bgra_to_gray, diff_ratio
from src.core.vision.slide_detector import DEFAULT_AUTO_CAPTURE_DIFF, SlideDetector
from src.utils.config import DEFAULTS

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


class FakeCapturer:
    """行为对齐 ScreenCapturer 公开接口的假截图器"""

    def __init__(self, frames: list[np.ndarray]) -> None:
        self.frames = frames
        self.i = 0
        self.grab_threads: set[int] = set()

    def grab_full(self) -> np.ndarray:
        self.grab_threads.add(threading.get_ident())
        f = self.frames[min(self.i, len(self.frames) - 1)]
        self.i += 1
        return f

    def grab_small(self, width: int = 256) -> np.ndarray:
        full = self.grab_full()
        return (0.299 * full[..., 2] + 0.587 * full[..., 1]
                + 0.114 * full[..., 0])

    def close(self) -> None:
        pass


import threading  # noqa: E402  (FakeCapturer 用到)


def make_bgra(widths: list[int], seed: int = 0) -> np.ndarray:
    """合成一张 BGRA 幻灯片（白底 + 若干黑线条）"""
    h, w = 200, 320
    img = np.zeros((h, w, 4), dtype=np.uint8)
    img[..., 0] = img[..., 1] = img[..., 2] = 245.0
    img[..., 3] = 255
    rs = np.random.RandomState(seed)
    for i, ln in enumerate(widths):
        y0 = 20 + i * 30
        img[y0:y0 + 16, 30:30 + ln, :3] = 30.0
    if seed:
        img[..., :3] = np.clip(
            img[..., :3] + rs.normal(0, 2, img[..., :3].shape), 0, 255)
    return img


def build(frames, shots_dir, on_slide, **kw) -> SlideDetector:
    det = SlideDetector(shots_dir=shots_dir, on_slide=on_slide,
                        interval_sec=0.05, cooldown_sec=0.0, **kw)
    det.capturer = FakeCapturer(frames)
    return det


def wait_until(fn, timeout=8.0, step=0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return False


def diff_of(a: np.ndarray, b: np.ndarray) -> float:
    return diff_ratio(bgra_to_gray(a), bgra_to_gray(b))


# ─────────────────────────── 配置默认值 ───────────────────────────
def case_defaults() -> None:
    print("\n[1] 默认配置：自动截图关闭，阈值 10%")
    v = DEFAULTS["vision"]
    check(v.get("auto_capture") is False, "默认 auto_capture=False（全靠手动）")
    check(abs(v.get("auto_capture_diff", 0) - 0.10) < 1e-9,
          "默认变化率阈值 = 10%")
    check(abs(DEFAULT_AUTO_CAPTURE_DIFF - 0.10) < 1e-9, "检测线程默认阈值一致")


# ─────────────────────────── 手动模式 ───────────────────────────
def case_manual_only(tmp: Path) -> None:
    print("\n[2] 不勾选自动截图：页面再怎么变也不自动截，手动截图有效")
    A = make_bgra([200, 150])
    B = make_bgra([280, 280, 280, 280, 260, 260], seed=2)   # 大幅换页
    events = []
    frames = [A] * 6 + [B] * 10        # 中途大幅换页
    det = build(frames, tmp / "m1", events.append, auto_capture=False)
    det.start()
    time.sleep(0.8)                    # 足够多的 tick（interval=0.05s）
    auto_events = len(events)
    det.request_capture()              # 手动截图（当前帧已是 B）
    ok = wait_until(lambda: len(events) > auto_events, timeout=5)
    time.sleep(0.3)
    det.stop()
    det.join(timeout=5)

    check(ok, "手动截图命令被执行（有事件产生）")
    shots = list((tmp / "m1").glob("*.png"))
    check(len(shots) == 1,
          f"全程只有手动这 1 张截图（实际 {len(shots)}，换页未被自动保存）")
    check(det.stats_counter.get("ticks", 0) > 0, "检测线程保持运行（可响应手动）")
    check(det.stats_counter.get("capture_errors", 0) == 0, "无错误")
    # B 页的 diff 必须确实超过阈值（证明"没截"不是因为画面没变）
    d = diff_of(A, B)
    check(d >= 0.10, f"测试帧组本身变化率超阈值（A→B diff={d:.3f} ≥ 0.10）")


# ─────────────────────────── 自动模式 ───────────────────────────
def case_auto_trigger(tmp: Path) -> None:
    print("\n[3] 勾选自动截图：变化率超 10% 才截，小变化不截")
    A = make_bgra([200, 150])
    A2 = make_bgra([200, 150, 90])              # 加一行短文字（≈2%，阈值之下）
    B = make_bgra([280, 280, 280, 280, 260, 260], seed=2)   # 大幅换页
    d_small = diff_of(A, A2)
    d_big = diff_of(A, B)
    print(f"      实测变化率：A→A'={d_small:.4f}（应 <0.10），A→B={d_big:.4f}（应 ≥0.10）")

    events = []
    frames = [A] * 6 + [A2] * 6 + [B] * 10
    det = build(frames, tmp / "a1", events.append,
                auto_capture=True, auto_capture_diff=0.10)
    det.start()
    ok = wait_until(lambda: len(events) >= 2, timeout=8)
    time.sleep(0.4)
    det.stop()
    det.join(timeout=5)

    check(ok, f"大幅换页被自动截（events={len(events)}）")
    shots = list((tmp / "a1").glob("*.png"))
    check(len(shots) == 2,
          f"只有 首页 + B 页 2 张（噪声页未触发，实际 {len(shots)}）")
    check(d_small < 0.10 and d_big >= 0.10,
          "阈值语义正确：小变化在阈值下，大变化在阈值上")


def case_auto_below_threshold(tmp: Path) -> None:
    print("\n[4] 自动模式：变化率恰好在阈值附近的行为")
    A = make_bgra([200, 150])
    # 加两行短文字 ≈ 16*90*2 = 2880px / 64000 ≈ 4.5%（阈值之下）
    A3 = make_bgra([200, 150, 90, 90], seed=1)
    d = diff_of(A, A3)
    print(f"      实测变化率 A→A3 = {d:.4f}")
    events = []
    det = build([A] * 6 + [A3] * 12, tmp / "a2", events.append,
                auto_capture=True, auto_capture_diff=0.10)
    det.start()
    time.sleep(1.0)
    det.stop()
    det.join(timeout=5)
    shots = list((tmp / "a2").glob("*.png"))
    if d < 0.10:
        check(len(shots) == 1,
              f"阈值下的变化不触发自动截图（{len(shots)} 张，仅首页）")
        check(len(events) == 1, "无多余事件")
    else:
        check(len(shots) == 2,
              f"阈值上的变化触发自动截图（{len(shots)} 张）")


def case_manual_during_auto(tmp: Path) -> None:
    print("\n[5] 自动模式下手动截图依然可用")
    A = make_bgra([200, 150])
    B = make_bgra([280, 280, 280, 280, 260, 260], seed=2)   # 大幅换页
    events = []
    frames = [A] * 8 + [B] * 12
    det = build(frames, tmp / "a3", events.append,
                auto_capture=True, auto_capture_diff=0.10)
    det.start()
    det.request_capture()              # 录制刚开始就手动截一张（A 页）
    ok = wait_until(lambda: len(events) >= 3, timeout=8)
    time.sleep(0.4)
    det.stop()
    det.join(timeout=5)

    check(ok, f"首页自动 + 手动 + B页自动 全部产生（events={len(events)}）")
    shots = list((tmp / "a3").glob("*.png"))
    check(len(shots) == 3, f"落盘 3 张（首页/手动/B，实际 {len(shots)}）")
    check(det.stats_counter.get("capture_errors", 0) == 0, "无错误")
    check(det.stats_counter.get("captures", 0) == 3,
          f"截图计数 = 3（{det.stats_counter.get('captures')}）")


def main() -> int:
    print("=" * 70)
    print("自动/手动截图模式回归测试")
    print("=" * 70)
    case_defaults()
    with tempfile.TemporaryDirectory(prefix="lr_autocap_") as td:
        tmp = Path(td)
        case_manual_only(tmp)
        case_auto_trigger(tmp)
        case_auto_below_threshold(tmp)
        case_manual_during_auto(tmp)
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
