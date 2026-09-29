# -*- coding: utf-8 -*-
"""实时字幕面板：滚动显示转写文本"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import QLabel, QTextEdit, QVBoxLayout, QWidget


class SubtitlePanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 0, 12, 0)

        header = QLabel("实时字幕")
        header.setObjectName("panelHeader")
        lay.addWidget(header)

        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        font = QFont()
        font.setPointSize(11)
        self.text_edit.setFont(font)
        self.text_edit.setPlaceholderText("开始录制后，课程语音将实时转换为文字显示在这里……")
        lay.addWidget(self.text_edit)

    def append_line(self, time_label: str, text: str) -> None:
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertHtml(
            f'<span style="color:#1F4E79;"><b>{time_label}</b></span> {text}<br>')
        self.text_edit.setTextCursor(cursor)
        self.text_edit.ensureCursorVisible()

    def clear_all(self) -> None:
        self.text_edit.clear()
