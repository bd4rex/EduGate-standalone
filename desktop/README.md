# EduGate Windows 启动与打包

## 打包版

教师获得完整 `EduGate-Standalone` 文件夹后，双击 `EduGate-Standalone.exe`。程序在后台监督 Uvicorn，并自动打开本机教师控制台：

```text
教师控制台  http://127.0.0.1:实际端口/admin.html
学生页面    http://教师电脑局域网IP:实际端口/student.html#...
```

打包版包含后端依赖和 `runtime\python` 学生代码解释器，不需要预装 Python。运行数据位于 EXE 旁的 `config` 和 `data`。

教师端默认只允许本机访问；学生设备不能使用本机自动登录，也不能调用管理 API。

## 源码启动

源码要求 64 位 Python 3.10+：

- 首次安装：`install_backend_deps.bat`
- 日常启动：`run_standalone.bat`
- 虚拟环境：项目根目录 `runtime\venv`
- 配置与数据：项目根目录 `config`、`data`

## 打包

运行 `build_windows.bat`。脚本验证 Python 版本，创建 `.venv-build`，安装 PyInstaller 和后端依赖，并生成：

```text
dist\EduGate-Standalone\
  EduGate-Standalone.exe
  README.txt
  runtime\python\python.exe
  _internal\
```

发布和验收步骤见 [开发与测试](../docs/开发与测试.md)。
