# -*- coding: utf-8 -*-
"""系统声音环回录音：WASAPI Loopback 采集，输出 16kHz 单声道 int16 流

- 不需要麦克风，捕获系统正在播放的声音（在线课程声音）
- 暂停时丢弃音频（不写入、不推进时间轴）
- 音频同时写入 WAV 存档
"""
import threading
import time
import wave

import numpy as np

from src.utils.logger import logger

TARGET_RATE = 16000


class LoopbackRecorder(threading.Thread):
    """环回录音线程

    on_chunk(ts: float, pcm: bytes)：每块回调，ts 为该块起点的录制相对时间（秒）
    """

    def __init__(self, wav_path, on_chunk=None, on_level=None, on_status=None) -> None:
        super().__init__(daemon=True, name="LoopbackRecorder")
        self.wav_path = str(wav_path)
        self.on_chunk = on_chunk
        self.on_level = on_level
        self.on_status = on_status
        self._stop_evt = threading.Event()
        self._pause_evt = threading.Event()
        self._elapsed = 0.0          # 累计有效录制时长（秒，不含暂停）
        self._level = 0.0            # 当前音量电平 0~1
        self._error: str | None = None
        self._stream = None          # 保持引用，停止时可主动 abort 解除阻塞

    # ---------- 外部控制 ----------
    def stop(self) -> None:
        self._stop_evt.set()
        # 主动停止音频流，解除可能阻塞中的 stream.read（如设备一直无数据）
        s = self._stream
        if s is not None:
            try:
                s.stop_stream()
            except Exception:  # noqa: BLE001
                pass

    def pause(self) -> None:
        self._pause_evt.set()

    def resume(self) -> None:
        self._pause_evt.clear()

    @property
    def elapsed(self) -> float:
        return self._elapsed

    @property
    def level(self) -> float:
        return self._level

    @property
    def error(self) -> str | None:
        return self._error

    # ---------- 设备查找 ----------
    @staticmethod
    def _find_loopback_device(p) -> dict | None:
        """找到默认输出声道对应的 WASAPI Loopback 设备

        注意：paWASAPI 是 pyaudiowpatch 的模块级常量，不是 PyAudio 实例属性。
        """
        try:
            default_out = p.get_device_info_by_index(
                p.get_default_output_device_info()["index"])
            if default_out.get("isLoopbackDevice"):
                return default_out
            # 查找与默认输出同名的环回设备（环回设备名 = 输出名 + " [Loopback]"）
            for lb in p.get_loopback_device_info_generator():
                if default_out["name"] in lb["name"]:
                    return lb
            logger.warning(f"默认输出「{default_out['name']}」没有匹配的环回设备，使用第一个环回设备兜底")
        except Exception as e:  # noqa: BLE001
            logger.error(f"查找默认输出对应的环回设备失败: {e}")
        # 兜底：任意一个环回设备
        try:
            for lb in p.get_loopback_device_info_generator():
                return lb
        except Exception:  # noqa: BLE001
            pass
        return None

    # ---------- 主循环 ----------
    def run(self) -> None:
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            self._error = "未安装 PyAudioWPatch，无法录制系统声音"
            logger.error(self._error)
            return

        p = pyaudio.PyAudio()
        try:
            dev = self._find_loopback_device(p)
            if dev is None:
                self._error = ("未找到系统声音环回设备。请确认 Windows 输出设备正常，"
                               "且至少播放过一次声音。")
                logger.error(self._error)
                return

            src_rate = int(dev["defaultSampleRate"])
            src_channels = min(int(dev["maxInputChannels"]) or 2, 2)
            logger.info(f"环回录音设备: {dev['name']}  {src_rate}Hz/{src_channels}ch")

            wf = wave.open(self.wav_path, "wb")
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(TARGET_RATE)

            stream = p.open(
                format=pyaudio.paInt16,
                channels=src_channels,
                rate=src_rate,
                input=True,
                input_device_index=int(dev["index"]),
                frames_per_buffer=1024,
            )
            self._stream = stream

            # 提示用户正在从哪个设备录音（方便排查"没声音"问题）
            msg = f"正在录制系统声音：{dev['name']}（{src_rate}Hz）"
            logger.info(msg)
            if self.on_status:
                try:
                    self.on_status(msg)
                except Exception:  # noqa: BLE001
                    pass

            last_data_t = time.monotonic()

            # 看门狗：长时间收不到任何音频数据时提醒（如选错播放设备）
            def _no_data_watchdog():
                while not self._stop_evt.is_set():
                    if time.monotonic() - last_data_t > 20:
                        msg = ("超过 20 秒未收到系统声音。若电平条一直为 0，"
                               "请确认课程声音是从默认输出设备播放的")
                        logger.warning(msg)
                        if self.on_status:
                            try:
                                self.on_status(msg)
                            except Exception:  # noqa: BLE001
                                pass
                    self._stop_evt.wait(5)

            threading.Thread(target=_no_data_watchdog, daemon=True,
                             name="NoDataWatchdog").start()

            try:
                while not self._stop_evt.is_set():
                    data = stream.read(1024, exception_on_overflow=False)
                    last_data_t = time.monotonic()
                    if self._pause_evt.is_set():
                        continue  # 暂停：丢弃

                    pcm16 = self._process(np.frombuffer(data, dtype=np.int16),
                                          src_channels, src_rate)
                    n = len(pcm16)
                    if n == 0:
                        continue

                    # 音量电平（RMS）
                    self._level = float(np.sqrt(np.mean(
                        (pcm16.astype(np.float32) / 32768.0) ** 2)))
                    if self.on_level:
                        try:
                            self.on_level(self._level)
                        except Exception:  # noqa: BLE001
                            pass

                    wf.writeframes(pcm16.tobytes())
                    if self.on_chunk:
                        self.on_chunk(self._elapsed, pcm16.tobytes())
                    self._elapsed += n / TARGET_RATE
                    last_data_t = time.monotonic()
            finally:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
                self._stream = None
                wf.close()
        except Exception as e:  # noqa: BLE001
            if self._stop_evt.is_set():
                # 主动停止时 abort 音频流可能抛 -9999 Unanticipated host error，属正常关停
                logger.info(f"录音流已停止（{e}）")
            else:
                self._error = f"录音失败: {e}"
                logger.exception("环回录音异常")
        finally:
            p.terminate()
            logger.info("录音线程已退出")

    # ---------- 重采样 ----------
    @staticmethod
    def _process(samples: np.ndarray, channels: int, src_rate: int) -> np.ndarray:
        """多声道 → 单声道 → 线性插值重采样到 16kHz，输出 int16"""
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        if src_rate == TARGET_RATE:
            return samples.astype(np.int16)
        n_src = len(samples)
        if n_src == 0:
            return samples.astype(np.int16)
        n_dst = max(1, int(round(n_src * TARGET_RATE / src_rate)))
        x_src = np.arange(n_src, dtype=np.float64)
        x_dst = np.linspace(0.0, n_src - 1, n_dst)
        resampled = np.interp(x_dst, x_src, samples.astype(np.float64))
        return np.clip(resampled, -32768, 32767).astype(np.int16)
