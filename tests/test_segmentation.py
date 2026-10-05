# -*- coding: utf-8 -*-
"""离线重转写与实时分段的算法测试（合成音频，不依赖模型）

运行：python tests/test_segmentation.py
"""
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from src.ai.asr.offline_retranscribe import (SAMPLE_RATE, read_wav_mono16k,
                                             split_by_silence)

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  [OK] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILURES.append(msg)


def tone(dur: float, freq: float = 220.0, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(dur * SAMPLE_RATE)) / SAMPLE_RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(dur: float) -> np.ndarray:
    return np.zeros(int(dur * SAMPLE_RATE), dtype=np.float32)


def build_audio() -> np.ndarray:
    """4 段 10s 语音（间隔 1.2s 静音）+ 1 段 40s 超长语音"""
    parts = []
    for i in range(4):
        parts.append(tone(10.0, 220.0 + i * 20))
        parts.append(silence(1.2))
    parts.append(tone(40.0, 180.0))
    return np.concatenate(parts)


def test_offline_split() -> None:
    print("\n[1] 离线静音分段")
    audio = build_audio()
    total_sec = len(audio) / SAMPLE_RATE
    segs = split_by_silence(audio, max_sec=30.0, silence_sec=0.5, min_sec=1.0)
    print(f"  合成音频 {total_sec:.1f}s → 切出 {len(segs)} 段")
    for i, (ts, s) in enumerate(segs):
        print(f"    段{i + 1}: ts={ts:6.2f}s  时长={len(s) / SAMPLE_RATE:5.2f}s")

    check(len(segs) >= 5, f"至少切出 5 段（4 段语音 + 40s 段硬切后≥2 段），实得 {len(segs)}")
    max_allowed = int(29.5 * SAMPLE_RATE)
    check(all(len(s) <= max_allowed for _, s in segs), "超长段已按 max_sec 硬切（≤29.5s）")

    covered = sum(len(s) for _, s in segs)
    ratio = covered / len(audio)
    #覆盖率不可能是 100%：段间的静音间隙（4 × 1.2s = 4.8s，占 5.7%）本就不该送 ASR。
    # 因此判据是"语音部分几乎全覆盖"，用总时长减去已知静音间隙来估算语音总长。
    expected_speech = 4 * 10.0 + 40.0            # 80s 语音
    print(f"  覆盖率 {ratio * 100:.1f}%（总音频 {total_sec:.1f}s，"
          f"其中语音 {expected_speech:.0f}s）")
    check(abs(covered / SAMPLE_RATE - expected_speech) / expected_speech < 0.03,
          f"语音部分几乎全覆盖（{covered / SAMPLE_RATE:.1f}s / {expected_speech:.0f}s），"
          f"缺口仅为段间静音")

    check(all(segs[i][0] < segs[i + 1][0] for i in range(len(segs) - 1)),
          "各段时间戳严格单调递增")

    # 时间戳应与音频实际位置对应：每段起点不能超过总时长
    check(all(0 <= ts < total_sec for ts, _ in segs), "时间戳均在音频时长范围内")


def test_wav_roundtrip() -> None:
    print("\n[2] WAV 读写往返")
    audio = build_audio()[: SAMPLE_RATE * 5]      # 前 5 秒
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "audio.wav"
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
        with wave.open(str(p), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm.tobytes())
        back = read_wav_mono16k(p)
        check(back.shape == audio.shape, f"形状一致 {back.shape}")
        err = float(np.abs(back - audio).max())
        check(err < 1e-3, f"量化误差极小 ({err:.2e})，int16 往返无损")

        # 格式不符时应明确报错，而不是静默出错
        bad = Path(td) / "bad.wav"
        with wave.open(str(bad), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(np.zeros(44100 * 2, dtype=np.int16).tobytes())
        try:
            read_wav_mono16k(bad)
            check(False, "非 16k/mono 音频应抛错")
        except ValueError:
            check(True, "非 16k/mono 音频明确抛错（而非静默出错）")


def test_edge_cases() -> None:
    print("\n[3] 边界条件")
    check(split_by_silence(np.zeros(0, dtype=np.float32)) == [], "空音频返回空列表")
    check(split_by_silence(np.zeros(SAMPLE_RATE, dtype=np.float32)) == [],
          "纯静音返回空列表（不产生垃圾段）")
    tiny = tone(0.2)
    check(len(split_by_silence(tiny)) == 0, "过短音频（<0.3s）被过滤")

    # 全部为语音的超长音频：应按 max_sec 等分
    long_speech = tone(95.0)
    segs = split_by_silence(long_speech, max_sec=30.0)
    print(f"  95s 连续语音 → {len(segs)} 段")
    check(len(segs) >= 4, f"连续语音被切成多段（{len(segs)}）")
    covered = sum(len(s) for _, s in segs)
    check(covered / len(long_speech) > 0.95, "连续语音几乎无丢失")


def test_live_vad_regression() -> None:
    """回归测试：验证 v1.2 修复的 VAD 静音累积逻辑

    这是 P0-1 的核心断言。原实现把 _tail_silence 直接赋值，
    等价于 64ms 静音即出段；修复后必须累积到 silence_sec 才出段。
    """
    print("\n[4] 实时 VAD 静音累积（P0-1 回归）")
    from src.ai.asr.live_transcriber import LiveTranscriber
    from src.core.audio.ring_buffer import RingBuffer
    from src.core.timeline import TranscriptEvent

    emitted: list[TranscriptEvent] = []

    class FakeEngine:
        name = "fake"

        def transcribe_segment(self, audio, language="zh"):
            return ("测试文本" * 5, 0.9)

    ring = RingBuffer()
    tr = LiveTranscriber(ring=ring, engine=FakeEngine(),
                         on_transcript=emitted.append,
                         silence_sec=0.8, min_segment_sec=1.5,
                         max_segment_sec=15.0, pre_roll_sec=0.0)
    tr.start()

    # 构造：10s 语音 + 1s 静音（应恰好 1 段），再 10s 语音 + 1s 静音（第 2 段）
    speech_chunk = tone(0.064)          # 1024 samples @16k
    n_speech = int(10 / 0.064)
    silent_chunk = np.zeros(1024, dtype=np.float32)

    for _ in range(n_speech):
        ring.put(0.0, (speech_chunk * 32767).astype(np.int16).tobytes())
    for _ in range(int(1.0 / 0.064) + 2):
        ring.put(0.0, (silent_chunk * 32767).astype(np.int16).tobytes())
    for _ in range(n_speech):
        ring.put(0.0, (speech_chunk * 32767).astype(np.int16).tobytes())
    for _ in range(int(1.0 / 0.064) + 2):
        ring.put(0.0, (silent_chunk * 32767).astype(np.int16).tobytes())

    tr.stop()
    tr.join(timeout=15)

    print(f"  20s 语音（分 2 段，中间 1s 静音）→ 实际输出 {len(emitted)} 段")
    # 修复前：64ms 静音即出段，会产生约 20/0.064/10 ≈ 大量碎片段
    # 修复后：应接近 2 段
    check(len(emitted) <= 3,
          f"静音累积生效，未产生碎片段（{len(emitted)} 段，应≤3）")
    check(len(emitted) >= 1, "至少产出一段转写")
    print(f"  转写统计: {tr.stats}")


if __name__ == "__main__":
    print("=" * 66)
    print("离线分段与实时 VAD 测试")
    print("=" * 66)
    test_offline_split()
    test_wav_roundtrip()
    test_edge_cases()
    test_live_vad_regression()
    print("\n" + "=" * 66)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("全部通过")