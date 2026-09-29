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
# loguru 资源
datas += collect_data_files("loguru")

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
