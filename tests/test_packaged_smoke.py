# -*- coding: utf-8 -*-
"""打包产物自检探针：调用 dist 里的 EXE 跑 `--selftest` 并断言结果

由 build_package.py 自动调用，也可单独运行：
    python tests/test_packaged_smoke.py --exe "dist/在线课程录播器/在线课程录播器.exe"
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 自检必须覆盖到的关键能力（缺任一项即视为产物不合格）
REQUIRED_CHECKS = (
    "AppContext 初始化",
    "DPAPI 加密往返",
    "磁盘无明文",
    "会话目录创建",
    "逐字稿 MD 生成",
    "事件流恢复",
    "Word 导出",
    "元数据含运行时指标",
    "pHash 换页可辨",
    "SSIM 换页可辨",
    "配置落盘无明文密钥",
    "配置重载还原明文",
    "主窗口构建",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True, help="打包产物的 EXE 路径")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    exe = Path(args.exe).resolve()
    if not exe.exists():
        print(f"[FAIL] EXE 不存在: {exe}")
        return 1
    print(f"[1] 目标 EXE: {exe}")
    print(f"    体积 {exe.stat().st_size / 1024 / 1024:.1f} MB")

    # 打包产物不应依赖源码目录：把 cwd 设为临时目录，确保没有 fallback
    with tempfile.TemporaryDirectory(prefix="lr_pkgprobe_") as workdir:
        out_json = Path(workdir) / "selftest.json"
        print(f"[2] 以独立工作目录运行自检（验证不依赖源码树）…")
        try:
            p = subprocess.run(
                [str(exe), "--selftest-to", str(out_json)],
                cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                timeout=args.timeout)
        except subprocess.TimeoutExpired:
            print(f"[FAIL] EXE 自检超时（>{args.timeout}s）")
            return 1

        raw = out_json.read_text(encoding="utf-8") if out_json.exists() else ""
        if not raw.strip():
            # 回退：从 stdout 里找 JSON（若 PyInstaller 保留了 stdout）
            raw = p.stdout or ""
            start = raw.find("{")
            if start >= 0:
                raw = raw[start:]
        if not raw.strip():
            print("[FAIL] EXE 未产出自检报告，可能启动即崩溃")
            print("--- stdout ---")
            print(p.stdout[-3000:])
            print("--- stderr ---")
            print(p.stderr[-3000:])
            return 1

        try:
            report = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"[FAIL] 自检报告不是合法 JSON: {e}")
            print(raw[:2000])
            return 1

    checks = report.get("checks", [])
    done = {c["name"]: c for c in checks}
    print(f"[3] 自检报告：{len(checks)} 项，ok={report.get('ok')}")

    missing = [n for n in REQUIRED_CHECKS if n not in done]
    failed = [c["name"] for c in checks if not c.get("ok")]

    for c in checks:
        mark = "OK  " if c.get("ok") else "FAIL"
        print(f"    [{mark}] {c['name']}"
              + (f"  — {c['detail']}" if c.get("detail") else ""))

    if report.get("error"):
        print(f"\n[异常] {report['error']}")
        print(report.get("traceback", "")[-2000:])

    ok = True
    if missing:
        print(f"\n[FAIL] 产物缺少关键能力: {missing}")
        ok = False
    if failed:
        print(f"\n[FAIL] 自检未通过项: {failed}")
        ok = False
    if not report.get("ok"):
        ok = False

    print("\n" + ("[PASS] 打包产物自检通过" if ok else "[FAIL] 打包产物不合格"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())