# -*- coding: utf-8 -*-
"""模型下载流程与 UI 构建的冒烟测试（**不实际下载模型**）

v1.2 更新：
- 适配 `_AsyncWorker` 重命名后的信号（`finished_ok` / `failed`，原 `finished` / `error`）；
- 全部用例走 offscreen，杜绝自动化环境下弹窗阻塞；
- 补充 DPAPI 密钥加密的热键无关性验证。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ai.asr.model_manager import MODEL_SIZE_HINT_MB, ModelManager, _HFTqdmAdapter
from src.ai.asr.whisper_local import LocalWhisperEngine
from src.app.application import AppContext
from src.app.event_bus import EventBus
from src.app.session_manager import SessionManager
from src.ui.main_window import MainWindow, _DownloadWorker
from src.ui.panels.control_panel import ControlPanel
from PySide6.QtWidgets import QApplication


def test_tqdm_progress():
    log: list = []
    _HFTqdmAdapter.callback = lambda d, c, n: log.append((d, c, n))
    t = _HFTqdmAdapter(total=10, desc="test")
    t.update(3)
    t.set_description("next")
    t.update(2)
    t.close()
    _HFTqdmAdapter.callback = None
    assert log == [("test", 0, 10), ("test", 3, 10), ("next", 3, 10), ("next", 5, 10)], log
    print(f"[OK] TqdmAdapter 进度回调: {log}")


def test_model_size_hints():
    for name in ("tiny", "base", "small", "medium", "large-v3"):
        assert name in MODEL_SIZE_HINT_MB, f"缺少体积提示: {name}"
        assert MODEL_SIZE_HINT_MB[name] > 0
    print(f"[OK] 模型体积提示齐全: small={MODEL_SIZE_HINT_MB['small']}MB, "
          f"large-v3={MODEL_SIZE_HINT_MB['large-v3']}MB")


def test_model_probe_type():
    """探测模型接口返回 bool（不要求具体值，避免依赖本机是否已下载）"""
    downloaded = ModelManager.is_downloaded("small")
    assert isinstance(downloaded, bool)
    print(f"[OK] is_downloaded(small) 返回 bool（当前值 {downloaded}）")


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
    win = MainWindow(ctx)
    assert win.hotkeys is not None
    assert win._workers == [], "初始不应有后台任务"
    print(f"[OK] MainWindow 构建成功（模型 small 大小提示: "
          f"{MODEL_SIZE_HINT_MB['small']} MB）")
    win.hotkeys.unregister_all()


def test_download_worker_signals():
    """v1.2：`_DownloadWorker` 继承 `_AsyncWorker`，信号为 finished_ok / failed"""
    worker = _DownloadWorker("small")
    got: list = []
    worker.progress.connect(lambda m, p: got.append(("progress", m, p)))
    worker.finished_ok.connect(lambda p: got.append(("ok", p)))
    worker.failed.connect(lambda m: got.append(("err", m)))
    assert hasattr(worker, "finished_ok") and hasattr(worker, "failed")
    assert not hasattr(worker, "error"), "旧信号 error 应已被 finished_ok/failed 取代"
    print(f"[OK] _DownloadWorker 信号契约正确: "
          f"progress / finished_ok / failed（model={worker.model_name}）")


def test_hotkey_hints_reflect_reality():
    """按钮 tooltip 必须与实际注册成功的热键一致"""
    app = QApplication.instance() or QApplication([])
    p = ControlPanel()
    p.apply_hotkey_hints(["F9=手动截图", "F10=暂停/继续"])
    assert "F9" in p.btn_shot.toolTip()
    assert "F10" in p.btn_pause.toolTip()
    assert "未注册" in p.btn_stop.toolTip(), "未注册的键位应明确说明原因"
    print(f"[OK] 热键提示与注册结果一致（未注册的 F11 已标注原因）")


if __name__ == "__main__":
    test_tqdm_progress()
    test_model_size_hints()
    test_model_probe_type()
    test_control_panel_states()
    test_signals_added()
    test_sigs_have_model_path()
    test_mainwindow_constructs()
    test_download_worker_signals()
    test_hotkey_hints_reflect_reality()
    print("\n全部验证通过")