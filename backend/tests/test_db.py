from pathlib import Path

from app.db import BusinessDB


def test_sqlite_business_db_stores_teachers_and_logs(tmp_path: Path) -> None:
    db = BusinessDB(str(tmp_path / "edugate.sqlite3"))
    db.init()
    db.seed_teacher(username="admin", password="edugate", display_name="Admin", role="admin")

    admin = db.authenticate_teacher("admin", "edugate")
    assert admin is not None
    assert admin["username"] == "admin"
    assert admin["is_active"] is True

    teacher = db.upsert_teacher(
        username="zhang",
        password="secret123",
        display_name="Zhang",
        role="teacher",
    )
    assert teacher["display_name"] == "Zhang"
    assert db.get_teacher("zhang")["role"] == "teacher"

    db.log_request(
        route="/chat",
        scenario_id="teacher:zhang",
        teacher_id="zhang",
        model="deepseek-chat",
        knowledge_source_id=None,
        user_message_preview="hello",
        status_code=200,
        latency_ms=12,
        usage={"total_tokens": 3},
    )

    logs = db.list_logs()
    assert len(logs) == 1
    assert logs[0]["teacher_id"] == "zhang"
    assert logs[0]["total_tokens"] == 3
    assert db.dashboard()["total_requests"] == 1


def test_first_admin_setup_is_one_time_and_has_no_default_password(tmp_path: Path) -> None:
    db = BusinessDB(str(tmp_path / "first-run.sqlite3"))
    db.init()

    assert db.is_admin_initialized("admin") is False
    db.setup_admin(username="admin", password="teacher-chosen-password", display_name="Admin")
    assert db.is_admin_initialized("admin") is True
    assert db.authenticate_teacher("admin", "edugate") is None
    assert db.authenticate_teacher("admin", "teacher-chosen-password") is not None

    try:
        db.setup_admin(username="admin", password="another-password", display_name="Admin")
    except ValueError as error:
        assert "already" in str(error)
    else:
        raise AssertionError("second administrator setup should be rejected")
