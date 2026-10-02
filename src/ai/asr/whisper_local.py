# -*- coding: utf-8 -*-
"""本地 ASR：faster-whisper（CTranslate2 加速，CPU/GPU 自动选择，离线可用）"""
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

    模型来源：
      - 若提供 model_path（推荐），直接加载本地目录，不再触发下载
      - 若不提供，按 model_size 名字加载（首次会触发 HuggingFace 下载，可能阻塞）
    """

    name = "local-whisper"

    def __init__(self, model_size: str = "small", device: str = "auto",
                 compute_type: str = "int8", model_path: str | None = None) -> None:
        from faster_whisper import WhisperModel  # 延迟导入，加快启动
        if device == "auto":
            try:
                import ctranslate2
                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:  # noqa: BLE001
                device = "cpu"
        if device == "cpu":
            compute_type = "int8"
        src = model_path or model_size

        # GPU 探测：仅检测到显卡不代表 CUDA 运行库可用（常缺 cublas64_12.dll/
        # cudnn 等）。加载后立即做一次微型转写探测，失败自动回落 CPU，
        # 否则每段转写都会抛异常导致"没有文字"。
        if device == "cuda":
            try:
                logger.info(f"加载 Whisper 模型: {src} (cuda/{compute_type}) ...")
                probe = WhisperModel(src, device="cuda", compute_type=compute_type)
                self._probe_run(probe)
                self.model = probe
            except Exception as e:  # noqa: BLE001
                logger.warning(f"CUDA 不可用（{e}），自动回落 CPU")
                device, compute_type = "cpu", "int8"
                self.model = WhisperModel(src, device=device, compute_type=compute_type)
        else:
            logger.info(f"加载 Whisper 模型: {src} ({device}/{compute_type}) ...")
            self.model = WhisperModel(src, device=device, compute_type=compute_type)
        self.device = device
        logger.info(f"Whisper 模型加载完成（device={device}）")

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