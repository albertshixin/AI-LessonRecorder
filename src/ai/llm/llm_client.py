# -*- coding: utf-8 -*-
"""LLM 客户端：OpenAI 兼容 Chat Completions 协议（智谱GLM/DeepSeek/通义/OpenAI 均可）"""
import time

import requests

from src.utils.logger import logger


class LLMClient:
    def __init__(self, cfg: dict) -> None:
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.api_key = cfg.get("api_key", "")
        self.model = cfg.get("model", "deepseek-chat")
        self.temperature = float(cfg.get("temperature", 0.3))
        self.max_retries = 3
        self.timeout = 300

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def chat(self, prompt: str, system: str = "") -> str:
        """对话补全，带指数退避重试"""
        if not self.configured:
            raise RuntimeError("LLM 未配置：请在设置中填写 Base URL 与 API Key")

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        last_err: Exception | None = None
        payload: dict = {"model": self.model, "messages": messages}
        # 部分服务商（如智谱 GLM）要求 0 < temperature < 1，区间外不传该参数
        if 0.0 < self.temperature < 1.0:
            payload["temperature"] = self.temperature
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json"},
                    json=payload,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
            except Exception as e:  # noqa: BLE001
                last_err = e
                wait = 2 ** attempt
                logger.warning(f"LLM 调用失败(第{attempt}次): {e}，{wait}s 后重试")
                if attempt < self.max_retries:
                    time.sleep(wait)
        raise RuntimeError(f"LLM 调用失败（已重试 {self.max_retries} 次）: {last_err}")
