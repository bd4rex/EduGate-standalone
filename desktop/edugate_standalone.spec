# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_submodules


root = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(root / "backend"))
hiddenimports = collect_submodules("app") + collect_submodules("uvicorn")
is_macos = sys.platform == "darwin"
icon_path = root / "desktop" / "assets" / ("edugate.icns" if is_macos else "edugate.ico")
app_version = os.environ.get("EDUGATE_VERSION", "0.0.0")

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
    # The macOS subprocess runner needs real stdin/stdout pipes. LaunchServices
    # still opens the .app without a Terminal window.
    console=is_macos,
    icon=str(icon_path),
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

if is_macos:
    app = BUNDLE(
        bundle,
        name="EduGate.app",
        icon=str(icon_path),
        bundle_identifier="com.bd4rex.edugate.standalone",
        info_plist={
            "CFBundleDisplayName": "EduGate",
            "CFBundleName": "EduGate",
            "CFBundleShortVersionString": app_version,
            "CFBundleVersion": app_version,
            "LSMinimumSystemVersion": "11.0",
            "LSUIElement": True,
            "NSHighResolutionCapable": True,
        },
    )
