from __future__ import annotations

from app.security import SessionStore, SlidingWindowRateLimiter, StudentSessionStore


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


def test_student_session_is_reused_and_scoped_to_current_classroom(monkeypatch) -> None:
    now = 1_000.0
    monkeypatch.setattr("app.security.time.time", lambda: now)
    store = StudentSessionStore(ttl_seconds=60)

    token, record = store.issue("classroom-a")
    reused_token, reused_record = store.issue("classroom-a", existing_token=token)

    assert reused_token == token
    assert reused_record.student_id == record.student_id
    assert store.resolve(token, classroom_token="classroom-a") == record
    assert store.resolve(token, classroom_token="classroom-b") is None


def test_student_session_expires(monkeypatch) -> None:
    now = 1_000.0
    monkeypatch.setattr("app.security.time.time", lambda: now)
    store = StudentSessionStore(ttl_seconds=10)
    token, _ = store.issue("classroom")

    now = 1_011.0
    assert store.resolve(token, classroom_token="classroom") is None
