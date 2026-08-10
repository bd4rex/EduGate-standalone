from __future__ import annotations

import logging
import zipfile
from pathlib import Path

import pytest

from desktop import edugate_standalone as launcher


def _configure_launcher_paths(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> Path:
    archive = data_dir / "pending-restore.zip"
    config_path = data_dir.parent / "config" / "edugate.env"
    monkeypatch.setattr(launcher, "DATA_DIR", data_dir)
    monkeypatch.setattr(launcher, "CONFIG_PATH", config_path)
    monkeypatch.setattr(launcher, "RESTORE_ARCHIVE", archive)
    return archive


def test_pending_restore_replaces_configuration_and_knowledge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _configure_launcher_paths(monkeypatch, tmp_path)
    knowledge = tmp_path / "knowledge_files"
    knowledge.mkdir()
    (knowledge / "old.txt").write_text("old", encoding="utf-8")
    with zipfile.ZipFile(archive, "w") as backup:
        backup.writestr(".env", "DEFAULT_MODEL=restored\n")
        backup.writestr("knowledge_files/new.txt", "new")

    launcher.apply_pending_restore(logging.getLogger("test-launcher"))

    assert (tmp_path.parent / "config" / "edugate.env").read_text(encoding="utf-8") == "DEFAULT_MODEL=restored\n"
    assert not (knowledge / "old.txt").exists()
    assert (knowledge / "new.txt").read_text(encoding="utf-8") == "new"
    assert not archive.exists()


def test_pending_restore_rejects_path_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _configure_launcher_paths(monkeypatch, tmp_path)
    with zipfile.ZipFile(archive, "w") as backup:
        backup.writestr("../outside.txt", "unsafe")

    with pytest.raises(ValueError, match="Unsafe backup path"):
        launcher.apply_pending_restore(logging.getLogger("test-launcher"))

    assert not (tmp_path.parent / "outside.txt").exists()
    assert archive.exists()


def test_healthcheck_requires_edugate_identity_header(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status = 200

        def __init__(self, marker: str | None) -> None:
            self.headers = {"X-EduGate-App": marker} if marker else {}

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

    monkeypatch.setattr(launcher.urllib.request, "urlopen", lambda *args, **kwargs: FakeResponse(None))
    assert launcher.healthcheck("http://127.0.0.1:8000/health") is False

    monkeypatch.setattr(
        launcher.urllib.request,
        "urlopen",
        lambda *args, **kwargs: FakeResponse("EduGate"),
    )
    assert launcher.healthcheck("http://127.0.0.1:8000/health") is True


def test_legacy_data_is_imported_into_portable_folder_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "portable" / "data"
    config_path = tmp_path / "portable" / "config" / "edugate.env"
    legacy_dir = tmp_path / "legacy"
    data_dir.mkdir(parents=True)
    config_path.parent.mkdir(parents=True)
    (legacy_dir / "knowledge_files").mkdir(parents=True)
    (legacy_dir / "runtime_config.json").write_text('{"version": 1}', encoding="utf-8")
    (legacy_dir / "knowledge_files" / "lesson.txt").write_text("lesson", encoding="utf-8")
    (legacy_dir / ".env").write_text("DEFAULT_MODEL=legacy\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "DATA_DIR", data_dir)
    monkeypatch.setattr(launcher, "CONFIG_PATH", config_path)
    monkeypatch.setattr(launcher, "LEGACY_DATA_DIR", legacy_dir)
    monkeypatch.delenv("EDUGATE_SKIP_LEGACY_IMPORT", raising=False)

    assert launcher.migrate_legacy_data() is True
    assert (data_dir / "runtime_config.json").read_text(encoding="utf-8") == '{"version": 1}'
    assert (data_dir / "knowledge_files" / "lesson.txt").read_text(encoding="utf-8") == "lesson"
    assert config_path.read_text(encoding="utf-8") == "DEFAULT_MODEL=legacy\n"
    assert launcher.migrate_legacy_data() is False


def test_first_available_port_skips_an_occupied_port() -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        assert launcher.first_available_port(port, attempts=2) == port + 1
