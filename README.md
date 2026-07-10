# EduGate Standalone

EduGate 单机课堂版运行在教师电脑上，面向单教师、单节课或小型课堂。它向上连接 OpenAI-compatible 模型公司 API，向下提供受课堂令牌保护的学生页面和 EduGate API。

## Windows 首次安装

1. 安装 Python 3.9 或更高版本，并勾选 `Add python.exe to PATH`。
2. 双击 `desktop\install_backend_deps.bat`。脚本固定使用清华 PyPI 镜像，并创建 `%LOCALAPPDATA%\EduGate\venv`，不会向系统 Python 安装包。
3. 双击 `desktop\run_standalone.bat`。
4. 在自动打开的教师控制台创建管理员密码。系统没有默认密码。
5. 在“配置 -> 模型与供应商管理”中填写模型 ID、Base URL 和 API Key，保存后点击“测试连接”。

以后上课只需双击 `desktop\run_standalone.bat`。教师控制台、学生页面和 API 都由同一个 `8000` 端口提供，不需要配置跨域或单独启动网页服务器。

安装脚本会自动处理部分 Python 3.9 在含空格 Windows 用户目录中无法创建 `venv` 的问题；启动异常记录在 `%LOCALAPPDATA%\EduGate\launcher.log`。

## 上课流程

1. 登录教师控制台，选择模型、知识库和课堂策略。
2. 复制“学生课堂入口”中的完整链接发给学生。
3. 学生链接包含本次运行生成的临时课堂令牌，并绑定当前教师策略。
4. 需要让旧链接失效时，点击“换一个链接”。

模型 API Key 使用当前 Windows 用户的 DPAPI 加密。运行数据默认保存在：

```text
%LOCALAPPDATA%\EduGate
  .env
  edugate.sqlite3
  knowledge.sqlite3
  knowledge_files\
  runtime_config.json
  secrets.json
  venv\
```

卸载程序时可保留此目录来保留课堂数据。复制到另一台电脑时，`secrets.json` 无法解密，需要重新填写模型 API Key。

## 安全默认值

- 管理员只能从教师电脑首次创建，密码至少 10 个字符。
- 教师登录会话默认 8 小时过期；退出、改密码或停用账号会立即使会话失效。
- `/chat`、`/chat/stream` 和 `/run_python` 要求临时课堂令牌并有限流。
- `/v1/chat/completions` 仅在配置 `PLATFORM_API_KEY` 后启用。
- Python 运行器默认关闭；启用后仍有语法、超时、内存和并发限制。
- 上传默认限制 25 MB，PDF 默认限制 200 页。
- 请求日志默认不保存师生消息正文，最多保留 5000 条。

## 开发验证

```powershell
python -m pytest -q
```

Windows 打包：

```text
desktop\build_windows.bat
```

输出位于 `dist\EduGate-Standalone`。打包脚本同样使用清华 PyPI 镜像。

详细说明见 [安装与故障排查](docs/安装与故障排查.txt)、[使用手册](docs/使用手册.txt) 和 [验收清单](docs/单机版验收清单.txt)。
