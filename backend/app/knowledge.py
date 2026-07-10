from __future__ import annotations

import html
import re
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict


SUPPORTED_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".json",
    ".html",
    ".htm",
    ".xml",
    ".log",
    ".pdf",
}


class KnowledgeSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str = ""
    file_count: int = 0
    chunk_count: int = 0
    created_at: str


class KnowledgeFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source_id: str
    filename: str
    content_type: str | None = None
    size_bytes: int
    chunk_count: int
    created_at: str


class KnowledgeHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    file_id: str
    filename: str
    chunk_index: int
    score: int
    text: str


class KnowledgeStore:
    def __init__(
        self,
        db_path: str,
        storage_dir: str,
        *,
        max_upload_bytes: int = 25 * 1024 * 1024,
        max_pdf_pages: int = 200,
    ) -> None:
        self.db_path = Path(db_path)
        self.storage_dir = Path(storage_dir)
        self.max_upload_bytes = max_upload_bytes
        self.max_pdf_pages = max_pdf_pages
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    content_type TEXT,
                    size_bytes INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY,
                    file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO sources (id, name, description, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "general",
                    "通用百科",
                    "默认知识库，可上传课堂资料、教材片段或学习单文本。",
                    _now(),
                ),
            )

    def list_sources(self) -> list[KnowledgeSource]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    s.id,
                    s.name,
                    s.description,
                    s.created_at,
                    COUNT(DISTINCT f.id) AS file_count,
                    COUNT(c.id) AS chunk_count
                FROM sources s
                LEFT JOIN files f ON f.source_id = s.id
                LEFT JOIN chunks c ON c.file_id = f.id
                GROUP BY s.id
                ORDER BY s.created_at ASC
                """
            ).fetchall()
        return [KnowledgeSource(**dict(row)) for row in rows]

    def upsert_source(self, source_id: str, name: str, description: str = "") -> KnowledgeSource:
        source_id = _safe_id(source_id)
        if not source_id:
            raise HTTPException(status_code=400, detail="source_id is required")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sources (id, name, description, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description
                """,
                (source_id, name, description, _now()),
            )
        return self.get_source(source_id)

    def get_source(self, source_id: str) -> KnowledgeSource:
        for source in self.list_sources():
            if source.id == source_id:
                return source
        raise HTTPException(status_code=404, detail=f"Unknown knowledge source: {source_id}")

    def delete_source(self, source_id: str) -> None:
        if source_id == "general":
            raise HTTPException(status_code=400, detail="The default source cannot be deleted")
        self.get_source(source_id)
        with self._connect() as conn:
            files = conn.execute("SELECT path FROM files WHERE source_id = ?", (source_id,)).fetchall()
            conn.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM files WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        for row in files:
            Path(row["path"]).unlink(missing_ok=True)

    async def add_file(self, source_id: str, upload: UploadFile) -> KnowledgeFile:
        self.get_source(source_id)
        filename = Path(upload.filename or "upload.txt").name
        extension = Path(filename).suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported file type: {extension}. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
            )

        file_id = uuid.uuid4().hex
        target_dir = self.storage_dir / source_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{file_id}{extension}"

        size = 0
        with target_path.open("wb") as out:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > self.max_upload_bytes:
                    out.close()
                    target_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"File exceeds the {self.max_upload_bytes // (1024 * 1024)} MB upload limit",
                    )
                out.write(chunk)

        try:
            text = await run_in_threadpool(extract_text, target_path, max_pdf_pages=self.max_pdf_pages)
            chunks = chunk_text(text)
        except Exception:
            target_path.unlink(missing_ok=True)
            raise
        if not chunks:
            target_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="No readable text was extracted from the file")

        created_at = _now()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO files (id, source_id, filename, content_type, size_bytes, path, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (file_id, source_id, filename, upload.content_type, size, str(target_path), created_at),
                )
                conn.executemany(
                    """
                    INSERT INTO chunks (id, file_id, source_id, chunk_index, text)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (uuid.uuid4().hex, file_id, source_id, index, chunk)
                        for index, chunk in enumerate(chunks)
                    ],
                )
        except Exception:
            target_path.unlink(missing_ok=True)
            raise

        return self.get_file(file_id)

    def list_files(self, source_id: str | None = None) -> list[KnowledgeFile]:
        params: tuple[Any, ...] = ()
        where = ""
        if source_id:
            self.get_source(source_id)
            where = "WHERE f.source_id = ?"
            params = (source_id,)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    f.id,
                    f.source_id,
                    f.filename,
                    f.content_type,
                    f.size_bytes,
                    f.created_at,
                    COUNT(c.id) AS chunk_count
                FROM files f
                LEFT JOIN chunks c ON c.file_id = f.id
                {where}
                GROUP BY f.id
                ORDER BY f.created_at DESC
                """,
                params,
            ).fetchall()
        return [KnowledgeFile(**dict(row)) for row in rows]

    def get_file(self, file_id: str) -> KnowledgeFile:
        for file in self.list_files():
            if file.id == file_id:
                return file
        raise HTTPException(status_code=404, detail=f"Unknown knowledge file: {file_id}")

    def delete_file(self, file_id: str) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT path FROM files WHERE id = ?", (file_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"Unknown knowledge file: {file_id}")
            conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        Path(row["path"]).unlink(missing_ok=True)

    def search(self, source_id: str, query: str, limit: int = 5) -> list[KnowledgeHit]:
        self.get_source(source_id)
        terms = tokenize(query)
        if not terms:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.file_id, c.source_id, c.chunk_index, c.text, f.filename
                FROM chunks c
                JOIN files f ON f.id = c.file_id
                WHERE c.source_id = ?
                """,
                (source_id,),
            ).fetchall()

        scored: list[KnowledgeHit] = []
        for row in rows:
            text = row["text"]
            lower = text.lower()
            score = sum(lower.count(term) for term in terms)
            if score <= 0:
                continue
            scored.append(
                KnowledgeHit(
                    source_id=row["source_id"],
                    file_id=row["file_id"],
                    filename=row["filename"],
                    chunk_index=row["chunk_index"],
                    score=score,
                    text=text,
                )
            )
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:limit]


def extract_text(path: Path, *, max_pdf_pages: int = 200) -> str:
    if path.suffix.lower() == ".pdf":
        return extract_pdf_text(path, max_pdf_pages=max_pdf_pages)
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="ignore")
    if path.suffix.lower() in {".html", ".htm", ".xml"}:
        text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
    return normalize_text(text)


def extract_pdf_text(path: Path, *, max_pdf_pages: int = 200) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise HTTPException(status_code=500, detail="PDF support requires pypdf") from error
    reader = PdfReader(str(path))
    if len(reader.pages) > max_pdf_pages:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"PDF exceeds the {max_pdf_pages}-page limit",
        )
    pages = [(page.extract_text() or "") for page in reader.pages]
    return normalize_text("\n".join(pages))


def chunk_text(text: str, *, chunk_size: int = 900, overlap: int = 120) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunk = text[start : start + chunk_size].strip()
        if chunk:
            chunks.append(chunk)
        start += chunk_size - overlap
    return chunks


def tokenize(query: str) -> list[str]:
    normalized = normalize_text(query).lower()
    words = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", normalized)
    terms = set(words)
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    for size in (2, 3, 4):
        for index in range(0, max(0, len(chinese) - size + 1)):
            terms.add(chinese[index : index + size])
    return sorted(terms, key=len, reverse=True)[:40]


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-").lower()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
