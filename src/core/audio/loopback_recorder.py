# -*- coding: utf-8 -*-
"""系统声音环回录音：WASAPI Loopback 采集，输出 16kHz 单声道 int16 流

- 不需要麦克风，捕获系统正在播放的声音（在线课程声音）
- 暂停时丢弃音频（不写入、不推进时间轴）
- 音频同时写入 WAV 存档
- **设备断开自愈**：蓝牙耳机断连 / 播放设备被移除时不再让软件崩溃，
  而是等待设备恢复后重新打开并接着录（写入同一个 WAV，时间轴连续）；
  超时未恢复才结束录音，断开前的内容完整保留。

崩溃根因备忘（勿回退）：
  设备被移除后 PortAudio 内部的 COM/流句柄已失效，此时再调用
  stop_stream() / close() / Pa_Terminate() 会让**整个进程**直接崩掉，
  且无法用 try/except 拦住（是内存访问违规，不是 Python 异常）。
  因此：一旦判定设备失效，立即放弃该流与 PyAudio 实例，绝不再碰。
"""
import threading
import time
import wave

import numpy as np

from src.utils.logger import logger

TARGET_RATE = 16000
READ_POLL_SEC = 0.02          # 无数据时的轮询间隔：read() 不再长时间阻塞
NO_DATA_WARN_SEC = 20.0       # 超过这么久没声音就提醒（可能选错播放设备）
RECONNECT_RETRY_SEC = 2.0     # 设备断开后的重试间隔
RECONNECT_WAIT_SEC = 90.0     # 设备断开后最多等待多久才放弃


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
        self._stream = None          # 仅作引用；失效后立即置空
        self._last_data_t = time.monotonic()
        self._outage_t0: float | None = None   # 设备中断起始时刻（用于自动重连）

    # ---------- 外部控制 ----------
    def stop(self) -> None:
        # 只置停止标志：读取循环是**轮询式**的，最多 READ_POLL_SEC 就会自己退出。
        # 这里绝不能跨线程调用 stop_stream()/Terminate —— 设备被移除（蓝牙断连）
        # 后这些 PortAudio 调用会访问已失效的 COM 对象，历史上正是它让进程崩溃。
        self._stop_evt.set()

    def _notify_status(self, msg: str) -> None:
        if not self.on_status:
            return
        try:
            self.on_status(msg)
        except Exception:  # noqa: BLE001
            pass

    def _notify_level(self, lvl: float) -> None:
        if not self.on_level:
            return
        try:
            self.on_level(lvl)
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

        wf = None
        try:
            # WAV 全文一个文件：设备断开重连后接着写，不另起文件
            wf = wave.open(self.wav_path, "wb")
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(TARGET_RATE)

            threading.Thread(target=self._no_data_watchdog, daemon=True,
                             name="NoDataWatchdog").start()

            # 一次 _record_once = 一条设备生命周期；设备断开后自动重开继续录
            while not self._stop_evt.is_set():
                if not self._record_once(pyaudio, wf):
                    break
        except Exception as e:  # noqa: BLE001
            if self._stop_evt.is_set():
                logger.info(f"录音流已停止（{e}）")
            else:
                self._error = f"录音失败: {e}"
                logger.exception("环回录音异常")
        finally:
            if wf is not None:
                try:
                    wf.close()
                except Exception:  # noqa: BLE001
                    pass
            logger.info("录音线程已退出")

    def _record_once(self, pyaudio, wf) -> bool:
        """打开环回设备并录制，直到停止或出错。

        返回 True：设备异常消失，等待后请重新打开重试（自动重连）
        返回 False：本次设备生命周期结束（正常停止，或放弃重连）
        """
        p = None
        stream = None
        broken = False      # 设备是否失效（失效后绝不再触碰 PortAudio 对象）
        opened = False
        try:
            p = pyaudio.PyAudio()
            dev = self._find_loopback_device(p)
            if dev is None:
                self._error = ("未找到系统声音环回设备。请确认 Windows 输出设备正常，"
                               "且至少播放过一次声音。")
                logger.error(self._error)
                return False

            src_rate = int(dev["defaultSampleRate"])
            src_channels = min(int(dev["maxInputChannels"]) or 2, 2)
            logger.info(f"环回录音设备: {dev['name']}  {src_rate}Hz/{src_channels}ch")

            stream = p.open(
                format=pyaudio.paInt16,
                channels=src_channels,
                rate=src_rate,
                input=True,
                input_device_index=int(dev["index"]),
                frames_per_buffer=1024,
            )
            opened = True
            self._stream = stream
            self._last_data_t = time.monotonic()

            if self._outage_t0 is not None:
                # 能打开不等于已恢复：必须**真的读到数据**才算恢复，
                # 否则"打开成功但立刻又断"会造成假恢复、无限重试。
                logger.info("已重新打开音频设备，等待数据确认恢复 …")
            else:
                # 提示正在从哪个设备录音（方便排查"没声音"问题）
                msg = f"正在录制系统声音：{dev['name']}（{src_rate}Hz）"
                logger.info(msg)
                self._notify_status(msg)

            while not self._stop_evt.is_set():
                # 轮询读取：只取已就绪的帧，绝不长时间阻塞在 read() 里。
                # 好处①：停止指令最多 20ms 内被响应，无需跨线程 stop_stream()；
                # 好处②：设备消失时由 PortAudio 立刻报错，不会卡死在内核调用中。
                avail = stream.get_read_available()
                if not avail:
                    self._stop_evt.wait(READ_POLL_SEC)
                    continue
                data = stream.read(min(int(avail), 1024),
                                   exception_on_overflow=False)
                self._last_data_t = time.monotonic()
                if self._outage_t0 is not None:
                    # 真正读到数据 → 确认设备已恢复
                    gap = time.monotonic() - self._outage_t0
                    logger.info(f"音频设备已恢复，继续录制（中断 {gap:.0f}s）")
                    self._notify_status(
                        f"音频设备已恢复，继续录制（中断约 {int(gap)} 秒）")
                    self._outage_t0 = None
                    self._error = None
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
                self._notify_level(self._level)

                wf.writeframes(pcm16.tobytes())
                if self.on_chunk:
                    self.on_chunk(self._elapsed, pcm16.tobytes())
                self._elapsed += n / TARGET_RATE
            return False
        except Exception as e:  # noqa: BLE001
            if self._stop_evt.is_set():
                # 主动停止时音频流可能抛 -9999 Unanticipated host error，属正常关停
                logger.info(f"录音流已停止（{e}）")
                return False
            broken = True
            if opened:
                logger.exception("环回录音异常（音频设备可能已断开）")
            else:
                logger.warning(f"打开录音设备失败: {e}")
            return self._wait_device_back(e, opened)
        finally:
            self._stream = None
            if broken:
                # 设备已失效：跳过一切 PortAudio 收尾（stop_stream/close/terminate）。
                # 宁可泄漏这一个实例，也不能让进程崩掉。
                logger.warning("音频设备已失效：跳过 PortAudio 收尾以避免崩溃")
            else:
                if stream is not None:
                    try:
                        stream.stop_stream()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        stream.close()
                    except Exception:  # noqa: BLE001
                        pass
                if p is not None:
                    try:
                        p.terminate()
                    except Exception as e2:  # noqa: BLE001
                        logger.warning(f"PyAudio 释放失败（已忽略）: {e2}")

    def _wait_device_back(self, err, opened: bool) -> bool:
        """音频设备消失（蓝牙断连/设备被移除）后等待恢复。

        恢复 → 返回 True（调用方重新打开设备，接着写同一个 WAV）
        超时 → 返回 False（结束录音，断开前的内容已保留在 WAV 中）
        """
        if self._outage_t0 is None:
            self._outage_t0 = time.monotonic()
            logger.warning(f"音频设备断开（{err}），等待恢复 …")
            self._level = 0.0
            self._notify_level(0.0)
            self._notify_status("音频设备已断开（如蓝牙耳机断连），正在等待恢复…"
                                "（已录内容不会丢失）")
        waited = time.monotonic() - self._outage_t0
        if waited >= RECONNECT_WAIT_SEC:
            self._error = (f"录音设备已断开且 {RECONNECT_WAIT_SEC:g} 秒内未恢复"
                           f"（{err}）。已保留断开前录到的内容。")
            logger.error(self._error)
            return False
        if not opened:
            logger.warning(f"暂无法打开录音设备（{err}），"
                           f"{RECONNECT_RETRY_SEC:g}s 后重试 …")
        # 等一小会儿再试；期间若用户点停止则立即放弃
        if self._stop_evt.wait(RECONNECT_RETRY_SEC):
            return False
        return True

    def _no_data_watchdog(self) -> None:
        """长时间收不到任何音频数据时提醒（如选错播放设备）"""
        warned = False
        while not self._stop_evt.is_set():
            quiet = time.monotonic() - self._last_data_t
            if quiet > NO_DATA_WARN_SEC and not warned:
                warned = True
                msg = (f"已 {int(quiet)} 秒未收到系统声音。若电平条一直为 0，"
                       "请确认课程声音是从默认输出设备播放的")
                logger.warning(msg)
                self._notify_status(msg)
            elif quiet <= NO_DATA_WARN_SEC and warned:
                warned = False
            self._stop_evt.wait(5)

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
