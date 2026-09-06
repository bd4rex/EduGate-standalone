from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TEST_DATA_DIR = _PROJECT_ROOT / f".pytest-runtime-{uuid.uuid4().hex}"
_TEST_DATA_DIR.mkdir()
atexit.register(shutil.rmtree, _TEST_DATA_DIR, ignore_errors=True)

_SYSTEM_TEMP_DIR = _TEST_DATA_DIR / "system-temp"
_SYSTEM_TEMP_DIR.mkdir()
os.environ["TEMP"] = str(_SYSTEM_TEMP_DIR)
os.environ["TMP"] = str(_SYSTEM_TEMP_DIR)


def _sandbox_safe_mkdtemp(
    suffix: str | None = None,
    prefix: str | None = None,
    dir: str | os.PathLike[str] | None = None,
) -> str:
    """Create test temp directories without Python 3.12's Windows 0o700 ACL."""

    parent = Path(dir) if dir is not None else _SYSTEM_TEMP_DIR
    name = f"{prefix or 'tmp'}{uuid.uuid4().hex}{suffix or ''}"
    path = parent / name
    path.mkdir()
    return str(path)


# tempfile.TemporaryDirectory resolves mkdtemp at runtime.  Replacing it in
# the test process also covers backup and Python-runner integration paths.
tempfile.tempdir = str(_SYSTEM_TEMP_DIR)
tempfile.mkdtemp = _sandbox_safe_mkdtemp

os.environ["EDUGATE_DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["EDUGATE_FRONTEND_DIR"] = str(_PROJECT_ROOT / "frontend")
os.environ["EDUGATE_MODE"] = "standalone"
os.environ["EDUGATE_PORTABLE_MODE"] = "false"
os.environ["PORTABLE_AUTO_LOGIN"] = "false"
os.environ["SECRET_STORE_MODE"] = "dpapi" if os.name == "nt" else "portable"
os.environ["UPSTREAM_BASE_URL"] = ""
os.environ["UPSTREAM_API_KEY"] = ""
os.environ["PLATFORM_API_KEY"] = ""
os.environ["PYTHON_RUNNER_ENABLED"] = "false"
os.environ["CORS_ORIGINS"] = ""
os.environ["ALLOW_LAN_ADMIN"] = "true"


@pytest.fixture
def tmp_path() -> Path:
    """Provide Windows-sandbox-safe, per-test temporary storage.

    Python 3.12 applies a restrictive ACL when pytest creates its built-in
    temporary directories with mode 0o700.  In Windows sandboxed CI the
    process token can then lose access to the directory it just created.
    Directories created with Path.mkdir's portable default do not have that
    issue and retain the same per-test isolation contract.
    """

    path = _TEST_DATA_DIR / "cases" / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
