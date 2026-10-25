"""Prepare the pinned Chinese embedding model for source use or desktop builds."""
from __future__ import annotations

import argparse
import json
import sys
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.knowledge_retrieval import LocalEmbeddings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "data" / "knowledge_models")
    parser.add_argument("--check", action="store_true", help="Validate existing files without downloading")
    parser.add_argument("--export", type=Path, help="Write a flat, self-contained model directory for packaging")
    args = parser.parse_args()
    model = LocalEmbeddings(args.directory)
    if not args.check:
        model.prepare()
    if not model.load():
        raise SystemExit("Local knowledge model is not ready; run without --check to prepare it.")
    vector = model.query("教室里的多台电脑怎样共享一个公网地址？")
    if not vector or len(vector) != 512:
        raise SystemExit("Embedding inference did not return the expected 512 dimensions")
    if args.export:
        manifest = json.loads((args.directory / "model.json").read_text(encoding="utf-8"))
        source = (args.directory / manifest["relative_path"]).resolve()
        args.export.mkdir(parents=True, exist_ok=True)
        from app.knowledge_retrieval import MODEL_FILES
        for filename in MODEL_FILES:
            shutil.copy2(source / filename, args.export / filename)
        manifest["relative_path"] = "."
        (args.export / "model.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ready": True, "dimensions": len(vector), "signature": model.signature}, ensure_ascii=False))


if __name__ == "__main__":
    main()
