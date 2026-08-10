import sqlite3
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


def test_classroom_records_are_grouped_by_class_and_scoped_to_teacher(tmp_path: Path) -> None:
    db = BusinessDB(str(tmp_path / "records.sqlite3"))
    db.init()
    for username in ("teacher-a", "teacher-b"):
        db.upsert_teacher(
            username=username,
            password="secure-password",
            display_name=username,
            role="teacher",
        )

    first_run_id = db.record_classroom_turn(
        classroom_instance_id="classroom-one",
        teacher_username="teacher-a",
        student_session_id="student-session-1",
        computer_name="LAB-PC-01",
        client_ip="192.168.10.31",
        kind="chat",
        input_content="为什么要通分？",
        output_content="因为需要相同的计数单位。",
        status_code=200,
        latency_ms=120,
    )
    second_turn_run_id = db.record_classroom_turn(
        classroom_instance_id="classroom-one",
        teacher_username="teacher-a",
        student_session_id="student-session-2",
        computer_name="LAB-PC-02",
        client_ip="192.168.10.32",
        kind="python",
        input_content="print(1 + 1)",
        output_content="2\n",
        status_code=200,
        latency_ms=30,
        queue_wait_ms=4,
        timed_out=False,
    )
    db.record_classroom_turn(
        classroom_instance_id="classroom-one",
        teacher_username="teacher-b",
        student_session_id="student-session-3",
        computer_name="LAB-PC-03",
        client_ip="192.168.10.33",
        kind="chat",
        input_content="另一个老师的问题",
        output_content="另一个老师的回答",
        status_code=200,
        latency_ms=50,
    )

    assert first_run_id == second_turn_run_id
    teacher_a_records = db.list_classroom_records(teacher_username="teacher-a")
    assert len(teacher_a_records) == 1
    assert teacher_a_records[0]["student_count"] == 2
    assert teacher_a_records[0]["chat_count"] == 1
    assert teacher_a_records[0]["python_count"] == 1
    assert len(db.list_classroom_records(teacher_username=None)) == 2

    detail = db.get_classroom_record(first_run_id, teacher_username="teacher-a")
    assert detail is not None
    assert [turn["kind"] for turn in detail["turns"]] == ["chat", "python"]
    assert detail["turns"][0]["student_session_id"] == "student-session-1"
    assert detail["turns"][0]["computer_name"] == "LAB-PC-01"
    assert detail["turns"][0]["client_ip"] == "192.168.10.31"
    assert db.get_classroom_record(first_run_id, teacher_username="teacher-b") is None

    db.end_classroom_instance("classroom-one")
    ended = db.get_classroom_record(first_run_id, teacher_username="teacher-a")
    assert ended is not None and ended["session"]["ended_at"] is not None
    assert db.delete_classroom_record(first_run_id, teacher_username="teacher-b") is False
    assert db.delete_classroom_record(first_run_id, teacher_username="teacher-a") is True


def test_classroom_record_schema_adds_computer_identity_without_deleting_old_data(tmp_path: Path) -> None:
    path = tmp_path / "old-records.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE classroom_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                classroom_run_id TEXT NOT NULL,
                student_session_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                input_content TEXT NOT NULL,
                output_content TEXT NOT NULL DEFAULT '',
                status_code INTEGER NOT NULL,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                queue_wait_ms INTEGER,
                timed_out INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO classroom_turns (
                classroom_run_id, student_session_id, kind, input_content, status_code
            ) VALUES ('old-run', 'old-session', 'chat', 'old question', 200);
            """
        )

    BusinessDB(str(path)).init()

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(classroom_turns)")}
        old_row = conn.execute(
            "SELECT computer_name, client_ip FROM classroom_turns WHERE id = 1"
        ).fetchone()
    assert {"computer_name", "client_ip"} <= columns
    assert old_row == ("", "")


def test_classroom_record_retention_and_content_limits_are_enforced(tmp_path: Path) -> None:
    path = tmp_path / "retention.sqlite3"
    db = BusinessDB(
        str(path),
        classroom_record_retention_days=7,
        classroom_record_max_content_chars=500,
    )
    db.init()
    db.upsert_teacher(
        username="teacher",
        password="secure-password",
        display_name="Teacher",
        role="teacher",
    )
    old_run = db.record_classroom_turn(
        classroom_instance_id="old-classroom",
        teacher_username="teacher",
        student_session_id="student-old",
        kind="chat",
        input_content="x" * 800,
        output_content="y" * 800,
        status_code=200,
        latency_ms=1,
    )
    old_detail = db.get_classroom_record(old_run, teacher_username="teacher")
    assert old_detail is not None
    assert len(old_detail["turns"][0]["input_content"]) == 500
    assert len(old_detail["turns"][0]["output_content"]) == 500

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE classroom_runs SET last_activity_at = '2000-01-01 00:00:00' WHERE id = ?",
            (old_run,),
        )
    db.record_classroom_turn(
        classroom_instance_id="current-classroom",
        teacher_username="teacher",
        student_session_id="student-current",
        kind="chat",
        input_content="current",
        output_content="current answer",
        status_code=200,
        latency_ms=1,
    )
    assert db.get_classroom_record(old_run, teacher_username="teacher") is None
