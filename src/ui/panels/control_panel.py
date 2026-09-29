# -*- coding: utf-8 -*-
"""录制控制面板：课程名输入 + 开始/暂停/停止按钮 + 计时"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from src.utils.timeutil import fmt_ts


class ControlPanel(QWidget):
    start_requested = Signal(str)
    pause_requested = Signal()
    resume_requested = Signal()
    stop_requested = Signal()
    screenshot_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)

        # 第一行：课程名
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("课程名称："))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：机器学习入门 第3讲（用于命名输出目录与文档）")
        row1.addWidget(self.name_edit, stretch=1)
        lay.addLayout(row1)

        # 第二行：按钮
        row2 = QHBoxLayout()
        self.btn_start = QPushButton("● 开始录制")
        self.btn_start.setObjectName("btnStart")
        self.btn_pause = QPushButton("⏸ 暂停")
        self.btn_stop = QPushButton("■ 停止")
        self.btn_shot = QPushButton("📷 手动截图")
        self.timer_label = QLabel("00:00:00")
        self.timer_label.setObjectName("timerLabel")
        self.timer_label.setAlignment(Qt.AlignCenter)
        self.timer_label.setMinimumWidth(90)

        row2.addWidget(self.btn_start)
        row2.addWidget(self.btn_pause)
        row2.addWidget(self.btn_stop)
        row2.addWidget(self.btn_shot)
        row2.addStretch(1)
        row2.addWidget(QLabel("录制时长："))
        row2.addWidget(self.timer_label)
        lay.addLayout(row2)

        # 信号
        self.btn_start.clicked.connect(self._on_start)
        self.btn_pause.clicked.connect(self._on_toggle_pause)
        self.btn_stop.clicked.connect(self.stop_requested.emit)
        self.btn_shot.clicked.connect(self.screenshot_requested.emit)

        self._paused = False
        self._set_state("idle")

    def _on_start(self) -> None:
        name = self.name_edit.text().strip() or "未命名课程"
        self.start_requested.emit(name)

    def _on_toggle_pause(self) -> None:
        if self._paused:
            self.resume_requested.emit()
        else:
            self.pause_requested.emit()

    # ---------- 状态刷新 ----------
    def _set_state(self, state: str) -> None:
        self.btn_start.setEnabled(state == "idle")
        self.btn_pause.setEnabled(state in ("recording", "paused"))
        self.btn_stop.setEnabled(state in ("recording", "paused"))
        self.btn_shot.setEnabled(state in ("recording", "paused"))
        if state == "paused":
            self.btn_pause.setText("▶ 继续")
        else:
            self.btn_pause.setText("⏸ 暂停")
        self._paused = state == "paused"

    def on_state_changed(self, state: str) -> None:
        self._set_state(state)

    def update_timer(self, seconds: float) -> None:
        self.timer_label.setText(fmt_ts(seconds))

    def set_recording_name(self, name: str) -> None:
        self.name_edit.setText(name)
