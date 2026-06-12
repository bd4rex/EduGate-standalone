from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class BusinessDB:
    def __init__(self, database_url: str | None, sqlite_path: str = "edugate.sqlite3") -> None:
        self.database_url = database_url
        self.sqlite_path = sqlite_path
        self.enabled = True

    def init(self) -> None:
        Path(self.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
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
                )
                """
            )
            conn.execute(
                """
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
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_feedback_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    request_log_id INTEGER REFERENCES ai_request_logs(id) ON DELETE SET NULL,
                    score INTEGER NOT NULL,
                    comment TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
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
                )
                """
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
                return
            conn.execute(
                """
                INSERT INTO edugate_teachers (username, password_hash, display_name, role, is_active)
                VALUES (?, ?, ?, ?, 1)
                """,
                (username, hash_password(password), display_name, role),
            )

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

    def list_teachers(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, username, display_name, role, is_active, created_at, updated_at, last_login_at
                FROM edugate_teachers
                ORDER BY id
                """
            ).fetchall()
        return [_json_ready(row) for row in rows]

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

    def upsert_teacher(
        self,
        *,
        username: str,
        password: str | None,
        display_name: str = "",
        role: str = "teacher",
        is_active: bool = True,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM edugate_teachers WHERE username = ?",
                (username,),
            ).fetchone()
            if existing:
                if password:
                    conn.execute(
                        """
                        UPDATE edugate_teachers
                        SET password_hash = ?,
                            display_name = ?,
                            role = ?,
                            is_active = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE username = ?
                        """,
                        (hash_password(password), display_name, role, int(is_active), username),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE edugate_teachers
                        SET display_name = ?,
                            role = ?,
                            is_active = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE username = ?
                        """,
                        (display_name, role, int(is_active), username),
                    )
            else:
                if not password:
                    raise ValueError("password is required for new teacher")
                conn.execute(
                    """
                    INSERT INTO edugate_teachers (username, password_hash, display_name, role, is_active)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (username, hash_password(password), display_name, role, int(is_active)),
                )
            row = self._teacher_row(conn, username)
        return _json_ready(row)

    def update_teacher_password(self, username: str, password: str) -> dict[str, Any] | None:
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

    def set_teacher_active(self, username: str, is_active: bool) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE edugate_teachers
                SET is_active = ?, updated_at = CURRENT_TIMESTAMP
                WHERE username = ?
                """,
                (int(is_active), username),
            )
            row = self._teacher_row(conn, username)
        return _json_ready(row) if row else None

    def delete_teacher(self, username: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = self._teacher_row(conn, username)
            if not row:
                return None
            conn.execute("DELETE FROM edugate_teaching_sessions WHERE teacher_username = ?", (username,))
            conn.execute("DELETE FROM edugate_teachers WHERE username = ?", (username,))
        return _json_ready(row)

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
        try:
            with self._connect() as conn:
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
                        route,
                        scenario_id,
                        teacher_id,
                        model,
                        knowledge_source_id,
                        user_message_preview,
                        status_code,
                        latency_ms,
                        usage.get("prompt_tokens"),
                        usage.get("completion_tokens"),
                        usage.get("total_tokens"),
                        _optional_bool(stream_done),
                        stream_chunks,
                        stream_bytes,
                        stream_duration_ms,
                        stream_finish_reason,
                        error,
                    ),
                )
        except Exception:
            return

    def list_logs(self, limit: int = 50) -> list[dict[str, Any]]:
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
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
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
        elif key in {"is_active", "stream_done"} and value is not None:
            output[key] = bool(value)
        else:
            output[key] = value
    return output


def _optional_bool(value: bool | None) -> int | None:
    if value is None:
        return None
    return int(value)
