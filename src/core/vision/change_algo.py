# -*- coding: utf-8 -*-
"""画面变化检测算法：dHash 粗筛 + pHash(DCT) 复核，纯 numpy 实现，无需 OpenCV"""
import numpy as np


def bgra_to_gray(img: np.ndarray) -> np.ndarray:
    """BGRA(HxWx4) → 灰度图 float32"""
    if img.ndim == 3:
        if img.shape[2] == 4:
            b, g, r = img[..., 0], img[..., 1], img[..., 2]
        else:  # BGR
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
