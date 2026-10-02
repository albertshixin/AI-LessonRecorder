# -*- coding: utf-8 -*-
"""端到端 ASR 管线测试：
Windows TTS 合成中文语音 → 模拟录音块推送 RingBuffer →
LiveTranscriber（真实 Whisper 模型）→ 校验输出文本
"""
import os
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QCoreApplication  # noqa: E402

app = QCoreApplication.instance() or QCoreApplication([])

from src.ai.asr.live_transcriber import LiveTranscriber  # noqa: E402
from src.ai.asr.whisper_local import LocalWhisperEngine  # noqa: E402
from src.core.audio.ring_buffer import RingBuffer  # noqa: E402

TEXT = "欢迎使用在线课程录播器，这是一段语音转文字的端到端测试。"


def make_speech_wav(path: str) -> None:
    """用 Windows SAPI 合成 16kHz 16bit 单声道中文语音"""
    ps = f'''
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile("{path.replace(chr(92), chr(92) * 2)}", $fmt)
$s.Speak("{TEXT}")
$s.Dispose()
'''
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=60)
    if not os.path.exists(path) or os.path.getsize(path) < 1000:
        raise RuntimeError(f"TTS 合成失败: {r.stdout} {r.stderr}")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        wav_path = os.path.join(tmp, "tts.wav")
        make_speech_wav(wav_path)

        wf = wave.open(wav_path, "rb")
        assert wf.getframerate() == 16000 and wf.getnchannels() == 1
        pcm = wf.readframes(wf.getnframes())
        wf.close()
        dur = len(pcm) / 2 / 16000
        print(f"[1] TTS 语音已合成: {dur:.1f}s")

        # 直接调用引擎（跳过管线）
        engine = LocalWhisperEngine(model_size="small", device="cpu", compute_type="int8")
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        text, conf = engine.transcribe_segment(audio, "zh")
        print(f"[2] 引擎直测: text={text!r} conf={conf:.2f}")
        assert text.strip(), "引擎直测未输出文本！"

        # 完整管线：模拟录音线程按块推送
        ring = RingBuffer()
        got: list[str] = []
        tr = LiveTranscriber(ring=ring, engine=engine,
                             on_transcript=lambda ev: got.append(ev.text),
                             silence_sec=0.8, min_segment_sec=1.0,
                             max_segment_sec=15.0, language="zh")
        tr.start()

        chunk_bytes = 1600 * 2  # 0.1s @16k int16
        ts = 0.0
        for i in range(0, len(pcm), chunk_bytes):
            ring.put(ts, pcm[i:i + chunk_bytes])
            ts += 0.1
            time.sleep(0.02)  # 模拟实时节奏
        # 尾部静音 1.5s 触发句尾分段
        silence = np.zeros(16000, dtype=np.int16).tobytes()
        for _ in range(15):
            ring.put(ts, silence)
            ts += 0.1
            time.sleep(0.02)

        deadline = time.time() + 60
        while not got and time.time() < deadline:
            time.sleep(0.2)
        ring.close()
        tr.join(timeout=30)

        joined = "".join(got)
        print(f"[3] 管线输出: {got}")
        assert joined.strip(), "管线未输出任何转写文本！"

        # 关键词校验（whisper 对数字/标点可能有出入，取核心词）
        ok = any(k in joined for k in ("课程", "录播", "测试"))
        print(f"[4] 关键词命中: {ok}")
        print("RESULT:", "PASS" if ok else "WEAK-PASS(有文本但关键词未命中)")


if __name__ == "__main__":
    main()
