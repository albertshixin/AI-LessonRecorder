# -*- coding: utf-8 -*-
"""历史会话列表对话框：查看/重导出/生成AI教程"""
from pathlib import Path

from PySide6.QtWidgets import (QDialog, QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from src.core.session_store import scan_sessions
from src.app.session_manager import SessionManager


class SessionListDialog(QDialog):
    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("历史会话")
        self.resize(760, 420)
        self.config = config

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
        b_open = QPushButton("打开目录")
        b_close = QPushButton("关闭")
        btns.addWidget(b_md)
        btns.addWidget(b_word)
        btns.addWidget(b_course)
        btns.addStretch(1)
        btns.addWidget(b_open)
        btns.addWidget(b_close)
        lay.addLayout(btns)

        b_md.clicked.connect(lambda: self._act("md"))
        b_word.clicked.connect(lambda: self._act("word"))
        b_course.clicked.connect(lambda: self._act("course"))
        b_close.clicked.connect(self.reject)

        from src.utils.fsutil import open_in_explorer
        b_open.clicked.connect(self._open_selected)

        self._sessions = []
        self._reload()

    def _reload(self) -> None:
        self._sessions = scan_sessions(self.config.output_root)
        self.table.setRowCount(len(self._sessions))
        for i, s in enumerate(self._sessions):
            meta = s.read_meta()
            self.table.setItem(i, 0, QTableWidgetItem(
                s.root.name.split("_", 2)[0] if "_" in s.root.name else s.root.name))
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

    def _act(self, action: str) -> None:
        root = self._selected_root()
        if root is None:
            return
        try:
            if action == "md":
                p = SessionManager.rebuild_transcript(str(root))
                QMessageBox.information(self, "完成", f"已重建：\n{p}")
            elif action == "word":
                p = SessionManager.export_word(str(root))
                QMessageBox.information(self, "完成", f"已导出：\n{p}")
            elif action == "course":
                llm_cfg = self.config.as_dict()["llm"]
                if not (llm_cfg.get("base_url") and llm_cfg.get("api_key")):
                    QMessageBox.warning(self, "未配置 LLM",
                                        "请先在【设置 → AI 梳理 (LLM)】中填写 Base URL 与 API Key")
                    return
                QMessageBox.information(
                    self, "处理中",
                    "AI 教程生成已在后台开始，完成后将弹出提示。\n（本对话框可先关闭）")
                import threading
                def _run():
                    try:
                        md, docx = SessionManager.generate_course(str(root), llm_cfg)
                        QMessageBox.information(None, "完成", f"AI 教程已生成：\n{docx}")
                    except Exception as e:  # noqa: BLE001
                        QMessageBox.critical(None, "失败", f"生成失败：{e}")
                threading.Thread(target=_run, daemon=True).start()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "错误", f"操作失败：{e}")
