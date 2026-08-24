# EduGate 桌面启动与打包

**中文** · [English](README.en.md)

## 打包版

Windows 教师获得完整 `EduGate-Standalone` 文件夹后双击 `EduGate-Standalone.exe`；Apple 芯片 Mac 教师打开 `EduGate.app`。程序在后台监督 Uvicorn，并自动打开本机教师控制台：

```text
教师控制台  http://127.0.0.1:实际端口/admin.html
学生页面    http://教师电脑局域网IP:实际端口/student.html#...
```

打包版包含后端依赖，不需要预装 Python。Windows 版使用 `runtime\python` 运行学生代码，数据位于 EXE 旁的 `config` 和 `data`；macOS 版用独立受限应用子进程运行学生代码，数据位于 `~/Library/Application Support/EduGate`。

教师端默认只允许本机访问；学生设备不能使用本机自动登录，也不能调用管理 API。

## 源码启动

源码要求 64 位 Python 3.10+。Windows：

- 首次安装：`install_backend_deps.bat`
- 日常启动：`run_standalone.bat`
- 虚拟环境：项目根目录 `runtime\venv`
- 配置与数据：项目根目录 `config`、`data`

macOS：

```bash
python3 -m venv runtime/venv
runtime/venv/bin/python -m pip install -r backend/requirements.txt
runtime/venv/bin/python desktop/edugate_standalone.py
```

## 打包

Windows 运行 `build_windows.bat`。脚本验证 Python 版本，创建 `.venv-build`，安装 PyInstaller 和后端依赖，并生成：

```text
dist\EduGate-Standalone\
  EduGate-Standalone.exe
  README.txt
  runtime\python\python.exe
  _internal\
```

Apple Silicon Mac 运行：

```bash
EDUGATE_VERSION=2.2.0 desktop/build_macos.sh
```

脚本生成 `dist/EduGate.app` 和 `dist/EduGate-Standalone-v2.2.0-macos-arm64.zip`，执行 ad-hoc 签名并验证应用包结构。没有 Apple Developer ID 时无法完成公证，发布说明必须保留首次 Control 点击打开的提示。

发布和验收步骤见 [开发与测试](../docs/开发与测试.md)。
