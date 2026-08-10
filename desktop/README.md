# EduGate Windows 启动与打包

## 教师电脑（推荐打包版）

教师获得完整的 `EduGate-Standalone` 文件夹后，直接双击 `EduGate-Standalone.exe`。打包版已包含全部后端依赖和 `runtime\python` 学生代码解释器，不需要安装 Python。

启动程序没有独立 Windows 界面。它在后台监督 Uvicorn 服务，并自动打开教师 Web 控制台：

```text
教师控制台  http://127.0.0.1:实际端口/admin.html
学生页面    http://教师电脑局域网IP:实际端口/student.html#...
```

教师电脑会自动进入控制台；学生设备不能使用本机自动登录。初始账号 `admin`、密码 `edugate` 位于 `config\edugate.env`，可作为自动登录失败时的兜底。

运行数据全部位于 EXE 旁的 `config` 和 `data`，密钥采用可复制的便携格式。“结束课堂”只关闭学生入口；复制整个文件夹前请在“系统”页点击“停止服务”。

## 源码启动

- 首次安装：双击 `install_backend_deps.bat`。
- 日常启动：双击 `run_standalone.bat`。
- 虚拟环境：项目根目录 `runtime\venv`。
- 配置与数据：项目根目录 `config`、`data`。
- Python 包源：`https://pypi.tuna.tsinghua.edu.cn/simple`。

## 打包

开发电脑双击 `build_windows.bat`。脚本创建仓库内的 `.venv-build`，从清华源安装 PyInstaller 和后端依赖，使用 `edugate_standalone.spec` 生成 one-directory 程序，并加入独立 Python 运行时：

```text
dist\EduGate-Standalone\
  EduGate-Standalone.exe
  README.txt
  runtime\python\python.exe
  _internal\
```

`edugate_desktop.py` 和 `run.bat` 是连接已有远端后端的早期原型，不属于单机课堂版启动流程。
