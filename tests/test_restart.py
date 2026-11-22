# -*- coding: utf-8 -*-
"""停止后重新录制的回归测试（真实录音线程 + 假 ASR 引擎）

背景：用户反馈「录制中停止，再点开始，软件卡死不动」。
本测试用**真实** LoopbackRecorder / LiveTranscriber / SlideDetector 复现
start → stop → start 全流程，并用看门狗在超时时打印所有线程栈，定位卡死点。
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from src.app import session_manager as sm_mod
from src.app.session_manager import SessionManager
from src.core.session_store import (_next_free_name, create_session,
                                    session_name_of)
from src.utils.config import AppConfig

FAILED: list[str] = []


def check(ok: bool, msg: str) -> None:
    print(f"  [{'OK  ' if ok else 'FAIL'}] {msg}")
    if not ok:
        FAILED.append(msg)


class DummyEngine:
    """不加载真实模型，立即返回固定文本"""

    def transcribe_segment(self, audio, language="zh"):
        return ("测试转写文本", 0.9)


def dump_threads(tag: str) -> None:
    print(f"  ---- 线程栈快照（{tag}）----")
    for tid, frame in sys._current_frames().items():
        name = next((t.name for t in threading.enumerate()
                     if t.ident == tid), f"tid={tid}")
        print(f"  ### 线程 {name} (id={tid})")
        for line in traceback.format_stack(frame)[-6:]:
            print("      " + line.strip())
    print("  ---- 快照结束 ----")


def wait_until(pred, timeout: float, tag: str) -> bool:
    """带看门狗的等待：超时则打印线程栈"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    dump_threads(tag)
    return False


