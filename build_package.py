# -*- coding: utf-8 -*-
"""一键打包 + 发布脚本（v1.2）

用法（项目根目录执行）：
    python build_package.py              # 仅打包到 dist/
    python build_package.py --release    # 打包并生成 release/*.zip + SHA256

产物：
    dist/在线课程录播器/          整个文件夹拷到任意位置即可运行（免 Python）
    release/AI-LessonRecorder-v1.2.0-win-x64.zip

设计要点：
- **onedir 而非 onefile**：免去每次启动解压 1~2GB 到临时目录，
  启动快、内存占用低，也避免杀毒软件对大体积自解压程序的误报。
- **打包后自检**：构建完成即运行 `--selftest`，验证 EXE 能真正启动并
  完成一次离线全流程（录音除外），不满足要求则构建失败。
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
APP_NAME = "在线课程录播器"
DIST = ROOT / "dist" / APP_NAME
RELEASE = ROOT / "release"
VERSION = "1.2.0"
BUILDER = ROOT / "build.spec"

README = f"""在线课程录播器（AI LessonRecorder）v{VERSION}
================================================

直接双击「{APP_NAME}.exe」运行，无需安装 Python。

目录说明
--------
  {APP_NAME}.exe    主程序
  config\\settings.json    配置文件（在程序【设置】界面修改后自动保存）
  logs\\             运行日志（排查问题时查看此目录）
  output\\           每次录制的产物：逐字稿 MD / Word / 截图 / audio.wav
  _internal\\        程序依赖库（请勿删除或改名）

首次使用
--------
  1. 【设置 → AI 梳理(LLM)】填写服务商与 API Key
     —— API Key 使用 Windows DPAPI 加密存储，磁盘上看不到明文，
        密钥由你的 Windows 账户保管，其他账户/其他机器无法解密。
  2. 首次录制会自动下载语音识别模型 small（约 460MB，存到用户缓存目录，
     之后完全离线可用）。若直连 huggingface 缓慢，可在
     【设置 → 语音识别】把 HF 镜像设为 https://hf-mirror.com
  3. 播放课程声音 → 点【开始录制】

全局快捷键（录制时主窗口常失焦，必须用系统级热键）
--------------------------------------------------
  F9    手动截图（翻页漏检时的补救）
  F10   暂停 / 继续
  F11   停止录制并生成逐字稿
  若被其他程序占用会自动降级为仅界面按钮可用，启动日志与窗口底部有提示。

使用建议：双档转写
------------------
  录制时用小模型（small）保证字幕实时跟得上；
  课后用【导出 → 离线重转写（提高准确率）】以 large-v3 重转写 audio.wav，
  得到最终交付用的准确逐字稿。原 events.jsonl 会自动备份为 .bak。

常见问题
--------
  Q: 没有声音 / 字幕一直不出字？
  A: 确认课程声音正在从本机扬声器播放（本工具录的是系统输出，不是麦克风）；
     观察界面「声音电平」条是否有起伏。

  Q: 翻页截图没反应？
  A: 用 F9 手动截图补救；或到【设置 → 视觉】把「内容区ROI」框住PPT 正文
     区域，排除播放器控制栏的干扰。

  Q: 提示模型加载失败？
  A: 走【设置 → 语音识别 → 模型下载】重试，并检查网络/镜像设置。
"""


def sh(msg: str) -> None:
    print(f"\n=== {msg} ===", flush=True)


def clean() -> None:
    for d in ("build", "dist"):
        p = ROOT / d
        if p.exists():
            print(f"  清理 {d}/ …")
            shutil.rmtree(p, ignore_errors=True)
    # 旧的 spec 缓存会让 PyInstaller 用过期的 Analysis 结果
    for f in ROOT.glob("**/__pycache__"):
        shutil.rmtree(f, ignore_errors=True)


def build() -> None:
    t0 = time.monotonic()
    r = subprocess.run([PY, "-m", "PyInstaller", "--noconfirm", "--clean",
                        str(BUILDER)], cwd=str(ROOT))
    if r.returncode != 0:
        sys.exit("❌ 打包失败，请查看上方 PyInstaller 日志")
    if not (DIST / f"{APP_NAME}.exe").exists():
        sys.exit(f"❌ 未生成 EXE：{DIST}")
    print(f"  耗时 {time.monotonic() - t0:.0f}s")


def copy_runtime_files() -> None:
    cfg_dst = DIST / "config"
    if cfg_dst.exists():
        shutil.rmtree(cfg_dst)
    src_cfg = ROOT / "config"
    if src_cfg.exists():
        shutil.copytree(src_cfg, cfg_dst)
    # 示例配置改名，避免用户误改示例文件
    ex = cfg_dst / "settings.example.json"
    if ex.exists():
        ex.rename(cfg_dst / "settings.json.example")
    for d in ("logs", "output"):
        (DIST / d).mkdir(exist_ok=True)
    (DIST / "使用说明.txt").write_text(README, encoding="utf-8")


def dir_size_mb(p: Path) -> float:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1024 / 1024


def selftest() -> None:
    """打包后自检：EXE 必须能真正启动并跑通离线流程"""
    exe = DIST / f"{APP_NAME}.exe"
    sh("打包后自检（EXE 启动 + 离线流程）")
    env_probe = subprocess.run(
        [PY, str(ROOT / "tests" / "test_packaged_smoke.py"),
         "--exe", str(exe)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=900)
    print(env_probe.stdout[-4000:])
    if env_probe.returncode != 0:
        print(env_probe.stderr[-2000:])
        sys.exit("❌ 打包自检失败，产物不可交付")


def make_release() -> Path:
    sh("生成发布包")
    RELEASE.mkdir(exist_ok=True)
    zip_path = RELEASE / f"AI-LessonRecorder-v{VERSION}-win-x64.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zf:
        for f in sorted(DIST.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(DIST.parent))
    size = zip_path.stat().st_size / 1024 / 1024
    h = hashlib.sha256()
    with open(zip_path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    digest = h.hexdigest()
    (RELEASE / f"AI-LessonRecorder-v{VERSION}-win-x64.zip.sha256").write_text(
        f"{digest}  {zip_path.name}\n", encoding="utf-8")
    print(f"  {zip_path.name}  {size:.0f} MB")
    print(f"  SHA256 {digest}")
    return zip_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", action="store_true", help="额外生成 zip 发布包")
    ap.add_argument("--skip-selftest", action="store_true",
                    help="跳过打包自检（不推荐）")
    args = ap.parse_args()

    print("=" * 66)
    print(f"AI LessonRecorder v{VERSION} 构建")
    print(f"  Python {sys.version.split()[0]}")
    print(f"  项目   {ROOT}")
    print("=" * 66)

    sh("1/4 清理旧构建")
    clean()

    sh("2/4 PyInstaller 打包（首次约 5-10 分钟）")
    build()

    sh("3/4 复制运行时文件")
    copy_runtime_files()
    print(f"  产物目录 {DIST}")
    print(f"  体积     {dir_size_mb(DIST):.0f} MB")
    print(f"  文件数   {sum(1 for _ in DIST.rglob('*') if _.is_file())}")

    if not args.skip_selftest:
        sh("4/4 打包自检")
        selftest()
    else:
        print("\n=== 已跳过自检（--skip-selftest）===")

    if args.release:
        make_release()

    print("\n" + "=" * 66)
    print("✅ 构建完成")
    print(f"   可直接运行：{DIST / (APP_NAME + '.exe')}")
    if args.release:
        print(f"   发布包：    {RELEASE}")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())