from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile
from pypdf import PdfWriter

from app.knowledge import KnowledgeStore, extract_pdf_text


def test_upload_size_limit_removes_partial_file(tmp_path: Path) -> None:
    store = KnowledgeStore(
        str(tmp_path / "knowledge.sqlite3"),
        str(tmp_path / "files"),
        max_upload_bytes=4,
    )
    upload = UploadFile(filename="large.txt", file=BytesIO(b"12345"))

    with pytest.raises(HTTPException) as caught:
        asyncio.run(store.add_file("general", upload))

    assert caught.value.status_code == 413
    assert list((tmp_path / "files").rglob("*.txt")) == []


def test_uploaded_files_keep_readable_names_in_source_folder(tmp_path: Path) -> None:
    store = KnowledgeStore(
        str(tmp_path / "knowledge.sqlite3"),
        str(tmp_path / "files"),
    )

    first = asyncio.run(store.add_file("general", UploadFile(filename="lesson.txt", file=BytesIO(b"alpha"))))
    second = asyncio.run(store.add_file("general", UploadFile(filename="lesson.txt", file=BytesIO(b"bravo"))))

    assert first.filename == "lesson.txt"
    assert second.filename == "lesson (2).txt"
    assert (store.source_directory("general") / "lesson.txt").exists()
    assert (store.source_directory("general") / "lesson (2).txt").exists()


def test_pdf_page_limit(tmp_path: Path) -> None:
    pdf_path = tmp_path / "many-pages.pdf"
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=100, height=100)
    with pdf_path.open("wb") as handle:
        writer.write(handle)

    with pytest.raises(HTTPException) as caught:
        extract_pdf_text(pdf_path, max_pdf_pages=2)

    assert caught.value.status_code == 413


def test_source_folder_scan_adds_updates_and_removes_files(tmp_path: Path) -> None:
    store = KnowledgeStore(
        str(tmp_path / "knowledge.sqlite3"),
        str(tmp_path / "files"),
    )
    source_dir = store.source_directory("general")
    lesson = source_dir / "lesson.txt"
    lesson.write_text("alpha topic", encoding="utf-8")

    first = store.scan_source("general")

    assert first.added == 1
    assert first.updated == 0
    assert store.list_files("general")[0].filename == "lesson.txt"
    assert store.search("general", "alpha")

    lesson.write_text("bravo topic", encoding="utf-8")
    second = store.scan_source("general")
    third = store.scan_source("general")

    assert second.updated == 1
    assert not store.search("general", "alpha")
    assert store.search("general", "bravo")
    assert third.unchanged == 1

    lesson.unlink()
    fourth = store.scan_source("general")

    assert fourth.removed == 1
    assert store.list_files("general") == []


def test_source_folder_scan_skips_unsupported_files(tmp_path: Path) -> None:
    store = KnowledgeStore(
        str(tmp_path / "knowledge.sqlite3"),
        str(tmp_path / "files"),
    )
    source_dir = store.source_directory("general")
    (source_dir / "notes.docx").write_bytes(b"not indexed")

    result = store.scan_source("general")

    assert result.skipped == 1
    assert result.added == 0


def test_deleting_source_removes_its_folder(tmp_path: Path) -> None:
    store = KnowledgeStore(
        str(tmp_path / "knowledge.sqlite3"),
        str(tmp_path / "files"),
    )
    store.upsert_source("lesson", "Lesson")
    source_dir = store.source_directory("lesson")
    (source_dir / "manual.txt").write_text("content", encoding="utf-8")

    store.delete_source("lesson")

    assert not source_dir.exists()
