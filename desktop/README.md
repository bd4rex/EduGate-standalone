# EduGate 桌面启动与打包

**中文** · [English](README.en.md)

## 打包版

Windows 教师获得完整 `EduGate-Standalone` 文件夹后双击 `EduGate-Standalone.exe`；Apple 芯片 Mac 教师打开 `EduGate.app`。程序在后台监督 Uvicorn，并自动打开本机教师控制台：

```text
教师控制台  http://127.0.0.1:实际端口/admin.html
学生页面    http://教师电脑局域网IP:实际端口/student.html#...
```

打包版包含后端依赖和离线 Pyodide/WASM，不需要预装 Python。Windows 和 macOS 均只在学生浏览器内执行学生代码，不再提供教师端学生解释器。Windows 数据位于 EXE 旁的 `config` 和 `data`；macOS 数据位于 `~/Library/Application Support/EduGate`。

教师端默认只允许本机访问；学生设备不能使用本机自动登录，也不能调用管理 API。

## 源码启动

Windows 源码要求 64 位 Python 3.10+；macOS 打包使用 Python 3.10–3.12，以保留 macOS 11 的推理依赖兼容性。Windows：

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
dist\releases\2.4.0-时间戳\
  EduGate-Standalone\
    EduGate-Standalone.exe
    README.txt
    package.json
    _internal\
  EduGate-Standalone-2.4.0-windows-x64.zip
  release.json
```

Apple Silicon Mac 运行：

```bash
EDUGATE_VERSION=2.4.0 desktop/build_macos.sh
```

脚本生成 `dist/EduGate.app` 和对应版本的 macOS ZIP，执行 ad-hoc 签名并验证应用包结构。没有 Apple Developer ID 时无法完成公证，发布说明必须保留首次 Control 点击打开的提示。v2.4.0 本次只交付 Windows 二进制，macOS 构建仍需在真实设备验收。

“资源 → 黑名单与试问”使用已保存的模型和课堂规则生成完整回答，可在课前连续追问，不写入学生记录。该功能需要配置的上游模型可用；离线打包的是运行环境与资料检索模型，不是回答模型。

发布和验收步骤见 [开发与测试](../docs/开发与测试.md)。


## U 盘运行与保留数据升级

日常只需双击完整便携目录内的 EXE。换电脑时携带整个文件夹；“结束课堂并退出”会保存并关闭后台，Windows 在进程完全退出后给出确认。程序升级在“系统 → 备份与升级 → 程序升级”选择发布 ZIP，保留 config、data，并保存原程序副本。详细流程、目录隔离和验证边界见 [便携运行与升级](../docs/便携运行与升级.md)。
