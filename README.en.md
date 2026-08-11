<p align="center">
  <img src="frontend/assets/brand/edugate-logo-horizontal.svg" width="680" alt="EduGate - Local AI Teaching Gateway" />
</p>

# EduGate Standalone Classroom

<p align="center">
  <a href="README.md">中文</a> · <strong>English</strong>
</p>

EduGate runs on a teacher's 64-bit Windows computer for a single teacher and a local classroom. The teacher configures model providers, knowledge sources, and the active classroom policy in a web console. Students join through a stable LAN classroom link.

## Security model

- There is one teacher administrator; multi-teacher account and policy APIs are not part of the standalone edition.
- A classroom token is exchanged for an independent student session. Student clients never receive the teacher token.
- `ALLOW_LAN_ADMIN=false` by default: the teacher page, login, API docs, and every management API are limited to the host computer. A management tablet requires LAN administration plus an exact `ADMIN_ALLOWED_IPS` entry.
- The classroom token persists across classes and restarts. Ending a class revokes current student sessions and pauses the link; starting again reuses the same link. Only an explicit rotation permanently invalidates it.
- Classroom records, configuration, and knowledge files stay on the teacher computer.

## Run the packaged edition

Keep the complete `EduGate-Standalone` folder together and launch `EduGate-Standalone.exe`. Add a model provider, configure the default classroom, and copy the stable student link. The same link can be embedded in teaching material and reused until the teacher explicitly rotates it.

An IP allowlist is network admission control, not a replacement for authentication. A LAN management device must still sign in and use an `X-Admin-Token`; reserve its DHCP address and use this feature only on a trusted, isolated network without exposing plain HTTP beyond it.

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
