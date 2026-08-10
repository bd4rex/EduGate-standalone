<p align="center">
  <img src="frontend/assets/brand/edugate-logo-horizontal.svg" width="680" alt="EduGate - Local AI Teaching Gateway" />
</p>

# EduGate 单机课堂版

<p align="center">
  <strong>中文</strong> · <a href="README.en.md">English</a>
</p>

EduGate 单机课堂版运行在教师的 64 位 Windows 电脑上，面向单教师、单节课或小型课堂。它向上连接 OpenAI-compatible 模型公司 API，向下提供受课堂令牌保护的学生页面和 EduGate API。教师端只有 Web 控制台，没有需要同时操作的 Windows 窗口。

## 老师直接使用

1. 解压或复制完整的 `EduGate-Standalone` 文件夹，不要只复制 EXE。
2. 双击 `EduGate-Standalone.exe`，浏览器会自动打开教师控制台并在本机自动进入。
3. 在“系统 -> 上游模型 API 管理”填写模型 ID、Base URL 和 API Key，保存并测试连接。
4. 到“控制”页最下方的学生课堂入口点击“开始课堂”，再复制课堂链接发给学生。
5. 下课在学生课堂入口点击“结束课堂”。学生链接立即失效，教师控制台继续运行；需要退出程序时再使用“系统 -> 停止服务”。

打包版已经包含运行 EduGate 和学生 Python 代码所需的独立 Python 环境，教师电脑不需要预装 Python，也不需要联网安装依赖。如果 `8000` 端口被占用，启动器会自动尝试后续端口并打开正确页面。

以后整包复制到另一台电脑或 U 盘即可沿用模型、知识库、课堂策略和记录。请保留以下结构：

```text
EduGate-Standalone\
  EduGate-Standalone.exe
  README.txt
  config\
    edugate.env             # 启动设置及初始密码
  data\
    edugate.sqlite3         # 本机管理状态与课堂记录
    knowledge.sqlite3
    knowledge_files\
    runtime_config.json
    secrets.json            # 可复制的本地模型密钥
    launcher.log
  runtime\python\           # 学生代码独立解释器
  _internal\                # EduGate 程序资源
```

首次运行时会自动创建 `config` 和 `data`。便携模式的初始管理员账号为 `admin`，初始密码 `edugate` 写在 `config\edugate.env`；本机浏览器默认自动进入，因此正常上课无需输入密码。学生电脑访问局域网课堂地址时不能使用本机自动登录接口。

为了让复制文件夹后 API Key 继续可用，便携版的 `data\secrets.json` 不再绑定某个 Windows 用户。它不会在页面回显，但拥有该文件夹的人可以读取其中的本地凭据；请按课堂文件夹管理即可。

## 上课流程

1. 打开教师控制台，确认 AI 服务、模型和知识库状态。
2. 到控制页最下方的“学生课堂入口”点击“开始课堂”，再复制完整链接发给学生。
3. 学生首次打开课堂链接时填写电脑名或座位号，教师端同时记录请求 IP；课堂令牌仍会换取独立会话，因此即使全班经过同一个代理 IP，也按学生会话分别限流。
4. 学生页面在当前浏览器保存最近 50 条消息，刷新可继续对话；发送模型时只携带最近 10 条，避免上下文无限增长。
5. 点击“换一个链接”可立即让旧链接和旧学生会话失效。
6. 在学生课堂入口点击“结束课堂”只会撤销学生链接和会话，不会关闭教师端。要复制或移动文件夹，请在“系统”页点击“停止服务”，等待服务退出并完成 SQLite checkpoint。

“记录”页按课堂展示学生电脑名、IP、AI 对话和 Python 运行结果；这些内容仅保存在教师电脑中，默认保留 30 天。“系统”页提供运行状态、可复制的局域网地址、打开程序目录、日志、备份恢复、默认折叠的高级设置、重启和停止服务。

“资源”页把知识库列表放在最上方。每个知识库都可以编辑、打开教师电脑上的对应文件夹并执行“扫描同步”；把支持的资料直接放进该文件夹后，扫描会增量添加新文件、更新变更文件，并移除已经从文件夹删除的索引。默认的 `general` 知识库不可删除，其他未被课堂配置使用的知识库可以连同文件和索引一起删除。低频的新建/编辑表单默认折叠。

Python 执行器默认启用 4 个独立执行槽位和最多 64 个排队任务，`/run_python/stream` 实时返回排队、运行、输出和完成事件。任务池是受控队列，不是缓存；每个任务使用新的受限 Python 子进程，不共享学生变量或执行状态。可在高级设置中调整到最多 8 路并发。

## 从源码运行

源码使用者需先安装 Python 3.9 或更高版本，然后双击：

```text
desktop\install_backend_deps.bat
desktop\run_standalone.bat
```

安装脚本固定使用清华 PyPI 镜像，并把虚拟环境放在项目内的 `runtime\venv`。配置和数据仍写在项目根目录的 `config`、`data` 中，复制整个项目文件夹即可继续使用。

## 开发验证

```powershell
python -m pytest -q --basetemp=.pytest-tmp
```

Windows 打包：

```text
desktop\build_windows.bat
```

输出位于 `dist\EduGate-Standalone`。打包脚本使用清华 PyPI 镜像，并把独立 Python 运行时加入输出目录。

详细说明见 [安装与故障排查](docs/安装与故障排查.txt)、[使用手册](docs/使用手册.txt)、[并发执行与课堂记录设计](docs/并发执行与课堂记录设计.md)、[验收清单](docs/单机版验收清单.txt) 和 [回归测试矩阵](docs/回归测试矩阵.md)。英文读者可从 [English README](README.en.md) 开始。
