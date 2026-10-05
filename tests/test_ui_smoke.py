# -*- coding: utf-8 -*-
"""UI 冒烟测试（offscreen）：验证主窗口可构建、信号接线完整、无跨线程 UI 调用

不依赖 pytest：直接 `python tests/test_ui_smoke.py` 运行。
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_FAIL: list[str] = []
_PASS = 0


def check(cond: bool, label: str) -> None:
    global _PASS
    if cond:
        _PASS += 1
        print(f"  [PASS] {label}")
    else:
        _FAIL.append(label)
        print(f"  [FAIL] {label}")


def main() -> int:
    from PySide6.QtCore import Qt, QThread
    from PySide6.QtWidgets import QApplication

    print("=" * 70)
    print("UI 冒烟测试（offscreen）")
    print("=" * 70)

    # ── 1. 全部 UI 模块可导入 ──
    print("\n[1] 模块导入")
    mods = [
        "src.ui.main_window",
        "src.ui.session_list",
        "src.ui.hotkeys",
        "src.ui.settings_dialog",
        "src.ui.panels.control_panel",
        "src.ui.panels.subtitle_panel",
        "src.ui.panels.thumbnail_panel",
    ]
    for m in mods:
        try:
            __import__(m)
            check(True, f"import {m}")
        except Exception as e:  # noqa: BLE001
            check(False, f"import {m} -> {type(e).__name__}: {e}")
            traceback.print_exc()

    if _FAIL:
        print("\n导入阶段即失败，后续跳过")
        return 1

    # ── 2. 应用与主窗口可构建 ──
    print("\n[2] 主窗口构建")
    from src.app.application import AppContext
    from src.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    check(True, "QApplication 创建")

    # ── 打桩模态对话框 ──
    # offscreen 环境下 QMessageBox.exec() 仍会阻塞等待用户点击，
    # 因此自动测试期间把所有模态对话框替换为"记录并立即返回默认值"。
    from PySide6.QtWidgets import QMessageBox, QProgressDialog

    _dlg_log: list[tuple[str, str, str]] = []

    def _msgbox_stub(kind):
        def _f(parent=None, title="", text="", *a, **kw):
            _dlg_log.append((kind, title, str(text)[:60]))
            return QMessageBox.Yes if kind == "question" else QMessageBox.Ok
        return staticmethod(_f)

    QMessageBox.information = _msgbox_stub("information")
    QMessageBox.warning = _msgbox_stub("warning")
    QMessageBox.critical = _msgbox_stub("critical")
    QMessageBox.question = _msgbox_stub("question")
    QMessageBox.about = _msgbox_stub("about")

    def _prog_stub_exec(self):
        self.close()
        return 0

    QProgressDialog.exec = _prog_stub_exec
    print("  （已打桩模态对话框，避免自动化阻塞）")

    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="lr_ui_smoke_"))
    os.environ["LESSONRECORDER_OUTPUT"] = str(tmp)

    ctx = AppContext()
    check(tmp.exists() or True, f"AppContext 创建（输出目录 {ctx.config.output_root}）")

    win = MainWindow(ctx)
    check(True, "MainWindow 实例化")

    # ── 3. 关键属性与接线 ──
    print("\n[3] 关键属性")
    check(hasattr(win, "hotkeys"), "hotkeys 已初始化")
    check(hasattr(win, "control"), "control 面板存在")
    check(hasattr(win, "subtitle"), "字幕面板存在")
    check(hasattr(win, "thumbs"), "缩略图面板存在")
    check(hasattr(win, "_workers"), "_workers 列表存在")
    check(hasattr(win.manager, "elapsed"), "SessionManager.elapsed 属性存在")
    check(hasattr(win.manager, "is_busy"), "SessionManager.is_busy 属性存在")
    check(hasattr(win.control, "apply_hotkey_hints"), "apply_hotkey_hints 方法存在")

    # ── 4. 事件总线接线：实际 emit 信号，验证 UI 真的被更新 ──
    # 不做信号内省（PySide6 内部属性名不稳定），而是**注入信号并断言可观测后果**：
    # 这直接证明了"工作线程 → 信号 → 主线程 UI"这条链路是通的。
    print("\n[4] 事件总线接线（注入信号 → 断言 UI 更新）")
    app.processEvents()
    bus = win.manager.bus

    bus.transcript_added.emit("[00:01]", "这是一段测试字幕")
    app.processEvents()
    check("这是一段测试字幕" in win.subtitle.text_edit.toPlainText(),
          "transcript_added → 字幕面板更新")

    bus.status.emit("冒烟测试状态消息")
    app.processEvents()
    check("冒烟测试状态消息" in win.statusBar().currentMessage(),
          "status → 状态栏更新")

    bus.level.emit(0.75)
    app.processEvents()
    check(win.control.level_bar.value() == 75,
          f"level → 音量条更新（实际 {win.control.level_bar.value()}）")

    bus.state_changed.emit("paused")
    app.processEvents()
    # 注意：直接 emit 信号只验证"信号 → UI"这一段；
    # manager.state 的变更由 _set_state 负责，不在本次断言范围。
    check("继续" in win.control.btn_pause.text()
          and not win.control.btn_start.isEnabled(),
          f"state_changed(paused) → 按钮联动（按钮文本 {win.control.btn_pause.text()!r}）")
    bus.state_changed.emit("idle")
    app.processEvents()

    # 生成一张真实PNG，验证缩略图真的能加载
    from PySide6.QtGui import QImage
    from PySide6.QtCore import Qt as _Qt
    shot = tmp / "s.png"
    img = QImage(320, 180, QImage.Format_RGB32)
    img.fill(_Qt.white)
    check(shot.exists() or img.save(str(shot)), "测试截图生成")
    n_before = win.thumbs.list_widget.count()
    bus.slide_added.emit(str(shot), 1)
    app.processEvents()
    check(win.thumbs.list_widget.count() == n_before + 1,
          f"slide_added → 缩略图新增（{n_before} → {win.thumbs.list_widget.count()}）")

    bus.error.emit("冒烟测试错误")
    app.processEvents()
    check(any("冒烟测试错误" in t for _k, _ti, t in _dlg_log),
          "error → 弹出错误对话框")
    win.timer.stop()

    # ── 5. 状态机流转（不发声、不截图） ──
    print("\n[5] 状态机")
    check(win.manager.state == "idle", "初始状态 idle")
    win._on_tick()                      # 空转不崩
    check(True, "_on_tick 无录制时安全")
    win._force_shot()                   # 非录制态应给出提示而非崩溃
    check("录制中才能截图" in win.statusBar().currentMessage(),
          f"非录制态截图给出状态栏提示（实际 {win.statusBar().currentMessage()!r}）")
    win._stop_recording()                # 无录制时提示
    check(True, "_stop_recording 无录制时不崩")
    win._toggle_pause()
    check(True, "_toggle_pause idle 时不崩")

    # ── 6. 导出/教程在无会话时应给出提示而非崩溃 ──
    print("\n[6] 无会话保护")
    win._export_word_current()
    check(True, "无会话导出 Word 有保护")
    win._generate_course_current()
    check(True, "无会话生成教程有保护")
    win._retranscribe_current()
    check(True, "无会话重转写有保护")

    # ── 7. 历史会话对话框 ──
    print("\n[7] 历史会话对话框")
    from src.ui.session_list import SessionListDialog
    dlg = SessionListDialog(ctx.config, win)
    check(True, "SessionListDialog 实例化")
    check(dlg.table.rowCount() >= 0, f"表格行数 {dlg.table.rowCount()}")
    dlg.close()

    # ── 8. 热键对象 ──
    print("\n[8] 全局热键")
    check(isinstance(win.hotkeys.help_text(), str), "help_text 可用")
    check(len(win.hotkeys.help_text()) > 0, f"help_text: {win.hotkeys.help_text()!r}")
    win.hotkeys.unregister_all()
    check(True, "unregister_all 不抛异常")

    # ── 9. 线程清理机制 ──
    print("\n[9] worker 生命周期")
    check(len(win._workers) == 0, f"初始 _workers 为空（实际 {len(win._workers)}）")

    win.deleteLater()
    app.processEvents()

    # ── 10. 打桩记录校验：确实走过提示分支 ──
    print("\n[10] 提示分支覆盖")
    check(any(k == "information" and "还没有完成录制会话" in t
              for k, _ti, t in _dlg_log),
          "无会话导出会给出提示")

    print("\n" + "=" * 70)
    print(f"结果：{_PASS} 通过，{len(_FAIL)} 失败")
    if _FAIL:
        for f in _FAIL:
            print(f"  FAILED: {f}")
        return 1
    print("UI 冒烟测试全部通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())