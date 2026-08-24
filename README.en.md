<p align="center">
  <img src="frontend/assets/brand/edugate-logo-horizontal.svg" width="680" alt="EduGate - Local AI Teaching Gateway" />
</p>

# EduGate Standalone Classroom

<p align="center">
  <a href="README.md">中文</a> · <strong>English</strong>
</p>

EduGate runs on a teacher's 64-bit Windows computer or Apple Silicon Mac for a single teacher and a local classroom. The teacher configures model providers, knowledge sources, and the active classroom policy in a web console. Students join through a stable LAN classroom link.

It can also act as a classroom AI relay: trusted backend clients use an OpenAI-compatible API, while teaching webpages use a scoped student-session streaming API.

## Security model

- There is one teacher administrator; multi-teacher account and policy APIs are not part of the standalone edition.
- A classroom token is exchanged for an independent student session. Student clients never receive the teacher token.
- `ALLOW_LAN_ADMIN=false` by default: the teacher page, login, API docs, and every management API are limited to the host computer. A management tablet requires LAN administration plus an exact `ADMIN_ALLOWED_IPS` entry.
- The classroom token persists across classes and restarts. Ending a class revokes current student sessions and pauses the link; starting again reuses the same link. Only an explicit rotation permanently invalidates it.
- Classroom records, configuration, and knowledge files stay on the teacher computer.
- Backend integrations use a separate platform key. Browser code exchanges the classroom token for a student session and must never contain the platform key.
- Teachers can upload an HTML file or a ZIP containing `index.html` and publish it as the student entry. The bundled IP worksheet is now explicitly a Demo page; neither Demo nor published pages can use classroom services before class starts.

## Downstream integrations

The System page exposes copyable integration details:

- Server-side clients: `GET /v1/models` and `POST /v1/chat/completions`, with Base URL `http://teacher-ip:port/v1`, virtual model `edugate`, and `Authorization: Bearer <platform key>`.
- Teaching webpages: load `assets/edugate-client.js`, exchange the persistent classroom token through `/classroom/join`, then consume `/chat/stream` using the resulting student token.
- Hosted teaching pages: upload HTML/ZIP in the final System panel. EduGate switches the student link and QR code to the active page and injects a sandboxed `EduGate.ask()` streaming bridge without exposing the classroom token, teacher session, or platform key to the uploaded page.

The stable `edugate` model follows the teacher's current upstream model, prompt, and knowledge-base policy. Cross-origin teaching pages require an exact `CORS_ORIGINS` entry and a service restart. The platform key is server-side only; the classroom token embedded in a page grants student access and is revoked by rotating the classroom link.

## Run the packaged edition

On Windows, keep the complete `EduGate-Standalone` folder together and launch `EduGate-Standalone.exe`. On an Apple Silicon Mac running macOS 11 or later, extract the ZIP and open `EduGate.app`; because this build is ad-hoc signed and not notarized with an Apple Developer ID, Control-click the app and choose Open on first launch. The current macOS package does not support Intel Macs.

Add a model provider, configure the default classroom, and optionally publish a teaching webpage from the System page. Start class before distributing the stable student link or built-in QR code. The same token can be reused until the teacher explicitly rotates it; ending class makes both the hosted page and Demo unavailable.

Windows stores portable data beside the executable. macOS stores configuration, model credentials, knowledge files, records, and logs in `~/Library/Application Support/EduGate`, so replacing `EduGate.app` does not remove classroom data.

To support local backup and migration, macOS stores model credentials in a portable format and restricts the data directory to the current user. Do not share the complete EduGate data directory or upload it to a public drive or repository.

An IP allowlist is network admission control, not a replacement for authentication. A LAN management device must still sign in and use an `X-Admin-Token`; reserve its DHCP address and use this feature only on a trusted, isolated network without exposing plain HTTP beyond it.

The packaged edition includes its own runtime. The source edition requires 64-bit Python 3.10 or newer. Windows can use:

```text
desktop\install_backend_deps.bat
desktop\run_standalone.bat
```

macOS can use:

```bash
python3 -m venv runtime/venv
runtime/venv/bin/python -m pip install -r backend/requirements.txt
runtime/venv/bin/python desktop/edugate_standalone.py
```

## Development

```powershell
python -m pytest -q --basetemp=.pytest-tmp
python -m compileall -q backend\app
```

The backend is split into application composition (`main.py`), shared state, dependencies, chat services, a published-page store, schemas, runtime configuration, and domain routers. See the [Chinese documentation index](docs/README.md) and the [teaching page publishing guide](docs/教学网页发布.md) for the maintained operations, security, and development guides.

Build the Windows folder with `desktop\build_windows.bat`. On Apple Silicon, run `EDUGATE_VERSION=2.1.0 desktop/build_macos.sh` to produce the signed `.app` and release ZIP.
