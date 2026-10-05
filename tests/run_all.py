# -*- coding: utf-8 -*-
"""全量测试入口：一次跑完所有测试套件，输出汇总

用法：
    python tests/run_all.py            # 跑全部
    python tests/run_all.py --fast     # 跳过依赖大模型的端到端测试

约定：
- 不依赖 pytest（环境未安装），各测试文件自带 __main__ 入口；
- 退出码 0 = 全部通过；1 = 有失败。SKIP 不计入失败。
"""
from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

# (显示名, 脚本, 是否属于 slow 组)
SUITES = [
    ("冒烟（事件流/MD/Word/教程导出）", "test_smoke.py", False),
    ("状态机", "test_state_machine.py", False),
    ("视觉判据（dHash/pHash/SSIM/diff_ratio）", "test_vision_algo.py", False),
    ("翻页检测线程（线程安全/状态基准/熔断）", "test_slide_detector.py", False),
    ("实时分段 VAD + 离线静音切分", "test_segmentation.py", False),
    ("UI 冒烟（offscreen 构建与信号链路）", "test_ui_smoke.py", False),
    ("模型下载流程与 UI 构建", "test_download.py", False),
    ("端到端 ASR 管线（需本地 small 模型）", "test_asr_pipeline.py", True),
]

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")


def run_one(script: str, timeout: int) -> tuple[int, str, float]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["QT_QPA_PLATFORM"] = "offscreen"
    t0 = time.monotonic()
    buf = io.StringIO()
    try:
        p = subprocess.run([PY, str(ROOT / "tests" / script)],
                           cwd=str(ROOT), env=env, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = p.stdout.decode("utf-8", "replace")
        buf.write(out)
        code = p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace")
        buf.write(out)
        buf.write(f"\n[超时] 超过 {timeout}s 未结束")
        code = -9
    except Exception as e:  # noqa: BLE001
        buf.write(f"\n[异常] {type(e).__name__}: {e}")
        code = -1
    return code, buf.getvalue(), time.monotonic() - t0


def summarize(out: str, code: int) -> str:
    """从输出里提取一行结论"""
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.startswith("RESULT:"):
            return ln.split(":", 1)[1].strip()
    for key in ("全部通过", "全部验证通过", "全部冒烟测试通过"):
        if any(key in ln for ln in lines):
            return "PASS"
    # 无总结行时以退出码为准（各测试文件不一定都打印结论）
    return "PASS" if code == 0 else "FAIL"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="跳过依赖大模型的慢测试")
    ap.add_argument("--timeout", type=int, default=600, help="单套件超时秒数")
    ap.add_argument("--only", default="", help="只跑名称包含该关键字的套件")
    args = ap.parse_args()

    print("=" * 74)
    print("AI LessonRecorder 全量测试")
    print(f"Python {sys.version.split()[0]}  |  工作目录 {ROOT}")
    print("=" * 74)

    results = []
    for name, script, slow in SUITES:
        if args.fast and slow:
            print(f"{DIM}跳过（--fast）{RESET} {name}")
            results.append((name, "SKIP", 0.0))
            continue
        if args.only and args.only not in name and args.only not in script:
            continue
        print(f"\n▶ {name}")
        sys.stdout.flush()
        code, out, secs = run_one(script, args.timeout)
        verdict = summarize(out, code)
        if verdict == "SKIP" or "RESULT: SKIP" in out:
            tag, color = "SKIP", YELLOW
        elif code == 0 and verdict == "PASS":
            tag, color = "PASS", GREEN
        else:
            tag, color = "FAIL", RED
        results.append((name, tag, secs))
        # 失败时打印尾部日志便于定位；通过时只打印结论行
        tail = [ln for ln in out.splitlines() if ln.strip()][-14:]
        marker = "✓" if tag == "PASS" else ("-" if tag == "SKIP" else "✗")
        for ln in tail:
            if tag != "PASS" or ln.startswith(("RESULT", "全部")):
                print(f"  {DIM}{ln}{RESET}")
        print(f"  {color}{marker} {tag}{RESET}  ({secs:.1f}s)")

    print("\n" + "=" * 74)
    print("汇总")
    print("=" * 74)
    width = max(len(n) for n, _t, _s in results) if results else 10
    n_pass = n_skip = n_fail = 0
    for name, tag, secs in results:
        color = {"PASS": GREEN, "SKIP": YELLOW, "FAIL": RED}[tag]
        mark = {"PASS": "✓", "SKIP": "-", "FAIL": "✗"}[tag]
        print(f"  {color}{mark}{RESET} {name.ljust(width)}  {secs:6.1f}s")
        n_pass += tag == "PASS"
        n_skip += tag == "SKIP"
        n_fail += tag == "FAIL"
    total = n_pass + n_skip + n_fail
    print("-" * 74)
    color = GREEN if n_fail == 0 else RED
    print(f"  {color}合计 {total} 套：通过 {n_pass}，跳过 {n_skip}，失败 {n_fail}{RESET}")
    print("=" * 74)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())