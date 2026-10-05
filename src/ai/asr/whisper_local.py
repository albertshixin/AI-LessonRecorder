# -*- coding: utf-8 -*-
"""本地 ASR：faster-whisper（CTranslate2 加速，CPU/GPU 自动选择，离线可用）

v1.2 修复：**已有本地模型却仍联网**（表现为 ProxyError 502 / 超时）
- 原实现直接 `WhisperModel(model_size)`，faster-whisper 会先访问 HuggingFace
  确认版本 → 即使模型已完整缓存在本地，只要网络不通（或代理异常）就**加载失败**。
  这在���境内网/代理不稳时是硬伤：明明已经下过一次，却用不了。
- 现改为：优先 `ModelManager.local_path()` 解析本地快照目录并以
  `local_files_only=True` 加载，全程零网络；只有本地确实没有才允许联网下载。
"""
from __future__ import annotations

import os

import numpy as np

from src.utils.logger import logger


def to_simplified(text: str) -> str:
    """繁体 → 大陆简体（Whisper 中文常输出繁体，统一转换）

    zhconv 未安装时原样返回，不影响主流程。
    """
    if not text:
        return text
    try:
        from zhconv import convert
        return convert(text, "zh-cn")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"简繁转换失败（{e}），原样输出")
        return text


class LocalWhisperEngine:
    """faster-whisper 本地转写引擎

    transcribe_segment(audio: np.ndarray float32 [-1,1] @16k, language) -> (text, conf)

    模型来源优先级：
      1. 显式传入的 model_path（本地目录）→离线加载
      2. 按 model_size 解析本地 HF 缓存快照 → **离线加载（不联网）**
      3. 本地确实没有 → 退回按名字加载（会触发下载）
    """

    name = "local-whisper"

    def __init__(self, model_size: str = "small", device: str = "auto",
                 compute_type: str = "int8", model_path: str | None = None,
                 allow_download: bool = True) -> None:
        from faster_whisper import WhisperModel  # 延迟导入，加快启动

        if device == "auto":
            try:
                import ctranslate2
                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:  # noqa: BLE001
                device = "cpu"
        if device == "cpu":
            compute_type = "int8"

        src, local_only = self._resolve_source(model_size, model_path, allow_download)
        self._model_ref = src

        def build(dev: str, ct: str):
            return WhisperModel(src, device=dev, compute_type=ct,
                                local_files_only=local_only)

        # GPU 探测：仅检测到显卡不代表 CUDA 运行库可用（常缺 cublas64_12.dll/
        # cudnn 等）。加载后立即做一次微型转写探测，失败自动回落 CPU，
        # 否则每段转写都会抛异常导致"没有文字"。
        if device == "cuda":
            try:
                logger.info(f"加载 Whisper 模型: {src} (cuda/{compute_type}) ...")
                probe = build("cuda", compute_type)
                self._probe_run(probe)
                self.model = probe
            except Exception as e:  # noqa: BLE001
                logger.warning(f"CUDA 不可用（{e}），自动回落 CPU")
                device, compute_type = "cpu", "int8"
                self.model = build(device, compute_type)
        else:
            logger.info(f"加载 Whisper 模型: {src} ({device}/{compute_type}"
                        f"{'，离线' if local_only else ''}) ...")
            self.model = build(device, compute_type)
        self.device = device
        self.local_only = local_only
        logger.info(f"Whisper 模型加载完成（device={device}, "
                    f"source={os.path.basename(str(src))}, local_only={local_only}）")

    @staticmethod
    def _resolve_source(model_size: str, model_path: str | None,
                        allow_download: bool) -> tuple[str, bool]:
        """返回 (加载源, 是否强制离线)

        这是"已下载模型却不能用"问题的修复核心：
        先把模型名解析成本地快照目录，后续加载全程 `local_files_only=True`。
        """
        if model_path:
            return model_path, True
        try:
            from src.ai.asr.model_manager import ModelManager
            local = ModelManager.local_path(model_size)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"解析本地模型目录失败（{e}）")
            local = None
        if local:
            return local, True
        if not allow_download:
            raise FileNotFoundError(
                f"本地未找到 Whisper 模型 {model_size}，"
                f"且已禁用自动下载。请先在【设置 → 语音识别】中下载该模型。")
        logger.warning(f"本地未找到模型 {model_size}，将尝试联网加载（可能较慢）")
        return model_size, False

    @staticmethod
    def _probe_run(model) -> None:
        """微型转写探测：验证 CUDA 运行库真正可用"""
        segments, _info = model.transcribe(
            np.zeros(1600, dtype=np.float32), language="zh", beam_size=1)
        for _ in segments:  # 触发惰性计算
            break

    def warmup(self) -> None:
        try:
            self.transcribe_segment(np.zeros(1600, dtype=np.float32), "zh")
        except Exception:  # noqa: BLE001
            pass

    def transcribe_segment(self, audio: np.ndarray, language: str = "zh") -> tuple[str, float]:
        segments, info = self.model.transcribe(
            audio,
            language=None if language == "auto" else language,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        texts: list[str] = []
        probs: list[float] = []
        for seg in segments:
            t = (seg.text or "").strip()
            if t:
                texts.append(t)
                probs.append(float(seg.avg_logprob))
        text = "".join(texts)
        conf = max(0.0, min(1.0, 1.0 + (sum(probs) / len(probs)))) if probs else 0.0
        return to_simplified(text), conf