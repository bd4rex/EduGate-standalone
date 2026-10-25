# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_submodules, collect_all, copy_metadata


root = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(root / "backend"))
hiddenimports = collect_submodules("app") + collect_submodules("uvicorn")
knowledge_datas = []
knowledge_binaries = []
for package in ("lancedb", "fastembed", "jieba", "onnxruntime", "tokenizers", "pyarrow"):
    package_datas, package_binaries, package_imports = collect_all(package, include_py_files=False)
    knowledge_datas.extend(package_datas)
    knowledge_binaries.extend(package_binaries)
    hiddenimports.extend(package_imports)
for distribution in ("lancedb", "fastembed", "jieba", "python-docx", "python-pptx", "beautifulsoup4"):
    knowledge_datas.extend(copy_metadata(distribution, recursive=True))
knowledge_datas.append((str(root / "desktop" / "licenses"), "licenses/knowledge"))
model_directory = Path(os.environ.get("EDUGATE_BUNDLED_KNOWLEDGE_MODEL_DIR", str(root / "build" / "knowledge-model")))
if not (model_directory / "model.json").is_file():
    raise RuntimeError("Prepare the local model first: python desktop/prepare_knowledge_model.py --export build/knowledge-model")
knowledge_datas.append((str(model_directory), "knowledge_models"))
is_macos = sys.platform == "darwin"
icon_path = root / "desktop" / "assets" / ("edugate.icns" if is_macos else "edugate.ico")
app_version = os.environ.get("EDUGATE_VERSION", "0.0.0")

analysis = Analysis(
    [str(root / "desktop" / "edugate_standalone.py")],
    pathex=[str(root / "backend")],
    binaries=knowledge_binaries,
    datas=[
        (str(root / "frontend"), "frontend"),
        (str(root / "backend" / ".env.example"), "backend"),
        (str(root / "desktop" / "portable_maintenance.ps1"), "desktop"),
    ] + knowledge_datas,
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
    # Preserve the macOS standard-stream bootloader; LaunchServices opens the
    # .app without a Terminal window. Student code runs only in the browser.
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
