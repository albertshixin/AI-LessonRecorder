# -*- coding: utf-8 -*-
"""设置对话框：ASR / 视觉检测 / LLM / 输出 四个页签

v1.2 补齐需求 FR-08 中"已列出但未实现"的配置项：
- ASR：设备(Device)、HF 镜像、静音阈值、最短/最长分段、pre-roll
- 视觉：抓屏范围(区域)、内容区 ROI、diff_ratio 与 SSIM 阈值
- 输出：音频存档开关、界面主题
同时新增**离线重转写默认模型**（精度档模型选择）。

密钥提示：API Key 保存后以Windows DPAPI 密文落盘，
在设置界面显示为掩码，读取时自动解密（见 utils/secrets_store.py）。
"""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QGroupBox, QLabel,
                               QLineEdit, QMessageBox, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

from src.utils.secrets_store import encryption_supported

# LLM 服务商预设：key → (显示名, Base URL, 默认模型, 常用模型列表)
LLM_PRESETS: dict[str, tuple[str, str, str, list[str]]] = {
    "glm": ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4.5",
            ["glm-4.5", "glm-4.5-air", "glm-4.5-flash", "glm-4-plus"]),
    "deepseek": ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat",
                 ["deepseek-chat", "deepseek-reasoner"]),
    "qwen": ("阿里通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "qwen-plus", ["qwen-plus", "qwen-max", "qwen-turbo"]),
    "openai": ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini",
               ["gpt-4o-mini", "gpt-4o"]),
    "custom": ("自定义", "", "", []),
}

MODEL_SIZES = ["tiny", "base", "small", "medium", "large-v2", "large-v3"]


def _section(title: str) -> QGroupBox:
    g = QGroupBox(title)
    g.setLayout(QFormLayout(g))
    return g


