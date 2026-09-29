# -*- coding: utf-8 -*-
"""主窗口：录制控制 / 实时字幕 / 截图缩略图 / 菜单（导出、设置、历史会话）"""
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QFileDialog, QLabel, QMainWindow, QMessageBox,
                               QProgressDialog, QStatusBar, QSplitter,
                               QToolBar, QVBoxLayout, QWidget)

from src.app.application import AppContext
from src.app.session_manager import SessionManager
from src.ui.panels.control_panel import ControlPanel
from src.ui.panels.subtitle_panel import SubtitlePanel
from src.ui.panels.thumbnail_panel import ThumbnailPanel
from src.ui.settings_dialog import SettingsDialog
from src.ui.session_list import SessionListDialog
from src.utils.fsutil import open_in_explorer
from src.utils.logger import logger

STYLE = """
QMainWindow { background: #f5f6f8; }
#panelHeader { font-weight: bold; color: #333; padding: 4px 0; }
#btnStart { background: #d93025; color: white; font-weight: bold; padding: 6px 16px; }
#btnStart:disabled { background: #b8b8b8; }
#timerLabel { font-size: 16px; font-weight: bold; color: #1a73e8;
              font-family: Consolas, monospace; }
QTextEdit, QListWidget { background: white; border: 1px solid #ddd; border-radius: 6px; }
QListWidget::item { background: #fff; border: 1px solid #eee; border-radius: 4px; }
"""


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.manager = SessionManager(ctx.config)
        self.setWindowTitle(f"在线课程录播器（AI LessonRecorder）")
        self.resize(1100, 720)
        self.setStyleSheet(STYLE)

        self._build_menu()
        self._build_ui()
        self._wire_bus()
        self._connect_panel()

        # 计时器：刷新录制时长
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_tick)

    # ================= UI 构建 =================
    def _build_menu(self) -> None:
        m_file = self.menuBar().addMenu("文件(&F)")
        act_open_out = QAction("打开输出目录", self)
        act_open_out.triggered.connect(
            lambda: open_in_explorer(str(self.ctx.config.output_root)))
        act_sessions = QAction("历史会话...", self)
        act_sessions.triggered.connect(self._open_sessions)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.close)
        m_file.addAction(act_open_out)
        m_file.addAction(act_sessions)
        m_file.addSeparator()
        m_file.addAction(act_quit)

        m_export = self.menuBar().addMenu("导出(&E)")
        act_word = QAction("导出 Word 逐字稿（本次）", self)
        act_word.triggered.connect(self._export_word_current)
        act_course = QAction("生成 AI 教程文档（本次）", self)
        act_course.triggered.connect(self._generate_course_current)
        m_export.addAction(act_word)
        m_export.addAction(act_course)

        m_set = self.menuBar().addMenu("设置(&S)")
        act_cfg = QAction("偏好设置...", self)
        act_cfg.triggered.connect(self._open_settings)
        m_set.addAction(act_cfg)

        m_help = self.menuBar().addMenu("帮助(&H)")
        act_about = QAction("关于", self)
        act_about.triggered.connect(lambda: QMessageBox.about(
            self, "关于",
            "<b>在线课程录播器（AI LessonRecorder）</b><br>"
            "录制系统声音 → 实时转文字 → PPT 翻页自动截图 →<br>"
            "生成图文逐字稿 MD / Word → AI 梳理成结构化教程文档"))
        m_help.addAction(act_about)

    def _build_ui(self) -> None:
        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self.control = ControlPanel()
        lay.addWidget(self.control)

        self.subtitle = SubtitlePanel()
        self.thumbs = ThumbnailPanel()

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.subtitle)
        splitter.addWidget(self.thumbs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        lay.addWidget(splitter, stretch=1)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

    # ================= 信号接线 =================
    def _connect_panel(self) -> None:
        self.control.start_requested.connect(self._on_start)
        self.control.pause_requested.connect(self.manager.pause)
        self.control.resume_requested.connect(self.manager.resume)
        self.control.stop_requested.connect(self.manager.stop)
        self.control.screenshot_requested.connect(self._force_shot)

    def _wire_bus(self) -> None:
        bus = self.manager.bus
        bus.transcript_added.connect(self.subtitle.append_line)
        bus.slide_added.connect(self.thumbs.add_screenshot)
        bus.status.connect(self.statusBar().showMessage)
        bus.error.connect(self._on_error)
        bus.finished.connect(self._on_finished)

    # ================= 动作 =================
    def _on_start(self, name: str) -> None:
        self.subtitle.clear_all()
        self.thumbs.clear_all()
        if self.manager.start(name):
            self.control.on_state_changed("recording")
            self.timer.start(500)
            self.statusBar().showMessage(f"录制中：{name}")

    def _on_tick(self) -> None:
        rec = self.manager._recorder
        if rec:
            self.control.update_timer(rec.elapsed)

    def _force_shot(self) -> None:
        det = self.manager._detector
        if det and det.is_alive():
            det.force_capture()

    def _on_error(self, msg: str) -> None:
        self.timer.stop()
        self.control.on_state_changed("idle")
        QMessageBox.critical(self, "错误", msg)

    def _on_finished(self, session_root: str) -> None:
        self.timer.stop()
        self.control.on_state_changed("idle")
        ret = QMessageBox.question(
            self, "录制完成",
            f"逐字稿已生成：\n{session_root}\\transcript.md\n\n"
            "是否立即导出 Word 逐字稿？",
            QMessageBox.Yes | QMessageBox.No)
        if ret == QMessageBox.Yes:
            self._export_root(session_root)

    # ---------- 导出 ----------
    def _export_word_current(self) -> None:
        if not self.manager.paths:
            QMessageBox.information(self, "提示", "本次运行还没有完成录制会话")
            return
        self._export_root(str(self.manager.paths.root))

    def _export_root(self, root: str) -> None:
        try:
            p = SessionManager.export_word(root)
            self.statusBar().showMessage(f"Word 已导出: {p}")
            QMessageBox.information(self, "完成", f"已导出：\n{p}")
        except Exception as e:  # noqa: BLE001
            logger.exception("Word 导出失败")
            QMessageBox.critical(self, "错误", f"导出失败：{e}")

    def _generate_course_current(self) -> None:
        if not self.manager.paths:
            QMessageBox.information(self, "提示", "本次运行还没有完成录制会话")
            return
        self._generate_course_for(str(self.manager.paths.root))

    def _generate_course_for(self, root: str) -> None:
        llm_cfg = self.ctx.config.as_dict()["llm"]
        if not (llm_cfg.get("base_url") and llm_cfg.get("api_key")):
            QMessageBox.warning(self, "未配置 LLM",
                                "请先在【设置 → AI 梳理 (LLM)】中填写 Base URL 与 API Key")
            return

        prog = QProgressDialog("AI 正在梳理总结课程内容...", "取消", 0, 100, self)
        prog.setWindowTitle("AI 教程文档生成")
        prog.setMinimumDuration(300)
        prog.setAutoClose(False)

        def on_progress(msg: str, pct: float) -> None:
            prog.setLabelText(msg)
            prog.setValue(int(pct * 100))

        def _run() -> None:
            try:
                md, docx = SessionManager.generate_course(root, llm_cfg, on_progress)
                prog.setValue(100)
                QMessageBox.information(
                    None, "完成", f"AI 教程文档已生成：\n{docx}")
            except Exception as e:  # noqa: BLE001
                logger.exception("AI 教程生成失败")
                QMessageBox.critical(None, "失败", f"生成失败：{e}")

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    # ---------- 对话框 ----------
    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.ctx.config, self)
        if dlg.exec():
            self.statusBar().showMessage("设置已保存")

    def _open_sessions(self) -> None:
        SessionListDialog(self.ctx.config, self).exec()

    # ---------- 关闭保护 ----------
    def closeEvent(self, event) -> None:
        if self.manager.state in ("recording", "paused"):
            ret = QMessageBox.question(
                self, "正在录制",
                "当前会话仍在录制中，停止录制并退出？",
                QMessageBox.Yes | QMessageBox.Cancel)
            if ret != QMessageBox.Yes:
                event.ignore()
                return
            self.manager.stop()
        event.accept()
