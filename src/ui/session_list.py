# -*- coding: utf-8 -*-
"""历史会话列表对话框：查看 / 重建逐字稿 / 导出 Word / 生成 AI 教程 / 离线重转写

v1.2 修复的 P0 级缺陷：
- **后台线程直接弹 QMessageBox**（与 main_window 的 P0-2 同源）：
  原实现在 `threading.Thread` 里调用 `QMessageBox.information(None, ...)`，
  Qt 控件非线程安全 → 随机崩溃。现改为与主窗口一致的 `_AsyncWorker` +
  `Qt.QueuedConnection` 模式：后台线程只发信号，UI 全在主线程操作。
- 顺带把该处重复的下载/重转写逻辑统一走 `MainWindow` 提供的公共服务，
  避免两处实现漂移。
"""
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QProgressDialog, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from src.app.session_manager import SessionManager
from src.core.session_store import scan_sessions


class _Job(QObject):
    """后台任务：只发信号，绝不碰 UI"""
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, fn) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn() or "完成")
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class SessionListDialog(QDialog):
    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("历史会话")
        self.resize(820, 440)
        self.config = config
        self._jobs: list[tuple[QThread, _Job]] = []

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("选择一个历史录制会话："))

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["时间", "课程名", "转写段数", "截图数"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self.table)

        btns = QHBoxLayout()
        b_md = QPushButton("重建逐字稿 MD")
        b_word = QPushButton("导出 Word 逐字稿")
        b_course = QPushButton("生成 AI 教程文档")
        b_retrans = QPushButton("离线重转写…")
        b_open = QPushButton("打开目录")
        b_close = QPushButton("关闭")
        btns.addWidget(b_md)
        btns.addWidget(b_word)
        btns.addWidget(b_course)
        btns.addWidget(b_retrans)
        btns.addStretch(1)
        btns.addWidget(b_open)
        btns.addWidget(b_close)
        lay.addLayout(btns)

        b_md.clicked.connect(lambda: self._act("md"))
        b_word.clicked.connect(lambda: self._act("word"))
        b_course.clicked.connect(lambda: self._act("course"))
        b_retrans.clicked.connect(lambda: self._act("retranscribe"))
        b_close.clicked.connect(self.reject)
        b_open.clicked.connect(self._open_selected)

        self._sessions = []
        self._reload()

    # ---------- 数据 ----------
    def _reload(self) -> None:
        self._sessions = scan_sessions(self.config.output_root)
        self.table.setRowCount(len(self._sessions))
        for i, s in enumerate(self._sessions):
            meta = s.read_meta()
            stamp = s.root.name.split("_", 2)[0] if "_" in s.root.name else s.root.name
            self.table.setItem(i, 0, QTableWidgetItem(stamp))
            self.table.setItem(i, 1, QTableWidgetItem(s.name))
            self.table.setItem(i, 2, QTableWidgetItem(str(meta.get("transcript_count", "-"))))
            self.table.setItem(i, 3, QTableWidgetItem(str(meta.get("slide_count", "-"))))

    def _selected_root(self) -> Path | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "提示", "请先选择一个会话")
            return None
        return self._sessions[rows[0].row()].root

    def _open_selected(self) -> None:
        root = self._selected_root()
        if root:
            from src.utils.fsutil import open_in_explorer
            open_in_explorer(str(root))

    # ---------- 动作分发 ----------
    def _act(self, action: str) -> None:
        root = self._selected_root()
        if root is None:
            return

        if action == "md":
            self._run_bg(
                "重建逐字稿",
                lambda p: f"已重建：\n{p}",
                lambda: str(SessionManager.rebuild_transcript(str(root))))
        elif action == "word":
            self._run_bg("导出 Word",
                         lambda: f"已导出：\n{SessionManager.export_word(str(root))}")
        elif action == "course":
            llm_cfg = self.config.as_dict()["llm"]
            if not (llm_cfg.get("base_url") and llm_cfg.get("api_key")):
                QMessageBox.warning(
                    self, "未配置 LLM",
                    "请先在【设置 → AI 梳理 (LLM)】中填写 Base URL 与 API Key")
                return
            self._run_bg(
                "生成 AI 教程文档",
                f"AI 教程已生成：\n{root}",
                lambda: f"AI 教程已生成：\n{SessionManager.generate_course(str(root), llm_cfg)[1]}")
        elif action == "retranscribe":
            # 复用主窗口能力（模型检查 / 确认 / 离线转写 / 取消），
            # 避免在两处重复实现同一业务流程。
            main = self._main_window()
            if main is None:
                QMessageBox.information(self, "提示", "无法定位主窗口，请从主窗口菜单操作")
                return
            self.accept()
            main._retranscribe_for(str(root))

    def _main_window(self):
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            return None
        for w in app.topLevelWidgets():
            if w.__class__.__name__ == "MainWindow":
                return w
        return None

    # ---------- 后台执行（UI 全在主线程） ----------
    def _run_bg(self, title: str, on_ok, fn) -> None:
        """fn 在子线程执行；成功/失败提示一律回主线程弹出

        `on_ok` 可以是字符串模板（.format(result)）或可调用对象。
        """
        prog = QProgressDialog(f"{title}中…", "", 0, 0, self)
        prog.setWindowTitle(title)
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)
        prog.setAutoClose(False)
        prog.setAutoReset(False)
        prog.setCancelButton(None)
        prog.setValue(0)

        job = _Job(fn)
        thread = QThread(self)
        job.moveToThread(thread)
        thread.started.connect(job.run)
        q = Qt.QueuedConnection

        def _on_done(result: str) -> None:
            prog.close()
            self._reload()
            msg = on_ok.format(result) if isinstance(on_ok, str) else on_ok(result)
            QMessageBox.information(self, "完成", msg)

        def _on_fail(msg: str) -> None:
            prog.close()
            QMessageBox.critical(self, "失败", f"{title}失败：{msg}")

        job.done.connect(_on_done, q)
        job.failed.connect(_on_fail, q)
        thread.finished.connect(job.deleteLater)
        self._jobs.append((thread, job))
        thread.start()
        prog.exec()

    def closeEvent(self, event) -> None:
        for thread, _job in list(self._jobs):
            if thread.isRunning():
                thread.requestInterruption()
                thread.quit()
        event.accept()