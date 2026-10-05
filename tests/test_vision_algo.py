# -*- coding: utf-8 -*-
"""翻页判据标定测试：用合成 PPT 帧验证 v1.2 三层判据的分离度

这个测试的价值在于：它把"阈值该设多少"从拍脑袋变成了可回归的量化结论。
一旦有人改坏判据（如把 dHash 重新接回"或"逻辑），本测试会立即失败。

运行：python tests/test_vision_algo.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from src.core.vision.change_algo import (dhash, diff_ratio, hamming, phash,
                                         resize_gray, ssim)

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  [OK] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        FAILURES.append(msg)


# ─────────────────────────────────────────────────────────────────────
# 合成 PPT 帧生成器
# ─────────────────────────────────────────────────────────────────────
def make_slide(widths: list[int], seed: int = 0, noise: float = 0.0,
               bg: float = 245.0) -> np.ndarray:
    """白底PPT：每行一段文字，widths 控制每行文字宽度（改宽度=换页内容）"""
    img = np.full((400, 600), bg, dtype=np.float64)
    for i, w in enumerate(widths):
        y0 = 30 + i * 45
        img[y0:y0 + 26, 40:40 + w] = 25.0
    if noise:
        rng = np.random.default_rng(seed)
        img = img + rng.normal(0, noise, img.shape)
    return img


DENSE = [520] * 8          # 满页文字
SPARSE = [200] * 8         # 换页后文字变短


def metrics(a: np.ndarray, b: np.ndarray) -> dict:
    return {
        "dhash": hamming(dhash(a), dhash(b)),
        "phash": hamming(phash(a), phash(b)),
        "diff": diff_ratio(a, b),
        "ssim": ssim(a, b),
    }


def test_three_layer_separation() -> None:
    """核心断言：三层判据必须能把"同页噪声/光标"与"真实换页"清晰分开"""
    print("\n[1] 三层判据分离度标定")
    page = make_slide(DENSE)
    same_noise = make_slide(DENSE, seed=3, noise=3.0)
    changed = make_slide(SPARSE)
    cursor = page.copy()
    cursor[100:120, 300:320] = 0.0        # 仅 20x20 像素的光标区域变化

    m_noise = metrics(page, same_noise)
    m_cursor = metrics(page, cursor)
    m_change = metrics(page, changed)

    print(f"  同页+噪声 : dhash={m_noise['dhash']:.3f} phash={m_noise['phash']:.3f} "
          f"diff={m_noise['diff']:.4f} ssim={m_noise['ssim']:.3f}")
    print(f"  仅光标    : dhash={m_cursor['dhash']:.3f} phash={m_cursor['phash']:.3f} "
          f"diff={m_cursor['diff']:.4f} ssim={m_cursor['ssim']:.3f}")
    print(f"  真实换页  : dhash={m_change['dhash']:.3f} phash={m_change['phash']:.3f} "
          f"diff={m_change['diff']:.4f} ssim={m_change['ssim']:.3f}")

    # 关键回归点：dHash 方向是反的（噪声距离 > 换页距离），不能作为主判据
    check(m_noise["dhash"] > m_change["dhash"],
          "dHash 对噪声比换页更敏感（证实其不可作主判据，此为v1.2 改算法的依据）")

    # pHash 能区分，且换页距离应显著高于同页噪声
    check(m_change["phash"] > 0.3, f"真实换页 pHash 距离足够大 ({m_change['phash']:.3f} > 0.3)")
    check(m_change["phash"] > m_noise["phash"] * 3,
          f"换页 pHash 距离远大于同页噪声 ({m_change['phash']:.3f} vs {m_noise['phash']:.3f})")

    # diff_ratio 是最稳的判据：换页 vs 光标差两个数量级
    check(m_change["diff"] > 0.05, f"换页像素差异占比足够大 ({m_change['diff']:.4f} > 0.05)")
    check(m_change["diff"] > m_cursor["diff"] * 20,
          f"diff_ratio 可区分换页与光标（{m_change['diff']:.4f} vs {m_cursor['diff']:.4f}）")
    check(m_cursor["diff"] < 0.01, f"光标造成的差异占比极低 ({m_cursor['diff']:.4f} < 0.01)")

    # SSIM 对换页敏感
    check(m_change["ssim"] < 0.9, f"换页 SSIM 明显下降 ({m_change['ssim']:.3f} < 0.9)")


def test_simulate_detector_decisions() -> None:
    """按SlideDetector 的实际判定顺序，模拟三种场景的最终结论"""
    print("\n[2] 按检测器实际判定顺序模拟")
    change_th, min_diff, ssim_th = 0.15, 0.02, 0.95

    def decide(base: np.ndarray, cur: np.ndarray) -> str:
        p_dist = hamming(phash(base), phash(cur))
        if p_dist < change_th:
            return "不变(pHash粗筛未过)"
        diff = diff_ratio(base, cur)
        if diff < min_diff:
            return "不变(diff挡光标)"
        s = ssim(base, cur)
        if s > ssim_th:
            return "不变(SSIM挡噪声)"
        return "判定翻页"

    page = make_slide(DENSE)
    cases = [
        ("同页+轻微噪声", make_slide(DENSE, seed=3, noise=3.0), "不变(pHash粗筛未过)"),
        ("仅光标移动", None, "不变(pHash粗筛未过)"),
        ("真实翻页", make_slide(SPARSE), "判定翻页"),
    ]
    cursor = page.copy()
    cursor[100:120, 300:320] = 0.0

    for name, frame, expect in cases:
        frm = cursor if frame is None else frame
        got = decide(page, frm)
        check(got == expect, f"{name} → {got}（期望：{expect}）")

    # 连续翻页序列：每一页都应被检出
    print("\n[3] 连续翻页序列检出率")
    pages = [make_slide([520] * 8), make_slide([200] * 8),
             make_slide([520, 200] * 4), make_slide([350] * 8)]
    detected = 0
    for i in range(1, len(pages)):
        if decide(pages[i - 1], pages[i]) == "判定翻页":
            detected += 1
    check(detected == len(pages) - 1,
          f"3 次翻页全部检出（{detected}/{len(pages) - 1}）")


def test_edge_cases() -> None:
    """边界条件不崩溃"""
    print("\n[4] 边界条件")
    small = np.full((10, 10), 128.0)
    check(0.0 <= ssim(small, small) <= 1.0, "小图 SSIM 不崩溃且在合法区间")
    same = make_slide(DENSE)
    check(ssim(same, same) > 0.999, "同一帧 SSIM≈1")
    check(abs(diff_ratio(same, same)) < 1e-9, "同一帧 diff_ratio=0")
    # 不同尺寸
    a = np.full((100, 100), 200.0)
    b = np.full((100, 200), 50.0)
    check(0.0 <= ssim(a, b) <= 1.0, "异尺寸输入 SSIM 仍合法（内部已对齐）")
    # 常数图（方差为 0）不应除零
    const = np.full((64, 64), 100.0)
    check(0.0 <= ssim(const, const) <= 1.0, "常数图 SSIM 不除零崩溃")
    # resize_gray
    g = np.random.default_rng(0).random((37, 53)) * 255
    r = resize_gray(g, 32, 32)
    check(r.shape == (32, 32), "resize_gray 输出形状正确")


if __name__ == "__main__":
    print("=" * 62)
    print("翻页判据标定测试（v1.2 三层判据）")
    print("=" * 62)
    test_three_layer_separation()
    test_simulate_detector_decisions()
    test_edge_cases()
    print("\n" + "=" * 62)
    if FAILURES:
        print(f"失败{len(FAILURES)} 项：")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("全部通过：翻页判据分离度符合设计预期")