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
import threading

import numpy as np

from src.utils.logger import logger

# ─────────────────────────────────────────────────────────────────────
#  本进程内的 CUDA 可用性结论（只判定一次，且只会被"证伪"一次）
# ─────────────────────────────────────────────────────────────────────
# 为什么需要记忆？——这是「停止后再开始录制，软件卡死」的根因所在：
#   第 1 次加载：`WhisperModel(device="cuda")` 抛异常（缺 cublas64_12.dll）→ 回落 CPU；
#   第 2 次加载：同样的调用**不再抛异常**，而是**永久卡死**在 encode()——
#                CUDA 上下文在首次失败后已处于损坏状态，第二次进入即死锁。
# 死锁没有任何异常可捕获，唯一的防御是：一旦证明 CUDA 不可用，本进程内永不重试。
_CUDA_MEMO: dict[str, object] = {"checked": False, "usable": False, "reason": ""}
_CUDA_MEMO_LOCK = threading.Lock()

# CUDA 构建 + 微型转写探测的超时上限（秒）。超时即判定不可用并回落 CPU。
CUDA_PROBE_TIMEOUT_SEC = 30.0


def cuda_state() -> tuple[bool, bool, str]:
    """返回 (是否已验证, 是否可用, 不可用原因)"""
    with _CUDA_MEMO_LOCK:
        return (bool(_CUDA_MEMO["checked"]), bool(_CUDA_MEMO["usable"]),
                str(_CUDA_MEMO["reason"]))


def mark_cuda_unusable(reason: str) -> None:
    """标记 CUDA 不可用（本进程内永久生效）"""
    with _CUDA_MEMO_LOCK:
        _CUDA_MEMO.update(checked=True, usable=False, reason=str(reason))
    logger.warning(f"CUDA 判定不可用（{reason}）：本进程内一律改用 CPU，不再重试 GPU")


def mark_cuda_usable() -> None:
    with _CUDA_MEMO_LOCK:
        _CUDA_MEMO.update(checked=True, usable=True, reason="")


def _resolve_device(requested: str) -> str:
    """把 "auto"/"cuda"/"cpu" 解析为实际设备，遵循本进程内已验证的结论"""
    req = (requested or "auto").lower()
    checked, usable, _ = cuda_state()
    if req == "cpu":
        return "cpu"
    if req == "cuda":
        # 已被证伪 → 不再重试（否则会卡死，见 _CUDA_MEMO 注释）
        return "cpu" if (checked and not usable) else "cuda"
    # auto
    if checked:
        return "cuda" if usable else "cpu"
    try:
        import ctranslate2
        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:  # noqa: BLE001
        return "cpu"


def _probe_run(model) -> None:
    """微型转写探测：验证 CUDA 运行库真正可用（拿到显卡≠能跑）"""
    segments, _info = model.transcribe(
        np.zeros(1600, dtype=np.float32), language="zh", beam_size=1)
    for _ in segments:      # 触发惰性计算
        break


def _build_with_cuda_probe(build, compute_type: str,
                           timeout: float = CUDA_PROBE_TIMEOUT_SEC):
    """带**超时**的 CUDA 构建 + 探测，返回 (model|None, 失败原因|None)

    必须放在子线程里跑：CUDA 运行库残缺时第二次加载会直接死锁在 encode()，
    死锁期间没有任何异常可捕获，只有 join(timeout) 能救回来。
    """
    box: dict = {}

    def work() -> None:
        try:
            m = build("cuda", compute_type)
            _probe_run(m)
            box["model"] = m
        except Exception as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=work, daemon=True, name="CudaProbe")
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        # 探测线程卡死：留着它自生自灭（daemon），本进程后续一律走 CPU
        return None, f"CUDA 探测 {timeout:g}s 未返回（疑似死锁）"
    if "model" in box:
        return box["model"], None
    return None, str(box.get("err", "未知原因"))


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

    # 引擎实例会被跨会话复用，推理串行化（同一时刻只有一个转写线程，锁基本无争用）
    _infer_lock = threading.Lock()

    def __init__(self, model_size: str = "small", device: str = "auto",
                 compute_type: str = "int8", model_path: str | None = None,
                 allow_download: bool = True) -> None:
        from faster_whisper import WhisperModel  # 延迟导入，加快启动

        # GPU 探测：仅检测到显卡不代表 CUDA 运行库可用（常缺 cublas64_12.dll/
        # cudnn 等）。加载后立即做一次微型转写探测，失败/超时自动回落 CPU，
        # 否则每段转写都会抛异常导致"没有文字"。
        # 关键：探测结论会**记住**，同一进程内失败过一次就永不重试 GPU
        # （重试会死锁，详见 _CUDA_MEMO 注释）。
        device = _resolve_device(device)
        if device == "cpu":
            compute_type = "int8"

        src, local_only = self._resolve_source(model_size, model_path, allow_download)
        self._model_ref = src

        def build(dev: str, ct: str):
            return WhisperModel(src, device=dev, compute_type=ct,
                                local_files_only=local_only)

        if device == "cuda":
            logger.info(f"加载 Whisper 模型: {src} (cuda/{compute_type}) ...")
            model, err = _build_with_cuda_probe(build, compute_type)
            if model is None:
                mark_cuda_unusable(err or "未知原因")
                device, compute_type = "cpu", "int8"
                self.model = build(device, compute_type)
            else:
                mark_cuda_usable()
                self.model = model
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

    def warmup(self) -> None:
        try:
            self.transcribe_segment(np.zeros(1600, dtype=np.float32), "zh")
        except Exception:  # noqa: BLE001
            pass

    def transcribe_segment(self, audio: np.ndarray, language: str = "zh") -> tuple[str, float]:
        # 引擎实例会被跨会话复用（见 asr_engine 缓存），加串行锁避免并发推理
        with self._infer_lock:
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