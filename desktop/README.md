# EduGate Windows 启动与打包

## 教师电脑

- 首次安装：双击 `install_backend_deps.bat`。
- 日常启动：双击 `run_standalone.bat`。
- 安装位置：`%LOCALAPPDATA%\EduGate\venv`。
- 数据位置：`%LOCALAPPDATA%\EduGate`。
- Python 包源：`https://pypi.tuna.tsinghua.edu.cn/simple`。

启动器只运行一个 Uvicorn 服务：

```text
教师控制台  http://127.0.0.1:8000/admin.html
学生页面    http://教师电脑局域网IP:8000/student.html#...
```

首次打开教师控制台时创建管理员密码。不存在 `admin / edugate` 之类的默认密码。

## 打包

开发电脑双击 `build_windows.bat`。脚本会创建仓库内的 `.venv-build`，安装 PyInstaller 和运行依赖，然后使用 `edugate_standalone.spec` 生成 one-directory Windows 程序：

```text
dist\EduGate-Standalone\EduGate-Standalone.exe
```

one-directory 模式便于检查、升级和防病毒软件扫描，也比 one-file 模式启动更快。程序文件和课堂数据分离，升级 `dist` 内容不会覆盖 `%LOCALAPPDATA%\EduGate`。

`edugate_desktop.py` 和 `run.bat` 是连接已有远端后端的早期原型，不属于单机课堂版启动流程。
