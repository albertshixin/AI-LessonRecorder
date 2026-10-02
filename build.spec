# -*- coding: utf-8 -*-
"""PyInstaller 打包配置：onedir 模式，输出到 dist/在线课程录播器/"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

PROJECT = Path(SPECPATH).resolve()

import importlib.util

datas = []


def _pkg_dir(name: str):
    spec = importlib.util.find_spec(name)
    return Path(spec.submodule_search_locations[0]) if spec and spec.submodule_search_locations else None


# PyAudioWPatch 的 DLL/资源
_pw = _pkg_dir("pyaudiowpatch")
if _pw:
    datas.append((str(_pw), "pyaudiowpatch"))
# faster-whisper 资产（silero VAD 的 onnx 模型等，缺失会导致转写全部失败）
_fw = _pkg_dir("faster_whisper")
if _fw:
    _assets = _fw / "assets"
    if _assets.exists():
        datas.append((str(_assets), "faster_whisper/assets"))
# loguru 资源
datas += collect_data_files("loguru")
# zhconv 简繁转换字典（zhcdict.json 缺失会导致转换静默失败，输出仍为繁体）
datas += collect_data_files("zhconv")

a = Analysis(
    [str(PROJECT / "main.py")],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "PySide6.QtSvg",              # 图标渲染
        "pyaudiowpatch",
        "ctranslate2",                # faster-whisper 后端
        "tokenizers",
        "onnxruntime",                # whisper VAD
        "zhconv",                     # 繁体转简体
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pandas", "scipy", "IPython", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="在线课程录播器",
    debug=False,
    strip=False,
    upx=False,
    console=False,          # GUI 程序，不弹黑窗
    icon=str(PROJECT / "assets" / "app.ico") if (PROJECT / "assets" / "app.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="在线课程录播器",
)
