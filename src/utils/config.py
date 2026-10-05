# -*- coding: utf-8 -*-
"""应用配置：加载/保存 config/settings.json，深度合并默认值

v1.2 变更：
- **敏感字段透明加解密**：API Key（LLM / 云端 ASR）在读取时自动解密、
  保存时自动用Windows DPAPI 加密，业务代码拿到的永远是明文，
  磁盘上永远是密文。敏感字段清单见 `SECRET_KEYS`。
- 配置损坏时自动备份为 `settings.json.corrupt` 再回落默认值（便于事后排查）。
"""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from src.utils.logger import logger
from src.utils.secrets_store import decrypt_secret, encrypt_secret

# 需要加密落盘的配置路径（点号路径）
SECRET_KEYS = (
    "llm.api_key",
    "asr.cloud.api_key",
)

# 默认配置（与 config/settings.example.json 保持一致，用于缺省兜底）
DEFAULTS: dict = {
    "asr": {
        "engine": "local",
        "language": "zh",
        "model": "small",
        "device": "auto",              # auto | cpu | cuda（auto 含 CUDA 探测回落）
        "compute_type": "int8",
        "hf_mirror": "https://hf-mirror.com",  # 模型下载镜像站；留空用官方源
        "silence_sec": 0.8,            # 句尾静音判定（秒，需真正累积到该值才出段）
        "min_segment_sec": 1.5,
        "max_segment_sec": 15.0,       # 连续说话时的最大出字间隔（秒）
        "pre_roll_sec": 0.3,           # 段首保留的音频，避免切掉第一个字
        "cloud": {"base_url": "https://api.openai.com/v1", "api_key": "", "model": "whisper-1"},
    },
    "vision": {
        "interval_sec": 1.5,
        # 三层判据阈值（标定依据见 tests/test_vision_algo.py）
        "phash_threshold": 0.15,       # pHash 距离：超过视为"可能是另一页"
        "min_diff_ratio": 0.02,        # 差异像素占比下限：低于此值判为光标/噪声
        "ssim_threshold": 0.95,        # SSIM 下限：高于此值判为"结构未变"
        "backtrack_tol": 0.08,         # 翻回复用容差
        "cooldown_sec": 3.0,           # 翻页冷却防抖
        "capture": "primary",          # primary | region
        "region": {"x": 0, "y": 0, "w": 1920, "h": 1080},
        # 内容区 ROI（归一化 0~1），用于排除播放器控制栏；null 表示全画面
        "roi": None,
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
    """配置管理器（单例风格，由 AppContext 持有）

    密钥处理约定：
    - `get()` / `as_dict()` 返回**解密后**的明文（业务无感）
    - `save()` 落盘时**加密**敏感字段
    """

    def __init__(self, config_path: str | Path):
        self.path = Path(config_path)
        self._data: dict = copy.deepcopy(DEFAULTS)
        self.load()

    # ---------- 加载 / 保存 ----------
    def load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self._data = _deep_merge(DEFAULTS, raw)
                self._decrypt_secrets()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"配置文件解析失败，已备份为 settings.json.corrupt，"
                               f"使用默认配置: {e}")
                self._backup_corrupt()
                self._data = copy.deepcopy(DEFAULTS)
        else:
            self.save()  # 首次运行生成默认配置

    def _backup_corrupt(self) -> None:
        try:
            if self.path.exists():
                shutil.copy2(self.path, self.path.with_suffix(".json.corrupt"))
        except Exception:  # noqa: BLE001
            logger.warning("备份损坏配置失败", exc_info=True)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self.as_dict()
        self._encrypt_secrets(data)
        # 原子写：避免断电/崩溃产生半截配置（配置损坏会导致下次启动异常）
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    # ---------- 密钥加解密 ----------
    def _get_path(self, data: dict, dotted: str) -> Any:
        node: Any = data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def _set_path(self, data: dict, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node: dict = data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def _decrypt_secrets(self) -> None:
        for key in SECRET_KEYS:
            val = self._get_path(self._data, key)
            if isinstance(val, str) and val:
                self._set_path(self._data, key, decrypt_secret(val))

    def _encrypt_secrets(self, data: dict) -> None:
        for key in SECRET_KEYS:
            val = self._get_path(data, key)
            if isinstance(val, str) and val:
                self._set_path(data, key, encrypt_secret(val))

    # ---------- 读写接口（对业务透明，均为明文）----------
    def get(self, dotted_key: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted_key: str, value: Any) -> None:
        parts = dotted_key.split(".")
        node: Any = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def as_dict(self) -> dict:
        """返回配置副本（密钥为明文，供运行时使用）"""
        return copy.deepcopy(self._data)

    def as_disk_dict(self) -> dict:
        """返回落盘形态（密钥已加密），用于查看/导出配置"""
        data = self.as_dict()
        self._encrypt_secrets(data)
        return data

    def update(self, data: dict) -> None:
        self._data = _deep_merge(self._data, data)
        self._decrypt_secrets()

    @property
    def output_root(self) -> Path:
        p = Path(self.get("output.dir", "output"))
        return p if p.is_absolute() else (self.path.parent.parent / p).resolve()