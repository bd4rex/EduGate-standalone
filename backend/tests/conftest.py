from __future__ import annotations

import atexit
import os
import shutil
import uuid
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TEST_DATA_DIR = _PROJECT_ROOT / f".pytest-runtime-{uuid.uuid4().hex}"
_TEST_DATA_DIR.mkdir()
atexit.register(shutil.rmtree, _TEST_DATA_DIR, ignore_errors=True)

os.environ["EDUGATE_DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["EDUGATE_FRONTEND_DIR"] = str(_PROJECT_ROOT / "frontend")
os.environ["EDUGATE_MODE"] = "standalone"
os.environ["EDUGATE_PORTABLE_MODE"] = "false"
os.environ["PORTABLE_AUTO_LOGIN"] = "false"
os.environ["SECRET_STORE_MODE"] = "dpapi"
os.environ["UPSTREAM_BASE_URL"] = ""
os.environ["UPSTREAM_API_KEY"] = ""
os.environ["PLATFORM_API_KEY"] = ""
os.environ["PYTHON_RUNNER_ENABLED"] = "false"
os.environ["CORS_ORIGINS"] = ""
