# -*- coding: utf-8 -*-
"""ASR 引擎抽象接口与工厂"""
from __future__ import annotations

import threading

_ENGINE_CACHE: dict[str, object] = {}
_ENGINE_CACHE_LOCK = threading.Lock()


class ASREngineBase:
    """语音转文本引擎基类：输入 16k mono float32 音频段，输出 (文本, 置信度)"""

    name = "base"

    def transcribe_segment(self, audio: "list[float]", language: str = "zh") -> tuple[str, float]:
        raise NotImplementedError

    def warmup(self) -> None:
        """可选的预热（加载模型等）"""


def _signature(cfg: dict, model_path: str | None, engine: str) -> str:
    """引擎配置指纹：配置未变则复用同一实例"""
    keys = ("model", "device", "compute_type")
    parts = [engine]
    for k in keys:
        parts.append(f"{k}={cfg.get(k)}")
    if engine == "local":
        parts.append(f"path={model_path}")
    else:
        parts.append(f"cloud={cfg.get('cloud', {})}")
    return "|".join(parts)


def create_asr_engine(cfg: dict, model_path: str | None = None,
                      use_cache: bool = True):
    """根据配置创建 ASR 引擎（**相同配置复用同一实例**）

    cfg 形如 config['asr']：{"engine": "local"|"cloud", ...}
    model_path：本地模型的已下载目录路径（仅 local 引擎有效，避免再触发下载）

    为什么复用：
    - Whisper 模型加载一次要数秒到数十秒（模型越大越久），而 start() 在
      **主线程**执行 —— 每次「停止后再开始」都重新加载会让界面长时间无响应，
      表现为"卡死"。复用后第二次开始录制是瞬时完成的。
    - 同时规避了 CUDA 探测重试的风险（见 whisper_local._CUDA_MEMO）。

    配置（模型档位/设备/云端参数）变化时指纹改变，会自动重建新实例。
    """
    engine = (cfg.get("engine") or "local").lower()
    sig = _signature(cfg, model_path, engine)

    if use_cache:
        with _ENGINE_CACHE_LOCK:
            cached = _ENGINE_CACHE.get(sig)
        if cached is not None:
            return cached

    if engine == "cloud":
        from src.ai.asr.cloud_asr import CloudASR
        inst = CloudASR(cfg.get("cloud", {}))
    else:
        from src.ai.asr.whisper_local import LocalWhisperEngine
        inst = LocalWhisperEngine(
            model_size=cfg.get("model", "small"),
            device=cfg.get("device", "auto"),
            compute_type=cfg.get("compute_type", "int8"),
            model_path=model_path,
        )

    if use_cache:
        with _ENGINE_CACHE_LOCK:
            _ENGINE_CACHE[sig] = inst
    return inst


def clear_engine_cache() -> None:
    """丢弃已缓存的引擎实例（切换模型/设备后调用，下次按需重建）"""
    with _ENGINE_CACHE_LOCK:
        _ENGINE_CACHE.clear()
