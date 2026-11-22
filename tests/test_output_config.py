# -*- coding: utf-8 -*-
"""输出目录与配置相关测试

覆盖：
1. AppConfig.output_root 解析（绝对 / 相对 / 缺省）
2. AppConfig 保存与重新加载对 output.dir 的 round trip
3. fsutil.open_in_explorer 在路径不存在 / 文件 / 目录 / 空串 时不抛异常
4. SettingsDialog _build_output_tab 在 offscreen Qt 下能构建并刷新预览
5. MainWindow 状态栏常驻链接（输出 / 配置）能在 offscreen 下被创建
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASSED = 0
FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  [OK] {msg}")
    else:
        FAILURES.append(msg)
        print(f"  [FAIL] {msg}")


def _run(name: str, fn, *args) -> None:
    """单个测试包 try/except，避免一处崩溃影响其他用例"""
    print(f"\n[{name}]")
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(False, f"{name} 不应抛异常：{e!r}")


# ─────────────────────────────────────────────────────────────────────
# 1) AppConfig.output_root 解析
# ─────────────────────────────────────────────────────────────────────
def test_output_root_parsing(tmp: Path) -> None:
    from src.utils.config import AppConfig
    cfg_path = tmp / "cfg" / "settings.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("{}", encoding="utf-8")
    cfg = AppConfig(cfg_path)

    p = cfg.output_root
    check(str(p).endswith(os.path.join("output")), f"缺省解析到 output（{p}）")
    check(p.is_absolute(), "output_root 是绝对路径")

    abs_dir = tmp / "my_out"
    cfg.set("output.dir", str(abs_dir))
    abs_dir.mkdir(parents=True, exist_ok=True)
    check(cfg.output_root.resolve() == abs_dir.resolve(), "绝对路径原样返回")

    cfg.set("output.dir", "sub/dir")
    rel = cfg.output_root
    check(str(rel).endswith(os.path.join("sub", "dir")) and rel.is_absolute(),
          f"相对路径解析为绝对（{rel}）")


# ─────────────────────────────────────────────────────────────────────
# 2) round trip
# ─────────────────────────────────────────────────────────────────────
def test_output_dir_roundtrip(tmp: Path) -> None:
    from src.utils.config import AppConfig
    cfg_path = tmp / "settings.json"
    cfg = AppConfig(cfg_path)
    cfg.set("output.dir", str(tmp / "absolute_out"))
    cfg.set("output.keep_audio", False)
    cfg.save()
    cfg2 = AppConfig(cfg_path)
    check(cfg2.get("output.dir") == str(tmp / "absolute_out"),
          "保存后重新加载：output.dir 完整")
    check(cfg2.get("output.keep_audio") is False, "保存后重新加载：keep_audio 完整")
    check(cfg2.output_root == (tmp / "absolute_out").resolve(),
          "output_root 与设置一致")


# ─────────────────────────────────────────────────────────────────────
# 3) open_in_explorer 防错
# ─────────────────────────────────────────────────────────────────────
def test_open_in_explorer_safety(tmp: Path) -> None:
    from src.utils.fsutil import open_in_explorer

    with mock.patch("os.startfile") as sf:
        ok = open_in_explorer(str(tmp / "nope"))
    check(ok is False and sf.call_count == 0, "不存在路径不调 startfile")

    with mock.patch("os.startfile") as sf:
        ok = open_in_explorer("")
    check(ok is False and sf.call_count == 0, "空串不调 startfile")

    real_dir = tmp / "real"
    real_dir.mkdir()
    with mock.patch("os.startfile") as sf:
        ok = open_in_explorer(str(real_dir))
    check(ok is True and sf.call_count == 1, "存在目录 → 调 startfile 一次")

    f = tmp / "a.txt"
    f.write_text("x", encoding="utf-8")
    with mock.patch("os.startfile") as sf:
        ok = open_in_explorer(str(f))
    check(ok is True and sf.call_count == 1, "文件 → 打开父目录")


# ─────────────────────────────────────────────────────────────────────
# 4) SettingsDialog 输出 Tab 烟雾
# ─────────────────────────────────────────────────────────────────────
def test_settings_output_tab_smoke(tmp: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from src.utils.config import AppConfig
    from src.ui.settings_dialog import SettingsDialog

    cfg_path = tmp / "settings.json"
    cfg_path.write_text("{}", encoding="utf-8")
    cfg = AppConfig(cfg_path)
    dlg = SettingsDialog(cfg)
    try:
        check(dlg.out_dir is not None, "out_dir 控件已创建")
        check(dlg.out_dir_preview.text() != "", "实际路径预览已填充")
        dlg.out_dir.setText(str(tmp / "custom_out"))
        check(str(tmp / "custom_out") in dlg.out_dir_preview.text(),
              "修改 out_dir 后预览更新")
        check(str(cfg_path) in dlg.cfg_path_lbl.text(),
              "配置文件位置标签展示绝对路径")
    finally:
        dlg.close()


# ─────────────────────────────────────────────────────────────────────
# 5) MainWindow 状态栏常驻链接烟雾
#  —— MainWindow 启动会注册全局热键 + 初始化截图/录音等，先单独测 _build_status_paths
# ─────────────────────────────────────────────────────────────────────
def test_main_window_status_paths(tmp: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QMainWindow, QStatusBar
    from src.utils.config import AppConfig
    from src.ui.main_window import MainWindow

    cfg_path = tmp / "cfg" / "settings.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("{}", encoding="utf-8")
    cfg = AppConfig(cfg_path)

    # 旁路掉会失败的副作用（热键、SessionManager 内的截图设备）
    with mock.patch("src.ui.main_window.GlobalHotkeys") as gh, \
         mock.patch("src.ui.main_window.SessionManager"):
        gh.return_value.register_defaults.return_value = {}
        gh.return_value.registered_keys = {}
        win = MainWindow.__new__(MainWindow)  # 绕过 __init__
        win.ctx = mock.MagicMock()
        win.ctx.config = cfg
        QMainWindow.__init__(win)             # 单独构造 QMainWindow 基类
        win.setStatusBar(QStatusBar())
        win._build_status_paths()             # 直接调用我们要测的逻辑

    try:
        text = win._status_paths.text()
        check("输出" in text and "配置" in text,
              f"状态栏链接同时含 输出/配置（{text!r}）")
        check("href=\"out\"" in text and "href=\"cfg\"" in text,
              "状态栏链接含 out/cfg href")
        check(str(cfg.output_root) in text, "链接展示实际输出目录绝对路径")
        check(str(cfg.path) in text, "链接展示实际配置文件绝对路径")
    finally:
        # 绕开 QMainWindow.closeEvent 触发的清理（我们没跑完整 __init__）
        from PySide6.QtGui import QCloseEvent
        win.closeEvent = lambda e: e.accept()  # type: ignore[assignment]
        win.close()


def main() -> int:
    print("=" * 70)
    print("输出目录与配置测试")
    print("=" * 70)
    with tempfile.TemporaryDirectory(prefix="lr_outcfg_") as td:
        tmp = Path(td)
        _run("1 output_root 解析", test_output_root_parsing, tmp)
        _run("2 round trip",      test_output_dir_roundtrip,  tmp)
        _run("3 open_in_explorer", test_open_in_explorer_safety, tmp)
        _run("4 SettingsDialog",  test_settings_output_tab_smoke, tmp)
        _run("5 MainWindow 状态栏", test_main_window_status_paths, tmp)

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
