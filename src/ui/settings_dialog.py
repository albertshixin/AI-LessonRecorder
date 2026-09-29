# -*- coding: utf-8 -*-
"""设置对话框：ASR / 视觉检测 / LLM / 输出 四个页签"""
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QInputDialog,
                               QLabel, QLineEdit, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget, QMessageBox)

# LLM 服务商预设：key → (显示名, Base URL, 默认模型, 常用模型列表)
LLM_PRESETS: dict[str, tuple[str, str, str, list[str]]] = {
    "glm": ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4.5",
            ["glm-4.5", "glm-4.5-air", "glm-4.5-flash", "glm-4-plus"]),
    "deepseek": ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat",
                 ["deepseek-chat", "deepseek-reasoner"]),
    "qwen": ("阿里通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus",
             ["qwen-plus", "qwen-max", "qwen-turbo"]),
    "openai": ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini",
               ["gpt-4o-mini", "gpt-4o"]),
    "custom": ("自定义", "", "", []),
}


class SettingsDialog(QDialog):
    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(520)
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

    # ---------- 页签构建 ----------
    def _build_asr_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.asr_engine = QComboBox()
        self.asr_engine.addItems(["local", "cloud"])
        self.asr_engine.setCurrentText(self.config.get("asr.engine", "local"))
        form.addRow("引擎：", self.asr_engine)

        self.asr_model = QComboBox()
        self.asr_model.addItems(["tiny", "base", "small", "medium", "large-v3"])
        self.asr_model.setCurrentText(self.config.get("asr.model", "small"))
        form.addRow("本地模型（越大越准越慢）：", self.asr_model)

        self.asr_language = QComboBox()
        self.asr_language.addItems(["zh", "en", "auto"])
        self.asr_language.setCurrentText(self.config.get("asr.language", "zh"))
        form.addRow("语言：", self.asr_language)

        self.asr_compute = QComboBox()
        self.asr_compute.addItems(["int8", "float16"])
        self.asr_compute.setCurrentText(self.config.get("asr.compute_type", "int8"))
        form.addRow("计算精度：", self.asr_compute)

        self.cloud_base = QLineEdit(self.config.get("asr.cloud.base_url", ""))
        form.addRow("云端 Base URL：", self.cloud_base)
        self.cloud_key = QLineEdit(self.config.get("asr.cloud.api_key", ""))
        self.cloud_key.setEchoMode(QLineEdit.Password)
        form.addRow("云端 API Key：", self.cloud_key)
        self.cloud_model = QLineEdit(self.config.get("asr.cloud.model", "whisper-1"))
        form.addRow("云端模型：", self.cloud_model)
        return w

    def _build_vision_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.v_interval = QDoubleSpinBox()
        self.v_interval.setRange(0.5, 5.0)
        self.v_interval.setSingleStep(0.5)
        self.v_interval.setValue(float(self.config.get("vision.interval_sec", 1.5)))
        form.addRow("抓屏间隔（秒）：", self.v_interval)

        self.v_threshold = QDoubleSpinBox()
        self.v_threshold.setRange(0.05, 0.5)
        self.v_threshold.setSingleStep(0.01)
        self.v_threshold.setValue(float(self.config.get("vision.phash_threshold", 0.18)))
        form.addRow("翻页灵敏度阈值（越小越灵敏）：", self.v_threshold)

        self.v_backtrack = QDoubleSpinBox()
        self.v_backtrack.setRange(0.01, 0.2)
        self.v_backtrack.setSingleStep(0.01)
        self.v_backtrack.setValue(float(self.config.get("vision.backtrack_tol", 0.08)))
        form.addRow("翻回识别容差：", self.v_backtrack)

        self.v_cooldown = QDoubleSpinBox()
        self.v_cooldown.setRange(0.5, 10.0)
        self.v_cooldown.setSingleStep(0.5)
        self.v_cooldown.setValue(float(self.config.get("vision.cooldown_sec", 3.0)))
        form.addRow("冷却时间（秒，防动画误检）：", self.v_cooldown)
        return w

    def _build_llm_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        # 服务商预设
        self.llm_provider = QComboBox()
        for key, (name, _base, _model, _models) in LLM_PRESETS.items():
            self.llm_provider.addItem(name, key)
        # 根据已保存的 base_url 反选服务商
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
        key_hint = QLabel("（智谱 GLM 的 Key 在 open.bigmodel.cn 获取，格式如 "
                          "xxxxxxxx.xxxxxxxx）")
        key_hint.setStyleSheet("color:#888; font-size:11px;")
        form.addRow("", key_hint)

        # 模型：可编辑下拉框（预设模型 + 自由输入）
        self.llm_model = QComboBox()
        self.llm_model.setEditable(True)
        cur_model = self.config.get("llm.model", "")
        self._fill_llm_models(cur_provider, cur_model)
        form.addRow("模型：", self.llm_model)

        self.llm_temp = QDoubleSpinBox()
        self.llm_temp.setRange(0.0, 1.0)
        self.llm_temp.setSingleStep(0.1)
        self.llm_temp.setValue(float(self.config.get("llm.temperature", 0.3)))
        form.addRow("温度：", self.llm_temp)
        return w

    def _fill_llm_models(self, provider: str, keep: str = "") -> None:
        """填充当前服务商的常用模型列表"""
        models = LLM_PRESETS.get(provider, ("", "", "", []))[3]
        self.llm_model.clear()
        if models:
            self.llm_model.addItems(models)
        if keep:
            self.llm_model.setCurrentText(keep)

    def _on_llm_provider_changed(self) -> None:
        """切换服务商：自动填充 Base URL 与默认模型（不覆盖已输入的 Key）"""
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
        return w

    # ---------- 保存 ----------
    def _save(self) -> None:
        cfg = self.config
        cfg.set("asr.engine", self.asr_engine.currentText())
        cfg.set("asr.model", self.asr_model.currentText())
        cfg.set("asr.language", self.asr_language.currentText())
        cfg.set("asr.compute_type", self.asr_compute.currentText())
        cfg.set("asr.cloud.base_url", self.cloud_base.text().strip())
        cfg.set("asr.cloud.api_key", self.cloud_key.text().strip())
        cfg.set("asr.cloud.model", self.cloud_model.text().strip())

        cfg.set("vision.interval_sec", self.v_interval.value())
        cfg.set("vision.phash_threshold", self.v_threshold.value())
        cfg.set("vision.backtrack_tol", self.v_backtrack.value())
        cfg.set("vision.cooldown_sec", self.v_cooldown.value())

        cfg.set("llm.provider", self.llm_provider.currentData() or "custom")
        cfg.set("llm.base_url", self.llm_base.text().strip())
        cfg.set("llm.api_key", self.llm_key.text().strip())
        cfg.set("llm.model", self.llm_model.currentText().strip())
        cfg.set("llm.temperature", self.llm_temp.value())

        cfg.set("output.dir", self.out_dir.text().strip())
        cfg.save()
        self.accept()
