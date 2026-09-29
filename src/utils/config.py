# -*- coding: utf-8 -*-
"""应用配置：加载/保存 config/settings.json，深度合并默认值"""
import json
import copy
from pathlib import Path
from typing import Any

from src.utils.logger import logger

# 默认配置（与 config/settings.json 保持一致，用于缺省兜底）
DEFAULTS: dict = {
    "asr": {
        "engine": "local",
        "language": "zh",
        "model": "small",
        "device": "auto",
        "compute_type": "int8",
        "silence_sec": 0.8,
        "min_segment_sec": 1.5,
        "max_segment_sec": 30.0,
        "cloud": {"base_url": "https://api.openai.com/v1", "api_key": "", "model": "whisper-1"},
    },
    "vision": {
        "interval_sec": 1.5,
        "phash_threshold": 0.18,
        "backtrack_tol": 0.08,
        "cooldown_sec": 3.0,
        "capture": "primary",          # primary | region
        "region": {"x": 0, "y": 0, "w": 1920, "h": 1080},
    },
    "llm": {
        "provider": "deepseek",     # glm | deepseek | qwen | openai | custom
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.3,
        "max_chunk_chars": 3000,
    },
    "output": {"dir": "output", "keep_audio": True},
    "ui": {"theme": "light"},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class AppConfig:
    """配置管理器（单例风格，由 AppContext 持有）"""

    def __init__(self, config_path: str | Path):
        self.path = Path(config_path)
        self._data: dict = copy.deepcopy(DEFAULTS)
        self.load()

    # ---------- 基本操作 ----------
    def load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = _deep_merge(DEFAULTS, json.load(f))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"配置文件解析失败，使用默认配置: {e}")
        else:
            self.save()  # 首次运行生成默认配置

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    # ---------- 读写接口 ----------
    def get(self, dotted_key: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted_key: str, value: Any) -> None:
        parts = dotted_key.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def as_dict(self) -> dict:
        return copy.deepcopy(self._data)

    def update(self, data: dict) -> None:
        self._data = _deep_merge(self._data, data)

    @property
    def output_root(self) -> Path:
        p = Path(self.get("output.dir", "output"))
        return p if p.is_absolute() else (self.path.parent.parent / p).resolve()
