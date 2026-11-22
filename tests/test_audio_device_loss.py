# -*- coding: utf-8 -*-
"""音频设备突然消失（蓝牙耳机断连 / 播放设备被移除）时的录音行为

用户反馈：蓝牙中断 → 录音故障 → **软件崩溃**。
本测试用假 PortAudio 注入"设备消失"，验证：
  1. 崩溃根因已消除：设备失效后**不再**对流调用 stop_stream/close/terminate
  2. 设备恢复后自动重开并**接着写同一个 WAV**，时间轴连续
  3. 设备始终不恢复时，录音线程能正常退出并给出明确错误，已录内容不丢
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.audio import loopback_recorder as lr_mod
from src.core.audio.loopback_recorder import LoopbackRecorder

FAILED: list[str] = []
_real_pyaudio_cls = None


def check(ok: bool, msg: str) -> None:
    print(f"  [{'OK  ' if ok else 'FAIL'}] {msg}")
    if not ok:
        FAILED.append(msg)


class _FakeStream:
    """假音频流：可按剧本突然"设备消失"（get_read_available 抛错）"""

    def __init__(self, pa: "_FakePyAudio") -> None:
        self.sc = pa.sc
        self.pa = pa
        self.broke = False

    def get_read_available(self) -> int:
        if self.sc.down:
            self.broke = True
            raise OSError(-9999, "Unanticipated host error")  # 设备已消失
        return 1024

    def read(self, n: int, exception_on_overflow: bool = False) -> bytes:
        return np.random.randint(-2000, 2000, n * 2).astype(np.int16).tobytes()

    def stop_stream(self) -> None:
        self.pa.log.append("stop_stream")

    def close(self) -> None:
        self.pa.log.append("close")


class _FakePyAudio:
    """假 PyAudio：每次实例化 = 一次设备打开尝试"""

    def __init__(self, scenario: "_Scenario") -> None:
        self.sc = scenario
        self.stream: _FakeStream | None = None
        self.log: list[str] = []          # 本实例收到的 PortAudio 收尾调用
        self.sc.instances.append(self)

    def get_default_output_device_info(self) -> dict:
        return {"index": 1, "name": "蓝牙耳机 (Fake)"}

    def get_device_info_by_index(self, i: int) -> dict:
        return {"index": 1, "name": "蓝牙耳机 (Fake) [Loopback]",
                "isLoopbackDevice": True,
                "defaultSampleRate": 48000.0, "maxInputChannels": 2}

    def get_loopback_device_info_generator(self):
        yield self.get_device_info_by_index(1)

    def open(self, **kw):
        self.log.append("open")
        self.stream = _FakeStream(self)
        return self.stream

    def terminate(self) -> None:
        self.log.append("terminate")


class _Scenario:
    def __init__(self) -> None:
        self.down = False
        self.instances: list[_FakePyAudio] = []
        self.log: list[str] = []


def _install_fake(sc: _Scenario) -> None:
    import pyaudiowpatch
    global _real_pyaudio_cls
    _real_pyaudio_cls = pyaudiowpatch.PyAudio
    pyaudiowpatch.PyAudio = lambda: _FakePyAudio(sc)


def _restore() -> None:
    import pyaudiowpatch
    if _real_pyaudio_cls is not None:
        pyaudiowpatch.PyAudio = _real_pyaudio_cls


# ─────────────────────────── 用例 1：断连后自动恢复 ───────────────────────────
def case_recover() -> None:
    print("\n[1] 设备断开 → 自动重连 → 接着录（同一个 WAV）")
    sc = _Scenario()
    _install_fake(sc)
    try:
        with tempfile.TemporaryDirectory(prefix="lr_deviceloss_") as td:
            wav = Path(td) / "audio.wav"
            statuses: list[str] = []
            rec = LoopbackRecorder(wav_path=wav,
                                   on_chunk=lambda ts, pcm: None,
                                   on_status=statuses.append)
            rec.start()

            # 先正常录 0.5s
            t0 = time.monotonic()
            while rec.elapsed < 0.5 and time.monotonic() - t0 < 10:
                time.sleep(0.05)
            e1 = rec.elapsed
            check(e1 >= 0.5, f"断开前正常录音（elapsed={e1:.2f}s）")

            # 蓝牙断连
            sc.down = True
            time.sleep(3.0)
            check(rec.is_alive(), "设备断开期间录音线程没有死掉（不崩溃）")
            check(any("断开" in s for s in statuses),
                  f"已提示设备断开（{statuses[-1][:40] if statuses else '无'}）")
            e_mid = rec.elapsed

            # 蓝牙重连
            sc.down = False
            t0 = time.monotonic()
            while rec.elapsed < e_mid + 0.5 and time.monotonic() - t0 < 10:
                time.sleep(0.05)
            check(rec.elapsed > e_mid + 0.4,
                  f"重连后继续推进时间轴（{e_mid:.2f}s → {rec.elapsed:.2f}s）")
            check(any("恢复" in s for s in statuses), "已提示设备恢复")
            check(len(sc.instances) >= 2,
                  f"确实重新打开了设备（实例数 {len(sc.instances)}）")

            rec.stop()
            rec.join(timeout=10)
            check(not rec.is_alive(), "录音线程能正常停止")
            check(rec.error is None, f"恢复后无残留错误（{rec.error}）")

            # 崩溃根因断言：失效的流/实例绝不能再被 PortAudio 收尾
            broken_insts = [i for i in sc.instances
                            if i.stream is not None and i.stream.broke]
            check(bool(broken_insts), "确实发生过设备失效（用于校验收尾被跳过）")
            bad = [i for i in broken_insts if "terminate" in i.log
                   or "stop_stream" in i.log or "close" in i.log]
            check(not bad,
                  f"失效设备未再调用 stop_stream/close/terminate（违规 {len(bad)} 个）")
            healthy = [i for i in sc.instances
                       if not (i.stream is not None and i.stream.broke)]
            check(bool(healthy) and all("terminate" in i.log for i in healthy),
                  "正常实例仍会正常收尾（未误伤常规释放路径）")

            # WAV 完整且只有一个（重连不另起文件）
            check(wav.exists(), "WAV 存在")
            with wave.open(str(wav), "rb") as w:
                n = w.getnframes()
            check(n > 0, f"WAV 含有效音频（{n} 帧 ≈ {n / 16000:.2f}s）")
            check(len(list(Path(td).iterdir())) == 1,
                  "重连没有另起新文件（始终写同一个 WAV）")
    finally:
        _restore()


# ───────────────────── 用例 2：设备始终不恢复 → 优雅收尾 ─────────────────────
def case_give_up() -> None:
    print("\n[2] 设备一直不恢复 → 超时后正常结束，已录内容保留")
    sc = _Scenario()
    old_wait, old_retry = lr_mod.RECONNECT_WAIT_SEC, lr_mod.RECONNECT_RETRY_SEC
    lr_mod.RECONNECT_WAIT_SEC, lr_mod.RECONNECT_RETRY_SEC = 5.0, 1.0
    _install_fake(sc)
    try:
        with tempfile.TemporaryDirectory(prefix="lr_giveup_") as td:
            wav = Path(td) / "audio.wav"
            statuses: list[str] = []
            rec = LoopbackRecorder(wav_path=wav, on_status=statuses.append)
            rec.start()
            t0 = time.monotonic()
            while rec.elapsed < 0.3 and time.monotonic() - t0 < 10:
                time.sleep(0.05)
            sc.down = True            # 蓝牙彻底断了
            rec.join(timeout=25)
            check(not rec.is_alive(), "超时后录音线程自行退出（未卡死）")
            check(rec.error and "未恢复" in rec.error,
                  f"给出明确错误（{rec.error}）" if rec.error else "应给出错误提示")
            with wave.open(str(wav), "rb") as w:
                n = w.getnframes()
            check(n > 0, f"断开前的内容已保留（{n} 帧 ≈ {n / 16000:.2f}s）")
    finally:
        lr_mod.RECONNECT_WAIT_SEC, lr_mod.RECONNECT_RETRY_SEC = old_wait, old_retry
        _restore()


def main() -> int:
    print("=" * 70)
    print("音频设备突然消失（蓝牙断连）回归测试")
    print("=" * 70)
    case_recover()
    case_give_up()
    print()
    if FAILED:
        print(f"[FAIL] {len(FAILED)} 项失败")
        for m in FAILED:
            print(f"   - {m}")
        return 1
    print("[PASS] 设备断开不再崩溃，可自愈重连，失败也能优雅收尾")
    return 0


if __name__ == "__main__":
    sys.exit(main())
