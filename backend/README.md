# EduGate Standalone Backend

FastAPI 后端负责教师身份、课堂访问、策略执行、知识库检索、上游模型代理和同源静态页面。

## 本地开发

```powershell
python -m pip install -r backend\requirements-dev.txt
$env:EDUGATE_DATA_DIR="$PWD\.local-data"
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000/admin.html`，首次创建管理员密码。正式教师安装请使用 `desktop\install_backend_deps.bat`，它会通过清华源建立独立虚拟环境。

## 主要接口

```text
GET  /health
GET  /auth/status
POST /auth/setup             仅回环地址、仅首次
POST /auth/login
POST /auth/logout
POST /auth/password

POST /chat                   X-Class-Token
POST /chat/stream            X-Class-Token
POST /run_python             X-Class-Token，默认关闭
POST /v1/chat/completions    Bearer PLATFORM_API_KEY，未配置时关闭

GET  /config                 X-Admin-Token
PUT  /config/scenarios/{id}  X-Admin-Token
GET  /admin/classroom        X-Admin-Token
POST /admin/classroom/rotate X-Admin-Token
POST /admin/models           管理员
POST /admin/providers/{id}/test 管理员
GET  /admin/system/status      管理员
GET  /admin/system/settings    管理员
PUT  /admin/system/settings    管理员，保存后需重启
GET  /admin/system/backup      管理员
POST /admin/system/restore     管理员，验证后重启恢复
POST /admin/system/action      管理员，restart/shutdown
```

## 运行配置

相对路径都解析到 `EDUGATE_DATA_DIR`。Windows 默认为 `%LOCALAPPDATA%\EduGate`。

```env
EDUGATE_MODE=standalone
EDUGATE_BACKEND_PORT=8000
EDUGATE_DATA_DIR=
ADMIN_USERNAME=admin
SESSION_TTL_SECONDS=28800
CLASSROOM_RATE_LIMIT_PER_MINUTE=30
LOGIN_RATE_LIMIT_PER_5_MINUTES=10
MODEL_MAX_CONCURRENCY=4
PLATFORM_API_KEY=

REQUEST_TIMEOUT_SECONDS=60
STREAM_READ_TIMEOUT_SECONDS=120
STREAM_HEARTBEAT_SECONDS=15

MAX_UPLOAD_BYTES=26214400
MAX_PDF_PAGES=200
LOG_MESSAGE_PREVIEW=false
LOG_MAX_RECORDS=5000

PYTHON_RUNNER_ENABLED=false
PYTHON_RUNNER_TIMEOUT_SECONDS=3
PYTHON_RUNNER_MEMORY_MB=128
CORS_ORIGINS=
```

模型公司 API Key 推荐从教师控制台录入。它由 Windows DPAPI 加密到 `secrets.json`，不会进入 `runtime_config.json` 或 API 响应。`UPSTREAM_API_KEY` 只保留作旧部署迁移兼容，不建议用于新安装。

## 测试

从仓库根目录运行：

```powershell
python -m pytest -q
```

测试覆盖首次初始化、会话过期与失效、课堂令牌、限流边界、DPAPI 密钥、供应商探测、上传/PDF 限制、Python 运行器和流式心跳。
