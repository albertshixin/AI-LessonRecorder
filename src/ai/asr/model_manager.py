# -*- coding: utf-8 -*-
"""语音模型管理：下载/检测 faster-whisper 模型，支持进度回调

- 模型源：HuggingFace 上 Systran 组织的 faster-whisper 模型仓库
- 下载目标：huggingface 默认缓存目录 C:\\Users\\<用户>\\.cache\\huggingface\\hub\\
- 通过 tqdm_class 适配器把下载进度回调出去
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from huggingface_hub import snapshot_download

ProgressCallback = Optional[Callable[[str, int, int], None]]


def _repo_id(model_name: str) -> str:
    """faster-whisper 模型名 → HF 仓库"""
    if model_name in ("tiny.en", "base.en"):
        return f"Systran/faster-whisper-{model_name}"
    return f"Systran/faster-whisper-{model_name}"


# ─────────────────────────────────────────────────────────────────────
#  进度适配器（实现 huggingface_hub 要求的 tqdm 接口）
# ─────────────────────────────────────────────────────────────────────
class _HFTqdmAdapter:
    """snapshot_download 会用 tqdm_class(total=, desc=) 实例化此适配器
    每次文件下载完成时调用 update(n=1)，把进度回调给 UI 层。
    """
    callback: ProgressCallback = None  # 静态，由 download() 注入

    def __init__(self, total: int | None = None, desc: str | None = None,
                 disable: bool | None = None, **kwargs) -> None:
        self.total = int(total or 0)
        self.desc = desc or ""
        self.n = 0
        self._fire()

    def _fire(self) -> None:
        if _HFTqdmAdapter.callback and self.total:
            try:
                _HFTqdmAdapter.callback(self.desc, self.n, self.total)
            except Exception:  # noqa: BLE001
                pass

    def update(self, n: int = 1) -> None:
        self.n += int(n)
        self._fire()

    def set_description(self, desc: str) -> None:
        self.desc = desc
        self._fire()

    def close(self) -> None:
        pass

    def __enter__(self) -> "_HFTqdmAdapter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ─────────────────────────────────────────────────────────────────────
#  对外模型管理 API
# ─────────────────────────────────────────────────────────────────────
# 文件大小估计（MB）——用于 UI 提示
MODEL_SIZE_HINT_MB = {
    "tiny": 75,
    "tiny.en": 75,
    "base": 142,
    "base.en": 142,
    "small": 466,
    "small.en": 466,
    "medium": 1500,
    "medium.en": 1500,
    "large-v1": 2900,
    "large-v2": 2900,
    "large-v3": 3100,
}


class ModelManager:
    _lock = threading.Lock()  # 同进程内只允许一个下载

    @classmethod
    def is_downloaded(cls, name: str) -> bool:
        """本地是否已有模型（只探测 config.json，最小成本）"""
        try:
            snapshot_download(
                repo_id=_repo_id(name),
                allow_patterns=["config.json"],
                local_files_only=True,
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    @classmethod
    def local_path(cls, name: str) -> Optional[str]:
        """返回本地模型目录路径；未下载返回 None"""
        if not cls.is_downloaded(name):
            return None
        return snapshot_download(
            repo_id=_repo_id(name),
            allow_patterns=["config.json"],
            local_files_only=True,
        )

    @classmethod
    def download(cls, name: str, on_progress: ProgressCallback = None) -> str:
        """下载模型并返回本地目录路径"""
        with cls._lock:
            _HFTqdmAdapter.callback = on_progress
            try:
                return snapshot_download(
                    repo_id=_repo_id(name),
                    allow_patterns=[
                        "*.json", "*.txt", "*.bin",
                        "tokenizer*", "*.tiktoken", "vocabulary*",
                    ],
                    tqdm_class=_HFTqdmAdapter,
                )
            finally:
                _HFTqdmAdapter.callback = None