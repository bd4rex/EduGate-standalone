"""Build into a fresh release directory; never clean a teacher's portable data."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.portable import package_manifest, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="2.4.0")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,60}", args.version):
        raise SystemExit("Invalid version")
    destination = ROOT / "dist" / "releases" / f"{args.version}-{datetime.now():%Y%m%d-%H%M%S}"
    destination.mkdir(parents=True, exist_ok=False)
    work = ROOT / "build" / destination.name
    env = {**os.environ, "EDUGATE_VERSION": args.version}
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(destination),
        "--workpath", str(work), str(ROOT / "desktop" / "edugate_standalone.spec")], cwd=ROOT, env=env, check=True)
    bundle = destination / "EduGate-Standalone"
    shutil.copyfile(ROOT / "desktop" / "PORTABLE-README.txt", bundle / "README.txt")
    manifest = package_manifest(bundle, args.version)
    write_json(bundle / "package.json", manifest)
    archive = destination / f"EduGate-Standalone-{args.version}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for relative in [*manifest["files"], "package.json"]:
            output.write(bundle / relative, relative)
    result = {"directory": str(bundle), "archive": str(archive), "version": args.version,
              "file_count": len(manifest["files"]), "uncompressed_bytes": sum(f["size"] for f in manifest["files"].values())}
    write_json(destination / "release.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
