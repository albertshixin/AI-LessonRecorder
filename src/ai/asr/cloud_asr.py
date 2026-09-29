# -*- coding: utf-8 -*-
"""云端 ASR：OpenAI 兼容 /audio/transcriptions 接口（按段非流式，简单可靠）"""
import io
import wave
from datetime import datetime

import numpy as np
import requests

from src.utils.logger import logger


class CloudASR:
    """OpenAI 兼容语音转写 API"""

    name = "cloud"

    def __init__(self, cfg: dict) -> None:
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.api_key = cfg.get("api_key", "")
        self.model = cfg.get("model", "whisper-1")
        self.timeout = 120

    def transcribe_segment(self, audio: np.ndarray, language: str = "zh") -> tuple[str, float]:
        # float32 → 16k mono WAV bytes
        pcm = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(pcm.tobytes())
        buf.seek(0)

        try:
            resp = requests.post(
                f"{self.base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": (f"seg_{datetime.now():%H%M%S}.wav", buf, "audio/wav")},
                data={"model": self.model, "response_format": "json"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("text", "").strip(), 1.0
        except Exception as e:  # noqa: BLE001
            logger.error(f"云端 ASR 调用失败: {e}")
            return "", 0.0
