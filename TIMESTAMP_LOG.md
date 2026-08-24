# EduGate 发布记录

**中文** · [English](TIMESTAMP_LOG.en.md)

## 2026-08-24 — v2.2.0 macOS Apple Silicon

- 新增原生 `EduGate.app` 与 `EduGate-Standalone-v2.2.0-macos-arm64.zip` 构建流程，支持 macOS 11+ 的 Apple Silicon（arm64），不支持 Intel Mac。
- macOS 数据固定保存在 `~/Library/Application Support/EduGate`；数据和配置目录权限为 `700`，配置与便携密钥文件权限为 `600`。
- 学生 Python 代码在独立应用子进程中运行；macOS 使用 RSS 监控执行内存上限，同时保留超时、导入限制、输出限制和并发队列控制。
- 应用包已执行 ad-hoc 签名并通过 `codesign --verify --deep --strict`。由于没有 Apple Developer ID，本版本未公证，首次打开须按住 Control 点击应用并选择“打开”。
- 本地非 E2E 回归结果：`172 passed, 1 deselected`，分支覆盖率 `85.06%`；同时通过 Python 编译、Shell 语法和 Git diff 检查。
- 最终 ZIP 已完成解压后验收：应用包结构与 arm64 架构、签名、教师控制台、课堂启停、学生加入、受限 Python 执行、内存/超时限制、文件权限和正常退出均通过；通过 Finder/LaunchServices 启动时也能自动选择可用端口。
- 发布包 SHA-256：`86eba5f697b5e5fd8b7ede45aa53a1ce27cda985fedb800b780404a1da262362`。

Windows x64 用户继续使用 [v2.1.0](https://github.com/bd4rex/EduGate-standalone/releases/tag/v2.1.0)。
