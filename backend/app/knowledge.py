from __future__ import annotations

import html
import hashlib
import mimetypes
import os
import re
import shutil
import stat
import sqlite3
import uuid
import json
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from fastapi import HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from app.knowledge_documents import PARSER_VERSION, SUPPORTED_EXTENSIONS, parse_document, split_sections
from app.knowledge_retrieval import DIMENSIONS, INDEX_VERSION, LocalEmbeddings, RetrievalIndex, lexical_text, sql_string, terms


class KnowledgeMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(default="", max_length=100)
    grade: str = Field(default="", max_length=100)
    edition: str = Field(default="", max_length=100)
    term: str = Field(default="", max_length=100)
    audience: Literal["student", "teacher"] = "student"


class KnowledgeScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_ids: list[str] = Field(default_factory=list, max_length=500)
    subject: str = Field(default="", max_length=100)
    grade: str = Field(default="", max_length=100)
    edition: str = Field(default="", max_length=100)
    term: str = Field(default="", max_length=100)
    chapter: str = Field(default="", max_length=200)


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
    content_hash: str = ""
    metadata: KnowledgeMetadata = Field(default_factory=KnowledgeMetadata)
    warnings: list[str] = Field(default_factory=list)
    index_error: str = ""
    parser_version: int = 0


class KnowledgeScanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)


class KnowledgeHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    file_id: str
    filename: str
    chunk_index: int
    score: float
    text: str
    chunk_id: str = ""
    content_hash: str = ""
    chapter: str = ""
    page: int | None = None
    semantic_score: float | None = None
    keyword_score: float | None = None
    context_text: str = ""


class KnowledgeSearchResult(BaseModel):
    query: str
    hits: list[KnowledgeHit] = Field(default_factory=list)
    mode: str = "keyword"
    elapsed_ms: float = 0
    revision: int = 0
    candidate_count: int = 0


