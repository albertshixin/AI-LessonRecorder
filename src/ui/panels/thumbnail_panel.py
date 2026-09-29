# -*- coding: utf-8 -*-
"""截图缩略图面板：横向滚动显示已捕获的 PPT 页"""
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QListWidget, QListWidgetItem, QLabel,
                               QVBoxLayout, QWidget)


class ThumbnailPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 0, 12, 0)

        header = QLabel("PPT 截图")
        header.setObjectName("panelHeader")
        lay.addWidget(header)

        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.IconMode)
        self.list_widget.setIconSize(QSize(220, 124))
        self.list_widget.setResizeMode(QListWidget.Adjust)
        self.list_widget.setMovement(QListWidget.Static)
        self.list_widget.setSpacing(8)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list_widget.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_widget.setMinimumHeight(160)
        lay.addWidget(self.list_widget)

    def add_screenshot(self, image_path: str, index: int) -> None:
        pm = QPixmap(image_path)
        if pm.isNull():
            return
        item = QListWidgetItem(pm, f"第{index}页")
        item.setData(Qt.UserRole, image_path)
        self.list_widget.addItem(item)
        self.list_widget.scrollToBottom()

    def clear_all(self) -> None:
        self.list_widget.clear()