class SettingsDialog(QDialog):
    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(560)
        self.config = config

        tabs = QTabWidget()
        tabs.addTab(self._build_asr_tab(), "语音识别")
        tabs.addTab(self._build_vision_tab(), "翻页检测")
        tabs.addTab(self._build_llm_tab(), "AI 梳理 (LLM)")
        tabs.addTab(self._build_output_tab(), "输出")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(buttons)

        if not encryption_supported():
            warn = QLabel("⚠ 当前系统不支持密钥加密（仅 Windows DPAPI 可用），"
                          "API Key 将以明文保存")
            warn.setStyleSheet("color:#c5221f; font-size:11px;")
            lay.addWidget(warn)

    # ---------- 页签构建 ----------
    def _build_asr_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        basic = _section("基础")
        f1 = basic.layout()
        self.asr_engine = QComboBox()
        self.asr_engine.addItems(["local", "cloud"])
        self.asr_engine.setCurrentText(self.config.get("asr.engine", "local"))
        self.asr_engine.currentTextChanged.connect(
            lambda t: self._toggle_local(t == "local"))
        f1.addRow("引擎：", self.asr_engine)

        self.asr_model = QComboBox()
        self.asr_model.addItems(MODEL_SIZES)
        self.asr_model.setCurrentText(self.config.get("asr.model", "small"))
        f1.addRow("实时模型（越大越准越慢）：", self.asr_model)

        self.asr_device = QComboBox()
        self.asr_device.addItems(["auto", "cpu", "cuda"])
        self.asr_device.setCurrentText(self.config.get("asr.device", "auto"))
        f1.addRow("设备（auto 会探测 CUDA 并回落）：", self.asr_device)

        self.asr_language = QComboBox()
        self.asr_language.addItems(["zh", "en", "auto"])
        self.asr_language.setCurrentText(self.config.get("asr.language", "zh"))
        f1.addRow("语言：", self.asr_language)

        self.asr_compute = QComboBox()
        self.asr_compute.addItems(["int8", "float16"])
        self.asr_compute.setCurrentText(self.config.get("asr.compute_type", "int8"))
        f1.addRow("计算精度：", self.asr_compute)

        self.offline_model = QComboBox()
        self.offline_model.addItems(MODEL_SIZES)
        self.offline_model.setCurrentText(self.config.get("asr.offline_model", "large-v3"))
        f1.addRow("离线重转写模型（精度档）：", self.offline_model)
        hint = QLabel("录制时用小模型保实时；课后用大模型离线重转写追准确率")
        hint.setStyleSheet("color:#888; font-size:11px;")
        f1.addRow("", hint)
        lay.addWidget(basic)

        seg = _section("实时分段（VAD）")
        f2 = seg.layout()
        self.asr_silence = QDoubleSpinBox()
        self.asr_silence.setRange(0.2, 3.0)
        self.asr_silence.setSingleStep(0.1)
        self.asr_silence.setDecimals(2)
        self.asr_silence.setValue(float(self.config.get("asr.silence_sec", 0.8)))
        f2.addRow("句尾静音判定（秒）：", self.asr_silence)

        self.asr_min_seg = QDoubleSpinBox()
        self.asr_min_seg.setRange(0.2, 10.0)
        self.asr_min_seg.setSingleStep(0.5)
        self.asr_min_seg.setDecimals(1)
        self.asr_min_seg.setValue(float(self.config.get("asr.min_segment_sec", 1.5)))
        f2.addRow("最短出段（秒）：", self.asr_min_seg)

        self.asr_max_seg = QDoubleSpinBox()
        self.asr_max_seg.setRange(3.0, 60.0)
        self.asr_max_seg.setSingleStep(1.0)
        self.asr_max_seg.setDecimals(1)
        self.asr_max_seg.setValue(float(self.config.get("asr.max_segment_sec", 15.0)))
        f2.addRow("最长出段（秒）：", self.asr_max_seg)

        self.asr_preroll = QDoubleSpinBox()
        self.asr_preroll.setRange(0.0, 1.0)
        self.asr_preroll.setSingleStep(0.1)
        self.asr_preroll.setDecimals(2)
        self.asr_preroll.setValue(float(self.config.get("asr.pre_roll_sec", 0.3)))
        f2.addRow("段首保留（秒，防切首字）：", self.asr_preroll)
        lay.addWidget(seg)

        cloud = _section("云端 ASR（OpenAI 兼容）")
        f3 = cloud.layout()
        self.cloud_base = QLineEdit(self.config.get("asr.cloud.base_url", ""))
        f3.addRow("Base URL：", self.cloud_base)
        self.cloud_key = QLineEdit(self.config.get("asr.cloud.api_key", ""))
        self.cloud_key.setEchoMode(QLineEdit.Password)
        f3.addRow("API Key：", self.cloud_key)
        self.cloud_model = QLineEdit(self.config.get("asr.cloud.model", "whisper-1"))
        f3.addRow("模型：", self.cloud_model)
        lay.addWidget(cloud)

        dl = _section("模型下载")
        f4 = dl.layout()
        self.hf_mirror = QLineEdit(self.config.get("asr.hf_mirror", ""))
        self.hf_mirror.setPlaceholderText("https://hf-mirror.com（留空用官方源）")
        f4.addRow("HF 镜像：", self.hf_mirror)
        lay.addWidget(dl)
        lay.addStretch(1)

        self._toggle_local(self.asr_engine.currentText() == "local")
        return w

    def _toggle_local(self, is_local: bool) -> None:
        for wdg in (self.asr_model, self.asr_device, self.asr_language,
                    self.asr_compute, self.offline_model):
            wdg.setEnabled(is_local)

    def _build_vision_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        basic = _section("抓屏")
        f1 = basic.layout()
        self.v_capture = QComboBox()
        self.v_capture.addItems(["primary", "region"])
        self.v_capture.setCurrentText(self.config.get("vision.capture", "primary"))
        self.v_capture.currentTextChanged.connect(
            lambda t: self.v_x.setEnabled(t == "region"))
        f1.addRow("抓屏范围：", self.v_capture)

        r = self.config.get("vision.region", {}) or {}
        self.v_x = QSpinBox(); self.v_x.setRange(0, 7680)
        self.v_x.setValue(int(r.get("x", 0)))
        self.v_y = QSpinBox(); self.v_y.setRange(0, 4320)
        self.v_y.setValue(int(r.get("y", 0)))
        self.v_w = QSpinBox(); self.v_w.setRange(16, 7680)
        self.v_w.setValue(int(r.get("w", 1920)))
        self.v_h = QSpinBox(); self.v_h.setRange(16, 4320)
        self.v_h.setValue(int(r.get("h", 1080)))
        f1.addRow("区域 X / Y：", _row(self.v_x, self.v_y))
        f1.addRow("区域 宽 / 高：", _row(self.v_w, self.v_h))
        lay.addWidget(basic)

        roi_box = _section("内容区 ROI（排除播放器控制栏/弹幕）")
        f2 = roi_box.layout()
        roi = self.config.get("vision.roi") or {}
        self.roi_enabled = QCheckBox("启用 ROI（留空则检测整幅画面）")
        self.roi_enabled.setChecked(bool(roi))
        self.roi_enabled.toggled.connect(self._toggle_roi)
        f2.addRow("", self.roi_enabled)
        self.roi_x = QDoubleSpinBox(); self.roi_x.setRange(0.0, 0.99)
        self.roi_x.setSingleStep(0.05); self.roi_x.setDecimals(2)
        self.roi_x.setValue(float(roi.get("x", 0.0)))
        self.roi_y = QDoubleSpinBox(); self.roi_y.setRange(0.0, 0.99)
        self.roi_y.setSingleStep(0.05); self.roi_y.setDecimals(2)
        self.roi_y.setValue(float(roi.get("y", 0.0)))
        self.roi_w = QDoubleSpinBox(); self.roi_w.setRange(0.01, 1.0)
        self.roi_w.setSingleStep(0.05); self.roi_w.setDecimals(2)
        self.roi_w.setValue(float(roi.get("w", 1.0)))
        self.roi_h = QDoubleSpinBox(); self.roi_h.setRange(0.01, 1.0)
        self.roi_h.setSingleStep(0.05); self.roi_h.setDecimals(2)
        self.roi_h.setValue(float(roi.get("h", 1.0)))
        f2.addRow("起点 X / Y（0~1）：", _row(self.roi_x, self.roi_y))
        f2.addRow("宽 / 高（0~1）：", _row(self.roi_w, self.roi_h))
        lay.addWidget(roi_box)

        detect = _section("翻页判据（阈值标定见 tests/test_vision_algo.py）")
        f3 = detect.layout()
        self.v_interval = QDoubleSpinBox()
        self.v_interval.setRange(0.5, 5.0)
        self.v_interval.setSingleStep(0.5)
        self.v_interval.setValue(float(self.config.get("vision.interval_sec", 1.5)))
        f3.addRow("抓屏间隔（秒）：", self.v_interval)

        self.v_threshold = QDoubleSpinBox()
        self.v_threshold.setRange(0.02, 0.6)
        self.v_threshold.setSingleStep(0.01)
        self.v_threshold.setDecimals(2)
        self.v_threshold.setValue(float(self.config.get("vision.phash_threshold", 0.15)))
        f3.addRow("① pHash 距离阈值（越大越迟钝）：", self.v_threshold)

        self.v_min_diff = QDoubleSpinBox()
        self.v_min_diff.setRange(0.0, 0.3)
        self.v_min_diff.setSingleStep(0.005)
        self.v_min_diff.setDecimals(3)
        self.v_min_diff.setValue(float(self.config.get("vision.min_diff_ratio", 0.02)))
        f3.addRow("② 差异像素占比下限（挡光标）：", self.v_min_diff)

        self.v_ssim = QDoubleSpinBox()
        self.v_ssim.setRange(0.5, 1.0)
        self.v_ssim.setSingleStep(0.01)
        self.v_ssim.setDecimals(2)
        self.v_ssim.setValue(float(self.config.get("vision.ssim_threshold", 0.95)))
        f3.addRow("③ SSIM 下限（挡亮度噪声）：", self.v_ssim)

        self.v_backtrack = QDoubleSpinBox()
        self.v_backtrack.setRange(0.01, 0.3)
        self.v_backtrack.setSingleStep(0.01)
        self.v_backtrack.setDecimals(2)
        self.v_backtrack.setValue(float(self.config.get("vision.backtrack_tol", 0.08)))
        f3.addRow("翻回识别容差：", self.v_backtrack)

        self.v_cooldown = QDoubleSpinBox()
        self.v_cooldown.setRange(0.5, 15.0)
        self.v_cooldown.setSingleStep(0.5)
        self.v_cooldown.setValue(float(self.config.get("vision.cooldown_sec", 3.0)))
        f3.addRow("冷却时间（秒，防动画误检）：", self.v_cooldown)
        lay.addWidget(detect)
        lay.addStretch(1)

        self.v_x.setEnabled(self.v_capture.currentText() == "region")
        self._toggle_roi(self.roi_enabled.isChecked())
        return w

    def _toggle_roi(self, on: bool) -> None:
        for wdg in (self.roi_x, self.roi_y, self.roi_w, self.roi_h):
            wdg.setEnabled(on)

    def _build_llm_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        self.llm_provider = QComboBox()
        for key, (name, _b, _m, _ms) in LLM_PRESETS.items():
            self.llm_provider.addItem(name, key)
        cur_base = (self.config.get("llm.base_url", "") or "").rstrip("/")
        cur_provider = self.config.get("llm.provider", "")
        if not cur_provider:
            cur_provider = next(
                (k for k, (_n, b, _m, _ms) in LLM_PRESETS.items()
                 if k != "custom" and b == cur_base), "custom")
        idx = self.llm_provider.findData(cur_provider)
        self.llm_provider.setCurrentIndex(max(0, idx))
        self.llm_provider.currentIndexChanged.connect(self._on_llm_provider_changed)
        form.addRow("服务商：", self.llm_provider)

        self.llm_base = QLineEdit(cur_base)
        self.llm_base.setPlaceholderText("https://open.bigmodel.cn/api/paas/v4")
        form.addRow("Base URL：", self.llm_base)

        self.llm_key = QLineEdit(self.config.get("llm.api_key", ""))
        self.llm_key.setEchoMode(QLineEdit.Password)
        form.addRow("API Key：", self.llm_key)
        sec_hint = QLabel("（保存后以Windows DPAPI 密文存储，仅本机当前用户可解密）")
        sec_hint.setStyleSheet("color:#888; font-size:11px;")
        form.addRow("", sec_hint)

        self.llm_model = QComboBox()
        self.llm_model.setEditable(True)
        self._fill_llm_models(cur_provider, self.config.get("llm.model", ""))
        form.addRow("模型：", self.llm_model)

        self.llm_temp = QDoubleSpinBox()
        self.llm_temp.setRange(0.0, 1.0)
        self.llm_temp.setSingleStep(0.1)
        self.llm_temp.setValue(float(self.config.get("llm.temperature", 0.3)))
        form.addRow("温度：", self.llm_temp)

        self.llm_chunk = QSpinBox()
        self.llm_chunk.setRange(500, 32000)
        self.llm_chunk.setSingleStep(500)
        self.llm_chunk.setValue(int(self.config.get("llm.max_chunk_chars", 3000)))
        form.addRow("分段字数（Map-Reduce）：", self.llm_chunk)
        return w

    def _fill_llm_models(self, provider: str, keep: str = "") -> None:
        models = LLM_PRESETS.get(provider, ("", "", "", []))[3]
        self.llm_model.clear()
        if models:
            self.llm_model.addItems(models)
        if keep:
            self.llm_model.setCurrentText(keep)

    def _on_llm_provider_changed(self) -> None:
        key = self.llm_provider.currentData()
        _name, base, model, _models = LLM_PRESETS.get(key, ("", "", "", []))
        if base:
            self.llm_base.setText(base)
            self._fill_llm_models(key)
            self.llm_model.setCurrentText(model)
        elif key == "custom":
            self.llm_base.clear()
            self.llm_model.clear()

    def _build_output_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.out_dir = QLineEdit(str(self.config.get("output.dir", "output")))
        form.addRow("输出目录：", self.out_dir)

        self.keep_audio = QCheckBox("保留 audio.wav 原始音频（关闭可省磁盘，"
                                     "但将无法离线重转写）")
        self.keep_audio.setChecked(bool(self.config.get("output.keep_audio", True)))
        form.addRow("", self.keep_audio)

        self.theme = QComboBox()
        self.theme.addItems(["light", "dark"])
        self.theme.setCurrentText(self.config.get("ui.theme", "light"))
        form.addRow("界面主题：", self.theme)
        return w

    # ---------- 保存 ----------
    def _save(self) -> None:
        cfg = self.config
        cfg.set("asr.engine", self.asr_engine.currentText())
        cfg.set("asr.model", self.asr_model.currentText())
        cfg.set("asr.device", self.asr_device.currentText())
        cfg.set("asr.language", self.asr_language.currentText())
        cfg.set("asr.compute_type", self.asr_compute.currentText())
        cfg.set("asr.offline_model", self.offline_model.currentText())
        cfg.set("asr.silence_sec", self.asr_silence.value())
        cfg.set("asr.min_segment_sec", self.asr_min_seg.value())
        cfg.set("asr.max_segment_sec", self.asr_max_seg.value())
        cfg.set("asr.pre_roll_sec", self.asr_preroll.value())
        cfg.set("asr.hf_mirror", self.hf_mirror.text().strip())
        cfg.set("asr.cloud.base_url", self.cloud_base.text().strip())
        cfg.set("asr.cloud.api_key", self.cloud_key.text().strip())
        cfg.set("asr.cloud.model", self.cloud_model.text().strip())

        cfg.set("vision.capture", self.v_capture.currentText())
        cfg.set("vision.region", {"x": self.v_x.value(), "y": self.v_y.value(),
                                  "w": self.v_w.value(), "h": self.v_h.value()})
        cfg.set("vision.roi", {"x": self.roi_x.value(), "y": self.roi_y.value(),
                               "w": self.roi_w.value(), "h": self.roi_h.value()}
                if self.roi_enabled.isChecked() else None)
        cfg.set("vision.interval_sec", self.v_interval.value())
        cfg.set("vision.phash_threshold", self.v_threshold.value())
        cfg.set("vision.min_diff_ratio", self.v_min_diff.value())
        cfg.set("vision.ssim_threshold", self.v_ssim.value())
        cfg.set("vision.backtrack_tol", self.v_backtrack.value())
        cfg.set("vision.cooldown_sec", self.v_cooldown.value())

        cfg.set("llm.provider", self.llm_provider.currentData() or "custom")
        cfg.set("llm.base_url", self.llm_base.text().strip())
        cfg.set("llm.api_key", self.llm_key.text().strip())
        cfg.set("llm.model", self.llm_model.currentText().strip())
        cfg.set("llm.temperature", self.llm_temp.value())
        cfg.set("llm.max_chunk_chars", self.llm_chunk.value())

        cfg.set("output.dir", self.out_dir.text().strip())
        cfg.set("output.keep_audio", self.keep_audio.isChecked())
        cfg.set("ui.theme", self.theme.currentText())

        if not self.out_dir.text().strip():
            QMessageBox.warning(self, "提示", "输出目录不能为空")
            return
        cfg.save()
        self.accept()


def _row(*widgets):
    """把多个控件横向排列"""
    from PySide6.QtWidgets import QHBoxLayout, QWidget as _W
    box = _W()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    for i, wdg in enumerate(widgets):
        lay.addWidget(wdg)
        if i < len(widgets) - 1:
            lay.addWidget(QLabel(""))
    lay.addStretch(1)
    return box