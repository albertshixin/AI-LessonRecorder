# -*- coding: utf-8 -*-
"""屏幕/窗口截图：mss 封装 + Win32 PrintWindow 窗口采集

采集模式（vision.capture）：
- ``primary``  整个显示器（mss，可指定多显示器序号）
- ``region``   屏幕指定区域（mss）
- ``window``   **指定应用窗口**（PrintWindow）：直接读窗口自身渲染内容，
  其他窗口遮挡/弹出不会进入截图；窗口移动也不影响。需配置
  ``vision.window_title``（录制开始时按当前标题解析句柄）。

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

import ctypes
import threading
from typing import Optional

import mss
import numpy as np

from src.utils.logger import logger

Region = tuple[int, int, int, int]  # (x, y, w, h)


# ================= Win32 窗口采集（ctypes，无额外依赖） =================
if ctypes.sizeof(ctypes.c_void_p) == 8:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _dwmapi = ctypes.WinDLL("dwmapi")
else:  # pragma: no cover - 仅 32 位解释器
    _user32 = ctypes.windll.user32
    _gdi32 = ctypes.windll.gdi32
    _dwmapi = ctypes.windll.dwmapi

import ctypes.wintypes as _wt

_PW_CLIENTONLY = 1        # PrintWindow：只画客户区（不含标题栏）
_PW_RENDERFULLCONTENT = 2  # PrintWindow：强制渲染硬件加速内容（Chrome/Electron 等）
_DWMWA_CLOAKED = 14       # UWP 挂起窗口会"可见但被遮蔽"，需排除
_WS_EX_TOOLWINDOW = 0x00000080
_GWL_EXSTYLE = -20


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", _wt.DWORD), ("biWidth", _wt.LONG), ("biHeight", _wt.LONG),
                ("biPlanes", _wt.WORD), ("biBitCount", _wt.WORD),
                ("biCompression", _wt.DWORD), ("biSizeImage", _wt.DWORD),
                ("biXPelsPerMeter", _wt.LONG), ("biYPelsPerMeter", _wt.LONG),
                ("biClrUsed", _wt.DWORD), ("biClrImportant", _wt.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", _wt.DWORD * 3)]


def _enum_windows() -> list[tuple[int, str, int, int]]:
    """枚举可采集的顶层窗口 → [(hwnd, 标题, 宽, 高)]（工作线程/主线程均可调用）"""
    out: list[tuple[int, str, int, int]] = []

    def _cb(hwnd, _lparam):
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            buf = ctypes.create_unicode_buffer(512)
            _user32.GetWindowTextW(hwnd, buf, 512)
            title = buf.value.strip()
            if not title:
                return True
            if _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW:
                return True
            cloaked = _wt.DWORD(0)
            if 0 == _dwmapi.DwmGetWindowAttribute(
                    hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), 4) and cloaked.value:
                return True
            rc = _wt.RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(rc))
            w, h = rc.right - rc.left, rc.bottom - rc.top
            if w < 240 or h < 160:   # 过滤悬浮球/小工具窗口
                return True
            out.append((hwnd, title, w, h))
        except Exception:  # noqa: BLE001 - 单个窗口异常不应中断枚举
            pass
        return True

    _WNDENUMPROC = ctypes.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
    _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return out


def _print_window_bgra(hwnd: int) -> np.ndarray:
    """PrintWindow 截取窗口客户区 → BGRA 数组。

    与屏幕截图不同，PrintWindow **直接读窗口自己的渲染内容**，
    其他窗口遮挡/压在上面不会进入截图。
    """
    if _user32.IsIconic(hwnd):
        raise RuntimeError("目标窗口已最小化，无法采集（请先还原窗口）")

    rc = _wt.RECT()
    _user32.GetClientRect(hwnd, ctypes.byref(rc))
    w, h = rc.right - rc.left, rc.bottom - rc.top
    if w <= 0 or h <= 0:
        raise RuntimeError("目标窗口尺寸无效（可能已关闭）")

    hdc_win = _user32.GetDC(hwnd)
    hdc_mem = _gdi32.CreateCompatibleDC(hdc_win)
    hbmp = _gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    old = _gdi32.SelectObject(hdc_mem, hbmp)
    try:
        ok = _user32.PrintWindow(hwnd, hdc_mem, _PW_CLIENTONLY | _PW_RENDERFULLCONTENT)
        if not ok:
            raise RuntimeError("PrintWindow 失败（该窗口可能禁止采集，建议改用屏幕模式）")

        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        # 先查询位图信息（不取数据），再以负 biHeight 请求自顶向下行序
        _gdi32.GetDIBits(hdc_mem, hbmp, 0, 0, None, ctypes.byref(bmi), 0)
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB
        buf = (_wt.BYTE * (w * h * 4))()
        got = _gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
        if got != h:
            raise RuntimeError(f"GetDIBits 失败（got={got}）")
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4).copy()
        return arr
    finally:
        _gdi32.SelectObject(hdc_mem, old)
        _gdi32.DeleteObject(hbmp)
        _gdi32.DeleteDC(hdc_mem)
        _user32.ReleaseDC(hwnd, hdc_win)


class ScreenCapturer:
    """线程安全的截图器（mss 实例在首次使用者线程内创建并绑定该线程）"""

    def __init__(self, capture: str = "primary", region: Optional[Region] = None,
                 monitor: int = 1, window_title: Optional[str] = None) -> None:
        self._mode = capture
        self._region = region or (0, 0, 0, 0)
        self._monitor = max(1, int(monitor or 1))  # mss 中 1 = 主显示器
        self._window_title = (window_title or "").strip()
        self._hwnd: int | None = None          # 解析到的窗口句柄（按标题匹配）
        self._black_warned = False             # 空白截图只警告一次
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
        if self._mode == "window":
            return self._grab_window_full()
        with self._lock:
            sct = self._ensure()
            raw = sct.grab(self._monitor_dict())
            return np.frombuffer(raw.raw, dtype=np.uint8).reshape(
                raw.height, raw.width, 4).copy()

    # ---------- 窗口采集 ----------
    def _resolve_hwnd(self) -> int:
        """按标题解析目标窗口句柄（缓存 + 失效重建）"""
        if self._hwnd is not None and _user32.IsWindow(self._hwnd):
            buf = ctypes.create_unicode_buffer(512)
            _user32.GetWindowTextW(self._hwnd, buf, 512)
            if buf.value.strip() == self._window_title:
                return self._hwnd
        self._hwnd = None
        for hwnd, title, _w, _h in _enum_windows():
            if title == self._window_title:
                self._hwnd = hwnd
                return hwnd
        # 精确匹配失败再退而求其次（标题可能被应用动态改写，如加了" - 播放中"）
        for hwnd, title, _w, _h in _enum_windows():
            if self._window_title in title:
                self._hwnd = hwnd
                logger.warning(f"窗口标题已变化，按包含关系匹配: {title!r}")
                return hwnd
        raise RuntimeError(
            f"找不到目标窗口「{self._window_title}」，请确认应用已打开（或改用屏幕模式）")

    def _grab_window_full(self) -> np.ndarray:
        arr = _print_window_bgra(self._resolve_hwnd())
        # 有些 DX 独占/DRM 窗口 PrintWindow 返回纯黑——提示一次，帮用户定位
        if not self._black_warned and float(arr[..., :3].mean()) < 1.0:
            self._black_warned = True
            logger.warning("窗口截图内容为全黑（应用可能限制窗口采集），建议改用屏幕模式")
        return arr

    @staticmethod
    def list_windows() -> list[tuple[str, int, int]]:
        """[(标题, 宽, 高)]，供 UI 选择采集的窗口"""
        try:
            return [(t, w, h) for _hwnd, t, w, h in _enum_windows()]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"枚举窗口失败: {e}")
            return []

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