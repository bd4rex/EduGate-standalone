<p align="center">
  <img src="frontend/assets/brand/edugate-logo-horizontal.svg" width="680" alt="EduGate - Local AI Teaching Gateway" />
</p>

# EduGate Standalone Classroom Edition

<p align="center">
  <a href="README.md">简体中文</a> · <strong>English</strong>
</p>

EduGate Standalone runs entirely on a teacher's Windows computer. It is designed for one teacher, one class session, or a small classroom rather than a school-wide deployment. EduGate connects to OpenAI-compatible model APIs upstream and provides students with a classroom-token-protected web page and API downstream.

## First-time setup on Windows

1. Install Python 3.9 or later and select `Add python.exe to PATH`.
2. Double-click `desktop\install_backend_deps.bat`. The installer always uses the Tsinghua PyPI mirror and creates `%LOCALAPPDATA%\EduGate\venv`; it does not modify the system Python environment.
3. Double-click `desktop\run_standalone.bat`.
4. Create the administrator password in the teacher web console that opens automatically. There is no default password.
5. Open **Resources -> Models and providers**, enter the model ID, base URL, and API key, save the configuration, and select **Test connection**.

For later classes, only `desktop\run_standalone.bat` is needed. The teacher console, student page, and API are served from the same port, `8000` by default. No separate web server or CORS setup is required.

EduGate does not open a separate Windows control window. The launcher runs a supervised background process and opens the teacher console in the browser. Classroom controls, resources, logs, statistics, backup and restore, advanced settings, restart, and shutdown are all available from the web console.

The installer handles a known Python 3.9 virtual-environment problem in Windows profile paths containing spaces. Startup diagnostics are written to `%LOCALAPPDATA%\EduGate\launcher.log`.

## Classroom workflow

1. Sign in to the teacher console and select the model, knowledge base, and classroom policy.
2. Copy the complete **Student classroom link** and share it with the class.
3. The temporary classroom token in the link is exchanged for an anonymous student session. Each student therefore receives an independent rate-limit identity even when the class shares one proxy IP.
4. Select **Generate a new link** whenever all previously shared links must be revoked.

The student page stores the latest 50 messages for the current classroom in that browser. A page refresh can therefore continue the conversation, while only the latest 10 messages are sent to the model to keep the request bounded. A newly generated classroom link uses a separate history scope.

The **System** view provides runtime status and logs, full backup download, restore, advanced configuration, restart, and shutdown. After shutdown, the web page becomes unavailable until the launcher is started again.

The **Records** view groups anonymous student AI conversations and Python results by classroom. Teachers can filter by anonymous student or activity type and permanently delete a class record. Content remains on the teacher computer, never stores student IP addresses, and is retained for 30 days by default. Administrators can disable recording or change the retention period in advanced settings.

API keys are encrypted with Windows DPAPI for the current Windows user. Runtime data is stored in:

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

Keep this directory when uninstalling if classroom data should be retained. `secrets.json` cannot be decrypted after it is copied to another computer, so model API keys must be entered again on the new machine.

## Secure defaults

- The first administrator can only be created from the teacher computer, and passwords must contain at least 10 characters.
- Teacher sessions expire after eight hours by default. Signing out, changing a password, or disabling an account revokes the session immediately.
- `/chat`, `/chat/stream`, and `/run_python` require an anonymous student session or the backward-compatible temporary classroom token and are rate-limited per student.
- `/v1/chat/completions` is disabled until `PLATFORM_API_KEY` is configured.
- The Python runner is disabled by default. When enabled, it uses four isolated execution slots and a bounded queue of 64 tasks. `/run_python/stream` emits queued, running, output, and completion events in real time. Every task still applies syntax, timeout, and memory limits and uses a minimal environment that excludes model credentials.
- Uploads are limited to 25 MB and PDF documents to 200 pages by default.
- Technical request logs omit teacher and student message bodies by default and retain at most 5,000 entries. The separate local classroom-content history is retained for 30 days by default and can be disabled or deleted per class.

## Development and verification

```powershell
python -m pytest -q --basetemp=.pytest-tmp
```

Build the Windows bundle with:

```text
desktop\build_windows.bat
```

The output is written to `dist\EduGate-Standalone`. The build script also uses the Tsinghua PyPI mirror.

See the [execution and classroom records design](docs/Execution-and-Classroom-Records-Design.md) and [English regression test matrix](docs/Regression-Test-Matrix.md). The detailed [installation guide](docs/安装与故障排查.txt), [user guide](docs/使用手册.txt), and [acceptance checklist](docs/单机版验收清单.txt) are currently maintained in Chinese. Return to the [Chinese project documentation](README.md) at any time.
