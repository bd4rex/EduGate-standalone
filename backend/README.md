# EduGate 后端

FastAPI 后端负责单教师认证、课堂令牌、学生会话、模型代理、知识库、Python 执行和同源静态页面。

## 开发启动

要求 64 位 Python 3.10+：

```powershell
python -m pip install -r backend\requirements-dev.txt
$env:EDUGATE_DATA_DIR="$PWD\.local-data"
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000/admin.html`。开发命令只监听本机；课堂分发请使用 Windows 启动器。

## 模块边界

- `app/main.py`：应用装配、中间件、OpenAPI、静态页面。
- `app/state.py`：共享服务和生命周期。
- `app/dependencies.py`：教师、学生和平台权限。
- `app/chat_service.py`：对话、模型、知识检索和流式处理。
- `app/runtime_config.py`：模型目录和唯一 `default` 场景。
- `app/routers/`：按业务域拆分的 API。
- `app/core.py`：旧导入路径兼容门面。

## 主要接口

```text
GET  /health
GET  /auth/status
POST /auth/setup                    仅教师本机、仅首次
POST /auth/login                    默认仅教师本机
POST /auth/logout                   X-Admin-Token
POST /auth/password                 X-Admin-Token

POST /classroom/join                X-Class-Token → X-Student-Token
POST /chat                          X-Student-Token
POST /chat/stream                   X-Student-Token
POST /run_python                    X-Student-Token
POST /run_python/stream             X-Student-Token

GET/PUT /config...                  X-Admin-Token
GET/POST /admin/classroom...        X-Admin-Token
GET/DELETE /admin/classroom-records... X-Admin-Token
GET/POST/PATCH/DELETE /admin/models... X-Admin-Token
GET/DELETE /admin/providers...      X-Admin-Token
GET/PUT/POST /admin/system...       X-Admin-Token

POST /v1/chat/completions           Bearer PLATFORM_API_KEY；未配置时关闭
```

默认 `ALLOW_LAN_ADMIN=false`，即使令牌有效，局域网来源也不能调用管理 API。详细边界见 [架构与安全边界](../docs/架构与安全边界.md)。

## 测试

```powershell
python -m pytest -q --basetemp=.pytest-tmp
```
