<p align="center">
  <img src="frontend/assets/brand/edugate-logo-horizontal.svg" width="680" alt="EduGate - Local AI Teaching Gateway" />
</p>

# EduGate Standalone Classroom

<p align="center">
  <a href="README.md">中文</a> · <strong>English</strong>
</p>

EduGate runs on a teacher's 64-bit Windows computer for a single teacher and a local classroom. The teacher configures model providers, knowledge sources, and the active classroom policy in a web console. Students join through a temporary LAN classroom link.

## Security model

- There is one teacher administrator; multi-teacher account and policy APIs are not part of the standalone edition.
- A classroom token is exchanged for an independent student session. Student clients never receive the teacher token.
- `ALLOW_LAN_ADMIN=false` by default: the teacher page, login, API docs, and every management API are limited to the host computer.
- Rotating the classroom link or ending the class invalidates the previous link and student sessions.
- Classroom records, configuration, and knowledge files stay on the teacher computer.

## Run the packaged edition

Keep the complete `EduGate-Standalone` folder together and launch `EduGate-Standalone.exe`. Add a model provider, configure the default classroom, start the class, and copy the generated student link.

The packaged edition includes its own runtime. The source edition requires 64-bit Python 3.10 or newer:

```text
desktop\install_backend_deps.bat
desktop\run_standalone.bat
```

## Development

```powershell
python -m pytest -q --basetemp=.pytest-tmp
python -m compileall -q backend\app
```

The backend is split into application composition (`main.py`), shared state, dependencies, chat services, schemas, runtime configuration, and domain routers. See the [Chinese documentation index](docs/README.md) for the maintained operations, security, and development guides.
