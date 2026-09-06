from __future__ import annotations

import asyncio
import io
import os
import shutil
import socket
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile

from app.knowledge import KnowledgeStore
from app.network import advertised_ip, local_interfaces
from app.secret_store import SecretStore
from app.security import ClassroomAccess, StudentIdentity
from app.classroom_tasks import run_in_classroom, stream_in_classroom
from app.system_ops import _coerce_value, SETTINGS_SCHEMA


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("delete_source", [False, True])
def test_copy_and_legacy_restore_never_delete_original_files(tmp_path, legacy, delete_source):
    original = tmp_path / "A"
    copied = tmp_path / "B"
    store = KnowledgeStore(str(original / "knowledge.sqlite3"), str(original / "knowledge_files"))
    store.upsert_source("lesson", "Lesson")
    folder = store.source_directory("lesson") / "nested" / "lesson"
    folder.mkdir(parents=True)
    fixture = folder / "example.txt"
    fixture.write_text("planets and stars", encoding="utf-8")
    assert store.scan_source("lesson").added == 1
    file = store.list_files()[0]
    if legacy:
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("UPDATE files SET path = ? WHERE id = ?", (str(fixture), file.id))
    store.checkpoint()
    shutil.copytree(original, copied)
    restored = KnowledgeStore(str(copied / "knowledge.sqlite3"), str(copied / "knowledge_files"))
    assert restored.scan_source("lesson").unchanged == 1
    with sqlite3.connect(restored.db_path) as conn:
        assert conn.execute("SELECT path FROM files").fetchone()[0] == "lesson/nested/lesson/example.txt"
    if delete_source:
        restored.delete_source("lesson")
    else:
        restored.delete_file(file.id)
    assert fixture.read_text(encoding="utf-8") == "planets and stars"
    assert not (copied / "knowledge_files/lesson/nested/lesson/example.txt").exists()


@pytest.mark.parametrize("path", ["../outside.txt", "general/../../outside.txt", "C:/outside.txt", "other/file.txt"])
def test_knowledge_delete_rejects_unsafe_stored_paths(tmp_path, path):
    store = KnowledgeStore(str(tmp_path / "kb.sqlite3"), str(tmp_path / "files"))
    item = asyncio.run(store.add_file("general", UploadFile(filename="fixture.txt", file=io.BytesIO(b"safe fixture"))))
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE files SET path = ? WHERE id = ?", (path, item.id))
    with pytest.raises(HTTPException):
        store.delete_file(item.id)
    assert (tmp_path / "files/general/fixture.txt").is_file()
    assert store.get_file(item.id).id == item.id


