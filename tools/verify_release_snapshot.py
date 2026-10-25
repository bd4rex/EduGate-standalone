"""Verify an archived runtime-source snapshot without executing archived code.

Requires Python 3.12 and PyInstaller. Filenames and line-number debug metadata
are excluded from code fingerprints; instructions, constants and exception
tables are retained. This verifies runtime code, not the lost original comments,
historical tests, build scripts, or the complete historical Git checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import marshal
from pathlib import Path
import sys
import tempfile
import types
import zipfile

from PyInstaller.archive.readers import CArchiveReader


def canonical(value):
    if isinstance(value, types.CodeType):
        return ["code", value.co_name, value.co_qualname, value.co_argcount,
                value.co_posonlyargcount, value.co_kwonlyargcount, value.co_nlocals,
                value.co_stacksize, value.co_flags, value.co_code.hex(),
                canonical(value.co_consts), canonical(value.co_names),
                canonical(value.co_varnames), canonical(value.co_freevars),
                canonical(value.co_cellvars), value.co_exceptiontable.hex()]
    if isinstance(value, tuple):
        return ["tuple", [canonical(item) for item in value]]
    if isinstance(value, frozenset):
        return ["frozenset", sorted([canonical(item) for item in value], key=lambda item: json.dumps(item, sort_keys=True))]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    return [type(value).__name__, repr(value)]


def fingerprint(code):
    return hashlib.sha256(json.dumps(canonical(code), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def contained(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Source path escaped the snapshot")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=Path("."))
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Use Python 3.12 to compare this release's bytecode format")
    metadata = json.loads((args.source / "ARCHIVE.json").read_text(encoding="utf-8"))
    assert digest(args.archive) == metadata["package"]["sha256"], "Release ZIP checksum mismatch"
    with tempfile.TemporaryDirectory(prefix="edugate-source-proof-") as temporary, zipfile.ZipFile(args.archive) as package:
        executable = Path(temporary) / "original.exe"
        executable.write_bytes(package.read("EduGate-Standalone.exe"))
        reader = CArchiveReader(str(executable))
        pyz = reader.open_embedded_archive(next(name for name in reader.toc if name.endswith(".pyz")))
        for module, record in metadata["application_modules"].items():
            original = fingerprint(pyz.extract(module))
            source = contained(args.source, record["source_file"])
            rebuilt = fingerprint(compile(source.read_bytes(), record["source_file"], "exec", dont_inherit=True, optimize=0))
            assert original == rebuilt == record["fingerprint"], module
        launcher = metadata["launcher"]
        original = fingerprint(marshal.loads(reader.extract("edugate_standalone")))
        rebuilt = fingerprint(compile(contained(args.source, launcher["source_file"]).read_bytes(), launcher["source_file"], "exec", dont_inherit=True, optimize=0))
        assert original == rebuilt == launcher["fingerprint"], "Launcher source mismatch"
        for relative, record in metadata["exact_release_files"].items():
            original = hashlib.sha256(package.read(record["archive_path"])).hexdigest()
            assert original == digest(contained(args.source, relative)) == record["sha256"], relative
    print(json.dumps({"version": metadata["version"], "application_modules": len(metadata["application_modules"]),
                      "launcher_verified": True, "exact_release_files": len(metadata["exact_release_files"]),
                      "status": "verified"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
