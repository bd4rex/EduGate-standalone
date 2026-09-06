# EduGate Release Log

[中文](TIMESTAMP_LOG.md) · **English**

## 2026-08-24 — v2.1.0 macOS Apple Silicon Asset

- Added a native `EduGate.app` and `EduGate-Standalone-v2.1.0-macos-arm64.zip` build flow to the existing v2.1.0 release; product content and feature version are unchanged. It supports Apple Silicon (arm64) on macOS 11 or later. Intel Macs are not supported.
- macOS data is stored in `~/Library/Application Support/EduGate`; data and configuration directories use mode `700`, while the configuration and portable secret files use mode `600`.
- Student Python code runs in a separate app subprocess. macOS enforces the memory limit with RSS monitoring while retaining timeout, import, output, and concurrency controls.
- The app bundle is ad-hoc signed and passed `codesign --verify --deep --strict`. This release is not notarized because no Apple Developer ID certificate was available, so first launch requires Control-clicking the app and choosing Open.
- Local non-E2E regression result: `172 passed, 1 deselected` with `85.06%` branch coverage. Python compilation, shell syntax, and Git diff checks also passed.
- The final ZIP passed post-extraction acceptance for bundle structure, arm64 architecture, signing, teacher console, classroom start/end, student join, restricted Python execution, memory and timeout limits, file permissions, and clean shutdown. Finder/LaunchServices startup also selected an available fallback port correctly.
- Release asset SHA-256: `14190035347549e74fbd838ab986f865e2c327a8c936b14784736d26293a143b`.

The Windows x64 and macOS Apple Silicon packages share the same [v2.1.0 release](https://github.com/bd4rex/EduGate-standalone/releases/tag/v2.1.0).
