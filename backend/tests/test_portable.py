from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import zipfile

import pytest

from app.portable import DirectoryLock, contained_directory, instance_id, portable_environment, stage_upgrade
from desktop import edugate_standalone as launcher


def test_folder_lock_and_identity_survive_copy_without_reusing_a_live_process(tmp_path):
    first = DirectoryLock(tmp_path)
    second = DirectoryLock(tmp_path)
    assert first.acquire()
    assert not second.acquire()
    identity = instance_id(tmp_path)
    assert instance_id(tmp_path) == identity
    first.release()
    assert second.acquire()
    second.release()


def test_portable_environment_ignores_machine_credentials_and_old_absolute_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("UPSTREAM_API_KEY", "another-teachers-key")
    monkeypatch.setenv("PLATFORM_API_KEY", "another-platform")
    monkeypatch.setenv("EDUGATE_DATA_DIR", str(tmp_path / "foreign"))
    monkeypatch.setenv("KNOWLEDGE_MODEL_DIR", str(tmp_path / "foreign-model"))
    config = {"UPSTREAM_PROVIDER": "My teacher", "KNOWLEDGE_DB_PATH": "E:/old-teacher/knowledge.sqlite3", "ADMIN_PASSWORD": "portable-password"}
    result = portable_environment(config, app_dir=tmp_path, data_dir=tmp_path / "data", config_path=tmp_path / "config/edugate.env", frontend_dir=tmp_path / "frontend")
    assert "UPSTREAM_API_KEY" not in result
    assert "PLATFORM_API_KEY" not in result
    assert result["KNOWLEDGE_DB_PATH"] == str(tmp_path / "data/knowledge.sqlite3")
    assert result["EDUGATE_DATA_DIR"] == str(tmp_path / "data")
    assert result["ADMIN_PASSWORD"] == "portable-password"
    assert result["SECRET_STORE_MODE"] == "portable"


def test_identity_healthcheck_rejects_another_teacher_and_a_stale_run(monkeypatch):
    class Reply:
        status = 200
        headers = {"X-EduGate-App": "EduGate", "X-EduGate-Instance": "teacher-b", "X-EduGate-Run": "new-run"}
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(launcher.urllib.request, "urlopen", lambda *a, **k: Reply())
    assert not launcher.healthcheck("http://127.0.0.1:8000/health", "teacher-a", "new-run")
    assert not launcher.healthcheck("http://127.0.0.1:8000/health", "teacher-b", "old-run")
    assert launcher.healthcheck("http://127.0.0.1:8000/health", "teacher-b", "new-run")


def test_data_folder_must_not_point_outside_the_portable_root(tmp_path):
    with pytest.raises(ValueError):
        contained_directory(tmp_path / "teacher-a", tmp_path / "teacher-b/data")


def archive_fixture(path: Path, extra=None, corrupt=False):
    files = {"EduGate-Standalone.exe": b"test executable", "_internal/runtime.txt": b"runtime", **(extra or {})}
    manifest = {"product": "EduGate-Standalone", "format": 1, "platform": "windows-amd64", "version": "test",
        "files": {name: {"sha256": hashlib.sha256(value).hexdigest(), "size": len(value)} for name, value in files.items()}}
    if corrupt:
        manifest["files"]["EduGate-Standalone.exe"]["sha256"] = "0" * 64
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("package.json", json.dumps(manifest))
        for name, value in files.items(): archive.writestr(name, value)
    return manifest


def test_complete_upgrade_package_is_staged_without_touching_teacher_data(tmp_path):
    original = tmp_path / "data"
    original.mkdir()
    (original / "secrets.json").write_text("my keys", encoding="utf-8")
    package = tmp_path / "release.zip"
    archive_fixture(package)
    staged = tmp_path / "staged"
    result = stage_upgrade(package, staged)
    assert result["version"] == "test"
    assert (staged / "EduGate-Standalone.exe").read_bytes() == b"test executable"
    assert (original / "secrets.json").read_text() == "my keys"


@pytest.mark.parametrize("path", ["data/secrets.json", "config/edugate.env", "../escape.txt", "_internal/../../data/a", "_internal/a:stream", "_internal/thing."])
def test_upgrade_cannot_supply_user_data_or_escape_program_paths(tmp_path, path):
    package = tmp_path / "release.zip"
    archive_fixture(package, {path: b"forbidden"})
    with pytest.raises(ValueError):
        stage_upgrade(package, tmp_path / "staged")
    assert not (tmp_path / "staged").exists()


def test_upgrade_rejects_modified_executable(tmp_path):
    package = tmp_path / "release.zip"
    archive_fixture(package, corrupt=True)
    with pytest.raises(ValueError, match="校验失败"):
        stage_upgrade(package, tmp_path / "staged")


