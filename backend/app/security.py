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
        if not candidate:
            return False
        with self._lock:
            return secrets.compare_digest(candidate, self._token)
