# -*- coding: utf-8 -*-
"""录制控制面板：课程名输入 + 屏幕/按钮/计时 + 自动截图开关 + 音量条"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel,
                               QLineEdit, QProgressBar, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from src.core.vision.screen_capturer import ScreenCapturer
from src.utils.timeutil import fmt_ts


class ControlPanel(QWidget):
    start_requested = Signal(str)
    pause_requested = Signal()
    resume_requested = Signal()
    stop_requested = Signal()
    screenshot_requested = Signal()
    auto_capture_toggled = Signal(bool)   # 自动截图开关（主界面随时切换）

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)

        # 第一行：课程名 + 采集来源（屏幕 / 应用窗口）
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("课程名称："))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：机器学习入门 第3讲（用于命名输出目录与文档）")
        row1.addWidget(self.name_edit, stretch=1)
        row1.addWidget(QLabel("采集来源："))
        self.source_combo = QComboBox()
        self.source_combo.setMinimumWidth(240)
        self.source_combo.setToolTip(
            "屏幕：截取整个显示器（会包含弹出的其他窗口）\n"
            "窗口：只截取所选应用自身界面，被遮挡/弹窗不影响\n"
            "（设置里配置过“区域”时，选第一项沿用区域）")
        btn_refresh_src = QPushButton("↻")
        btn_refresh_src.setFixedWidth(32)
        btn_refresh_src.setToolTip("刷新窗口列表")
        btn_refresh_src.clicked.connect(self.refresh_sources)
        self.refresh_sources()
        row1.addWidget(self.source_combo)
        row1.addWidget(btn_refresh_src)
        lay.addLayout(row1)

        # 第二行：按钮 + 计时
        row2 = QHBoxLayout()
        self.btn_start = QPushButton("● 开始录制")
        self.btn_start.setObjectName("btnStart")
        self.btn_pause = QPushButton("⏸ 暂停")
        self.btn_stop = QPushButton("■ 停止")
        self.btn_shot = QPushButton("📷 手动截图")
        # 自动截图开关：放主界面（不进设置），录制中也能随时切换；
        # 初始勾选态由 MainWindow 按配置回填（set_auto_capture_checked）
        self.chk_auto = QCheckBox("自动截图")
        self.chk_auto.setToolTip(
            "勾选：页面变化率超过阈值（设置里可调，默认 10%）时自动截图\n"
            "不勾选：全靠手动截图（F9 / 按钮）\n"
            "两种模式互不影响：自动开启时手动截图依然可用，随时切换即时生效")
        self.chk_auto.toggled.connect(self.auto_capture_toggled.emit)
        self.timer_label = QLabel("00:00:00")
        self.timer_label.setObjectName("timerLabel")
        self.timer_label.setAlignment(Qt.AlignCenter)
        self.timer_label.setMinimumWidth(90)

        row2.addWidget(self.btn_start)
        row2.addWidget(self.btn_pause)
        row2.addWidget(self.btn_stop)
        row2.addWidget(self.btn_shot)
        row2.addWidget(self.chk_auto)
        row2.addStretch(1)
        row2.addWidget(QLabel("录制时长："))
        row2.addWidget(self.timer_label)
        lay.addLayout(row2)

        # 第三行：音量条（实时显示系统声音电平，方便判断录音是否在工作）
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("声音电平："))
        self.level_bar = QProgressBar()
        self.level_bar.setRange(0, 100)
        self.level_bar.setValue(0)
        self.level_bar.setTextVisible(False)
        self.level_bar.setFixedHeight(10)
        self.level_bar.setStyleSheet(
            "QProgressBar { background:#eee; border:1px solid #ccc; border-radius:5px; }"
            "QProgressBar::chunk { background:#34a853; border-radius:4px; }"
        )
        row3.addWidget(self.level_bar, stretch=1)
        lay.addLayout(row3)

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

    def update_level(self, level: float) -> None:
        """电平 0~1 → 0~100"""
        v = max(0, min(100, int(level * 100)))
        self.level_bar.setValue(v)

    def set_auto_capture_checked(self, on: bool) -> None:
        """按配置回填勾选态（不发 toggled 信号）"""
        self.chk_auto.blockSignals(True)
        self.chk_auto.setChecked(bool(on))
        self.chk_auto.blockSignals(False)

    def refresh_monitors(self) -> None:
        """兼容旧调用：刷新采集来源列表"""
        if hasattr(self, "source_combo"):
            self.refresh_sources()

    def refresh_sources(self) -> None:
        """填充采集来源：屏幕显示器 + 当前打开的应用窗口（保持当前选择）"""
        prev = self.selected_source() if hasattr(self, "source_combo") else None
        combo = self.source_combo
        combo.blockSignals(True)
        combo.clear()
        # 第一项：跟随设置（primary/region，行为与旧版一致）
        combo.addItem("跟随设置（显示器/区域）", None)
        for idx, name, _w, _h in ScreenCapturer.list_monitors():
            combo.addItem(f"🖥 屏幕：{name}", ("monitor", idx))
        for title, w, h in ScreenCapturer.list_windows():
            t = title if len(title) <= 34 else title[:33] + "…"
            combo.addItem(f"🪟 窗口：{t}（{w}x{h}）", ("window", title))
        combo.blockSignals(False)
        # 恢复上次选择（按 data 匹配；窗口已关闭则回到第一项）
        pos = 0
        if prev is not None:
            for i in range(combo.count()):
                if combo.itemData(i) == prev:
                    pos = i
                    break
        combo.setCurrentIndex(pos)

    def selected_source(self) -> tuple[str, object] | None:
        """当前采集来源：None=跟随设置 | ("monitor", 序号) | ("window", 窗口标题)"""
        data = self.source_combo.currentData() if hasattr(self, "source_combo") else None
        return data

    def selected_monitor(self) -> int:
        """兼容旧调用：所选显示器序号（窗口模式返回 1）"""
        src = self.selected_source()
        if src and src[0] == "monitor":
            return int(src[1])
        return 1

    def apply_hotkey_hints(self, registered: list[str]) -> None:
        """把实际生效的全局热键回写到按钮 tooltip

        `registered` 形如 ["F9=手动截图", "F10=暂停/继续", "F11=停止录制"]。
        只提示真正注册成功的键位，避免"标了快捷键但按了没反应"的困惑。
        """
        mapping = {"F9": self.btn_shot, "F10": self.btn_pause,
                   "F11": self.btn_stop}
        base = {"F9": "手动截图", "F10": "暂停/继续", "F11": "停止录制"}
        for item in registered:
            key = item.split("=", 1)[0]
            btn = mapping.get(key)
            if btn:
                btn.setToolTip(f"{base[key]}　快捷键 {key}（全局，窗口失焦时可用）")
        # 未注册成功的键位明确告知"只能用按钮"
        for key, btn in mapping.items():
            if not btn.toolTip():
                btn.setToolTip(f"{base[key]}　（全局热键 {key} 未注册：可能被其他程序占用）")

    def on_state_changed(self, state: str) -> None:
        self._set_state(state)

    def update_timer(self, seconds: float) -> None:
        self.timer_label.setText(fmt_ts(seconds))

    def set_recording_name(self, name: str) -> None:
        self.name_edit.setText(name)
