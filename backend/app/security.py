from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionRecord:
    username: str
    expires_at: float


@dataclass(frozen=True)
class StudentSessionRecord:
    student_id: str
    classroom_token: str
    expires_at: float


class SessionStore:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, SessionRecord] = {}
        self._lock = threading.RLock()

    def issue(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._cleanup_locked()
            self._sessions[token] = SessionRecord(username=username, expires_at=time.time() + self.ttl_seconds)
        return token

    def resolve(self, token: str) -> SessionRecord | None:
        with self._lock:
            record = self._sessions.get(token)
            if record is None:
                return None
            if record.expires_at <= time.time():
                self._sessions.pop(token, None)
                return None
            return record

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_user(self, username: str) -> None:
        with self._lock:
            for token, record in list(self._sessions.items()):
                if record.username == username:
                    self._sessions.pop(token, None)

    def _cleanup_locked(self) -> None:
        now = time.time()
        for token, record in list(self._sessions.items()):
            if record.expires_at <= now:
                self._sessions.pop(token, None)


class SlidingWindowRateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.RLock()

    def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        threshold = now - window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= threshold:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True


class StudentSessionStore:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, StudentSessionRecord] = {}
        self._lock = threading.RLock()

    def issue(
        self,
        classroom_token: str,
        *,
        existing_token: str | None = None,
    ) -> tuple[str, StudentSessionRecord]:
        now = time.time()
        with self._lock:
            self._cleanup_locked(now)
            if existing_token:
                existing = self._sessions.get(existing_token)
                if existing and secrets.compare_digest(existing.classroom_token, classroom_token):
                    return existing_token, existing
            token = secrets.token_urlsafe(32)
            record = StudentSessionRecord(
                student_id=secrets.token_urlsafe(12),
                classroom_token=classroom_token,
                expires_at=now + self.ttl_seconds,
            )
            self._sessions[token] = record
            return token, record

    def resolve(self, token: str, *, classroom_token: str) -> StudentSessionRecord | None:
        if not token:
            return None
        with self._lock:
            record = self._sessions.get(token)
            if record is None:
                return None
            if record.expires_at <= time.time() or not secrets.compare_digest(
                record.classroom_token,
                classroom_token,
            ):
                self._sessions.pop(token, None)
                return None
            return record

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    def _cleanup_locked(self, now: float) -> None:
        for token, record in list(self._sessions.items()):
            if record.expires_at <= now:
                self._sessions.pop(token, None)


class ClassroomAccess:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._token = secrets.token_urlsafe(24)

    def token(self) -> str:
        with self._lock:
            return self._token

    def rotate(self) -> str:
        with self._lock:
            self._token = secrets.token_urlsafe(24)
            return self._token

    def matches(self, candidate: str | None) -> bool:
        return self.validated_token(candidate) is not None

    def validated_token(self, candidate: str | None) -> str | None:
        if not candidate:
            return None
        with self._lock:
            return self._token if secrets.compare_digest(candidate, self._token) else None
