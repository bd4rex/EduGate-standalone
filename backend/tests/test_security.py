from __future__ import annotations

from app.security import SessionStore, SlidingWindowRateLimiter


def test_session_expires_and_can_be_revoked(monkeypatch) -> None:
    now = 1_000.0
    monkeypatch.setattr("app.security.time.time", lambda: now)
    store = SessionStore(ttl_seconds=10)
    token = store.issue("teacher")
    assert store.resolve(token).username == "teacher"

    now = 1_011.0
    assert store.resolve(token) is None

    token = store.issue("teacher")
    store.revoke_user("teacher")
    assert store.resolve(token) is None


def test_sliding_window_rate_limit(monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr("app.security.time.monotonic", lambda: now)
    limiter = SlidingWindowRateLimiter()
    assert limiter.allow("student", limit=2, window_seconds=60) is True
    assert limiter.allow("student", limit=2, window_seconds=60) is True
    assert limiter.allow("student", limit=2, window_seconds=60) is False

    now = 161.0
    assert limiter.allow("student", limit=2, window_seconds=60) is True
