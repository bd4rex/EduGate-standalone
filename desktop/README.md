# EduGate Desktop Prototype

这个目录包含两个教师端桌面原型：

- `edugate_standalone.py`：单机课堂版启动器，负责在教师电脑上启动本地 EduGate 后端和教师控制台静态服务。
- `edugate_desktop.py`：早期桌面控制端原型，连接一个已经运行的 EduGate 后端。

## 单机课堂版

Windows 双击：

```text
run_standalone.bat
```

如果提示缺少后端依赖，先双击：

```text
install_backend_deps.bat
```

或命令行运行：

```powershell
cd desktop
py .\edugate_standalone.py
```

启动器会自动：

- 检查并复制 `backend/.env.example` 为 `.env`。
- 检查 FastAPI、Uvicorn、HTTPX 等后端依赖是否已经安装。
- 启动后端：`http://0.0.0.0:8000`。
- 启动教师控制台静态服务：`http://0.0.0.0:8080/admin.html`。
- 显示教师电脑局域网 IP，方便复制给学生端。

默认登录：

```text
用户名: admin
密码: edugate
```

首次上课前，请打开 `backend/.env`，填写模型公司的 API：

```env
UPSTREAM_PROVIDER=DeepSeek
UPSTREAM_BASE_URL=https://api.deepseek.com/v1
UPSTREAM_API_KEY=填入模型公司APIKey
DEFAULT_MODEL=deepseek-chat
```

也可以登录教师控制台，在“模型与供应商管理”里新增或更新 OpenAI-compatible 模型。

## 旧桌面控制端

如果已经有远端 EduGate 后端，可以继续运行：

```powershell
py .\edugate_desktop.py
```

这个版本只作为控制端，不会启动本地后端。

## 打包方向

后续可以用 PyInstaller 打包单机启动器：

```powershell
pip install pyinstaller
pyinstaller --onefile --windowed edugate_standalone.py
```

生成结果会在 `dist/edugate_standalone.exe`。
