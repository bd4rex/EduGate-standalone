# EduGate FastAPI 后端

EduGate 是面向教学场景的 AI Gateway 业务层。单机课堂版运行在教师电脑上，位于学生页面、教师控制台和上游模型公司 API 之间，负责执行教师策略，而不是让前端或第三方客户端直接决定模型、system prompt 和参数。

## 架构

```text
学生网页 / 第三方客户端
        -> EduGate FastAPI 后端
        -> OpenAI-compatible Provider
        -> OpenAI / 阿里百炼 / DeepSeek / Anthropic / 本地模型
```

## 当前职责

- 按教师当前策略决定模型、system prompt、temperature 和知识库。
- 屏蔽学生端的模型选择能力，避免学生绕过教师配置。
- 提供 `/chat`、`/chat/stream` 和 OpenAI 风格 `/v1/chat/completions`。
- 提供教师端管理接口：模型目录、教师账号、AI 开关、知识库、请求日志。
- 记录请求日志到本地 SQLite，并预留 Langfuse trace 能力。
- 使用本地文件 + SQLite chunks 做 V1 知识库检索增强。

## 主要接口

- `GET /health`：健康检查。
- `GET /docs`：中文分组的 FastAPI / OpenAPI 文档。
- `POST /auth/login`：教师端登录，返回管理 token。
- `POST /chat`：学生非流式聊天接口。
- `POST /chat/stream`：学生流式聊天接口，使用 POST + SSE。
- `POST /v1/chat/completions`：OpenAI 兼容入口，支持非流式和流式。
- `POST /config/ai`：教师端 AI 总开关，关闭后学生聊天接口返回 403。
- `GET /admin/teachers` / `POST /admin/teachers`：教师账号列表与新增/更新。
- `PATCH /admin/teachers/{username}/password`：修改教师密码。
- `DELETE /admin/teachers/{username}`：停用教师账号。
- `GET /admin/models` / `POST /admin/models` / `PATCH /admin/models/{id}`：模型目录管理。
- `POST /admin/models/{id}/set-default`：切换默认模型。
- `GET /admin/logs`：请求日志。
- `GET /knowledge/sources` / `POST /knowledge/sources` / `DELETE /knowledge/sources/{id}`：知识库管理。
- `GET /knowledge/files` / `POST /knowledge/files` / `DELETE /knowledge/files/{id}`：知识库文件管理。
- `POST /run_python`：课堂 HTML 页面运行短小 Python 示例代码。

## 本地启动

单机课堂版不需要 Docker、PostgreSQL 或 LiteLLM。首次启动会在当前目录生成本地运行文件：

```text
runtime_config.json   模型目录、教师策略、默认课堂配置
edugate.sqlite3       教师账号、请求日志、看板数据
knowledge.sqlite3     知识库索引
knowledge_files/      上传的课堂资料
```

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 --env-file .env
```

Windows PowerShell：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 --env-file .env
```

首次本地默认账号：

```text
用户名: admin
密码: edugate
管理 Token: local-admin-token
```

正式课堂使用前，请在 `.env` 中修改 `ADMIN_PASSWORD` 和 `ADMIN_API_KEY`。

访问：

```text
API 文档: http://localhost:8000/docs
健康检查: http://localhost:8000/health
```

## 环境变量

```env
EDUGATE_MODE=standalone
UPSTREAM_PROVIDER=DeepSeek
UPSTREAM_BASE_URL=https://api.deepseek.com/v1
UPSTREAM_API_KEY=replace-with-your-provider-api-key
DEFAULT_MODEL=deepseek-chat

ADMIN_API_KEY=local-admin-token
ADMIN_USERNAME=admin
ADMIN_PASSWORD=edugate

PLATFORM_API_KEY=change-me-platform-api-key
EDUGATE_SQLITE_DB_PATH=edugate.sqlite3

RUNTIME_CONFIG_PATH=runtime_config.json
KNOWLEDGE_DIR=knowledge_files
KNOWLEDGE_DB_PATH=knowledge.sqlite3
KNOWLEDGE_SEARCH_LIMIT=5

LANGFUSE_BASE_URL=
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=

REQUEST_TIMEOUT_SECONDS=60
PYTHON_RUNNER_ENABLED=true
PYTHON_RUNNER_TIMEOUT_SECONDS=3
PYTHON_RUNNER_MAX_CODE_CHARS=6000
CORS_ORIGINS=http://localhost:5173,http://localhost:3000,null,http://服务器IP:8080

# 兼容旧 LiteLLM 部署，可留空。
LITELLM_BASE_URL=http://127.0.0.1:4000
LITELLM_API_PREFIX=/v1
LITELLM_API_KEY=
```

也可以不在 `.env` 里预置模型，登录教师控制端后到“模型与供应商管理”中填写：

```text
模型 ID: deepseek-chat
模型来源: EduGate 直连 OpenAI-compatible
Base URL: https://api.deepseek.com/v1
API Key: 模型公司 API Key
```

## 学生聊天示例

默认策略：

```bash
curl http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user", "content": "请解释什么是私有 IP 地址"}
    ]
  }'
```

教师维度策略：

```json
{
  "teacher_id": "zhang",
  "messages": [
    {
      "role": "user",
      "content": "请解释什么是私有 IP 地址"
    }
  ]
}
```

说明：公开学生接口不再使用 `session_id`。如果请求体中传入 `session_id`，会因为 `extra=forbid` 被 FastAPI 拒绝。

流式接口：

```bash
curl -N http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d '{
    "teacher_id": "zhang",
    "messages": [
      {"role": "user", "content": "用三句话解释 TCP/IP"}
    ]
  }'
```

## 知识库

V1 知识库是轻量实现：

```text
上传文件 -> 抽取文本 -> 切分 chunks -> 写入 SQLite -> 关键词检索 -> 注入 system message
```

支持文件类型：

```text
.txt .md .markdown .csv .json .html .htm .xml .log .pdf
```

`knowledge_strict=true` 是 prompt 级约束，不是硬规则引擎。它会让模型在资料不足时更倾向拒答，但后续仍建议加入引用展示、答案校验和 Langfuse 反馈闭环。

## 测试

```bash
cd backend
pytest -q
```

## 生产路径

单机版运行期文件默认位于后端启动目录：

```text
.env
runtime_config.json
edugate.sqlite3
knowledge_files/
knowledge.sqlite3
```
