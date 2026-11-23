# -*- coding: utf-8 -*-
"""主窗口：录制控制 / 实时字幕 / 截图缩略图 / 菜单（导出、离线重转写、设置、历史）

v1.2 修复的 P0 级缺陷：
- **后台线程直接操作 UI**（原 P0-2）：原实现在 AI 教程生成的后台线程里直接调用
  `QMessageBox.information` / `QProgressDialog.setValue`。Qt 控件**不是线程安全的**，
  跨线程调用会导致随机崩溃（该路径必崩）。现改为：后台线程只发信号，
  所有 UI 操作经由 Qt 信号回到主线程执行（`_AsyncWorker` QObject + Signal），
  且所有连接**显式指定 Qt.QueuedConnection**，不依赖 PySide 对普通函数的
  线程推断（这是"稳定可靠"的硬保证，而非依赖约定）。
- **截图热键跨线程**：改用 `SessionManager.force_capture()` 投递命令，
  不再从 UI 线程直接操作 mss。
- **QThread 泄漏**：原实现把每个 (thread, worker) 永久留在列表里，
  长时间使用会累积已死线程对象。现改为任务结束自动摘除。
- 新增**全局快捷键**（F9/F10/F11）与**离线重转写**入口。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QProgressDialog,
                               QPushButton, QSplitter, QStatusBar,
                               QVBoxLayout, QWidget)

from src.ai.asr.model_manager import MODEL_SIZE_HINT_MB, ModelManager
from src.ai.asr.offline_retranscribe import OfflineRetranscriber
from src.app.application import AppContext
from src.app.session_manager import SessionManager
from src.ui.hotkeys import GlobalHotkeys
from src.ui.panels.control_panel import ControlPanel
from src.ui.panels.subtitle_panel import SubtitlePanel
from src.ui.panels.thumbnail_panel import ThumbnailPanel
from src.ui.session_list import SessionListDialog
from src.ui.settings_dialog import SettingsDialog
from src.utils.fsutil import open_in_explorer
from src.utils.logger import logger

STYLE_LIGHT = """
QMainWindow { background: #f5f6f8; }
#panelHeader { font-weight: bold; color: #333; padding: 4px 0; }
#btnStart { background: #d93025; color: white; font-weight: bold; padding: 6px 16px; border-radius: 4px; }
#btnStart:disabled { background: #b8b8b8; }
#timerLabel { font-size: 16px; font-weight: bold; color: #1a73e8; font-family: Consolas, monospace; }
QTextEdit, QListWidget { background: white; border: 1px solid #ddd; border-radius: 6px; }
QListWidget::item { background: #fff; border: 1px solid #eee; border-radius: 4px; }
"""

STYLE_DARK = """
QMainWindow { background: #1e1f22; }
QLabel, QMenuBar, QStatusBar { color: #e3e3e6; }
#panelHeader { font-weight: bold; color: #cfcfd4; padding: 4px 0; }
#btnStart { background: #c5221f; color: white; font-weight: bold; padding: 6px 16px; border-radius: 4px; }
#btnStart:disabled { background: #4a4b50; }
#timerLabel { font-size: 16px; font-weight: bold; color: #8ab4f8; font-family: Consolas, monospace; }
QTextEdit, QListWidget { background: #2b2c30; color: #e3e3e6; border: 1px solid #3c3d42; border-radius: 6px; }
QListWidget::item { background: #2b2c30; border: 1px solid #3c3d42; border-radius: 4px; }
QPushButton { background: #35363b; color: #e3e3e6; border: 1px solid #45464c;
              border-radius: 4px; padding: 4px 10px; }
QPushButton:disabled { background: #2f3034; color: #6a6b70; }
QPushButton:hover:enabled { background: #3f4046; }
QMenuBar::item:selected, QMenu::item:selected { background: #3a3b40; }
"""


# ─────────────────────────────────────────────────────────────────────
#  后台 Worker（关键：后台线程只发信号，绝不碰 UI）
# ─────────────────────────────────────────────────────────────────────
class _AsyncWorker(QObject):
    """通用后台任务：run() 在子线程执行，结果/进度经信号回主线程"""

    progress = Signal(str, float)          # 文本, 0~1
    finished_ok = Signal(str)              # 成功消息
    failed = Signal(str)                   # 错误消息

    def __init__(self, fn: Callable[["_AsyncWorker"], str]) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            msg = self._fn(self)            # fn 接收 worker 自身以便 emit 进度
            self.finished_ok.emit(msg or "完成")
        except Exception as e:  # noqa: BLE001
            logger.exception("后台任务失败")
            self.failed.emit(str(e))


class _DownloadWorker(_AsyncWorker):
    """下载 faster-whisper 模型"""

    def __init__(self, model_name: str) -> None:
        super().__init__(self._run_download)
        self.model_name = model_name
        self.local_path = ""

    def _run_download(self, _w) -> str:
        self.local_path = ModelManager.download(
            self.model_name,
            on_progress=lambda desc, cur, total: self.progress.emit(
                f"正在下载 {self.model_name}… {desc}", cur / total if total else 0.0))
        return f"模型 {self.model_name} 已就绪"


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.manager = SessionManager(ctx.config)
        self.setWindowTitle("在线课程录播器（AI LessonRecorder）")
        self.resize(1180, 760)
        self._apply_theme()

        # 活动后台任务：(thread, worker)。任务结束自动摘除，避免线程对象泄漏。
        self._workers: list[tuple[QThread, QObject]] = []
        self._retranscriber: OfflineRetranscriber | None = None
        self._progress_dialog: QProgressDialog | None = None
        self._pending_session_name = "未命名课程"
        self._hotkey_result: dict[str, bool] = {}

        self._build_menu()
        self._build_ui()
        self._wire_bus()
        self._connect_panel()
        self._setup_hotkeys()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_tick)

    # ================= UI 构建 =================
    def _apply_theme(self) -> None:
        theme = self.ctx.config.get("ui.theme", "light")
        self.setStyleSheet(STYLE_DARK if theme == "dark" else STYLE_LIGHT)

    def _build_menu(self) -> None:
        m_file = self.menuBar().addMenu("文件(&F)")
        act_open_out = QAction("打开输出目录", self)
        act_open_out.triggered.connect(
            lambda: open_in_explorer(str(self.ctx.config.output_root)))
        act_open_cfg = QAction("打开配置文件（所在目录）", self)
        act_open_cfg.triggered.connect(
            lambda: open_in_explorer(str(self.ctx.config.path.parent)))
        act_open_sessions = QAction("打开历史会话目录", self)
        act_open_sessions.triggered.connect(
            lambda: open_in_explorer(str(self.ctx.config.output_root)))
        act_sessions = QAction("历史会话...", self)
        act_sessions.triggered.connect(self._open_sessions)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.close)
        m_file.addAction(act_open_out)
        m_file.addAction(act_open_sessions)
        m_file.addAction(act_open_cfg)
        m_file.addSeparator()
        m_file.addAction(act_quit)

        m_export = self.menuBar().addMenu("导出(&E)")
        act_word = QAction("导出 Word 逐字稿（本次）", self)
        act_word.triggered.connect(self._export_word_current)
        act_course = QAction("生成 AI 教程文档（本次）", self)
        act_course.triggered.connect(self._generate_course_current)
        m_export.addAction(act_word)
        m_export.addAction(act_course)
        m_export.addSeparator()
        act_retrans = QAction("离线重转写（提高准确率）...", self)
        act_retrans.setToolTip("用 audio.wav 以大模型重新转写，得到更准确的逐字稿")
        act_retrans.triggered.connect(self._retranscribe_current)
        m_export.addAction(act_retrans)

        m_set = self.menuBar().addMenu("设置(&S)")
        act_cfg = QAction("偏好设置...", self)
        act_cfg.triggered.connect(self._open_settings)
        m_set.addAction(act_cfg)

        m_help = self.menuBar().addMenu("帮助(&H)")
        act_manual = QAction("使用说明", self)
        act_manual.setToolTip("打开随软件安装到本地的图文使用说明（浏览器打开）")
        act_manual.triggered.connect(self._open_manual)
        m_help.addAction(act_manual)
        m_help.addSeparator()
        act_keys = QAction("快捷键", self)
        act_keys.triggered.connect(self._show_hotkeys)
        act_about = QAction("关于", self)
        act_about.triggered.connect(self._show_about)
        m_help.addAction(act_keys)
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

        self.hotkey_label = QLabel("")
        # 必须允许换行：QLabel 不换行时 minimumSizeHint = 整行文字宽度，
        # 曾把主窗口最小宽度顶到热键说明一行那么宽，导致窗口无法收窄。
        self.hotkey_label.setWordWrap(True)
        self.hotkey_label.setStyleSheet(
            "color:#888; font-size:11px; padding:0 12px 6px 12px;")
        lay.addWidget(self.hotkey_label)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self._build_status_paths()

    def _build_status_paths(self) -> None:
        """状态栏右侧常驻显示「输出目录 / 配置文件」，点击可在资源管理器中打开。

        让用户随时确认配置生效在哪里、配置文件在哪（便于备份/排障），
        不必打开【设置】也能看到。
        """
        self._status_paths = QLabel()
        self._status_paths.setTextFormat(Qt.RichText)
        self._status_paths.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self._status_paths.setStyleSheet("padding:0 8px;")
        self._status_paths.linkActivated.connect(self._on_status_link)
        self.statusBar().addPermanentWidget(self._status_paths)
        self.refresh_status_paths()

    def refresh_status_paths(self) -> None:
        """配置变更后刷新状态栏（设置保存后调用）

        只显示短标签：完整路径放在 tooltip。
        状态栏永久部件的宽度会顶高主窗口最小宽度——带完整路径的长文本
        曾让窗口"只能拉长、无法调短"。
        """
        out = self.ctx.config.output_root
        cfg = self.ctx.config.path
        self._status_paths.setText(
            '<a href="out" style="color:#444;text-decoration:none;">📁 输出目录</a>'
            '　'
            '<a href="cfg" style="color:#444;text-decoration:none;">⚙ 配置文件</a>')
        self._status_paths.setToolTip(f"输出目录：{out}\n配置文件：{cfg}")

    def _on_status_link(self, href: str) -> None:
        if href == "out":
            open_in_explorer(str(self.ctx.config.output_root))
        elif href == "cfg":
            open_in_explorer(str(self.ctx.config.path.parent))

    def _setup_hotkeys(self) -> None:
        """注册全局快捷键（录制时窗口常失焦，必须系统级热键）"""
        self.hotkeys = GlobalHotkeys()
        self._hotkey_result = self.hotkeys.register_defaults(
            on_capture=self._force_shot,
            on_toggle_pause=self._toggle_pause,
            on_stop=self._stop_recording,
            on_activate=self._activate_window,
        )
        # 把实际生效的键位回写到按钮 tooltip，避免"提示与实际不一致"
        self.control.apply_hotkey_hints(self.hotkeys.registered_keys)
        failed = [k for k, v in self._hotkey_result.items() if not v]
        if failed:
            self.hotkey_label.setText(
                self.hotkeys.help_text()
                + f"　⚠ {'/'.join(failed)} 被其他程序占用，请用界面按钮")
        else:
            self.hotkey_label.setText(self.hotkeys.help_text())

    # ================= 信号接线 =================
    def _connect_panel(self) -> None:
        self.control.start_requested.connect(self._on_start)
        self.control.pause_requested.connect(self.manager.pause)
        self.control.resume_requested.connect(self.manager.resume)
        self.control.stop_requested.connect(self._stop_recording)
        self.control.screenshot_requested.connect(self._force_shot)
        self.control.auto_capture_toggled.connect(self._on_auto_capture_toggled)
        # 初始勾选态来自配置（主界面开关，设置里不再提供）
        self.control.set_auto_capture_checked(
            bool(self.ctx.config.get("vision.auto_capture", False)))

    def _on_auto_capture_toggled(self, on: bool) -> None:
        """自动截图开关：即时生效 + 写回配置（下次启动沿用）"""
        self.ctx.config.set("vision.auto_capture", bool(on))
        self.ctx.config.save()
        self.manager.set_auto_capture(bool(on))
        self.statusBar().showMessage(
            "已开启自动截图（页面变化率超阈值时自动保存）" if on
            else "已关闭自动截图（全靠手动截图 F9）")

    def _wire_bus(self) -> None:
        bus = self.manager.bus
        # 显式队列连接：无论信号在哪个线程 emit，槽函数必在主线程执行。
        # 这是 Qt 跨线程铁律的"机器可验证"写法，不依赖 PySide 的自动推断。
        q = Qt.QueuedConnection
        bus.transcript_added.connect(self.subtitle.append_line, q)
        bus.slide_added.connect(self.thumbs.add_screenshot, q)
        bus.status.connect(self.statusBar().showMessage, q)
        bus.error.connect(self._on_error, q)
        bus.finished.connect(self._on_finished, q)
        bus.model_download_required.connect(self._on_download_required, q)
        bus.state_changed.connect(self.control.on_state_changed, q)
        bus.level.connect(self.control.update_level, q)

    # ================= 录制动作 =================
    def _on_start(self, name: str) -> None:
        self.subtitle.clear_all()
        self.thumbs.clear_all()
        # 采集来源：控制面板显式选择了屏幕/窗口则写入运行时配置（覆盖设置默认），
        # 选"跟随设置"则沿用 vision.capture（primary/region）。
        src = self.control.selected_source()
        monitor = 1
        if src:
            kind, val = src
            if kind == "monitor":
                self.ctx.config.set("vision.capture", "primary")
                monitor = int(val)
            elif kind == "window":
                self.ctx.config.set("vision.capture", "window")
                self.ctx.config.set("vision.window_title", str(val))
        if self.manager.start(name, monitor=monitor):
            self.timer.start(500)
            self.statusBar().showMessage(f"录制中：{name}")

    def _toggle_pause(self) -> None:
        self.manager.toggle_pause()

    def _stop_recording(self) -> None:
        if self.manager.state in ("recording", "paused"):
            self.manager.stop()
        else:
            self.statusBar().showMessage("当前没有正在进行的录制")

    def _on_tick(self) -> None:
        self.control.update_timer(self.manager.elapsed)

    def _activate_window(self) -> None:
        """全局热键唤起主窗口（录制中用户可能在别的程序里）"""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _force_shot(self) -> None:
        """手动截图（线程安全：仅投递命令给检测线程，绝不在此处碰 mss）"""
        if self.manager.state in ("recording", "paused"):
            if self.manager.force_capture():
                self.statusBar().showMessage("已手动截图（正在保存）")
            else:
                self.statusBar().showMessage("截图失败：检测线程未运行")
        else:
            self.statusBar().showMessage("录制中才能截图")

    def _on_error(self, msg: str) -> None:
        self.timer.stop()
        QMessageBox.critical(self, "错误", msg)

    def _on_finished(self, session_root: str) -> None:
        self.timer.stop()
        if not session_root:
            return
        ret = QMessageBox.question(
            self, "录制完成",
            f"逐字稿已生成：\n{session_root}\\transcript.md\n\n"
            "是否立即导出 Word 逐字稿？\n"
            "（准确率不满意时，可用【导出 → 离线重转写】提升）",
            QMessageBox.Yes | QMessageBox.No)
        if ret == QMessageBox.Yes:
            # 导出完成后 on_done 会自动打开 docx 所在目录
            self._export_root(session_root)
        else:
            # 不导出也直接打开会话目录，避免"找不到文件"的尴尬
            open_in_explorer(session_root)

    # ================= 异步任务框架 =================
    def _run_async(self, worker: _AsyncWorker, title: str,
                   cancellable: bool = False,
                   on_done: Callable[[str], None] | None = None,
                   ) -> QProgressDialog:
        """在子线程跑 worker，UI 全部在主线程操作。

        进度对话框用 `show()` 而非 `exec()`（模态），因此主事件循环始终可响应，
        用户可继续最小化窗口、查看字幕。
        """
        prog = QProgressDialog("正在处理...", "取消" if cancellable else "", 0, 100, self)
        prog.setWindowTitle(title)
        prog.setMinimumDuration(0)      # 0 = 立即显示
        prog.setAutoClose(False)
        prog.setAutoReset(False)
        prog.setValue(0)
        if not cancellable:
            prog.setCancelButton(None)
        self._progress_dialog = prog

        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)

        q = Qt.QueuedConnection
        worker.progress.connect(
            lambda msg, pct: self._on_async_progress(prog, msg, pct), q)
        worker.finished_ok.connect(
            lambda msg: self._on_async_done(prog, msg, on_done), q)
        worker.failed.connect(lambda msg: self._on_async_fail(prog, msg), q)
        if cancellable:
            prog.canceled.connect(self._cancel_async, q)

        thread.finished.connect(worker.deleteLater)
        self._track_worker(thread, worker)
        thread.start()
        prog.show()
        return prog

    def _track_worker(self, thread: QThread, worker: QObject) -> None:
        """登记后台任务，并在其结束后自动摘除（防线程对象泄漏）"""
        self._workers.append((thread, worker))

        def _cleanup() -> None:
            try:
                self._workers.remove((thread, worker))
            except ValueError:
                pass

        thread.finished.connect(_cleanup, Qt.QueuedConnection)

    def _on_async_progress(self, prog: QProgressDialog,
                           msg: str, pct: float) -> None:
        prog.setLabelText(msg)
        prog.setValue(int(max(0.0, min(1.0, pct)) * 100))

    def _on_async_done(self, prog: QProgressDialog, msg: str,
                       on_done: Callable[[str], None] | None = None) -> None:
        prog.setValue(100)
        prog.close()
        if self._progress_dialog is prog:
            self._progress_dialog = None
        self.statusBar().showMessage(msg.replace("\n", " "))
        if on_done:
            on_done(msg)
        else:
            QMessageBox.information(self, "完成", msg)

    def _on_async_fail(self, prog: QProgressDialog, msg: str) -> None:
        prog.close()
        if self._progress_dialog is prog:
            self._progress_dialog = None
        QMessageBox.critical(self, "失败", msg)

    def _cancel_async(self) -> None:
        if self._retranscriber:
            self._retranscriber.cancel()
            self.statusBar().showMessage("正在取消，请稍候…（当前段转写完后停止）")
        elif self._progress_dialog:
            self._progress_dialog.setLabelText("正在取消…")

    # ================= 导出 =================
    def _export_word_current(self) -> None:
        if not self.manager.paths:
            QMessageBox.information(self, "提示", "本次运行还没有完成录制会话")
            return
        self._export_root(str(self.manager.paths.root))

    def _export_root(self, root: str) -> None:
        """导出 Word（异步）：完成后在资源管理器中打开 docx 所在目录"""
        def task(w: _AsyncWorker) -> str:
            p = SessionManager.export_word(root)
            return f"Word 已导出：\n{p}"

        def after(msg: str) -> None:
            # 末行是文档绝对路径（Word 文档/课程 .docx）
            last = [ln for ln in msg.strip().splitlines() if ln.strip()][-1]
            target = Path(last)
            open_in_explorer(str(target if target.is_dir() else target.parent))

        worker = _AsyncWorker(task)
        self._run_async(worker, "导出 Word 逐字稿", on_done=after)

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

        def after(msg: str) -> None:
            # 末行是 .docx 绝对路径
            last = [ln for ln in msg.strip().splitlines() if ln.strip()][-1]
            target = Path(last)
            open_in_explorer(str(target if target.is_dir() else target.parent))

        worker = _AsyncWorker(lambda w: self._course_task(root, llm_cfg, w))
        self._run_async(worker, "AI 教程文档生成", on_done=after)

    def _course_task(self, root: str, llm_cfg: dict, w: _AsyncWorker) -> str:
        """后台纯计算：只发进度信号，不碰任何 UI"""
        def on_progress(msg: str, pct: float) -> None:
            w.progress.emit(msg, pct)

        _md, docx = SessionManager.generate_course(root, llm_cfg, on_progress)
        return f"AI 教程文档已生成：\n{docx}"

    # ================= 离线重转写 =================
    def _retranscribe_current(self) -> None:
        if not self.manager.paths:
            QMessageBox.information(self, "提示", "本次运行还没有完成录制会话")
            return
        self._retranscribe_for(str(self.manager.paths.root))

    def _retranscribe_for(self, root: str) -> None:
        root_p = Path(root)
        if not (root_p / "audio.wav").exists():
            QMessageBox.warning(
                self, "无法重转写",
                "该会话没有 audio.wav（可能录制时关闭了音频存档）。\n"
                "请到【设置 → 输出】勾选「保留 audio.wav」后重新录制。")
            return

        model = self.ctx.config.get("asr.offline_model", "large-v3")
        if not ModelManager.is_downloaded(model):
            ret = QMessageBox.question(
                self, "需要下载模型",
                f"离线重转写需要模型 {model}（约 "
                f"{MODEL_SIZE_HINT_MB.get(model, 1500)} MB），尚未下载。\n"
                f"是否现在下载？\n\n提示：CPU 上转写速度可能只有 0.2~0.5 倍实时速度，"
                f"1 小时课程约需 2~5 小时。",
                QMessageBox.Yes | QMessageBox.No)
            if ret != QMessageBox.Yes:
                return
            self._download_model_then(model, lambda: self._retranscribe_for(root))
            return
        model_path = ModelManager.local_path(model)

        warn = ("离线重转写会用大模型重新转写 audio.wav，"
                "覆盖本会话的转写内容（原 events.jsonl 将备份为 .bak）。\n"
                f"模型：{model}\n\n是否继续？")
        if QMessageBox.question(self, "确认离线重转写", warn,
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return

        asr_cfg = self.ctx.config.as_dict()["asr"]
        self._retranscriber = OfflineRetranscriber(asr_cfg, model_path=model_path)

        def task(w: _AsyncWorker) -> str:
            res = self._retranscriber.run(
                root_p, model_size=model,
                on_progress=lambda m, p: w.progress.emit(m, p))
            if res.get("cancelled"):
                return "已取消离线重转写（events.jsonl 未改动）"
            return (f"离线重转写完成：{res['segments']} 段 / {res['chars']} 字，"
                    f"耗时 {res['elapsed_sec'] / 60:.1f} 分钟"
                    f"（{res.get('speed_x', 0)}x 实时速度）\n"
                    f"逐字稿已更新：transcript.md")

        worker = _AsyncWorker(task)
        # 完成后打开 session 目录：里面能看到新 transcript.md（events.jsonl 也已更新）
        self._run_async(
            worker, "离线重转写（提高准确率）", cancellable=True,
            on_done=lambda _m: (setattr(self, "_retranscriber", None),
                                open_in_explorer(str(root_p))))

    # ================= 模型下载（统一入口） =================
    def _download_model_then(self, model: str, then: Callable[[], None]) -> None:
        """下载模型完成后自动继续执行 then()"""
        self._start_download(
            model, MODEL_SIZE_HINT_MB.get(model, 1500),
            hint="请检查网络。若直连 huggingface 缓慢，可在【设置 → 语音识别】"
                 "中把 HF 镜像设为 https://hf-mirror.com",
            on_ok=lambda: then())

    def _on_download_required(self, model_name: str, size_hint_mb: int) -> None:
        """录制前触发：模型缺失 → 下载 → 自动开始录制"""
        self._pending_session_name = (
            self.control.name_edit.text().strip() or "未命名课程")
        self.control.on_state_changed("downloading")
        self.control.btn_start.setEnabled(False)
        self._start_download(
            model_name, size_hint_mb,
            hint="请检查网络。若直连 huggingface 缓慢，可在"
                 "【设置 → 语音识别 → 模型下载】中把 HF 镜像设为 "
                 "https://hf-mirror.com 后重试。",
            on_ok=lambda: self._on_start(self._pending_session_name))

    def _start_download(self, model: str, size_hint_mb: int,
                        hint: str, on_ok: Callable[[], None]) -> None:
        """模型下载：后台线程下载，主线程更新进度条（模态，下载期间禁止重复触发）"""
        prog = QProgressDialog(
            f"首次使用需下载语音模型 {model}（约 {size_hint_mb} MB，仅一次）…",
            "", 0, 100, self)
        prog.setWindowTitle("首次准备")
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)
        prog.setAutoClose(False)
        prog.setAutoReset(False)
        prog.setCancelButton(None)      # faster-whisper 不支持中断下载，隐藏取消避免误导
        prog.setValue(0)

        worker = _DownloadWorker(model)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        q = Qt.QueuedConnection

        def _restore_idle() -> None:
            prog.close()
            self.control.on_state_changed("idle")
            self.control.btn_start.setEnabled(True)

        def _on_done(_msg: str) -> None:
            _restore_idle()
            self.statusBar().showMessage(f"模型就绪：{model}")
            try:
                on_ok()
            except Exception:  # noqa: BLE001
                logger.exception("模型就绪后的后续动作失败")
                QMessageBox.critical(self, "错误", "模型已就绪，但后续操作失败，详见日志")

        def _on_err(msg: str) -> None:
            _restore_idle()
            QMessageBox.critical(self, "模型下载失败",
                                 f"无法下载模型 {model}：\n{msg}\n\n{hint}")

        worker.progress.connect(
            lambda m, p: prog.setValue(int(max(0.0, min(1.0, p)) * 100)), q)
        worker.finished_ok.connect(_on_done, q)
        worker.failed.connect(_on_err, q)
        thread.finished.connect(worker.deleteLater)
        self._track_worker(thread, worker)
        thread.start()
        prog.exec()

    # ================= 对话框 =================
    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.ctx.config, self)
        if dlg.exec():
            self._apply_theme()
            self.control.refresh_monitors()
            self.refresh_status_paths()  # 输出目录/配置路径如有变化
            self.statusBar().showMessage("设置已保存")

    def _open_sessions(self) -> None:
        SessionListDialog(self.ctx.config, self).exec()

    def _show_hotkeys(self) -> None:
        QMessageBox.information(
            self, "快捷键",
            self.hotkeys.help_text() +
            "\n\n说明：录制时你在课程播放器里，"
            "本工具窗口通常处于失焦状态，因此使用系统级全局热键。\n"
            "若某个热键被其他程序占用，会退回为仅界面按钮可用"
            "（启动日志与本窗口底部会提示）。")

    def _open_manual(self) -> None:
        """打开随软件安装到本地的使用说明（HTML，浏览器渲染）"""
        from src.utils.fsutil import open_file, resource_path
        manual = resource_path("assets/user_manual.html")
        if open_file(str(manual)):
            return
        # 资源缺失（如手工删除）：退而求其次，打开所在目录
        QMessageBox.warning(
            self, "使用说明缺失",
            f"未找到使用说明文件：\n{manual}\n\n"
            "可能是安装目录被精简过，请重新解压完整安装包。")

    def _show_about(self) -> None:
        from src.utils.fsutil import resource_path

        dlg = QDialog(self)
        dlg.setWindowTitle("关于 在线课程录播器")
        lay = QVBoxLayout(dlg)
        lay.setSpacing(12)

        intro = QLabel(
            "<b>在线课程录播器（AI LessonRecorder）</b><br>"
            "录制系统声音 → 实时转文字 → 课程截图 →<br>"
            "生成图文逐字稿 MD / Word → AI 梳理成结构化教程文档<br><br>"
            "双档转写：录制时小模型保实时，课后离线大模型保准确率")
        intro.setTextFormat(Qt.RichText)
        intro.setWordWrap(True)
        lay.addWidget(intro)

        dev = QLabel(
            "<b>开发者：Albert 老石</b><br>"
            "如需<b>购买、功能定制、批量部署或其他合作</b>，欢迎扫码加微信洽谈")
        dev.setTextFormat(Qt.RichText)
        dev.setWordWrap(True)
        lay.addWidget(dev)

        from PySide6.QtGui import QPixmap
        qr_path = resource_path("assets/wechat_qr.png")
        if qr_path.exists():
            row = QHBoxLayout()
            pic = QLabel()
            pic.setPixmap(QPixmap(str(qr_path)).scaled(
                160, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            pic.setToolTip("微信二维码（右键菜单暂不支持保存，可截图）")
            row.addStretch(1)
            row.addWidget(pic)
            row.addStretch(1)
            lay.addLayout(row)
        else:
            logger.warning(f"关于页二维码缺失: {qr_path}")

        btn = QPushButton("关闭")
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn, 0, Qt.AlignCenter)
        dlg.setMinimumWidth(360)
        dlg.exec()

    # ================= 关闭保护 =================
    def closeEvent(self, event) -> None:
        # 1) 拦下仍在运行的后台任务（离线重转写可能跑几小时，不能静默丢弃）
        running = [t for t, _w in self._workers if t.isRunning()]
        if running:
            ret = QMessageBox.warning(
                self, "仍有后台任务在运行",
                f"当前有 {len(running)} 个后台任务仍在运行"
                "（如离线重转写 / AI 教程生成）。\n\n"
                "强制退出会中断该任务，可能留下不完整的输出文件。\n"
                "是否仍要退出？",
                QMessageBox.Yes | QMessageBox.No)
            if ret != QMessageBox.Yes:
                event.ignore()
                return

        # 2) 录制中：询问如何收尾
        if self.manager.state in ("recording", "paused"):
            ret = QMessageBox.question(
                self, "正在录制",
                "当前会话仍在录制中。\n\n"
                "「停止并退出」= 停止录制、生成逐字稿后退出；\n"
                "「仅退出」= 直接退出（已录制内容仍保存在事件流中，可事后重建）。",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if ret == QMessageBox.Cancel:
                event.ignore()
                return
            if ret == QMessageBox.Yes:
                self.manager.stop()
                self._wait_finalize(timeout_ms=15000)

        # 3) 注销热键，避免退出后残留占用 F9/F10/F11
        try:
            self.hotkeys.unregister_all()
        except Exception:  # noqa: BLE001
            logger.debug("注销热键异常", exc_info=True)

        # 4) 请求后台线程退出（已中断的线程会在事件循环结束后自行收尾）
        for thread, _worker in list(self._workers):
            if thread.isRunning():
                thread.requestInterruption()
                thread.quit()
        event.accept()

    def _wait_finalize(self, timeout_ms: int = 15000) -> None:
        """等待收尾完成；期间持续泵事件，保持界面响应且能让 finished 信号被处理"""
        import time
        app = QApplication.instance()
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline and self.manager.state == "finalizing":
            if app:
                app.processEvents()
            time.sleep(0.05)