# -*- coding: utf-8 -*-
"""画面变化检测算法：dHash 粗筛 + pHash 复核 + 结构相似度(SSIM) 确认，纯 numpy 实现

v1.2 说明：方案文档 §2.1(3) 声称"SSIM 复核"，但原实现只有 dHash/pHash，
文档与实现不一致。此处补上真正的结构复核，并说明三者的分工：

- **dHash**（9x8 差异哈希）：对亮度/平移敏感 → 抗压缩噪声弱，但对"整页变亮"极敏感，
  适合作粗筛，**不能单独作判据**。
- **pHash**（32x32 DCT 低频）：抗轻微噪声与缩放，是主力判据。
- **SSIM**（局部均值/方差/协方差统计）：能识别"内容变了但整体色调相近"的情况
  （例如同为白底PPT、只换了文字），这是纯哈希最容易漏检的场景。

三者组合：dHash 或 pHash 超阈值（粗筛）→ SSIM 低于阈值（确认内容性变化）→ 判定翻页。
纯哈希相同但 SSIM 显示结构变化时，仍会触发，避免漏检。
"""
from __future__ import annotations

import numpy as np

# SSIM 计算窗口（8x8 滑动窗，高斯加权）
_SSIM_WIN = 8
_SSIM_SIGMA = 1.5
_C1 = (0.01 * 255.0) ** 2
_C2 = (0.03 * 255.0) ** 2


def bgra_to_gray(img: np.ndarray) -> np.ndarray:
    """BGRA(HxWx4) → 灰度图 float32"""
    if img.ndim == 3:
        b, g, r = img[..., 0], img[..., 1], img[..., 2]
        return 0.299 * r + 0.587 * g + 0.114 * b
    return img.astype(np.float32)


def resize_gray(gray: np.ndarray, w: int, h: int) -> np.ndarray:
    """最近邻缩放灰度图（仅用于哈希，速度优先）"""
    H, W = gray.shape
    if H == h and W == w:
        return gray
    yi = (np.arange(h) * (H / h)).astype(np.int32).clip(0, H - 1)
    xi = (np.arange(w) * (W / w)).astype(np.int32).clip(0, W - 1)
    return gray[np.ix_(yi, xi)]


def dhash(gray: np.ndarray) -> np.ndarray:
    """差异哈希：9x8 → 64bit，输出 bool 数组"""
    g = resize_gray(gray, 9, 8)
    return (g[:, 1:] > g[:, :-1]).flatten()


def phash(gray: np.ndarray) -> np.ndarray:
    """感知哈希：32x32 → DCT → 左上 8x8 低频 → 64bit，输出 bool 数组"""
    g = resize_gray(gray, 32, 32)
    g = g - g.mean()
    # 二维 DCT-II（基于一维 DCT 矩阵）
    n = 32
    k = np.arange(n)
    dct_mat = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * n))
    dct_mat[0] *= 1.0 / np.sqrt(2)
    dct_mat *= np.sqrt(2.0 / n)
    d = dct_mat @ g @ dct_mat.T
    low = d[:8, :8].flatten()
    med = np.median(low)
    return low > med


def hamming(a: np.ndarray, b: np.ndarray) -> float:
    """归一化汉明距离 0(相同)~1(完全不同)"""
    return float(np.count_nonzero(a != b)) / len(a)


def frame_signature(img_bgra: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """一次计算 (dhash, phash)"""
    gray = bgra_to_gray(img_bgra)
    return dhash(gray), phash(gray)


# ─────────────────────────────────────────────────────────────────────
#  SSIM（结构相似度）
# ─────────────────────────────────────────────────────────────────────
def _local_stats(x: np.ndarray, y: np.ndarray):
    """一次卷积算出 SSIM 所需的全部局部统计量（避免重复卷积 5 次）"""
    coords = np.arange(_SSIM_WIN, dtype=np.float64) - (_SSIM_WIN - 1) / 2.0
    g = np.exp(-(coords ** 2) / (2 * _SSIM_SIGMA ** 2))
    g /= g.sum()
    k = np.outer(g, g)                       # 高斯核（8x8，开销可忽略）
    from numpy.lib.stride_tricks import sliding_window_view

    def filt(img: np.ndarray) -> np.ndarray:
        win = sliding_window_view(img, (_SSIM_WIN, _SSIM_WIN))
        return np.einsum("ijkl,kl->ij", win, k)

    mu_x, mu_y = filt(x), filt(y)
    sigma_x = filt(x * x) - mu_x * mu_x
    sigma_y = filt(y * y) - mu_y * mu_y
    sigma_xy = filt(x * y) - mu_x * mu_y
    return mu_x, mu_y, sigma_x, sigma_y, sigma_xy


def ssim(x: np.ndarray, y: np.ndarray) -> float:
    """两幅灰度图的平均结构相似度 SSIM∈[0,1]；1=结构完全一致

    用于在哈希判定"变化"后进一步确认这是**内容性变化**，
    而非亮度/噪声/光标造成的伪变化。
    """
    x = np.ascontiguousarray(x, dtype=np.float64)
    y = np.ascontiguousarray(y, dtype=np.float64)
    if x.shape != y.shape:
        y = resize_gray(y, x.shape[1], x.shape[0]).astype(np.float64)
    if min(x.shape) < _SSIM_WIN:
        # 图太小，退化为归一化互相关
        xf, yf = x.ravel() - x.mean(), y.ravel() - y.mean()
        denom = np.sqrt((xf ** 2).sum() * (yf ** 2).sum())
        return float(np.clip(xf @ yf / denom, -1.0, 1.0)) if denom > 0 else 1.0

    mu_x, mu_y, s_x, s_y, s_xy = _local_stats(x, y)
    ssim_map = ((2 * mu_x * mu_y + _C1) * (2 * s_xy + _C2)) / \
               ((mu_x ** 2 + mu_y ** 2 + _C1) * (s_x + s_y + _C2))
    return float(np.clip(np.mean(ssim_map), 0.0, 1.0))


def diff_ratio(x: np.ndarray, y: np.ndarray) -> float:
    """像素级差异占比（|x-y| > 24 视为不同像素），作为 SSIM 的补充证据

    光标移动/水印/弹幕通常只改动极小面积像素 → 差异占比很低；
    整页 PPT 切换 → 差异占比很高。
    """
    x = x.astype(np.float32, copy=False)
    y = y.astype(np.float32, copy=False)
    if x.shape != y.shape:
        y = resize_gray(y, x.shape[1], x.shape[0])
    return float(np.count_nonzero(np.abs(x - y) > 24)) / float(x.size)