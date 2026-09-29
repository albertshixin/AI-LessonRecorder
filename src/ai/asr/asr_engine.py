# -*- coding: utf-8 -*-
"""ASR 引擎抽象接口与工厂"""


class ASREngineBase:
    """语音转文本引擎基类：输入 16k mono float32 音频段，输出 (文本, 置信度)"""

    name = "base"

    def transcribe_segment(self, audio: "list[float]", language: str = "zh") -> tuple[str, float]:
        raise NotImplementedError

    def warmup(self) -> None:
        """可选的预热（加载模型等）"""


def create_asr_engine(cfg: dict, model_path: str | None = None):
    """根据配置创建 ASR 引擎

    cfg 形如 config['asr']：{"engine": "local"|"cloud", ...}
    model_path：本地模型的已下载目录路径（仅 local 引擎有效，避免再触发下载）
    """
    engine = (cfg.get("engine") or "local").lower()
    if engine == "cloud":
        from src.ai.asr.cloud_asr import CloudASR
        return CloudASR(cfg.get("cloud", {}))
    from src.ai.asr.whisper_local import LocalWhisperEngine
    return LocalWhisperEngine(
        model_size=cfg.get("model", "small"),
        device=cfg.get("device", "auto"),
        compute_type=cfg.get("compute_type", "int8"),
        model_path=model_path,
    )