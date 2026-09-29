# -*- coding: utf-8 -*-
"""在线课程录播器（AI LessonRecorder）程序入口"""
import sys
import os

# 保证以任意工作目录启动时都能找到 src 包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication

from src.app.application import AppContext
from src.ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("在线课程录播器")
    app.setOrganizationName("HIDOTEK")

    ctx = AppContext.instance()  # 加载配置、初始化日志
    win = MainWindow(ctx)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
