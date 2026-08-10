<p align="center">
  <img src="frontend/assets/brand/edugate-logo-horizontal.svg" width="680" alt="EduGate - Local AI Teaching Gateway" />
</p>

# EduGate Standalone Classroom Edition

<p align="center">
  <a href="README.md">简体中文</a> · <strong>English</strong>
</p>

EduGate Standalone runs on a teacher's 64-bit Windows computer for one teacher and a single class or small classroom. It connects to OpenAI-compatible model APIs upstream and serves a classroom-token-protected student page and API downstream. The teacher uses one complete web console; there is no separate Windows control window.

## Teacher quick start

1. Extract or copy the complete `EduGate-Standalone` folder. Do not copy only the EXE.
2. Double-click `EduGate-Standalone.exe`. The browser opens the teacher console and signs in locally.
3. Open **System -> Upstream Model API Management**, enter a source name, API key, API URL, and optional path, then select **Fetch**. Search the modal, edit display names, and batch-import the checked models.
4. Select **Start class**, then copy the student link from the bottom of **Control** and share it with students.
5. Select **End class** after the lesson. Student links and sessions are revoked while the teacher console stays online. Use **System -> Stop service** only when exiting EduGate.

The bundle includes independent Python runtimes for EduGate and student code execution. A teacher computer does not need a preinstalled Python or an online dependency installation. If port `8000` is busy, the launcher selects a nearby available port and opens the correct page.

Copy the entire folder to another Windows computer or removable drive to retain models, knowledge, policies, credentials, and classroom records:

```text
EduGate-Standalone\
  EduGate-Standalone.exe
  README.txt
  config\edugate.env       # startup settings and initial password
  data\                     # databases, knowledge, credentials, logs
  runtime\python\           # isolated student-code interpreter
  _internal\                # EduGate application resources
```

The first run creates `config` and `data`. The portable administrator is `admin`, with initial password `edugate` stored in `config\edugate.env`. The local browser signs in automatically, so the password is normally only a fallback. Student devices connecting over the LAN cannot call the local automatic-login endpoint.

Portable `data\secrets.json` is deliberately not tied to a Windows account so API keys survive copying the folder. Keys are not echoed in the web UI, but anyone with access to the folder can read its local credentials.

## Classroom workflow

Start and end controls sit beside the student entry at the bottom of Control. Starting creates a fresh student link; ending revokes that link and all student sessions without stopping the teacher console. Students join silently without entering a name, computer name, or seat label. The browser keeps a stable local device ID; the teacher service derives a device label from it and records the request IP. Sessions and rate limits remain independent even behind a shared proxy IP. The student page keeps the latest 50 local messages per classroom and sends only the latest 10 to the model. Generating a new classroom link revokes old links and sessions.

The **Records** view groups AI conversations and Python results by the generated device label and IP. Recording is silent and requires no student participation. These records remain on the teacher computer and are retained for 30 days by default. The **System** view provides status, a copyable LAN address, direct access to the program folder, logs, backup and restore, collapsed-by-default advanced settings, restart, and shutdown.

The **Resources** view puts the knowledge-base list first. Each source can be edited, opened in the teacher computer's file explorer, and incrementally synchronized from that folder. A scan adds new supported files, reindexes changed files, and removes indexes for files deleted from the folder. The default `general` source cannot be deleted; other sources can be removed when no classroom policy uses them. The lower-frequency create/edit form is collapsed by default.

The Python runner defaults to four isolated execution slots with a bounded queue of 64 tasks. `/run_python/stream` emits queued, running, output, and completion events. This is a controlled task queue, not a cache: every task receives a fresh restricted subprocess and shares no student interpreter state. Advanced settings allow up to eight slots.

## Running from source

Source users install Python 3.9 or later, then run:

```text
desktop\install_backend_deps.bat
desktop\run_standalone.bat
```

The installer always uses the Tsinghua PyPI mirror and creates `runtime\venv` inside the project. Source-mode configuration and data also stay in the project's `config` and `data` directories.

## Development and verification

```powershell
python -m pytest -q --basetemp=.pytest-tmp
```

Build the Windows bundle with `desktop\build_windows.bat`. Output is written to `dist\EduGate-Standalone`; the build uses the Tsinghua mirror and adds the standalone student-code Python runtime.

See the [execution and classroom records design](docs/Execution-and-Classroom-Records-Design.md), [model concurrency benchmark](docs/Model-Concurrency-Benchmark-2026-08-10.md), [double-load test report](docs/Double-Load-Test-Report-2026-08-10.md), and [English regression test matrix](docs/Regression-Test-Matrix.md). The detailed [installation guide](docs/安装与故障排查.txt), [user guide](docs/使用手册.txt), and [acceptance checklist](docs/单机版验收清单.txt) are maintained primarily in Chinese. Return to the [Chinese project documentation](README.md) at any time.
