# EduGate desktop launchers and packaging

[中文](README.md) · **English**

## Packaged editions

Windows teachers launch `EduGate-Standalone.exe` from the complete portable folder. Apple Silicon Mac teachers open `EduGate.app`. Both editions supervise Uvicorn in the background and open the local teacher console automatically.

The packaged editions include backend dependencies and do not require a separate Python installation. Windows runs student code through `runtime\python` and stores data beside the EXE. macOS runs student code in an isolated app subprocess and stores data in `~/Library/Application Support/EduGate`.

Teacher management stays local by default. Student devices cannot use local auto-login or management APIs.

## Source mode

The source edition requires 64-bit Python 3.10 or later. Windows uses `install_backend_deps.bat` and `run_standalone.bat`. macOS can use:

```bash
python3 -m venv runtime/venv
runtime/venv/bin/python -m pip install -r backend/requirements.txt
runtime/venv/bin/python desktop/edugate_standalone.py
```

## Packaging

Run `build_windows.bat` for the Windows portable folder. On Apple Silicon, run:

```bash
EDUGATE_VERSION=2.2.0 desktop/build_macos.sh
```

The macOS script creates `dist/EduGate.app` and `dist/EduGate-Standalone-v2.2.0-macos-arm64.zip`, applies an ad-hoc signature, and verifies the bundle. Without an Apple Developer ID certificate the app cannot be notarized, so the release instructions must retain the first-launch Control-click guidance.

See the [Chinese development and testing guide](../docs/开发与测试.md) for release acceptance checks.
