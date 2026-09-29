# 一键打包脚本（在项目根目录执行）：
#   & "C:\Users\Albert\.workbuddy\binaries\python\versions\3.11.9\python.exe" build_package.py
#
# 产物：dist\在线课程录播器\  整个文件夹拷到任意位置即可运行
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
DIST = ROOT / "dist" / "在线课程录播器"

print("=== 1/3 清理旧构建 ===")
for d in ("build", "dist"):
    p = ROOT / d
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)

print("=== 2/3 PyInstaller 打包（约 5-10 分钟）===")
r = subprocess.run([PY, "-m", "PyInstaller", "--noconfirm",
                    str(ROOT / "build.spec")], cwd=str(ROOT))
if r.returncode != 0:
    sys.exit("打包失败，请查看上方日志")

print("=== 3/3 复制运行时配置 ===")
# config 放到 EXE 旁边（用户可编辑；缺失时程序也会自动生成默认配置）
cfg_dst = DIST / "config"
if cfg_dst.exists():
    shutil.rmtree(cfg_dst)
shutil.copytree(ROOT / "config", cfg_dst)
# 说明文件
readme = DIST / "使用说明.txt"
readme.write_text(
    "在线课程录播器（AI LessonRecorder）\n"
    "====================================\n"
    "直接双击「在线课程录播器.exe」运行。\n\n"
    "目录说明：\n"
    "  config\\settings.json  配置文件（设置界面修改后保存在这里）\n"
    "  logs\\                 运行日志\n"
    "  output\\               每次录制的逐字稿/截图/Word/音频\n"
    "  _internal\\            程序依赖库（请勿删除）\n\n"
    "首次使用：\n"
    "  1. 设置 → AI 梳理(LLM) → 服务商选「智谱 GLM」→ 填 API Key\n"
    "  2. 首次录制会自动下载语音识别模型（约 460MB 到用户缓存目录）\n"
    "  3. 播放课程声音后点「开始录制」\n",
    encoding="utf-8")

print(f"\n打包完成！→ {DIST}")
print("整个文件夹拷贝到 C 盘或 D 盘任意位置即可独立运行（无需安装 Python）。")