def _locked(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def _is_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


class KnowledgeStore:
    def __init__(
        self,
        db_path: str,
        storage_dir: str,
        *,
        max_upload_bytes: int = 25 * 1024 * 1024,
        max_pdf_pages: int = 500,
        model_dir: str | None = None,
        index_dir: str | None = None,
        semantic_enabled: bool | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.storage_dir = Path(storage_dir).resolve()
        self.max_upload_bytes = max_upload_bytes
        self.max_pdf_pages = max_pdf_pages
        self._lock = threading.RLock()
        self._job_lock = threading.Lock()
        self._jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="edugate-knowledge")
        self._job: dict[str, Any] = {"status": "idle", "action": "", "message": ""}
        enabled = semantic_enabled if semantic_enabled is not None else os.getenv("KNOWLEDGE_SEMANTIC_ENABLED", "true").lower() == "true"
        self.embeddings = LocalEmbeddings(Path(model_dir) if model_dir else self.db_path.parent / "knowledge_models", enabled=enabled)
        self.index = RetrievalIndex(Path(index_dir) if index_dir else self.db_path.parent / "knowledge_index")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._migrate_file_paths()

    def _relative_file_path(self, value: str, source_id: str, *, migrate: bool = False, filename: str = "") -> str:
        """Rebase legacy absolute paths without ever accessing their old targets."""
        normalized = value.replace("\\", "/")
        path = PurePosixPath(normalized)
        if PureWindowsPath(value).is_absolute() or path.is_absolute():
            flavor = PureWindowsPath if PureWindowsPath(value).is_absolute() else PurePosixPath
            try:
                path = PurePosixPath(*flavor(value).relative_to(flavor(self.storage_dir)).parts)
            except ValueError:
                if not migrate:
                    raise HTTPException(status_code=409, detail="Knowledge path leaves its storage directory") from None
                parts = path.parts
                candidates = [PurePosixPath(*parts[index:]) for index, part in enumerate(parts[:-1]) if part == source_id]
                if len(candidates) > 1:
                    candidates = [candidate for candidate in candidates if candidate.relative_to(source_id).as_posix() == filename.replace("\\", "/")]
                if len(candidates) != 1:
                    raise HTTPException(status_code=409, detail="Legacy knowledge path cannot be safely migrated; restore a valid backup")
                path = candidates[0]
        if path.is_absolute() or len(path.parts) < 2 or path.parts[0] != source_id or ".." in path.parts or any(":" in part for part in path.parts):
            raise HTTPException(status_code=409, detail="Unsafe knowledge file path")
        return path.as_posix()

    def _file_path(self, value: str, source_id: str) -> Path:
        relative = self._relative_file_path(value, source_id)
        candidate = self.storage_dir.joinpath(*PurePosixPath(relative).parts)
        for part in (candidate, *candidate.parents):
            if part == self.storage_dir:
                break
            if _is_link(part):
                raise HTTPException(status_code=409, detail="Knowledge paths cannot contain links")
        if not candidate.resolve().is_relative_to(self.storage_dir):
            raise HTTPException(status_code=409, detail="Knowledge path leaves its storage directory")
        return candidate

    def _migrate_file_paths(self) -> None:
        with self._connect() as conn:
            for row in conn.execute("SELECT id, source_id, path, filename FROM files").fetchall():
                relative = self._relative_file_path(row["path"], row["source_id"], migrate=True, filename=row["filename"])
                if relative != row["path"]:
                    conn.execute("UPDATE files SET path = ? WHERE id = ?", (relative, row["id"]))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def checkpoint(self) -> None:
        if not Path(self.db_path).exists():
            return
        with self._connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

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
                    content_hash TEXT NOT NULL DEFAULT '',
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
            file_columns = {row["name"] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
            if "content_hash" not in file_columns:
                conn.execute("ALTER TABLE files ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")
            for name, definition in {
                "metadata_json": "TEXT NOT NULL DEFAULT '{}'", "warnings_json": "TEXT NOT NULL DEFAULT '[]'",
                "index_error": "TEXT NOT NULL DEFAULT ''", "parser_version": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in file_columns:
                    conn.execute(f"ALTER TABLE files ADD COLUMN {name} {definition}")
            chunk_columns = {row["name"] for row in conn.execute("PRAGMA table_info(chunks)")}
            for name, definition in {
                "chapter": "TEXT NOT NULL DEFAULT ''", "page": "INTEGER", "lexical_terms": "TEXT NOT NULL DEFAULT ''",
                "embedding": "BLOB", "embedding_model": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in chunk_columns:
                    conn.execute(f"ALTER TABLE chunks ADD COLUMN {name} {definition}")
            conn.executescript("""
                CREATE INDEX IF NOT EXISTS chunks_source_idx ON chunks(source_id);
                CREATE INDEX IF NOT EXISTS chunks_file_idx ON chunks(file_id);
                CREATE INDEX IF NOT EXISTS chunks_file_position_idx ON chunks(file_id,chunk_index);
                CREATE INDEX IF NOT EXISTS files_source_idx ON files(source_id);
                CREATE TABLE IF NOT EXISTS knowledge_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO knowledge_meta VALUES ('revision', '0');
            """)
            conn.execute("INSERT OR IGNORE INTO knowledge_meta VALUES ('instance', ?)", (uuid.uuid4().hex,))
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
        self.source_directory(source_id)
        return self.get_source(source_id)

    def get_source(self, source_id: str) -> KnowledgeSource:
        with self._connect() as conn:
            row = conn.execute("""SELECT s.*,
                (SELECT COUNT(*) FROM files WHERE source_id=s.id) AS file_count,
                (SELECT COUNT(*) FROM chunks WHERE source_id=s.id) AS chunk_count
                FROM sources s WHERE s.id=?""", (source_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Unknown knowledge source: {source_id}")
        return KnowledgeSource(**dict(row))

    @_locked
    def delete_source(self, source_id: str) -> None:
        if source_id == "general":
            raise HTTPException(status_code=400, detail="The default source cannot be deleted")
        self.get_source(source_id)
        with self._connect() as conn:
            files = conn.execute("SELECT path FROM files WHERE source_id = ?", (source_id,)).fetchall()
            targets = [self._file_path(row["path"], source_id) for row in files]
            directory = self.source_directory(source_id)
            # Inspect all descendants before a recursive operation; do not traverse links.
            for current, directories, filenames in os.walk(directory, followlinks=False):
                for name in directories + filenames:
                    self._file_path(str(Path(current) / name), source_id)
            for target in targets:
                target.unlink(missing_ok=True)
            shutil.rmtree(directory)
            conn.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM files WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
            self._bump_revision(conn)

    def source_directory(self, source_id: str) -> Path:
        source = self.get_source(source_id)
        storage_root = self.storage_dir.resolve()
        directory = storage_root / source.id
        if _is_link(directory):
            raise HTTPException(status_code=409, detail="Knowledge source cannot be a link")
        directory = directory.resolve()
        if storage_root not in directory.parents:
            raise HTTPException(status_code=400, detail="Invalid knowledge source directory")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

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
        target_dir = self.source_directory(source_id)
        stored_filename = _safe_filename(filename)
        target_path = _unique_target_path(target_dir / stored_filename)

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
            await run_in_threadpool(self._index_file, file_id, source_id, target_path.name, target_path, upload.content_type)
        except Exception:
            target_path.unlink(missing_ok=True)
            raise

        return self.get_file(file_id)

    def scan_source(self, source_id: str, *, force: bool = False) -> KnowledgeScanResult:
        source_dir = self.source_directory(source_id)
        self.embeddings.load()
        result = KnowledgeScanResult(source_id=source_id)
        with self._connect() as conn:
            indexed_rows = conn.execute(
                """
                SELECT f.id, f.filename, f.path, f.size_bytes, f.content_hash, f.parser_version, f.index_error,
                    (SELECT COUNT(*) FROM chunks c WHERE c.file_id=f.id AND c.embedding_model<>?) AS pending_vectors
                FROM files f
                WHERE f.source_id = ?
                """,
                (self.embeddings.signature, source_id),
            ).fetchall()
        indexed = {_path_key(self._file_path(row["path"], source_id)): row for row in indexed_rows}
        discovered: set[str] = set()

        for path in self._scan_paths(source_dir, source_id):
            self._file_path(str(path), source_id)
            if not path.is_file():
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                result.skipped += 1
                continue
            key = _path_key(path)
            discovered.add(key)
            relative_name = path.relative_to(source_dir).as_posix()
            try:
                size = path.stat().st_size
                if size > self.max_upload_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"File exceeds the {self.max_upload_bytes // (1024 * 1024)} MB upload limit",
                    )
                content_hash = _file_hash(path)
                existing = indexed.get(key)
                if (
                    existing is not None
                    and not force
                    and existing["parser_version"] == PARSER_VERSION
                    and not existing["index_error"]
                    and (not self.embeddings.signature or existing["pending_vectors"] == 0)
                    and existing["content_hash"] == content_hash
                    and existing["size_bytes"] == size
                ):
                    result.unchanged += 1
                    continue
                file_id = existing["id"] if existing is not None else uuid.uuid4().hex
                filename = existing["filename"] if existing is not None else relative_name
                content_type = mimetypes.guess_type(path.name)[0]
                self._index_file(file_id, source_id, filename, path, content_type)
                if existing is None:
                    result.added += 1
                else:
                    result.updated += 1
            except Exception as error:
                detail = error.detail if isinstance(error, HTTPException) else str(error)
                result.errors.append(f"{relative_name}: {detail}")
                existing = indexed.get(key)
                if existing:
                    with self._lock, self._connect() as conn:
                        conn.execute("UPDATE files SET index_error=? WHERE id=?", (str(detail)[:500], existing["id"]))
                        self._bump_revision(conn)

        missing_ids = [row["id"] for key, row in indexed.items() if key not in discovered]
        if missing_ids:
            with self._lock, self._connect() as conn:
                conn.executemany("DELETE FROM chunks WHERE file_id = ?", [(file_id,) for file_id in missing_ids])
                conn.executemany("DELETE FROM files WHERE id = ?", [(file_id,) for file_id in missing_ids])
                self._bump_revision(conn)
            result.removed = len(missing_ids)
        return result

    def _bump_revision(self, conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE knowledge_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")

    def _index_file(self, file_id: str, source_id: str, filename: str, path: Path, content_type: str | None) -> None:
        digest = _file_hash(path)
        document = parse_document(path, max_pdf_pages=self.max_pdf_pages)
        chunks = split_sections(document)
        if not chunks:
            raise HTTPException(status_code=400, detail="No readable text was extracted from the file；扫描资料请先进行 OCR。")
        vectors: list[list[float]] = []
        if self.embeddings.load():
            with self._connect() as conn:
                previous = conn.execute("SELECT chapter,text,embedding FROM chunks WHERE file_id=? AND embedding_model=? AND embedding IS NOT NULL", (file_id, self.embeddings.signature)).fetchall()
            cached = {(row["chapter"], row["text"]): list(struct.unpack(f"<{DIMENSIONS}f", row["embedding"])) for row in previous}
            missing = [chunk for chunk in chunks if (chunk.chapter, chunk.text) not in cached]
            generated = self.embeddings.passages([f"{filename[:40]} {chunk.chapter[:40]}\n{chunk.text}" for chunk in missing])
            cached.update({(chunk.chapter, chunk.text): vector for chunk, vector in zip(missing, generated)})
            vectors = [cached[(chunk.chapter, chunk.text)] for chunk in chunks]
        with self._lock, self._connect() as conn:
            if _file_hash(path) != digest:
                raise HTTPException(status_code=409, detail="资料在处理过程中发生变化，请重新扫描")
            conn.execute("""INSERT INTO files
                (id,source_id,filename,content_type,size_bytes,path,content_hash,created_at,warnings_json,parser_version)
                VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                content_type=excluded.content_type,size_bytes=excluded.size_bytes,path=excluded.path,
                content_hash=excluded.content_hash,warnings_json=excluded.warnings_json,
                parser_version=excluded.parser_version,index_error=''""", (
                    file_id, source_id, filename, content_type, path.stat().st_size,
                    path.relative_to(self.storage_dir).as_posix(), digest, _now(),
                    json.dumps(document.warnings, ensure_ascii=False), PARSER_VERSION,
                ))
            conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
            conn.executemany("""INSERT INTO chunks
                (id,file_id,source_id,chunk_index,text,chapter,page,lexical_terms,embedding,embedding_model)
                VALUES (?,?,?,?,?,?,?,?,?,?)""", [(
                    uuid.uuid4().hex, file_id, source_id, index, chunk.text, chunk.chapter, chunk.page,
                    lexical_text(f"{filename} {chunk.chapter} {chunk.text}"),
                    struct.pack(f"<{DIMENSIONS}f", *vectors[index]) if vectors else None,
                    self.embeddings.signature if vectors else "",
                ) for index, chunk in enumerate(chunks)])
            self._bump_revision(conn)

    def _index_identity(self) -> tuple[str, int]:
        with self._connect() as conn:
            metadata = dict(conn.execute("SELECT key,value FROM knowledge_meta").fetchall())
        revision = int(metadata["revision"])
        return f"{metadata['instance']}:{revision}:{INDEX_VERSION}:{self.embeddings.signature}", revision

    def _index_rows(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("""SELECT c.*,f.filename,f.content_hash,f.metadata_json FROM chunks c
                JOIN files f ON f.id=c.file_id WHERE f.index_error=''""").fetchall()
        output = []
        for row in rows:
            metadata = KnowledgeMetadata.model_validate_json(row["metadata_json"])
            has_vector = bool(row["embedding"] and row["embedding_model"] == self.embeddings.signature)
            output.append({
                "chunk_id": row["id"], "file_id": row["file_id"], "source_id": row["source_id"],
                "filename": row["filename"], "content_hash": row["content_hash"],
                "chapter": row["chapter"], "page": row["page"], "chunk_index": row["chunk_index"], "text": row["text"],
                "terms": row["lexical_terms"] or lexical_text(f"{row['filename']} {row['text']}"),
                **metadata.model_dump(), "has_vector": has_vector,
                "vector": list(struct.unpack(f"<{DIMENSIONS}f", row["embedding"])) if has_vector else [0.0] * DIMENSIONS,
            })
        return output

    @_locked
    def warm_index(self) -> None:
        self.embeddings.load()
        identity, _ = self._index_identity()
        self.index.ensure(identity, self._index_rows)

    def validate_scope(self, source_id: str | None, scope: KnowledgeScope) -> None:
        if not source_id:
            if scope.file_ids or scope.chapter or any(getattr(scope, field) for field in ("subject", "grade", "edition", "term")):
                raise HTTPException(status_code=400, detail="请先选择知识库，再设置课堂资料范围")
            return
        self.get_source(source_id)
        with self._connect() as conn:
            for file_id in scope.file_ids:
                row = conn.execute("SELECT source_id,metadata_json FROM files WHERE id=?", (file_id,)).fetchone()
                if not row or row["source_id"] != source_id:
                    raise HTTPException(status_code=400, detail="课堂资料范围包含其他知识库或不存在的文件")
                if json.loads(row["metadata_json"]).get("audience") == "teacher":
                    raise HTTPException(status_code=400, detail="教师参考资料不能加入学生课堂范围")

    def _where_scope(self, source_id: str, scope: KnowledgeScope, *, include_teacher: bool = False) -> str:
        clauses = [f"source_id = {sql_string(source_id)}"]
        if not include_teacher:
            clauses.append("audience = 'student'")
        if scope.file_ids:
            clauses.append("file_id IN (" + ",".join(sql_string(value) for value in scope.file_ids) + ")")
        for field in ("subject", "grade", "edition", "term"):
            if value := getattr(scope, field):
                clauses.append(f"{field} = {sql_string(value)}")
        if scope.chapter:
            clauses.append(f"contains(chapter, {sql_string(scope.chapter)})")
        return " AND ".join(clauses)

    @_locked
    def update_file_metadata(self, file_id: str, metadata: KnowledgeMetadata) -> KnowledgeFile:
        self.get_file(file_id)
        with self._connect() as conn:
            conn.execute("UPDATE files SET metadata_json=? WHERE id=?", (metadata.model_dump_json(), file_id))
            self._bump_revision(conn)
        return self.get_file(file_id)

    def status(self) -> dict[str, Any]:
        with self._job_lock:
            job = dict(self._job)
        if job["status"] != "running":
            self.embeddings.load()
        with self._connect() as conn:
            counts = dict(conn.execute("""SELECT COUNT(*) AS chunks,
                SUM(CASE WHEN embedding IS NOT NULL AND embedding_model=? THEN 1 ELSE 0 END) AS embedded FROM chunks""", (self.embeddings.signature,)).fetchone())
            errors = conn.execute("SELECT COUNT(*) FROM files WHERE index_error<>''").fetchone()[0]
            outdated = conn.execute("SELECT COUNT(*) FROM files WHERE parser_version<>?", (PARSER_VERSION,)).fetchone()[0]
        ready = bool(self.embeddings.signature)
        return {
            "engine": "LanceDB", "mode": "hybrid" if ready and counts["embedded"] else "keyword",
            "model_ready": ready, "model_name": "BAAI/bge-small-zh-v1.5", "model_error": self.embeddings.error,
            "chunk_count": counts["chunks"], "embedded_count": counts["embedded"] or 0,
            "file_error_count": errors, "outdated_file_count": outdated, "job": job,
        }

    def start_job(self, action: str, source_id: str | None = None) -> dict[str, Any]:
        if action not in {"prepare", "reindex", "scan"}:
            raise HTTPException(status_code=400, detail="Unknown knowledge job")
        if source_id:
            self.get_source(source_id)
        with self._job_lock:
            if self._job["status"] == "running":
                raise HTTPException(status_code=409, detail="资料处理正在进行，请等待完成")
            self._job = {"id": uuid.uuid4().hex, "action": action, "status": "running", "message": "正在处理资料…", "errors": []}
        if self._jobs is None:
            self._jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="edugate-knowledge")
        self._jobs.submit(self._run_job, action, source_id)
        return dict(self._job)

    def _run_job(self, action: str, source_id: str | None) -> None:
        try:
            if action == "prepare":
                with self._job_lock:
                    self._job["message"] = "正在下载并校验约 90 MB 的中文语义模型…"
                self.embeddings.prepare()
            sources = [source_id] if source_id else [source.id for source in self.list_sources()]
            errors = []
            for position, source in enumerate(sources, 1):
                with self._job_lock:
                    self._job["message"] = f"正在处理知识库 {position}/{len(sources)}…"
                result = self.scan_source(source, force=action in {"prepare", "reindex"})
                errors.extend(result.errors)
            self.warm_index()
            with self._job_lock:
                self._job.update(status="failed" if errors else "completed", message="部分资料处理失败，请查看文件提示" if errors else "资料处理完成", errors=errors)
        except Exception as error:
            with self._job_lock:
                self._job.update(status="failed", message="资料处理失败，可重试；原始文件已保留", errors=[str(error)[:500]])

    def close(self) -> None:
        if self._jobs is not None:
            self._jobs.shutdown(wait=True, cancel_futures=True)
            self._jobs = None

    def _scan_paths(self, source_dir: Path, source_id: str) -> list[Path]:
        paths = []
        for current, directories, filenames in os.walk(source_dir, followlinks=False):
            for name in directories + filenames:
                path = self._file_path(str(Path(current) / name), source_id)
                if name in filenames:
                    paths.append(path)
        return sorted(paths)

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
                    f.created_at, f.content_hash, f.metadata_json, f.warnings_json, f.index_error, f.parser_version,
                    COUNT(c.id) AS chunk_count
                FROM files f
                LEFT JOIN chunks c ON c.file_id = f.id
                {where}
                GROUP BY f.id
                ORDER BY f.created_at DESC
                """,
                params,
            ).fetchall()
        return [self._file_record(row) for row in rows]

    @staticmethod
    def _file_record(row) -> KnowledgeFile:
        data = dict(row)
        data["metadata"] = json.loads(data.pop("metadata_json", "{}"))
        data["warnings"] = json.loads(data.pop("warnings_json", "[]"))
        data.pop("path", None)
        return KnowledgeFile(**data)

    def get_file(self, file_id: str) -> KnowledgeFile:
        with self._connect() as conn:
            row = conn.execute("""SELECT f.*,
                (SELECT COUNT(*) FROM chunks WHERE file_id=f.id) AS chunk_count
                FROM files f WHERE f.id=?""", (file_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Unknown knowledge file: {file_id}")
        return self._file_record(row)

    @_locked
    def delete_file(self, file_id: str) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT path, source_id FROM files WHERE id = ?", (file_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"Unknown knowledge file: {file_id}")
            self._file_path(row["path"], row["source_id"]).unlink(missing_ok=True)
            conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
            self._bump_revision(conn)

    def search(self, source_id: str, query: str, limit: int = 5, *, scope: KnowledgeScope | None = None) -> list[KnowledgeHit]:
        return self.search_details(source_id, query, limit=limit, scope=scope).hits

    def search_details(self, source_id: str, query: str, *, limit: int = 5, scope: KnowledgeScope | None = None, mode: str = "hybrid") -> KnowledgeSearchResult:
        started = time.perf_counter()
        self.get_source(source_id)
        if mode not in {"hybrid", "keyword", "vector"}:
            raise HTTPException(status_code=400, detail="Unknown retrieval mode")
        scope = scope or KnowledgeScope()
        # A scope with removed or moved files stays empty; never broaden silently.
        vector = self.embeddings.query(query) if mode != "keyword" else None
        with self._lock:
            self.warm_index()
            _, revision = self._index_identity()
            table = self.index.snapshot()
        if not query.strip():
            return KnowledgeSearchResult(query=query, revision=revision)
        if mode == "vector" and vector is None:
            raise HTTPException(status_code=409, detail="语义模型尚未准备，请先准备本地模型")
        rows = self.index.search(query, self._where_scope(source_id, scope), vector, limit=max(1, min(limit, 20)), mode=mode, table=table)
        hits = []
        # Recheck current metadata after querying a Lance snapshot. A concurrent
        # delete, failed update or teacher-only change must not return stale text.
        for row in rows:
            try:
                hit = self.citation(row["chunk_id"], source_id, scope, content_hash=row["content_hash"])
            except HTTPException as error:
                if error.status_code in {404, 409}:
                    continue
                raise
            hits.append(hit.model_copy(update={"score":row["_relevance_score"],
                "semantic_score":row.get("semantic_score"),"keyword_score":row.get("keyword_score")}))
        return KnowledgeSearchResult(query=query, hits=hits, candidate_count=len(hits), revision=revision,
            mode=mode if vector is not None else "keyword", elapsed_ms=round((time.perf_counter()-started)*1000, 2))

    def _context_text(self, file_id: str, chunk_index: int, chapter: str, page: int | None) -> str:
        with self._connect() as conn:
            rows = conn.execute("""SELECT text FROM chunks WHERE file_id=? AND chapter=? AND page IS ?
                AND chunk_index BETWEEN ? AND ? ORDER BY chunk_index""", (file_id, chapter, page, chunk_index-1, chunk_index+1)).fetchall()
        return "\n\n".join(row["text"] for row in rows)

    def preview_chunks(self, file_id: str, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        file = self.get_file(file_id)
        with self._connect() as conn:
            rows = conn.execute("SELECT id AS chunk_id,chunk_index,text,chapter,page FROM chunks WHERE file_id=? ORDER BY chunk_index LIMIT ? OFFSET ?", (file_id, min(limit, 100), max(offset, 0))).fetchall()
        return {"file": file.model_dump(), "chunks": [dict(row) for row in rows], "offset": offset, "total": file.chunk_count}

    @_locked
    def citation(self, chunk_id: str, source_id: str, scope: KnowledgeScope, *, content_hash: str = "") -> KnowledgeHit:
        with self._connect() as conn:
            row = conn.execute("""SELECT c.*,f.filename,f.content_hash,f.metadata_json,f.index_error
                FROM chunks c JOIN files f ON f.id=c.file_id WHERE c.id=? AND c.source_id=?""", (chunk_id, source_id)).fetchone()
        if row is None or row["index_error"]:
            raise HTTPException(status_code=404, detail="资料已更新或不在当前课堂范围内")
        metadata = KnowledgeMetadata.model_validate_json(row["metadata_json"])
        allowed = metadata.audience == "student" and (not scope.file_ids or row["file_id"] in scope.file_ids)
        allowed = allowed and all(not getattr(scope, field) or getattr(scope, field) == getattr(metadata, field) for field in ("subject", "grade", "edition", "term"))
        allowed = allowed and (not scope.chapter or scope.chapter in row["chapter"])
        if not allowed:
            raise HTTPException(status_code=404, detail="资料不在当前课堂范围内")
        if content_hash and content_hash != row["content_hash"]:
            raise HTTPException(status_code=409, detail="教材版本已更新，请重新提问")
        return KnowledgeHit(source_id=source_id,file_id=row["file_id"],filename=row["filename"],
            chunk_index=row["chunk_index"],text=row["text"],score=0,chunk_id=chunk_id,
            content_hash=row["content_hash"],chapter=row["chapter"],page=row["page"],
            context_text=self._context_text(row["file_id"],row["chunk_index"],row["chapter"],row["page"]))


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
    return list(terms(query))


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-").lower()


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", Path(value).name).strip(" .")
    return cleaned or "upload.txt"


def _unique_target_path(target: Path) -> Path:
    if not target.exists():
        return target
    for index in range(2, 10000):
        candidate = target.with_name(f"{target.stem} ({index}){target.suffix}")
        if not candidate.exists():
            return candidate
    raise HTTPException(status_code=409, detail="Too many files share this filename")


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
