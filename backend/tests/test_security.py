from __future__ import annotations

from app.security import ClassroomAccess, SessionStore, SlidingWindowRateLimiter, StudentSessionStore


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

    token, record = store.issue(
        "classroom-a",
        computer_name="LAB-PC-01",
        client_ip="192.168.1.21",
    )
    reused_token, reused_record = store.issue(
        "classroom-a",
        existing_token=token,
        computer_name="LAB-PC-01-UPDATED",
        client_ip="192.168.1.22",
    )

    assert reused_token == token
    assert reused_record.student_id == record.student_id
    assert reused_record.computer_name == "LAB-PC-01-UPDATED"
    assert reused_record.client_ip == "192.168.1.22"
    assert store.identity(reused_record).student_id == record.student_id
    assert store.resolve(token, classroom_token="classroom-a") == reused_record
    assert store.resolve(token, classroom_token="classroom-b") is None


def test_student_session_expires(monkeypatch) -> None:
    now = 1_000.0
    monkeypatch.setattr("app.security.time.time", lambda: now)
    store = StudentSessionStore(ttl_seconds=10)
    token, _ = store.issue("classroom")

    now = 1_011.0
    assert store.resolve(token, classroom_token="classroom") is None


def test_legacy_student_identity_is_private_and_rotates_with_classroom() -> None:
    access = ClassroomAccess()
    first = access.legacy_student_id("192.168.1.25")

    assert first == access.legacy_student_id("192.168.1.25")
    assert "192.168.1.25" not in first
    access.rotate()
    assert access.legacy_student_id("192.168.1.25") != first


def test_classroom_access_reuses_persisted_token_across_sessions() -> None:
    access = ClassroomAccess("persisted-classroom-token")
    old_token = access.token()

    assert access.active() is True
    access.end()
    assert access.active() is False
    assert access.validated_token(old_token) is None

    new_token = access.start()
    assert access.active() is True
    assert new_token == old_token
    assert access.matches(new_token) is True

    rotated_token = access.rotate()
    assert rotated_token != old_token
    assert access.matches(old_token) is False
    assert access.matches(rotated_token) is True
