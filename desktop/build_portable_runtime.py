from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


EXCLUDED_DIRS = {
    "__pycache__",
    "ensurepip",
    "idlelib",
    "site-packages",
    "test",
    "tkinter",
    "turtledemo",
    "venv",
}


def _ignore(_: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in EXCLUDED_DIRS or name.endswith((".pyc", ".pyo"))
    }


def build(target: Path) -> None:
    source = Path(sys.base_prefix).resolve()
    target = target.resolve()
    if target.name.lower() != "python" or target.parent.name.lower() != "runtime":
        raise ValueError(f"Refusing to replace unexpected runtime target: {target}")
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)

    for name in ("python.exe", "pythonw.exe", "LICENSE.txt"):
        path = source / name
        if path.exists():
            shutil.copy2(path, target / name)
    for pattern in ("python*.dll", "vcruntime*.dll"):
        for path in source.glob(pattern):
            shutil.copy2(path, target / path.name)
    for directory in ("DLLs", "Lib"):
        source_dir = source / directory
        if source_dir.exists():
            shutil.copytree(source_dir, target / directory, ignore=_ignore)

    executable = target / "python.exe"
    if not executable.exists():
        raise RuntimeError(f"Portable Python executable was not copied from {source}")
    result = subprocess.run(
        [str(executable), "-I", "-S", "-c", "import ast, json, sys; print(sys.version_info[:2])"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    )
    print(f"Portable Python ready: {result.stdout.strip()} -> {target}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: build_portable_runtime.py TARGET")
    build(Path(sys.argv[1]))
