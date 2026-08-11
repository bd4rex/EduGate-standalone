from __future__ import annotations

import base64
import hashlib
import logging
import os
import queue
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _WriteItem:
    kind: str
    payload: dict[str, Any] | None = None
    completed: threading.Event | None = None


class BusinessDB:
    def __init__(
        self,
        sqlite_path: str = "edugate.sqlite3",
        *,
        log_max_records: int = 5000,
        classroom_record_retention_days: int = 30,
        classroom_record_max_records: int = 20000,
        classroom_record_max_content_chars: int = 12000,
        write_queue_size: int = 4096,
        write_batch_size: int = 100,
        write_flush_interval_ms: int = 20,
        cleanup_interval_seconds: float = 300,
    ) -> None:
        self.sqlite_path = sqlite_path
        self.log_max_records = max(100, log_max_records)
        self.classroom_record_retention_days = max(1, classroom_record_retention_days)
        self.classroom_record_max_records = max(100, classroom_record_max_records)
        self.classroom_record_max_content_chars = max(500, classroom_record_max_content_chars)
        self.write_queue_size = max(128, write_queue_size)
        self.write_batch_size = min(max(1, write_batch_size), self.write_queue_size)
        self.write_flush_interval_seconds = max(0.001, write_flush_interval_ms / 1000)
        self.cleanup_interval_seconds = max(0.01, cleanup_interval_seconds)
        self.enabled = True
        self._write_queue: queue.Queue[_WriteItem] = queue.Queue(maxsize=self.write_queue_size)
        self._writer_thread: threading.Thread | None = None
        self._writer_stop = threading.Event()
        self._writer_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._write_stats: dict[str, Any] = {
            "enqueued": 0,
            "written": 0,
            "dropped": 0,
            "failed": 0,
            "batches": 0,
            "last_error": None,
            "last_cleanup_at": None,
        }

    def init(self) -> None:
        Path(self.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS edugate_teachers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'teacher',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_login_at TEXT
                );

                CREATE TABLE IF NOT EXISTS ai_request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    route TEXT NOT NULL,
                    scenario_id TEXT,
                    teacher_id TEXT,
                    model TEXT,
                    knowledge_source_id TEXT,
                    user_message_preview TEXT,
                    status_code INTEGER,
                    latency_ms INTEGER,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    stream_done INTEGER,
                    stream_chunks INTEGER,
                    stream_bytes INTEGER,
                    stream_duration_ms INTEGER,
                    stream_finish_reason TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS ai_feedback_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    request_log_id INTEGER REFERENCES ai_request_logs(id) ON DELETE SET NULL,
                    score INTEGER NOT NULL,
                    comment TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS edugate_teaching_sessions (
                    id TEXT PRIMARY KEY,
                    teacher_username TEXT NOT NULL REFERENCES edugate_teachers(username) ON DELETE RESTRICT,
                    title TEXT NOT NULL,
                    course_name TEXT NOT NULL DEFAULT '',
                    class_name TEXT NOT NULL DEFAULT '',
                    scenario_id TEXT NOT NULL DEFAULT 'default',
                    access_code TEXT NOT NULL UNIQUE,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS classroom_runs (
                    id TEXT PRIMARY KEY,
                    classroom_instance_id TEXT NOT NULL,
                    teacher_username TEXT NOT NULL REFERENCES edugate_teachers(username) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    ended_at TEXT,
                    last_activity_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(classroom_instance_id, teacher_username)
                );

                CREATE TABLE IF NOT EXISTS classroom_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    classroom_run_id TEXT NOT NULL REFERENCES classroom_runs(id) ON DELETE CASCADE,
                    student_session_id TEXT NOT NULL,
                    computer_name TEXT NOT NULL DEFAULT '',
                    client_ip TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL CHECK(kind IN ('chat', 'python')),
                    input_content TEXT NOT NULL,
                    output_content TEXT NOT NULL DEFAULT '',
                    status_code INTEGER NOT NULL,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    queue_wait_ms INTEGER,
                    timed_out INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_classroom_runs_teacher_activity
                    ON classroom_runs(teacher_username, last_activity_at DESC);
                CREATE INDEX IF NOT EXISTS idx_classroom_turns_run_id
                    ON classroom_turns(classroom_run_id, id);
                CREATE INDEX IF NOT EXISTS idx_classroom_turns_student
                    ON classroom_turns(student_session_id, id);
                """
            )
            self._ensure_column(conn, "classroom_turns", "computer_name", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "classroom_turns", "client_ip", "TEXT NOT NULL DEFAULT ''")
            conn.execute(
                "UPDATE classroom_runs SET ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP) WHERE ended_at IS NULL"
            )
            self._cleanup_storage(conn)

    def start_writer(self) -> None:
        with self._writer_lock:
            if self._writer_thread and self._writer_thread.is_alive():
                return
            self._writer_stop.clear()
            self._writer_thread = threading.Thread(
                target=self._writer_loop,
                name="edugate-sqlite-writer",
                daemon=True,
            )
            self._writer_thread.start()

    def stop_writer(self, timeout: float = 15) -> bool:
        with self._writer_lock:
            thread = self._writer_thread
        if not thread or not thread.is_alive():
            return True
        flushed = self.flush_writes(timeout=timeout)
        try:
            self._write_queue.put(_WriteItem(kind="stop"), timeout=max(0.1, timeout))
        except queue.Full:
            return False
        self._writer_stop.set()
        thread.join(timeout=timeout)
        stopped = not thread.is_alive()
        if stopped:
            with self._writer_lock:
                self._writer_thread = None
        return flushed and stopped

    def flush_writes(self, timeout: float = 10) -> bool:
        if not self._writer_active():
            return True
        completed = threading.Event()
        try:
            self._write_queue.put(
                _WriteItem(kind="barrier", completed=completed),
                timeout=max(0.1, timeout),
            )
        except queue.Full:
            return False
        return completed.wait(timeout=max(0.1, timeout))

    def writer_stats(self) -> dict[str, Any]:
        with self._stats_lock:
            stats = dict(self._write_stats)
        return {
            "enabled": self._writer_active(),
            "queued": self._write_queue.qsize(),
            "queue_capacity": self.write_queue_size,
            "batch_size": self.write_batch_size,
            "flush_interval_ms": round(self.write_flush_interval_seconds * 1000),
            "cleanup_interval_seconds": self.cleanup_interval_seconds,
            **stats,
        }

    def _writer_active(self) -> bool:
        thread = self._writer_thread
        return bool(thread and thread.is_alive())

    def _enqueue_write(self, item: _WriteItem) -> bool:
        try:
            self._write_queue.put_nowait(item)
        except queue.Full:
            with self._stats_lock:
                self._write_stats["dropped"] += 1
                dropped = self._write_stats["dropped"]
            if dropped == 1 or dropped & (dropped - 1) == 0:
                logger.warning(
                    "EduGate SQLite write queue is full; dropped %s item(s)",
                    dropped,
                )
            return False
        with self._stats_lock:
            self._write_stats["enqueued"] += 1
        return True

    def _writer_loop(self) -> None:
        next_cleanup = time.monotonic() + self.cleanup_interval_seconds
        try:
            with self._connect() as conn:
                conn.execute("PRAGMA synchronous = NORMAL")
                while True:
                    timeout = min(0.25, max(0.01, next_cleanup - time.monotonic()))
                    try:
                        first = self._write_queue.get(timeout=timeout)
                    except queue.Empty:
                        if time.monotonic() >= next_cleanup:
                            self._run_periodic_cleanup(conn)
                            next_cleanup = time.monotonic() + self.cleanup_interval_seconds
                        if self._writer_stop.is_set() and self._write_queue.empty():
                            break
                        continue

                    batch = [first]
                    deadline = time.monotonic() + self.write_flush_interval_seconds
                    while len(batch) < self.write_batch_size:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        try:
                            batch.append(self._write_queue.get(timeout=remaining))
                        except queue.Empty:
                            break

                    write_items = [item for item in batch if item.kind in {"request", "classroom"}]
                    cleanup_due = time.monotonic() >= next_cleanup
                    written = 0
                    failed = 0
                    error_message = None
                    try:
                        with conn:
                            for item in write_items:
                                if item.kind == "request":
                                    self._insert_request_log(conn, item.payload or {})
                                else:
                                    self._insert_classroom_turn(conn, item.payload or {})
                                written += 1
                            if cleanup_due:
                                self._cleanup_storage(conn)
                    except Exception as error:
                        failed = len(write_items)
                        written = 0
                        error_message = f"{type(error).__name__}: {error}"
                        logger.warning("Failed to write EduGate SQLite batch: %s", error_message)
                    finally:
                        with self._stats_lock:
                            self._write_stats["written"] += written
                            self._write_stats["failed"] += failed
                            self._write_stats["batches"] += int(bool(write_items))
                            if error_message:
                                self._write_stats["last_error"] = error_message
                            if cleanup_due and not error_message:
                                self._write_stats["last_cleanup_at"] = time.time()
                        if cleanup_due:
                            next_cleanup = time.monotonic() + self.cleanup_interval_seconds
                        for item in batch:
                            if item.completed:
                                item.completed.set()
                            self._write_queue.task_done()

                    if any(item.kind == "stop" for item in batch):
                        break
        except Exception as error:
            logger.exception("EduGate SQLite writer stopped unexpectedly: %s", error)
            with self._stats_lock:
                self._write_stats["last_error"] = f"{type(error).__name__}: {error}"
        finally:
            abandoned = 0
            while True:
                try:
                    item = self._write_queue.get_nowait()
                except queue.Empty:
                    break
                abandoned += int(item.kind in {"request", "classroom"})
                if item.completed:
                    item.completed.set()
                self._write_queue.task_done()
            if abandoned:
                with self._stats_lock:
                    self._write_stats["failed"] += abandoned

    def _run_periodic_cleanup(self, conn: sqlite3.Connection) -> None:
        try:
            with conn:
                self._cleanup_storage(conn)
            with self._stats_lock:
                self._write_stats["last_cleanup_at"] = time.time()
        except Exception as error:
            logger.warning("Failed to clean EduGate SQLite records: %s", error)
            with self._stats_lock:
                self._write_stats["last_error"] = f"{type(error).__name__}: {error}"

    def is_admin_initialized(self, username: str) -> bool:
        with self._connect() as conn:
            setting = conn.execute(
                "SELECT value FROM app_settings WHERE key = 'admin_initialized'"
            ).fetchone()
            if setting is not None:
                return setting["value"] == "1"
            row = conn.execute(
                "SELECT password_hash FROM edugate_teachers WHERE username = ? AND role = 'admin'",
                (username,),
            ).fetchone()
            if row is None or verify_password("edugate", row["password_hash"]):
                return False
            self._set_setting(conn, "admin_initialized", "1")
            return True

    def setup_admin(self, *, username: str, password: str, display_name: str) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            initialized = conn.execute(
                "SELECT value FROM app_settings WHERE key = 'admin_initialized'"
            ).fetchone()
            if initialized is not None and initialized["value"] == "1":
                raise ValueError("Administrator has already been initialized")
            conn.execute(
                """
                INSERT INTO edugate_teachers (username, password_hash, display_name, role, is_active)
                VALUES (?, ?, ?, 'admin', 1)
                ON CONFLICT(username) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    display_name = excluded.display_name,
                    role = 'admin',
                    is_active = 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (username, hash_password(password), display_name),
            )
            self._set_setting(conn, "admin_initialized", "1")
            row = self._teacher_row(conn, username)
        return _json_ready(row)

    def change_teacher_password(self, username: str, password: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE edugate_teachers
                SET password_hash = ?, updated_at = CURRENT_TIMESTAMP
                WHERE username = ?
                """,
                (hash_password(password), username),
            )
            row = self._teacher_row(conn, username)
        return _json_ready(row) if row else None

    @staticmethod
    def _set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )

    def seed_teacher(
        self,
        *,
        username: str,
        password: str | None,
        display_name: str = "系统管理员",
        role: str = "admin",
    ) -> None:
        if not username or not password:
            return
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT id FROM edugate_teachers WHERE username = ?",
                (username,),
            ).fetchone()
            if exists:
                if role == "admin":
                    self._set_setting(conn, "admin_initialized", "1")
                return
            conn.execute(
                """
                INSERT INTO edugate_teachers (username, password_hash, display_name, role, is_active)
                VALUES (?, ?, ?, ?, 1)
                """,
                (username, hash_password(password), display_name, role),
            )
            if role == "admin":
                self._set_setting(conn, "admin_initialized", "1")

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def checkpoint(self) -> None:
        if not Path(self.sqlite_path).exists():
            return
        self.flush_writes()
        with self._connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def authenticate_teacher(self, username: str, password: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, username, password_hash, display_name, role, is_active
                FROM edugate_teachers
                WHERE username = ?
                """,
                (username,),
            ).fetchone()
            if not row or not row["is_active"]:
                return None
            if not verify_password(password, row["password_hash"]):
                return None
            conn.execute(
                "UPDATE edugate_teachers SET last_login_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (row["id"],),
            )
        public_row = dict(row)
        public_row.pop("password_hash", None)
        return _json_ready(public_row)


    def get_teacher(self, username: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT username, display_name, role, is_active, created_at, updated_at
                FROM edugate_teachers
                WHERE username = ?
                """,
                (username,),
            ).fetchone()
        return _json_ready(row) if row else None





    def list_teaching_sessions(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, teacher_username, title, course_name, class_name, scenario_id,
                       is_active, created_at, updated_at
                FROM edugate_teaching_sessions
                ORDER BY created_at DESC, id DESC
                """
            ).fetchall()
        return [_json_ready(row) for row in rows]

    def get_teaching_session(
        self,
        *,
        session_id: str | None = None,
        access_code: str | None = None,
    ) -> dict[str, Any] | None:
        if not session_id and not access_code:
            return None
        where = "id = ?" if session_id else "access_code = ?"
        value = session_id or access_code
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT id, teacher_username, title, course_name, class_name, scenario_id,
                       is_active, created_at, updated_at
                FROM edugate_teaching_sessions
                WHERE {where}
                """,
                (value,),
            ).fetchone()
        return _json_ready(row) if row else None

    def upsert_teaching_session(
        self,
        *,
        session_id: str,
        teacher_username: str,
        title: str,
        course_name: str = "",
        class_name: str = "",
        scenario_id: str = "default",
        access_code: str | None = None,
        is_active: bool = True,
    ) -> dict[str, Any]:
        access_code = access_code or session_id
        with self._connect() as conn:
            teacher = conn.execute(
                "SELECT username FROM edugate_teachers WHERE username = ? AND is_active = 1",
                (teacher_username,),
            ).fetchone()
            if not teacher:
                raise ValueError(f"unknown or inactive teacher: {teacher_username}")
            conn.execute(
                """
                INSERT INTO edugate_teaching_sessions (
                    id, teacher_username, title, course_name, class_name,
                    scenario_id, access_code, is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    teacher_username = excluded.teacher_username,
                    title = excluded.title,
                    course_name = excluded.course_name,
                    class_name = excluded.class_name,
                    scenario_id = excluded.scenario_id,
                    access_code = excluded.access_code,
                    is_active = excluded.is_active,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    session_id,
                    teacher_username,
                    title,
                    course_name,
                    class_name,
                    scenario_id,
                    access_code,
                    int(is_active),
                ),
            )
            row = conn.execute(
                """
                SELECT id, teacher_username, title, course_name, class_name,
                       scenario_id, is_active, created_at, updated_at
                FROM edugate_teaching_sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        return _json_ready(row)

    def set_teaching_session_active(self, session_id: str, is_active: bool) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE edugate_teaching_sessions
                SET is_active = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(is_active), session_id),
            )
            row = conn.execute(
                """
                SELECT id, teacher_username, title, course_name, class_name,
                       scenario_id, is_active, created_at, updated_at
                FROM edugate_teaching_sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        return _json_ready(row) if row else None

    def record_classroom_turn(
        self,
        *,
        classroom_instance_id: str,
        teacher_username: str,
        student_session_id: str,
        computer_name: str = "",
        client_ip: str = "",
        kind: str,
        input_content: str,
        output_content: str,
        status_code: int,
        latency_ms: int,
        queue_wait_ms: int | None = None,
        timed_out: bool | None = None,
    ) -> str:
        if kind not in {"chat", "python"}:
            raise ValueError(f"unsupported classroom turn kind: {kind}")
        run_id = hashlib.sha256(
            f"{classroom_instance_id}:{teacher_username}".encode("utf-8")
        ).hexdigest()[:32]
        title = f"课堂记录 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        input_content = input_content[: self.classroom_record_max_content_chars]
        output_content = output_content[: self.classroom_record_max_content_chars]
        payload = {
            "run_id": run_id,
            "classroom_instance_id": classroom_instance_id,
            "teacher_username": teacher_username,
            "student_session_id": student_session_id,
            "computer_name": computer_name[:120],
            "client_ip": client_ip[:80],
            "kind": kind,
            "input_content": input_content,
            "output_content": output_content,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "queue_wait_ms": queue_wait_ms,
            "timed_out": _optional_bool(timed_out),
            "title": title,
        }
        if self._writer_active():
            self._enqueue_write(_WriteItem(kind="classroom", payload=payload))
            return run_id
        with self._connect() as conn:
            self._insert_classroom_turn(conn, payload)
            self._cleanup_storage(conn)
        return run_id

    @staticmethod
    def _insert_classroom_turn(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
        teacher = conn.execute(
            "SELECT username FROM edugate_teachers WHERE username = ? AND is_active = 1",
            (payload["teacher_username"],),
        ).fetchone()
        if not teacher:
            raise ValueError(f"unknown or inactive teacher: {payload['teacher_username']}")
        conn.execute(
            """
            INSERT INTO classroom_runs (
                id, classroom_instance_id, teacher_username, title, ended_at
            )
            VALUES (?, ?, ?, ?, NULL)
            ON CONFLICT(id) DO UPDATE SET
                last_activity_at = CURRENT_TIMESTAMP,
                ended_at = NULL
            """,
            (
                payload["run_id"],
                payload["classroom_instance_id"],
                payload["teacher_username"],
                payload["title"],
            ),
        )
        conn.execute(
            """
            INSERT INTO classroom_turns (
                classroom_run_id, student_session_id, computer_name, client_ip, kind, input_content,
                output_content, status_code, latency_ms, queue_wait_ms, timed_out
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["run_id"],
                payload["student_session_id"],
                payload["computer_name"],
                payload["client_ip"],
                payload["kind"],
                payload["input_content"],
                payload["output_content"],
                payload["status_code"],
                payload["latency_ms"],
                payload["queue_wait_ms"],
                payload["timed_out"],
            ),
        )
        conn.execute(
            "UPDATE classroom_runs SET last_activity_at = CURRENT_TIMESTAMP WHERE id = ?",
            (payload["run_id"],),
        )

    def end_classroom_instance(self, classroom_instance_id: str) -> None:
        self.flush_writes()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE classroom_runs
                SET ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP),
                    last_activity_at = CURRENT_TIMESTAMP
                WHERE classroom_instance_id = ?
                """,
                (classroom_instance_id,),
            )

    def list_classroom_records(
        self,
        *,
        teacher_username: str | None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.flush_writes()
        where = "WHERE r.teacher_username = ?" if teacher_username else ""
        params: list[Any] = [teacher_username] if teacher_username else []
        params.append(min(max(limit, 1), 200))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    r.id,
                    r.teacher_username,
                    t.display_name AS teacher_display_name,
                    r.title,
                    r.started_at,
                    r.ended_at,
                    r.last_activity_at,
                    COUNT(ct.id) AS turn_count,
                    COUNT(DISTINCT ct.student_session_id) AS student_count,
                    COALESCE(SUM(CASE WHEN ct.kind = 'chat' THEN 1 ELSE 0 END), 0) AS chat_count,
                    COALESCE(SUM(CASE WHEN ct.kind = 'python' THEN 1 ELSE 0 END), 0) AS python_count,
                    COALESCE(SUM(CASE WHEN ct.status_code >= 400 THEN 1 ELSE 0 END), 0) AS error_count
                FROM classroom_runs r
                JOIN edugate_teachers t ON t.username = r.teacher_username
                LEFT JOIN classroom_turns ct ON ct.classroom_run_id = r.id
                {where}
                GROUP BY r.id
                ORDER BY r.last_activity_at DESC, r.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_json_ready(row) for row in rows]

    def get_classroom_record(
        self,
        run_id: str,
        *,
        teacher_username: str | None,
        limit: int = 1000,
    ) -> dict[str, Any] | None:
        self.flush_writes()
        where = "r.id = ?"
        params: list[Any] = [run_id]
        if teacher_username:
            where += " AND r.teacher_username = ?"
            params.append(teacher_username)
        with self._connect() as conn:
            session = conn.execute(
                f"""
                SELECT r.id, r.teacher_username, t.display_name AS teacher_display_name,
                       r.title, r.started_at, r.ended_at, r.last_activity_at
                FROM classroom_runs r
                JOIN edugate_teachers t ON t.username = r.teacher_username
                WHERE {where}
                """,
                params,
            ).fetchone()
            if session is None:
                return None
            turns = conn.execute(
                """
                SELECT id, student_session_id, computer_name, client_ip, kind, input_content, output_content,
                       status_code, latency_ms, queue_wait_ms, timed_out, created_at
                FROM classroom_turns
                WHERE classroom_run_id = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (run_id, min(max(limit, 1), 5000)),
            ).fetchall()
        return {
            "session": _json_ready(session),
            "turns": [_json_ready(row) for row in turns],
        }

    def delete_classroom_record(
        self,
        run_id: str,
        *,
        teacher_username: str | None,
    ) -> bool:
        self.flush_writes()
        where = "id = ?"
        params: list[Any] = [run_id]
        if teacher_username:
            where += " AND teacher_username = ?"
            params.append(teacher_username)
        with self._connect() as conn:
            cursor = conn.execute(f"DELETE FROM classroom_runs WHERE {where}", params)
        return cursor.rowcount > 0

    def _cleanup_classroom_records(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            DELETE FROM classroom_runs
            WHERE last_activity_at < datetime('now', ?)
            """,
            (f"-{self.classroom_record_retention_days} days",),
        )
        conn.execute(
            """
            DELETE FROM classroom_turns
            WHERE id NOT IN (
                SELECT id FROM classroom_turns ORDER BY id DESC LIMIT ?
            )
            """,
            (self.classroom_record_max_records,),
        )
        conn.execute(
            """
            DELETE FROM classroom_runs
            WHERE ended_at IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM classroom_turns WHERE classroom_turns.classroom_run_id = classroom_runs.id
              )
            """
        )

    def _cleanup_storage(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            DELETE FROM ai_request_logs
            WHERE id NOT IN (
                SELECT id FROM ai_request_logs ORDER BY id DESC LIMIT ?
            )
            """,
            (self.log_max_records,),
        )
        self._cleanup_classroom_records(conn)

    def log_request(
        self,
        *,
        route: str,
        scenario_id: str | None,
        model: str | None,
        knowledge_source_id: str | None,
        user_message_preview: str | None,
        status_code: int,
        latency_ms: int,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
        teacher_id: str | None = None,
        stream_done: bool | None = None,
        stream_chunks: int | None = None,
        stream_bytes: int | None = None,
        stream_duration_ms: int | None = None,
        stream_finish_reason: str | None = None,
    ) -> None:
        usage = usage or {}
        payload = {
            "route": route,
            "scenario_id": scenario_id,
            "teacher_id": teacher_id,
            "model": model,
            "knowledge_source_id": knowledge_source_id,
            "user_message_preview": user_message_preview,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "stream_done": _optional_bool(stream_done),
            "stream_chunks": stream_chunks,
            "stream_bytes": stream_bytes,
            "stream_duration_ms": stream_duration_ms,
            "stream_finish_reason": stream_finish_reason,
            "error": error,
        }
        if self._writer_active():
            self._enqueue_write(_WriteItem(kind="request", payload=payload))
            return
        try:
            with self._connect() as conn:
                self._insert_request_log(conn, payload)
                self._cleanup_storage(conn)
        except Exception as error:
            logger.warning("Failed to write EduGate request log: %s", error)

    @staticmethod
    def _insert_request_log(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO ai_request_logs (
                route,
                scenario_id,
                teacher_id,
                model,
                knowledge_source_id,
                user_message_preview,
                status_code,
                latency_ms,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                stream_done,
                stream_chunks,
                stream_bytes,
                stream_duration_ms,
                stream_finish_reason,
                error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["route"],
                payload["scenario_id"],
                payload["teacher_id"],
                payload["model"],
                payload["knowledge_source_id"],
                payload["user_message_preview"],
                payload["status_code"],
                payload["latency_ms"],
                payload["prompt_tokens"],
                payload["completion_tokens"],
                payload["total_tokens"],
                payload["stream_done"],
                payload["stream_chunks"],
                payload["stream_bytes"],
                payload["stream_duration_ms"],
                payload["stream_finish_reason"],
                payload["error"],
            ),
        )

    def list_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        self.flush_writes()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    id,
                    created_at,
                    route,
                    scenario_id,
                    teacher_id,
                    model,
                    knowledge_source_id,
                    user_message_preview,
                    status_code,
                    latency_ms,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    stream_done,
                    stream_chunks,
                    stream_bytes,
                    stream_duration_ms,
                    stream_finish_reason,
                    error
                FROM ai_request_logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_json_ready(row) for row in rows]

    def dashboard(self) -> dict[str, Any]:
        self.flush_writes()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_requests,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    ROUND(AVG(latency_ms)) AS average_latency_ms,
                    SUM(CASE WHEN error IS NOT NULL OR status_code >= 400 THEN 1 ELSE 0 END) AS error_count
                FROM ai_request_logs
                """
            ).fetchone()
        return {"database_enabled": True, **_json_ready(row or {})}

    def _connect(self):
        conn = sqlite3.connect(self.sqlite_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @staticmethod
    def _teacher_row(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT id, username, display_name, role, is_active, created_at, updated_at, last_login_at
            FROM edugate_teachers
            WHERE username = ?
            """,
            (username,),
        ).fetchone()


def latest_user_preview(messages: list[Any], limit: int = 220) -> str:
    content = ""
    for message in reversed(messages):
        role = getattr(message, "role", None)
        if role == "user":
            content = getattr(message, "content", "")
            break
    return content[:limit]


def now_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return "pbkdf2_sha256$200000$%s$%s" % (
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = base64.b64decode(salt_text.encode("ascii"))
        expected = base64.b64decode(digest_text.encode("ascii"))
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return secrets.compare_digest(actual, expected)


def _json_ready(row: Any) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, datetime):
            output[key] = value.astimezone(timezone.utc).isoformat()
        elif key in {"is_active", "stream_done", "timed_out"} and value is not None:
            output[key] = bool(value)
        else:
            output[key] = value
    return output


def _optional_bool(value: bool | None) -> int | None:
    if value is None:
        return None
    return int(value)