@pytest.mark.parametrize("mode", ["portable", pytest.param("dpapi", marks=pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only"))])
def test_disabled_secret_override_survives_reload_and_copy(tmp_path, mode):
    store = SecretStore(str(tmp_path / "secrets.json"), mode=mode)
    assert store.get("platform", default="env-key") == "env-key"
    store.set("platform", "")
    assert store.get("platform", default="env-key") is None
    copied = tmp_path / "copied.json"
    shutil.copyfile(store.path, copied)
    reloaded = SecretStore(str(copied), mode=mode)
    assert reloaded.get("platform", default="env-key") is None
    reloaded.set("platform", "new-key")
    assert reloaded.get("platform", default="env-key") == "new-key"


def test_lan_selection_excludes_vpn_virtual_and_disconnected_interfaces(monkeypatch):
    data = {"FlClash": "198.18.0.1", "VMware VMnet8": "192.168.50.1", "以太网": "172.24.127.83", "Wi-Fi": "192.168.1.5"}
    monkeypatch.setattr("app.network.psutil.net_if_addrs", lambda: {name: [SimpleNamespace(family=socket.AF_INET, address=ip)] for name, ip in data.items()})
    monkeypatch.setattr("app.network.psutil.net_if_stats", lambda: {name: SimpleNamespace(isup=name != "Wi-Fi") for name in data})
    interfaces = local_interfaces()
    assert advertised_ip(interfaces=interfaces) == "172.24.127.83"
    assert advertised_ip("192.168.50.1", interfaces=interfaces) == "192.168.50.1"
    assert advertised_ip(interfaces=[]) == "127.0.0.1"


@pytest.mark.parametrize("value", ["0.0.0.0", "224.0.0.1", "::1", "not-an-ip", "198.18.0.1:8000"])
def test_advertised_ip_setting_rejects_unusable_addresses(value):
    with pytest.raises(HTTPException):
        _coerce_value("LAN_ADVERTISE_IP", value, SETTINGS_SCHEMA["LAN_ADVERTISE_IP"])


def test_knowledge_scan_and_delete_never_follow_directory_links(tmp_path):
    store = KnowledgeStore(str(tmp_path / "kb.sqlite3"), str(tmp_path / "files"))
    store.upsert_source("lesson", "Lesson")
    source = store.source_directory("lesson")
    target = tmp_path / "outside"
    target.mkdir()
    fixture = target / "outside.txt"
    fixture.write_text("must remain", encoding="utf-8")
    link = source / "linked"
    # Both targets are newly created test fixtures under this isolated temp root.
    assert target.resolve().is_relative_to(tmp_path.resolve())
    assert link.parent.resolve().is_relative_to(tmp_path.resolve())
    if os.name == "nt":
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                        "$ErrorActionPreference = 'Stop'; New-Item -ItemType Junction -Path $env:EDUGATE_TEST_LINK -Target $env:EDUGATE_TEST_TARGET | Out-Null"],
                       env={**os.environ, "EDUGATE_TEST_LINK": str(link), "EDUGATE_TEST_TARGET": str(target)}, check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        for action in (store.scan_source, store.delete_source):
            with pytest.raises(HTTPException) as error:
                action("lesson")
            assert error.value.status_code == 409
        assert fixture.read_text(encoding="utf-8") == "must remain"
        assert store.get_source("lesson").id == "lesson"
    finally:
        # Unlink the test link itself, never recurse into its target.
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()


def test_heartbeat_close_between_chunks_releases_source():
    from app.chat_service import _stream_with_heartbeat
    async def check():
        closed = asyncio.Event()
        async def source():
            try:
                yield b"chunk"
            finally:
                closed.set()
        stream = _stream_with_heartbeat(source())
        assert await anext(stream) == b"chunk"
        await stream.aclose()
        assert closed.is_set()
    asyncio.run(check())


@pytest.mark.parametrize("action", ["end", "start", "rotate"])
def test_classroom_revokes_waiting_and_running_operations(action):
    async def check():
        access = ClassroomAccess()
        student = StudentIdentity("student", "PC", "127.0.0.1", access.classroom_id(), access.lease())
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        async def operation():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        task = asyncio.create_task(run_in_classroom(student, operation()))
        await entered.wait()
        getattr(access, action)()
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(task, 1)
        assert error.value.status_code == 403
        assert cancelled.is_set()
        with pytest.raises(HTTPException):
            await run_in_classroom(student, asyncio.sleep(0))
    asyncio.run(check())


def test_classroom_stream_closes_upstream_on_revocation():
    async def check():
        access = ClassroomAccess()
        student = StudentIdentity("student", "PC", "127.0.0.1", access.classroom_id(), access.lease())
        closed = asyncio.Event()
        async def source():
            try:
                yield b"first"
                await asyncio.Event().wait()
            finally:
                closed.set()
        stream = stream_in_classroom(student, source())
        assert await anext(stream) == b"first"
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        access.end()
        with pytest.raises(HTTPException):
            await asyncio.wait_for(pending, 1)
        assert closed.is_set()
    asyncio.run(check())


def test_classroom_revocation_closes_source_during_downstream_backpressure():
    async def check():
        access = ClassroomAccess()
        student = StudentIdentity("student", "PC", "127.0.0.1", access.classroom_id(), access.lease())
        closed = asyncio.Event()
        async def source():
            try:
                while True:
                    yield b"chunk"
            finally:
                closed.set()
        stream = stream_in_classroom(student, source())
        assert await anext(stream) == b"chunk"
        # Consumer pauses here, just as StreamingResponse does during a slow send.
        access.end()
        await asyncio.wait_for(closed.wait(), 1)
        with pytest.raises(HTTPException):
            await anext(stream)
        await stream.aclose()
    asyncio.run(check())
