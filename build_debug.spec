# -*- coding: utf-8 -*-
"""调试版打包配置：带控制台，用于捕获启动异常堆栈"""
import sys
from pathlib import Path

import importlib.util
from PyInstaller.utils.hooks import collect_data_files

PROJECT = Path(SPECPATH).resolve()

datas = []


def _pkg_dir(name: str):
    spec = importlib.util.find_spec(name)
    return Path(spec.submodule_search_locations[0]) if spec and spec.submodule_search_locations else None


_pw = _pkg_dir("pyaudiowpatch")
if _pw:
    datas.append((str(_pw), "pyaudiowpatch"))
datas += collect_data_files("loguru")

a = Analysis(
    [str(PROJECT / "main.py")],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=datas,
    hiddenimports=["PySide6.QtSvg", "pyaudiowpatch", "ctranslate2", "tokenizers", "onnxruntime"],
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
    name="lessonrec-debug",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="lessonrec-debug",
)
