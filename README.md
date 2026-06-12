# EduGate Standalone

EduGate 单机课堂版运行在教师电脑上，为单节课/单教师场景提供本地 AI 教学网关。

它向上接入 OpenAI-compatible 模型公司 API，向下保留 EduGate 现有学生端接口：

- `POST /chat`
- `POST /chat/stream`
- `POST /v1/chat/completions`

## 快速启动

Windows 首次运行先安装依赖：

```text
desktop/install_backend_deps.bat
```

安装脚本会显示完整步骤，并默认使用清华 PyPI 镜像。详细说明见：

```text
docs/安装与故障排查.txt
```

启动单机课堂版：

```text
desktop/run_standalone.bat
```

启动器会显示：

- 教师控制台：`http://教师电脑IP:8080/admin.html`
- 学生 API：`http://教师电脑IP:8000`

默认登录：

```text
admin / edugate
```

正式课堂使用前，请修改 `backend/.env` 中的 `ADMIN_PASSWORD`、`ADMIN_API_KEY` 和模型公司 API Key。

## 目录

```text
backend/    FastAPI 本地网关、SQLite 业务库、知识库索引
frontend/   教师控制台、学生测试页、嵌入示例页
desktop/    Windows 桌面启动器和依赖安装脚本
docs/       单机版说明、使用手册、验收清单
samples/    示例知识库资料
```

## 验证

```powershell
cd backend
python -m pytest -q
```

当前已验证：

```text
31 passed
health smoke passed
```
