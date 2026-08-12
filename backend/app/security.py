from __future__ import annotations

import hashlib
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
    computer_name: str
    client_ip: str
    expires_at: float


@dataclass(frozen=True)
class StudentIdentity:
    student_id: str
    computer_name: str
    client_ip: str


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
        computer_name: str = "",
        client_ip: str = "",
        student_id: str | None = None,
    ) -> tuple[str, StudentSessionRecord]:
        now = time.time()
        with self._lock:
            self._cleanup_locked(now)
            if existing_token:
                existing = self._sessions.get(existing_token)
                if existing and secrets.compare_digest(existing.classroom_token, classroom_token):
                    updated = StudentSessionRecord(
                        student_id=existing.student_id,
                        classroom_token=existing.classroom_token,
                        computer_name=computer_name or existing.computer_name,
                        client_ip=client_ip or existing.client_ip,
                        expires_at=existing.expires_at,
                    )
                    self._sessions[existing_token] = updated
                    return existing_token, updated
            token = secrets.token_urlsafe(32)
            record = StudentSessionRecord(
                student_id=student_id or secrets.token_urlsafe(12),
                classroom_token=classroom_token,
                computer_name=computer_name,
                client_ip=client_ip,
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

    def identity(self, record: StudentSessionRecord) -> StudentIdentity:
        return StudentIdentity(
            student_id=record.student_id,
            computer_name=record.computer_name,
            client_ip=record.client_ip,
        )

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    def active_count(self, *, classroom_token: str | None = None) -> int:
        now = time.time()
        with self._lock:
            self._cleanup_locked(now)
            records = self._sessions.values()
            if classroom_token is not None:
                records = (
                    record
                    for record in records
                    if secrets.compare_digest(record.classroom_token, classroom_token)
                )
            return len({record.student_id for record in records})

    def _cleanup_locked(self, now: float) -> None:
        for token, record in list(self._sessions.items()):
            if record.expires_at <= now:
                self._sessions.pop(token, None)


class ClassroomAccess:
    def __init__(self, token: str | None = None) -> None:
        self._lock = threading.RLock()
        self._token = token or secrets.token_urlsafe(24)
        self._classroom_id = secrets.token_urlsafe(12)
        self._identity_secret = secrets.token_bytes(32)
        self._active = True

    def active(self) -> bool:
        with self._lock:
            return self._active

    def token(self) -> str:
        with self._lock:
            return self._token

    def classroom_id(self) -> str:
        with self._lock:
            return self._classroom_id

    def legacy_student_id(self, client_identity: str) -> str:
        with self._lock:
            digest = hashlib.blake2b(
                client_identity.encode("utf-8"),
                key=self._identity_secret,
                digest_size=10,
            ).hexdigest()
        return f"legacy-{digest}"

    def rotate(self) -> str:
        with self._lock:
            self._token = secrets.token_urlsafe(24)
            self._classroom_id = secrets.token_urlsafe(12)
            self._identity_secret = secrets.token_bytes(32)
            return self._token

    def start(self) -> str:
        with self._lock:
            self._classroom_id = secrets.token_urlsafe(12)
            self._identity_secret = secrets.token_bytes(32)
            self._active = True
            return self._token

    def end(self) -> None:
        with self._lock:
            self._active = False

    def matches(self, candidate: str | None) -> bool:
        return self.validated_token(candidate) is not None

    def validated_token(self, candidate: str | None) -> str | None:
        if not candidate:
            return None
        with self._lock:
            if not self._active:
                return None
            return self._token if secrets.compare_digest(candidate, self._token) else None
