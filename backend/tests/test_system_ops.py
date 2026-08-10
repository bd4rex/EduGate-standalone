from __future__ import annotations

import asyncio
import sqlite3
import time
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

from app.config import settings
from app.system_control import SystemControl
from app.system_ops import create_backup, read_advanced_settings, save_restore_archive, update_advanced_settings


def test_system_control_schedules_supervisor_action() -> None:
    control = SystemControl()
    actions: list[str] = []
    assert control.request("restart") is False
    control.bind(actions.append)
    assert control.supervised is True
    assert control.request("restart") is True
    time.sleep(0.6)
    assert actions == ["restart"]
    control.unbind()
    assert control.supervised is False


def test_advanced_settings_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    config_path = tmp_path / "config" / "edugate.env"
    config_path.parent.mkdir()
    config_path.write_text("PYTHON_RUNNER_ENABLED=false\n", encoding="utf-8")
    monkeypatch.setattr(settings, "config_path", str(config_path))

    result = update_advanced_settings(
        {"PYTHON_RUNNER_ENABLED": True, "MODEL_MAX_CONCURRENCY": 6}
    )
    assert result["restart_required"] is True

    values = {item["key"]: item["value"] for item in read_advanced_settings()["settings"]}
    assert values["PYTHON_RUNNER_ENABLED"] is True
    assert values["MODEL_MAX_CONCURRENCY"] == 6


def test_backup_contains_consistent_databases_and_knowledge(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    knowledge_dir = data_dir / "knowledge_files"
    knowledge_dir.mkdir(parents=True)
    (knowledge_dir / "lesson.txt").write_text("lesson", encoding="utf-8")
    (data_dir / ".env").write_text("EDUGATE_MODE=standalone\n", encoding="utf-8")
    (data_dir / "runtime_config.json").write_text("{}", encoding="utf-8")
    for name in ("edugate.sqlite3", "knowledge.sqlite3"):
        with sqlite3.connect(data_dir / name) as conn:
            conn.execute("CREATE TABLE sample (value TEXT)")
            conn.execute("INSERT INTO sample VALUES ('ok')")

    monkeypatch.setattr(settings, "data_dir", str(data_dir))
    monkeypatch.setattr(settings, "sqlite_db_path", str(data_dir / "edugate.sqlite3"))
    monkeypatch.setattr(settings, "knowledge_db_path", str(data_dir / "knowledge.sqlite3"))
    monkeypatch.setattr(settings, "knowledge_dir", str(knowledge_dir))
    monkeypatch.setattr(settings, "config_path", str(data_dir / ".env"))

    archive = create_backup()
    try:
        with zipfile.ZipFile(archive) as backup:
            names = set(backup.namelist())
        assert "edugate.sqlite3" in names
        assert "knowledge.sqlite3" in names
        assert "knowledge_files/lesson.txt" in names
        assert "backup-info.json" in names
    finally:
        import shutil

        shutil.rmtree(archive.parent, ignore_errors=True)


def test_restore_upload_is_validated_and_staged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    content = BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("runtime_config.json", "{}")
        archive.writestr("backup-info.json", "{}")
    upload = UploadFile(filename="backup.zip", file=BytesIO(content.getvalue()))

    target = asyncio.run(save_restore_archive(upload))

    assert target == tmp_path / "pending-restore.zip"
    assert target.exists()


@pytest.mark.parametrize(
    ("entry", "detail"),
    [
        ("../outside.txt", "Unsafe backup path"),
        ("unexpected.exe", "Unsupported backup entry"),
    ],
)
def test_restore_rejects_unsafe_or_unknown_entries(
    tmp_path: Path,
    monkeypatch,
    entry: str,
    detail: str,
) -> None:
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    content = BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr(entry, "not allowed")
    upload = UploadFile(filename="backup.zip", file=BytesIO(content.getvalue()))

    with pytest.raises(HTTPException, match=detail):
        asyncio.run(save_restore_archive(upload))

    assert not (tmp_path / "pending-restore.zip").exists()
    assert not (tmp_path / "pending-restore.tmp").exists()


def test_advanced_settings_reject_unknown_and_out_of_range_values(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "config_path", str(tmp_path / "config" / "edugate.env"))

    with pytest.raises(HTTPException, match="Unsupported settings"):
        update_advanced_settings({"UNRECOGNIZED": "value"})
    with pytest.raises(HTTPException, match="MODEL_MAX_CONCURRENCY must be at most 32"):
        update_advanced_settings({"MODEL_MAX_CONCURRENCY": 64})