def test_portable_browser_does_not_restore_a_previous_teachers_knowledge_selection():
    page = (Path(__file__).resolve().parents[2] / "frontend/admin.html").read_text(encoding="utf-8")
    assert "restoreRememberedMountedSource" not in page
    assert "edugate-mounted-source:" not in page
    assert "saved.instanceId !== state.instanceId" in page
    assert "saved.runId !== state.runId" in page


def test_shutdown_preparation_failure_leaves_service_available():
    from app.system_control import SystemControl
    control = SystemControl()
    def fail(action): raise RuntimeError("helper is blocked")
    control.bind(lambda action: None, fail)
    with pytest.raises(RuntimeError): control.request("shutdown")
    assert control.pending_action is None
    assert control.supervised
    control.finish_shutdown("disk unavailable")
    assert not control.shutdown_complete


def test_portable_embedding_assets_can_move_and_load_without_downloading(tmp_path, monkeypatch):
    """Exercise the offline model contract, not embedding quality (covered by EXE acceptance)."""
    import sys
    import shutil
    from types import SimpleNamespace
    from app.knowledge_retrieval import LocalEmbeddings, MODEL_FILES, MODEL_REVISION, DIMENSIONS
    original = tmp_path / "usb-e" / "model"
    snapshot = original / "snapshot"
    snapshot.mkdir(parents=True)
    for filename in MODEL_FILES: (snapshot / filename).write_bytes(("fixture-" + filename).encode())
    loaded = []
    class Vector:
        def tolist(self): return [0.25] * DIMENSIONS
    class Model:
        def __init__(self, name, **options):
            assert options["local_files_only"] is True
            loaded.append(options["specific_model_path"])
        def query_embed(self, text): return iter([Vector()])
        def passage_embed(self, texts, **options): return iter(Vector() for _ in texts)
    def download(repository, **options):
        assert options["revision"] == MODEL_REVISION
        assert options["token"] is False
        return str(snapshot)
    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=Model))
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=download))
    prepared = LocalEmbeddings(original)
    prepared.prepare()
    manifest = json.loads((original / "model.json").read_text())
    assert manifest["relative_path"] == "snapshot"
    assert set(manifest["files"]) == set(MODEL_FILES)
    moved = tmp_path / "usb-f" / "model"
    shutil.copytree(original, moved)
    def forbidden(*args, **kwargs): raise AssertionError("Normal classroom startup must not download")
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=forbidden))
    model = LocalEmbeddings(moved)
    assert model.load()
    assert Path(loaded[-1]).is_relative_to(moved)
    assert model.signature == prepared.signature
    assert len(model.query("迁移后的中文课堂问题" * 80)) == DIMENSIONS
    assert len(model.passages(["教材片段"] * 17)) == 17
    # Loading the same model twice must reuse its in-memory runtime.
    count = len(loaded)
    assert model.load() and len(loaded) == count
    for number in range(258): assert model.query(str(number))
    assert len(model._query_cache) <= 256


@pytest.mark.parametrize("damage", ["outside", "model-name", "weights", "tokenizer", "missing-weights", "missing-manifest", "disabled", "asset-list"])
def test_portable_embedding_corruption_fails_closed_without_remote_fetch(tmp_path, monkeypatch, damage):
    import sys
    from types import SimpleNamespace
    from app.knowledge_retrieval import LocalEmbeddings, MODEL_FILES, MODEL_NAME
    for filename in MODEL_FILES: (tmp_path / filename).write_bytes(b"model fixture")
    digest = hashlib.sha256(b"model fixture").hexdigest()
    manifest = {"model": MODEL_NAME, "relative_path": ".", "sha256": digest, "files": {name: digest for name in MODEL_FILES}}
    if damage == "outside": manifest["relative_path"] = ".."
    if damage == "model-name": manifest["model"] = "another-model"
    if damage == "weights": (tmp_path / "model_optimized.onnx").write_bytes(b"corrupted")
    if damage == "tokenizer": (tmp_path / "tokenizer.json").write_bytes(b"corrupted")
    if damage == "asset-list": manifest["files"].pop("tokenizer.json")
    if damage == "missing-weights": (tmp_path / "model_optimized.onnx").unlink()
    if damage != "missing-manifest": (tmp_path / "model.json").write_text(json.dumps(manifest))
    def forbidden(*args, **kwargs): raise AssertionError("Must not load/download invalid model assets")
    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=forbidden))
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=forbidden))
    model = LocalEmbeddings(tmp_path, enabled=damage != "disabled")
    assert not model.load()
    assert not model.signature
    assert model.query("test") is None
    assert model.passages(["test"]) == []
