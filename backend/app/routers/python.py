"""Browser execution policy and untrusted results. Never execute code here."""
from __future__ import annotations

from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from app.core import _record_classroom_turn, require_classroom_access, settings
from app.schemas import BrowserPythonResult
from app.security import StudentIdentity

router = APIRouter()
RUNTIME_ASSETS = {"pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json"}


@router.post("/run_python", deprecated=True)
@router.post("/run_python/stream", deprecated=True)
async def retired_server_python(student: StudentIdentity = Depends(require_classroom_access)):
    raise HTTPException(status_code=410, detail="Teacher-side Python execution has been removed. Use EduGateClient.runPython() in the student browser.")


@router.get("/classroom/python-config")
async def browser_python_config(student: StudentIdentity = Depends(require_classroom_access)):
    if not settings.python_runner_enabled:
        raise HTTPException(status_code=403, detail="Browser Python is disabled for this classroom")
    return {
        "execution": "browser", "classroom_id": student.classroom_id,
        "timeout_seconds": settings.python_runner_timeout_seconds,
        "max_code_chars": settings.python_runner_max_code_chars,
    }


@router.post("/classroom/python-result")
async def browser_python_result(result: BrowserPythonResult, student: StudentIdentity = Depends(require_classroom_access)):
    if not settings.python_runner_enabled or result.classroom_id != student.classroom_id or (student.lease and student.lease.revoked):
        raise HTTPException(status_code=403, detail="Python result does not belong to the active classroom")
    _record_classroom_turn(
        teacher_id=settings.admin_username, student=student, kind="python",
        input_content=result.code,
        output_content="[学生浏览器上报，未经服务端验证]\n" + result.stdout + result.stderr,
        status_code=200 if result.exit_code == 0 else 422,
        latency_ms=result.duration_ms, timed_out=result.timed_out,
    )
    return {"status": "recorded", "execution": "browser"}


@router.get("/assets/vendor/pyodide/{filename}", include_in_schema=False)
async def browser_python_asset(filename: str):
    if filename not in RUNTIME_ASSETS:
        raise HTTPException(status_code=404)
    media = "application/wasm" if filename.endswith(".wasm") else "text/javascript" if filename.endswith(".mjs") else None
    return FileResponse(
        Path(settings.frontend_dir) / "assets" / "vendor" / "pyodide" / filename,
        media_type=media,
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/python-sandbox.html", include_in_schema=False)
async def browser_python_sandbox(request: Request):
    origin = str(request.base_url).rstrip("/")
    # Opaque iframe + inherited worker CSP: no teacher API or filesystem bridge.
    policy = (
        "default-src 'none'; "
        f"script-src {origin}/assets/python-sandbox.js {origin}/assets/vendor/pyodide/ 'wasm-unsafe-eval'; "
        f"connect-src {origin}/assets/vendor/pyodide/; worker-src data:; "
        "base-uri 'none'; form-action 'none'; sandbox allow-scripts"
    )
    return HTMLResponse(
        '<!doctype html><meta charset="utf-8"><title>Python 隔离运行环境</title><script src="/assets/python-sandbox.js"></script>',
        headers={"Content-Security-Policy": policy, "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"},
    )
