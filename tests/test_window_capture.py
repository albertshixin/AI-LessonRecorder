# -*- coding: utf-8 -*-
"""窗口采集模式测试（Win32 PrintWindow）

验证 v1.3 新增的 ``capture="window"`` 模式：
1. 枚举窗口能发现测试自建的 Win32 窗口；
2. PrintWindow 能截到窗口客户区内容（自建窗口画了纯色底，非全黑）；
3. 标题按"精确 → 包含"两级匹配；
4. 目标窗口不存在时报清晰错误；
5. grab_small 降采样链路兼容窗口模式。

运行：python tests/test_window_capture.py（需交互式桌面会话）
"""
from __future__ import annotations

import ctypes
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from src.core.vision.screen_capturer import ScreenCapturer

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
# 自建一个真实 Win32 窗口（白底 + 固定标题），保证测试确定性。
# 窗口跑在**独立线程 + 消息泵**：否则 GetWindowTextW 会向同线程窗口同步发
# WM_GETTEXT，嵌套进入 Python 窗口过程 → ctypes 回调栈溢出（RecursionError）。
# 跨线程时 GetWindowTextW 直接读缓存标题，不会进入对方窗口过程。
# ─────────────────────────────────────────────────────────────────────
_u32 = ctypes.WinDLL("user32", use_last_error=True)
import ctypes.wintypes as _wt
import threading

_WNDPROC = ctypes.WINFUNCTYPE(_wt.LPARAM, _wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM)


def _py_wndproc(hwnd, msg, w, l):
    """显式处理 WM_ERASEBKGND：用浅灰刷子填底，确保 PrintWindow 任何时候都非黑。"""
    WM_ERASEBKGND = 0x0014
    if msg == WM_ERASEBKGND:
        hdc = w
        rect = _wt.RECT()
        _u32.GetClientRect(hwnd, ctypes.byref(rect))
        brush = _u32.CreateSolidBrush(0x00E0E0E0)   # 浅灰 RGB
        _u32.FillRect(hdc, ctypes.byref(rect), brush)
        _u32.DeleteObject(brush)
        return 1   # 已擦除
    return _u32.DefWindowProcW(hwnd, msg, w, l)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", _wt.UINT), ("lpfnWndProc", _WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", _wt.HINSTANCE), ("hIcon", _wt.HICON),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", _wt.HBRUSH),
                ("lpszMenuName", _wt.LPCWSTR), ("lpszClassName", _wt.LPCWSTR)]


# 显式声明签名：不设 argtypes 时 ctypes 把 64 位 WPARAM/LPARAM 当 32 位 int 截断，
# 回调内会抛 "int too long to convert" 并递归刷屏
_u32.DefWindowProcW.restype = ctypes.c_longlong       # LRESULT
_u32.DefWindowProcW.argtypes = [_wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM]
_u32.RegisterClassW.restype = _wt.ATOM
_u32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
_u32.CreateWindowExW.restype = _wt.HWND
_u32.CreateWindowExW.argtypes = [_wt.DWORD, _wt.LPCWSTR, _wt.LPCWSTR, _wt.DWORD,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 _wt.HWND, _wt.HMENU, _wt.HINSTANCE, _wt.LPVOID]
_u32.ShowWindow.argtypes = [_wt.HWND, ctypes.c_int]
_u32.UpdateWindow.argtypes = [_wt.HWND]
_u32.DestroyWindow.argtypes = [_wt.HWND]
_u32.PostThreadMessageW.argtypes = [_wt.DWORD, _wt.UINT, _wt.WPARAM, _wt.LPARAM]


class _TestWindow:
    """在独立线程创建窗口并跑消息泵；跨线程读标题不会触发回调嵌套"""

    def __init__(self, title: str) -> None:
        self.title = title
        self.ready = threading.Event()
        self.hwnd = 0
        self.tid = 0
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        if not self.ready.wait(5) or not self.hwnd:
            raise RuntimeError("测试窗口创建失败/超时")

    def _run(self) -> None:
        wc = _WNDCLASSW()
        wc.lpfnWndProc = _WNDPROC(_py_wndproc)
        wc.hInstance = _wt.HINSTANCE(0)
        wc.hbrBackground = _wt.HBRUSH(16)   # COLOR_BTNFACE+1，非黑底
        wc.lpszClassName = "LessonRecorderTestWnd"
        if not _u32.RegisterClassW(ctypes.byref(wc)):
            self.ready.set()
            return
        self.hwnd = _u32.CreateWindowExW(
            0, wc.lpszClassName, self.title,
            0x00CF0000,  # WS_OVERLAPPEDWINDOW
            100, 100, 480, 360, 0, 0, 0, None)
        _u32.ShowWindow(self.hwnd, 1)       # SW_SHOWNORMAL
        _u32.UpdateWindow(self.hwnd)
        self.tid = ctypes.WinDLL("kernel32").GetCurrentThreadId()
        self.ready.set()
        # 消息泵：维持窗口存活与响应
        msg = _wt.MSG()
        while _u32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            _u32.TranslateMessage(ctypes.byref(msg))
            _u32.DispatchMessageW(ctypes.byref(msg))

    def close(self) -> None:
        if self.tid:
            _u32.PostThreadMessageW(self.tid, 0x0012, 0, 0)  # WM_QUIT
        self._t.join(timeout=3)
        self.hwnd = 0


def main() -> int:
    print("=" * 70)
    print("窗口采集模式测试（PrintWindow）")
    print("=" * 70)
    title = f"LR测试窗口_{uuid.uuid4().hex[:8]}"
    win = _TestWindow(title)
    try:
        # 1) 枚举发现
        wins = ScreenCapturer.list_windows()
        check(any(t == title for t, _w, _h in wins),
              f"list_windows 发现测试窗口（共枚举 {len(wins)} 个）")

        # 2) 精确标题采集
        cap = ScreenCapturer("window", window_title=title)
        arr = cap.grab_full()
        check(arr.ndim == 3 and arr.shape[2] == 4, f"BGRA 四通道（shape={arr.shape}）")
        check(arr.shape[0] >= 200 and arr.shape[1] >= 300,
              f"客户区尺寸合理（{arr.shape[1]}x{arr.shape[0]}）")
        check(float(arr[..., :3].mean()) > 5.0,
              f"内容非全黑（均值 {float(arr[..., :3].mean()):.1f}）")

        # 3) 包含匹配（标题前缀）
        cap2 = ScreenCapturer("window", window_title=title[:6])
        arr2 = cap2.grab_full()
        check(arr2.shape == arr.shape, "标题包含匹配可用（同窗口）")

        # 4) grab_small 兼容
        small = cap.grab_small(256)
        check(small.ndim == 2 and small.shape[1] <= 256,
              f"降采样参考帧正常（shape={small.shape}）")

        # 5) 窗口不存在 → 明确报错
        cap3 = ScreenCapturer("window", window_title="不存在的窗口__xyz")
        try:
            cap3.grab_full()
            check(False, "目标窗口缺失时应抛 RuntimeError")
        except RuntimeError as e:
            check("找不到目标窗口" in str(e), f"错误信息清晰：{e}")
    finally:
        win.close()

    # 6) 窗口关闭后，原实例再抓 → 报错而不是返回垃圾数据
    cap4 = ScreenCapturer("window", window_title=title)
    try:
        cap4.grab_full()
        check(False, "窗口关闭后应抛 RuntimeError")
    except RuntimeError:
        check(True, "窗口关闭后正确报错")

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
