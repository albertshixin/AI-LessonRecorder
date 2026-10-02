# -*- coding: utf-8 -*-
"""屏幕截图：mss 封装，支持主显示器 / 自定义区域"""
from typing import Optional

import mss
import numpy as np

from src.utils.logger import logger

Region = tuple[int, int, int, int]  # (x, y, w, h)


class ScreenCapturer:
    """线程内的截图器（每个使用线程独立实例，mss 非线程安全）"""

    def __init__(self, capture: str = "primary", region: Optional[Region] = None,
                 monitor: int = 1) -> None:
        self._mode = capture
        self._region = region or (0, 0, 0, 0)
        self._monitor = max(1, int(monitor or 1))  # mss 中 1 = 主显示器

    def _monitor_dict(self) -> dict:
        with mss.mss() as sct:
            if self._mode == "region":
                x, y, w, h = self._region
                return {"top": int(y), "left": int(x), "width": int(w), "height": int(h)}
            # 指定显示器（越界时回落主显示器）
            idx = self._monitor if self._monitor < len(sct.monitors) \
                else (1 if len(sct.monitors) > 1 else 0)
            return dict(sct.monitors[idx])

    def grab_full(self) -> np.ndarray:
        """原始分辨率 BGRA 截图"""
        with mss.mss() as sct:
            raw = sct.grab(self._monitor_dict())
            return np.frombuffer(raw.raw, dtype=np.uint8).reshape(
                raw.height, raw.width, 4).copy()

    def grab_small(self, width: int = 256) -> np.ndarray:
        """缩放后的 BGRA 小图（用于哈希比对，速度快）"""
        full = self.grab_full()
        h, w = full.shape[:2]
        if w <= width:
            return full
        new_h = max(1, int(h * width / w))
        yi = (np.arange(new_h) * (h / new_h)).astype(np.int32).clip(0, h - 1)
        xi = (np.arange(width) * (w / width)).astype(np.int32).clip(0, w - 1)
        return full[np.ix_(yi, xi)]

    @staticmethod
    def list_monitors() -> list[tuple[int, str, int, int]]:
        """[(序号, 名称, 宽, 高)]，供 UI 选择；序号从 1 开始（1=主显示器）"""
        result: list[tuple[int, str, int, int]] = []
        try:
            with mss.mss() as sct:
                for i, m in enumerate(sct.monitors):
                    if i == 0:
                        continue
                    result.append((i, f"显示器 {i}（{m['width']}x{m['height']}）",
                                   m["width"], m["height"]))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"枚举显示器失败: {e}")
        return result
