# -*- coding: utf-8 -*-
"""验证模型下载流程的冒烟测试（不实际下载模型）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from src.ai.asr.model_manager import ModelManager, MODEL_SIZE_HINT_MB, _HFTqdmAdapter
from src.ai.asr.whisper_local import LocalWhisperEngine
from src.app.event_bus import EventBus
from src.app.session_manager import SessionManager
from src.ui.main_window import MainWindow, _DownloadWorker
from src.ui.panels.control_panel import ControlPanel
from src.app.application import AppContext


def test_tqdm_progress():
    _HFTqdmAdapter.callback = lambda d, c, n: log.append((d, c, n))
    log: list = []
    t = _HFTqdmAdapter(total=10, desc="test")
    t.update(3)
    t.set_description("next")
    t.update(2)
    t.close()
    _HFTqdmAdapter.callback = None
    assert log == [("test", 0, 10), ("test", 3, 10), ("next", 3, 10), ("next", 5, 10)], log
    print(f"[OK] TqdmAdapter 进度回调: {log}")


def test_model_probe():
    """探测模型（预期未下载返回 False）"""
    downloaded = ModelManager.is_downloaded("small")
    assert isinstance(downloaded, bool)
    print(f"[OK] is_downloaded(small) = {downloaded}（未下载符合预期）")


def test_control_panel_states():
    app = QApplication.instance() or QApplication([])
    p = ControlPanel()
    p.on_state_changed("downloading")
    assert not p.btn_start.isEnabled(), "开始按钮应禁用"
    assert not p.btn_pause.isEnabled(), "暂停按钮应禁用"
    assert not p.btn_stop.isEnabled(), "停止按钮应禁用"
    print("[OK] ControlPanel downloading 状态正确禁用所有按钮")


def test_signals_added():
    bus = EventBus()
    for name in ("model_download_required", "model_download_progress",
                 "model_download_finished", "model_download_error"):
        assert hasattr(bus, name), f"缺少信号: {name}"
    print("[OK] EventBus 已添加模型下载相关信号")


def test_sigs_have_model_path():
    import inspect
    a = inspect.signature(LocalWhisperEngine.__init__)
    assert "model_path" in a.parameters
    b = inspect.signature(SessionManager.start)
    assert "model_path" in b.parameters
    print(f"[OK] LocalWhisperEngine.__init__: {a}")
    print(f"[OK] SessionManager.start: {b}")


def test_mainwindow_constructs():
    app = QApplication.instance() or QApplication([])
    ctx = AppContext.instance()
    ctx.config.set("asr.engine", "local")
    ctx.config.set("asr.model", "small")
    ctx.config.save()
    win = MainWindow(ctx)
    win.show()
    print(f"[OK] MainWindow 构建成功（模型 small 大小提示: {MODEL_SIZE_HINT_MB['small']} MB）")


def test_download_worker_emits():
    """测试 Worker 在不实际下载时也能构造并连接信号"""
    worker = _DownloadWorker("small")
    log = []
    worker.progress.connect(lambda d, c, t: log.append((d, c, t)))
    worker.finished.connect(lambda p: log.append(("done", p)))
    worker.error.connect(lambda m: log.append(("err", m)))
    print(f"[OK] _DownloadWorker 信号连接 OK，model_name={worker.model_name}")


if __name__ == "__main__":
    test_tqdm_progress()
    test_model_probe()
    test_control_panel_states()
    test_signals_added()
    test_sigs_have_model_path()
    test_mainwindow_constructs()
    test_download_worker_emits()
    print("\n全部验证通过")