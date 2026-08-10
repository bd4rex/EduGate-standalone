# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_submodules


root = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(root / "backend"))
hiddenimports = collect_submodules("app") + collect_submodules("uvicorn")

analysis = Analysis(
    [str(root / "desktop" / "edugate_standalone.py")],
    pathex=[str(root / "backend")],
    binaries=[],
    datas=[
        (str(root / "frontend"), "frontend"),
        (str(root / "backend" / ".env.example"), "backend"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="EduGate-Standalone",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(root / "desktop" / "assets" / "edugate.ico"),
    disable_windowed_traceback=False,
)

bundle = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    name="EduGate-Standalone",
)
