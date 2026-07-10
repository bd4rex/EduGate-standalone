from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path


_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="edugate-tests-"))
atexit.register(shutil.rmtree, _TEST_DATA_DIR, ignore_errors=True)

os.environ["EDUGATE_DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["EDUGATE_FRONTEND_DIR"] = str(Path(__file__).resolve().parents[2] / "frontend")
os.environ["EDUGATE_MODE"] = "standalone"
os.environ["UPSTREAM_BASE_URL"] = ""
os.environ["UPSTREAM_API_KEY"] = ""
os.environ["PLATFORM_API_KEY"] = ""
os.environ["PYTHON_RUNNER_ENABLED"] = "false"
os.environ["CORS_ORIGINS"] = ""
