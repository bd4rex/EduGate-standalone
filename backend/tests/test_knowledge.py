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