def main() -> int:
    print("=" * 70)
    print("停止后重新录制（start → stop → start）回归测试")
    print("=" * 70)

    app = QApplication.instance() or QApplication([])
    sm_mod.create_asr_engine = lambda *a, **k: DummyEngine()   # 免模型加载

    with tempfile.TemporaryDirectory(prefix="lr_restart_") as td:
        tmp = Path(td)
        cfg = AppConfig(tmp / "settings.json")
        cfg.set("output.dir", str(tmp / "output"))
        mgr = SessionManager(cfg)

        # ── 第一次录制 ──
        print("\n[1] 第一次 start")
        ok = mgr.start("测试课程")
        check(ok, "第一次 start 成功")
        if not ok:
            return 1
        check(mgr.state == "recording", f"状态 recording（实际 {mgr.state}）")
        time.sleep(1.5)
        check(mgr.elapsed > 0.3, f"计时推进（elapsed={mgr.elapsed:.2f}s）")

        print("\n[2] 第一次 stop")
        mgr.stop()
        check(wait_until(lambda: mgr.state == "idle", 40, "stop→idle 超时"),
              "stop 后回到 idle（40s 内）")
        check(mgr._stopping is False, "_stopping 已复位")

        # ── 第二次录制（用户报卡死的地方）──
        print("\n[3] 第二次 start（复现点）")
        t0 = time.monotonic()
        ok2 = mgr.start("测试课程")
        dt = time.monotonic() - t0
        check(ok2, f"第二次 start 成功（耗时 {dt:.2f}s）")
        if not ok2:
            dump_threads("第二次 start 失败")
            return 1
        check(mgr.state == "recording", f"第二次状态 recording（实际 {mgr.state}）")
        ok_tick = wait_until(lambda: mgr.elapsed > 0.3, 15, "第二次计时未推进")
        check(ok_tick, f"第二次录制计时推进（elapsed={mgr.elapsed:.2f}s）")

        print("\n[4] 第二次 stop")
        mgr.stop()
        check(wait_until(lambda: mgr.state == "idle", 40, "第二次 stop→idle 超时"),
              "第二次 stop 后回到 idle（40s 内）")

        # ── 目录：同名再录必须加序号 -1，且各自独立 ──
        sessions = sorted((tmp / "output").iterdir()) if (tmp / "output").exists() else []
        check(len(sessions) >= 2, f"产出 >=2 个会话目录（实际 {len(sessions)}）")
        names = [session_name_of(d) for d in sessions]
        for d in sessions:
            print(f"      · {d.name}")
        check(names[:2] == ["测试课程", "测试课程-1"],
              f"同名再录自动顺延序号（实际 {names[:2]}）")

        # ── 暂停/继续：必须复用同一会话，不新建目录 ──
        print("\n[5] 暂停 → 继续：复用同一会话")
        mgr.start("暂停测试")
        time.sleep(0.8)
        root_before = mgr.paths.root
        mgr.pause()
        check(mgr.state == "paused", f"暂停状态（实际 {mgr.state}）")
        time.sleep(0.5)
        mgr.resume()
        check(mgr.state == "recording", f"继续状态（实际 {mgr.state}）")
        check(mgr.paths.root == root_before, "继续后仍是同一会话目录（未新建）")
        mgr.stop()
        wait_until(lambda: mgr.state == "idle", 40, "暂停用例 stop 超时")

        # ── 命名规则（纯逻辑，不依赖录音）──
        print("\n[6] 会话命名顺延规则")
        check(_next_free_name(set(), "人工智能") == "人工智能", "首次录制不加序号")
        check(_next_free_name({"人工智能"}, "人工智能") == "人工智能-1",
              "第二次录制 → 名称-1")
        check(_next_free_name({"人工智能", "人工智能-1"}, "人工智能") == "人工智能-2",
              "第三次录制 → 名称-2")
        check(_next_free_name({"人工智能-1", "人工智能-3"}, "人工智能") == "人工智能",
              "原名未被占用时仍用原名（序号只补空缺）")
        out2 = tmp / "out2"
        n1 = create_session(out2, "数据结构")
        n2 = create_session(out2, "数据结构")
        n3 = create_session(out2, "数据结构")
        check([n1.name, n2.name, n3.name] == ["数据结构", "数据结构-1", "数据结构-2"],
              f"连续三次同名录制依次为 原名/-1/-2（实际 "
              f"{[n1.name, n2.name, n3.name]}）")
        other = create_session(out2, "编译原理")
        check(other.name == "编译原理", "不同课程名不受影响")

        # ── 根因回归：ASR 引擎不得每次重新加载 ──
        print("\n[7] ASR 引擎跨会话复用（卡死根因）")
        try:
            from src.ai.asr.asr_engine import (clear_engine_cache,
                                               create_asr_engine)
            from src.ai.asr.model_manager import ModelManager
            from src.ai.asr.whisper_local import cuda_state
            acfg = cfg.as_dict()["asr"]
            mname = acfg.get("model", "small")
            if not ModelManager.is_downloaded(mname):
                print("      [跳过] 本地没有已下载模型")
            else:
                mp = ModelManager.local_path(mname)
                clear_engine_cache()
                t0 = time.monotonic()
                e1 = create_asr_engine(acfg, model_path=mp)
                d1 = time.monotonic() - t0
                t0 = time.monotonic()
                e2 = create_asr_engine(acfg, model_path=mp)
                d2 = time.monotonic() - t0
                check(e1 is e2, "相同配置复用同一引擎实例（不再重复加载模型）")
                check(d2 < 1.0, f"第二次获取引擎近乎瞬时（{d1:.2f}s → {d2:.2f}s）")
                clear_engine_cache()
                e3 = create_asr_engine(acfg, model_path=mp)
                check(e3 is not e1, "清缓存后重建新实例（换模型档位生效）")

                # CUDA 结论记忆：失败过一次就不再重试（重试会死锁在 encode）
                checked, usable, reason = cuda_state()
                if checked and not usable:
                    check(True, f"CUDA 不可用已记忆，后续一律走 CPU（{reason[:36]}…）")
                    box: dict = {}

                    def work() -> None:
                        box["e"] = create_asr_engine(acfg, model_path=mp,
                                                     use_cache=False)
                    th = threading.Thread(target=work, daemon=True)
                    th.start()
                    th.join(40)
                    check(not th.is_alive(),
                          "即便不复用缓存，再次加载也不会卡死（原缺陷已修复）")
        except Exception as e:  # noqa: BLE001
            check(False, f"引擎复用检查异常：{e}")

    print()
    if FAILED:
        print(f"[FAIL] {len(FAILED)} 项失败")
        for m in FAILED:
            print(f"   - {m}")
        return 1
    print("[PASS] 停止后重新录制流程正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
