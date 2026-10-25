# EduGate desktop launchers and packaging

[中文](README.md) · **English**

## Packaged editions

Windows teachers launch `EduGate-Standalone.exe` from the complete portable folder. Apple Silicon Mac teachers open `EduGate.app`. Both editions supervise Uvicorn in the background and open the local teacher console automatically.

The packaged editions include backend dependencies and offline Pyodide/WASM, with no separate Python installation required. On both Windows and macOS, student code runs only in student browsers. Windows stores data beside the EXE; macOS stores data in `~/Library/Application Support/EduGate`.

Teacher management stays local by default. Student devices cannot use local auto-login or management APIs.

## Source mode

The Windows source edition requires 64-bit Python 3.10 or later. macOS packaging uses Python 3.10–3.12 to retain inference dependency compatibility with macOS 11. Windows uses `install_backend_deps.bat` and `run_standalone.bat`. macOS can use:

```bash
python3 -m venv runtime/venv
runtime/venv/bin/python -m pip install -r backend/requirements.txt
runtime/venv/bin/python desktop/edugate_standalone.py
```

## Packaging

Run `build_windows.bat` for the Windows portable folder, package manifest, and upgrade ZIP under `dist/releases/2.4.0-<timestamp>/`. On Apple Silicon, run:

```bash
EDUGATE_VERSION=2.4.0 desktop/build_macos.sh
```

The macOS script creates `dist/EduGate.app` and the versioned macOS ZIP, applies an ad-hoc signature, and verifies the bundle. Without an Apple Developer ID certificate the app cannot be notarized, so release instructions must retain the first-launch Control-click guidance. v2.4.0 ships a Windows binary only; a real macOS build and acceptance run are still required.

Resources / Blacklist and trial generates complete answers using saved model and classroom settings, including follow-ups, without adding student records. The configured upstream answer model must be available. The offline package bundles the application and retrieval model, not an answer-generating model.

See the [Chinese development and testing guide](../docs/开发与测试.md) for release acceptance checks.


## Portable folders and data-preserving upgrades

On Windows, carry the complete folder and launch its single `EduGate-Standalone.exe`. Each folder locks its own data; duplicate launches only reopen that same instance. Other teachers use separate ports. The launcher does not import legacy machine data automatically, and the browser does not restore another teacher's selected knowledge base.

Use **End class and exit** (Chinese UI: 结束课堂并退出), then wait for the Windows confirmation before safely ejecting the drive. A disconnected browser is not proof that data was saved. The local OS helper checks that the application process exited and that database flushing completed.

Under System / Backup and upgrade / Program upgrade, upload the new program release ZIP. It validates the manifest, ends the class, saves, replaces only program files, and restarts. `config` and `data` remain in place; previous program files are kept under `data/updates`. Replacement failures attempt rollback. Do not unplug during an upgrade. The package includes the runtime and retrieval model; AI answers still require access to the configured upstream service.

New builds go to a fresh `dist/releases/<version>-<timestamp>/` directory instead of overwriting an existing portable folder. Real-process acceptance is available through `desktop/verify_portable.py --package <clean-package-folder> --upgrade <release.zip>`. This simulates relocation on the development computer; a second physical classroom PC and its LAN policy still need on-site acceptance.
