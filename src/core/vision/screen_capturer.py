# -*- coding: utf-8 -*-
"""屏幕截图：mss 封装，支持多显示器 / 指定区域 / ROI

v1.2 修复：
- **mss 实例复用**。原实现每次 grab 都 `with mss.mss() as sct` 新建+销毁实例，
  且 `_monitor_dict()` 又额外新建一次——翻页检测每 1.5s 抓 2 次屏，
  等于每分钟创建/销毁 160 次mss 实例（每次含 GDI 资源申请），是无谓开销。
  现改为**惰性创建 + 复用 + 显式 close**。
- **线程归属判定修正**（v1.2 二次修复）：`ScreenCapturer` 通常在**主线程构造**
  （SessionManager.start），而 `grab` 实际发生在 **SlideDetector 线程**。
  原实现把 `__init__` 所在线程记为 owner，导致 owner 永不匹配、
  `grab` 每次都抛 RuntimeError → **翻页截图功能 100% 失效**。
  现改为：mss 实例在**首次 grab 的线程**上创建并绑定该线程；
  若后续发现换了线程，则在锁内安全重建（GDI 句柄不跨线程共享）。
- 所有抓屏操作都在 `self._lock` 内串行化，杜绝并发进入 mss。
- 支持 **DPI 缩放**：Windows 125%/150% 下 mss 返回物理像素，
  这里统一按物理像素工作（截图要原始分辨率），仅在 UI 显示时缩放。
"""
from __future__ import annotations

import threading
from typing import Optional

import mss
import numpy as np

from src.utils.logger import logger

Region = tuple[int, int, int, int]  # (x, y, w, h)


class ScreenCapturer:
    """线程安全的截图器（mss 实例在首次使用者线程内创建并绑定该线程）"""

    def __init__(self, capture: str = "primary", region: Optional[Region] = None,
                 monitor: int = 1) -> None:
        self._mode = capture
        self._region = region or (0, 0, 0, 0)
        self._monitor = max(1, int(monitor or 1))  # mss 中 1 = 主显示器
        self._sct: mss.mss | None = None
        self._lock = threading.RLock()
        self._owner: int | None = None      # 当前持有 mss 实例的线程（首次 grab 时确定）
        self._mon_cache: list[dict] | None = None
        self.rebuild_count = 0# 因换线程而重建实例的次数（可观测）

    # ---------- 实例生命周期 ----------
    def _ensure(self) -> mss.mss:
        """在锁内获取本线程的 mss 实例；必要时重建。调用方必须已持锁。"""
        tid = threading.get_ident()
        if self._sct is not None and self._owner != tid:
            # mss 的 GDI 句柄不能跨线程使用 → 在新线程内安全重建
            logger.debug(f"截图线程切换（{self._owner} → {tid}），重建 mss 实例")
            try:
                self._sct.close()
            except Exception:  # noqa: BLE001
                logger.debug("跨线程重建前关闭 mss 失败", exc_info=True)
            self._sct = None
            self._rebuild()
        if self._sct is None:
            if self._owner != tid:
                self._owner = tid
            self._sct = mss.mss()
            self._mon_cache = [dict(m) for m in self._sct.monitors]
        return self._sct

    def _rebuild(self) -> None:
        self.rebuild_count += 1

    def close(self) -> None:
        """释放 GDI 资源（会话结束时调用）"""
        with self._lock:
            if self._sct is not None:
                try:
                    self._sct.close()
                except Exception:  # noqa: BLE001
                    logger.debug("关闭 mss 失败", exc_info=True)
                self._sct = None
                self._mon_cache = None
                self._owner = None

    def __del__(self):  # pragma: no cover - 解释器退出时的兜底
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    # ---------- 抓屏 ----------
    def _monitor_dict(self) -> dict:
        """必须在持锁状态下调用"""
        sct = self._ensure()
        if self._mode == "region":
            x, y, w, h = self._region
            if w > 0 and h > 0:
                return {"top": int(y), "left": int(x),
                        "width": int(w), "height": int(h)}
        monitors = self._mon_cache or []
        idx = self._monitor
        if monitors and idx >= len(monitors):
            idx = 1 if len(monitors) > 1 else 0
        if monitors:
            return dict(monitors[idx])
        return dict(sct.monitors[idx])

    def grab_full(self) -> np.ndarray:
        """原始分辨率 BGRA 截图（线程安全）"""
        with self._lock:
            sct = self._ensure()
            raw = sct.grab(self._monitor_dict())
            return np.frombuffer(raw.raw, dtype=np.uint8).reshape(
                raw.height, raw.width, 4).copy()

    def grab_small(self, width: int = 256) -> np.ndarray:
        """降采样小图（用于翻页判定的参考帧）

        降采样用**区域均值**而非最近邻：最近邻采样会在文字边缘产生
        锯齿与随机亮度抖动，导致同页帧的哈希距离被无谓放大
        （实测 σ=3 噪声下dHash 距离可达 0.406）。区域均值抗噪性更好。
        """
        full = self.grab_full()
        h, w = full.shape[:2]
        if w <= width:
            return full
        new_w = int(width)
        new_h = max(1, int(h * new_w / w))

        # 先灰度化（降采样只需亮度信息，省 3/4 内存与后续运算）
        gray = (0.299 * full[..., 2] + 0.587 * full[..., 1]
                + 0.114 * full[..., 0])
        del full
        # 区域均值降采样：step 取整数，保证 reshape 可整除
        step = max(1, w // new_w)
        eff_w = (w // step) * step
        eff_h = max(step, (h // step) * step)
        g = gray[:eff_h, :eff_w].reshape(eff_h // step, step, eff_w // step, step)
        small = g.mean(axis=(1, 3))
        # 若仍大于目标尺寸，再做一次最近邻收缩
        if small.shape[1] > new_w:
            xi = (np.arange(new_w) * (small.shape[1] / new_w)).astype(np.int32)
            small = small[:, xi]
        if small.shape[0] > new_h:
            yi = (np.arange(new_h) * (small.shape[0] / new_h)).astype(np.int32)
            small = small[yi, :]
        return small

    @staticmethod
    def list_monitors() -> list[tuple[int, str, int, int]]:
        """[(序号, 名称, 宽, 高)]，供 UI 选择；序号从 1 开始（1=主显示器）"""
        result: list[tuple[int, str, int, int]] = []
        sct = None
        try:
            sct = mss.mss()
            for i, m in enumerate(sct.monitors):
                if i == 0:
                    continue
                result.append((i, f"显示器 {i}（{m['width']}x{m['height']}）",
                               m["width"], m["height"]))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"枚举显示器失败: {e}")
        finally:
            if sct is not None:
                try:
                    sct.close()
                except Exception:  # noqa: BLE001
                    pass
        return result