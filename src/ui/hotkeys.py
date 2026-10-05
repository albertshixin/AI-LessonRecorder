# -*- coding: utf-8 -*-
"""全局快捷键：录制时主窗口往往失焦（用户在课程播放器里），按钮点不到，
必须用系统级热键。

实现：Win32 `RegisterHotKey` + Qt 原生事件过滤器（QAbstractNativeEventFilter）。
不引入额外依赖（无需 keyboard/pynput），只用 ctypes 调user32/kernel32。

热键冲突：若热键已被其他程序占用，RegisterHotKey 返回失败，
此时降级为"仅窗口内快捷键"并在日志中告知，不影响主流程。

默认键位：
- F9  手动截图（漏检补救）
- F10 暂停 / 继续
- F11 停止录制并生成逐字稿
- Ctrl+Shift+F9 强制打开主窗口
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication

from src.utils.logger import logger

# ── Win32 常量 ──
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000          # Windows 7+：按住不重复触发
WM_HOTKEY = 0x0312
WM_CLOSE = 0x0010

_HOTKEY_ID_BASE = 0xB100


class _HotkeyFilter(QAbstractNativeEventFilter):
    """把 WM_HOTKEY 消息转成回调"""

    def __init__(self, dispatcher: dict[int, Callable[[], None]]) -> None:
        super().__init__()
        self._dispatcher = dispatcher

    def nativeEventFilter(self, event_type, message):  # noqa: N802 (Qt 命名)
        try:
            msg = ctypes.wintypes.MSG.from_address(int(message))
        except Exception:  # noqa: BLE001
            return False, 0
        if msg.message == WM_HOTKEY:
            cb = self._dispatcher.get(int(msg.wParam))
            if cb:
                try:
                    cb()
                except Exception:  # noqa: BLE001
                    logger.exception("全局热键处理失败")
            return True, 0
        return False, 0


class GlobalHotkeys:
    """全局热键注册器（Windows only）"""

    def __init__(self, hwnd: int | None = None) -> None:
        self._dispatcher: dict[int, Callable[[], None]] = {}
        self._filter: _HotkeyFilter | None = None
        self._registered: dict[int, tuple[str, int, int]] = {}
        self._ok = sys.platform == "win32"
        self._hwnd = hwnd
        self.registered_keys: list[str] = []

    def is_supported(self) -> bool:
        return self._ok

    def _resolve_hwnd(self) -> int:
        """取主窗口句柄（Qt: QApplication.activeWindow() 或 topLevelWidgets）"""
        if self._hwnd:
            return self._hwnd
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            return 0
        w = app.activeWindow()
        if w is None:
            widgets = app.topLevelWidgets()
            w = widgets[0] if widgets else None
        if w is None:
            return 0
        try:
            self._hwnd = int(w.winId())
        except Exception:  # noqa: BLE001
            return 0
        return self._hwnd

    def register_defaults(self, on_capture: Callable[[], None],
                          on_toggle_pause: Callable[[], None],
                          on_stop: Callable[[], None],
                          on_activate: Callable[[], None] | None = None) -> dict[str, bool]:
        """注册默认热键；返回 {动作: 是否成功}"""
        if not self._ok:
            logger.warning("非 Windows 平台，全局热键不可用")
            return {}
        hwnd = self._resolve_hwnd()
        if not hwnd:
            logger.warning("无法获取窗口句柄，全局热键未注册")
            return {}

        if self._filter is None:
            self._filter = _HotkeyFilter(self._dispatcher)
            QCoreApplication.instance().installNativeEventFilter(self._filter)

        VK_F9 = 0x78
        specs = [
            ("F9", VK_F9, 0, on_capture, "手动截图"),
            ("F10", 0x79, 0, on_toggle_pause, "暂停/继续"),
            ("F11", 0x7A, 0, on_stop, "停止录制"),
        ]
        if on_activate:
            specs.append(("Ctrl+Shift+F9", VK_F9, MOD_CONTROL | MOD_SHIFT,
                          on_activate, "唤起主窗口"))

        result: dict[str, bool] = {}
        self.registered_keys = []
        for i, (label, vk, mods, cb, desc) in enumerate(specs):
            hid = _HOTKEY_ID_BASE + i
            ok = ctypes.windll.user32.RegisterHotKey(
                wt.HWND(hwnd), hid, mods | MOD_NOREPEAT, vk)
            if ok:
                self._dispatcher[hid] = cb
                self._registered[hid] = (label, mods, vk)
                self.registered_keys.append(f"{label}={desc}")
                logger.info(f"已注册全局热键 {label} → {desc}")
            else:
                logger.warning(f"注册全局热键 {label} 失败（可能被其他程序占用），"
                               f"该功能请用界面按钮")
                result[label] = False
        return result

    def unregister_all(self) -> None:
        if not self._ok:
            return
        try:
            user32 = ctypes.windll.user32
            for hid in list(self._registered):
                user32.UnregisterHotKey(None, hid)
        except Exception:  # noqa: BLE001
            logger.debug("注销热键失败", exc_info=True)
        self._registered.clear()
        self._dispatcher.clear()

    def help_text(self) -> str:
        if not self.registered_keys:
            return "全局热键：未启用（可使用界面按钮）"
        return "全局热键：" + "，".join(self.registered_keys)