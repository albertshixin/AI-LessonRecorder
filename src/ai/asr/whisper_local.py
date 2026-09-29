# -*- coding: utf-8 -*-
"""本地 ASR：faster-whisper（CTranslate2 加速，CPU/GPU 自动选择，离线可用）"""
import numpy as np

from src.utils.logger import logger


class LocalWhisperEngine:
    """faster-whisper 本地转写引擎

    transcribe_segment(audio: np.ndarray float32 [-1,1] @16k, language) -> (text, conf)
    """

    name = "local-whisper"

    def __init__(self, model_size: str = "small", device: str = "auto",
                 compute_type: str = "int8") -> None:
        from faster_whisper import WhisperModel  # 延迟导入，加快启动
        if device == "auto":
            try:
                import ctranslate2
                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:  # noqa: BLE001
                device = "cpu"
        if device == "cpu":
            compute_type = "int8"
        logger.info(f"加载 Whisper 模型: {model_size} ({device}/{compute_type}) ...")
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        logger.info("Whisper 模型加载完成")

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
        # avg_logprob 越接近 0 越好，映射到 0~1 粗略置信度
        conf = max(0.0, min(1.0, 1.0 + (sum(probs) / len(probs)))) if probs else 0.0
        return text, conf
