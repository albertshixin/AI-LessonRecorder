# -*- coding: utf-8 -*-
"""随软件分发的资源回归：使用说明文档 / 微信二维码 / 「关于」窗口

缺陷回归：
1. 打包后资源在 sys._MEIPASS（onedir 的 _internal/）下，路径解析错会导致
   「帮助 → 使用说明」与「关于」二维码全部失效
2. 「关于」对话框曾因 main_window 缺少 QHBoxLayout 导入而在点击时抛
   NameError（被 Qt 事件循环吞掉，表现为"点不出来"）——本套件直接调用
   该槽函数防回归
3. 二维码只出现在「关于」窗口；使用手册中不得内嵌二维码图片
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils.fsutil import resource_path  # noqa: E402

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


def case_files() -> None:
    print("\n[1] 资源文件")
    manual = resource_path("assets/user_manual.html")
    check(manual.exists(), f"使用说明存在：{manual.name}")

    if manual.exists():
        text = manual.read_text(encoding="utf-8")
        check("Albert 老石" in text, "使用说明含开发者署名（Albert 老石）")
        check("关于" in text, "使用说明引导用户到「帮助 → 关于」找联系方式")
        check("wechat_qr.png" not in text, "使用说明不内嵌二维码（二维码只在关于窗口）")
        check("自动截图" in text and "手动截图" in text,
              "使用说明覆盖截图功能（手动/自动开关）")
        check("离线重转写" in text, "使用说明覆盖离线重转写")
        check("<!DOCTYPE html>" in text, "HTML 格式有效（浏览器可直接打开）")

    qr = resource_path("assets/wechat_qr.png")
    check(qr.exists(), f"微信二维码存在（随包分发）：{qr.name}")
    if qr.exists():
        check(qr.stat().st_size > 10_000,
              f"二维码非空文件（{qr.stat().st_size / 1024:.0f} KB）")
        check(qr.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", "PNG 格式有效")

    check(manual.parent.name == "assets"
          and manual.parent.parent.name == "4_AI-LessonRecorder",
          "源码模式下解析到项目 assets/ 目录")


def case_about_dialog() -> None:
    print("\n[2] 「关于」窗口可正常弹出（缺 QHBoxLayout 曾致 NameError 点不出来）")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QDialog
    from src.utils.config import AppConfig
    from src.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="lr_res_"))
    cfg = AppConfig(tmp / "cfg" / "settings.json")
    cfg.set("output.dir", str(tmp / "output"))
    ctx = type("Ctx", (), {})()
    ctx.config = cfg
    ctx.app_root = tmp

    w = MainWindow(ctx)
    # 菜单里有「关于」且已接线
    help_menu = w.menuBar().actions()[-1].menu()
    texts = [a.text() for a in help_menu.actions()]
    check("关于" in texts, f"帮助菜单含「关于」（{texts}）")
    about_act = next((a for a in help_menu.actions() if a.text() == "关于"), None)
    check(about_act is not None, "「关于」菜单动作存在（连接性由下方直接调槽验证）")

    # exec 改为立即返回，直接调用槽：任何异常都算回归
    QDialog.exec = lambda self, *a, **k: 0
    try:
        w._show_about()
        ok = True
    except Exception:
        traceback.print_exc()
        ok = False
    check(ok, "调用「关于」槽函数无异常（对话框可弹出）")


def main() -> int:
    print("=" * 70)
    print("随软件分发资源测试（含「关于」窗口回归）")
    print("=" * 70)
    case_files()
    case_about_dialog()
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
