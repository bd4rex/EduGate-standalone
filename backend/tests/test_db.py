import sqlite3
import threading
import time
from pathlib import Path

from app.db import BusinessDB


def _log_request(db: BusinessDB, index: int) -> None:
    db.log_request(
        route="/chat",
        scenario_id="default",
        teacher_id="admin",
        model="test-model",
        knowledge_source_id=None,
        user_message_preview=f"message-{index}",
        status_code=200,
        latency_ms=index,
        usage={"total_tokens": index + 1},
    )


def test_async_writer_batches_logs_and_classroom_records(tmp_path: Path) -> None:
    db = BusinessDB(
        str(tmp_path / "async-writer.sqlite3"),
        write_queue_size=128,
        write_batch_size=64,
        write_flush_interval_ms=20,
    )
    db.init()
    db.seed_teacher(username="admin", password="edugate", display_name="Admin", role="admin")
    db.start_writer()
    try:
        for index in range(20):
            _log_request(db, index)
            db.record_classroom_turn(
                classroom_instance_id="async-classroom",
                teacher_username="admin",
                student_session_id=f"student-{index}",
                kind="chat",
                input_content=f"question-{index}",
                output_content=f"answer-{index}",
                status_code=200,
                latency_ms=index,
            )

        assert db.flush_writes(timeout=5) is True
        assert len(db.list_logs(limit=50)) == 20
        records = db.list_classroom_records(teacher_username="admin")
        assert records[0]["turn_count"] == 20
        stats = db.writer_stats()
        assert stats["written"] == 40
        assert 1 <= stats["batches"] < stats["written"]
        assert stats["dropped"] == 0

        db.end_classroom_instance("async-classroom")
        detail = db.get_classroom_record(records[0]["id"], teacher_username="admin")
        assert detail is not None and detail["session"]["ended_at"] is not None
    finally:
        assert db.stop_writer(timeout=5) is True


def test_async_writer_queue_is_bounded_and_reports_drops(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = BusinessDB(
        str(tmp_path / "bounded-writer.sqlite3"),
        write_queue_size=128,
        write_batch_size=1,
        write_flush_interval_ms=1,
    )
    db.init()
    entered = threading.Event()
    release = threading.Event()
    original_insert = db._insert_request_log

    def blocked_insert(conn, payload) -> None:
        entered.set()
        release.wait(timeout=5)
        original_insert(conn, payload)

    monkeypatch.setattr(db, "_insert_request_log", blocked_insert)
    db.start_writer()
    try:
        _log_request(db, 0)
        assert entered.wait(timeout=2)
        for index in range(1, 132):
            _log_request(db, index)
        assert db.writer_stats()["dropped"] >= 3
    finally:
        release.set()
        assert db.stop_writer(timeout=5) is True


def test_async_writer_runs_cleanup_on_a_timer(tmp_path: Path, monkeypatch) -> None:
    db = BusinessDB(
        str(tmp_path / "cleanup-writer.sqlite3"),
        cleanup_interval_seconds=0.05,
        write_flush_interval_ms=1,
    )
    db.init()
    cleanup_calls = 0
    original_cleanup = db._cleanup_storage

    def tracked_cleanup(conn) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        original_cleanup(conn)

    monkeypatch.setattr(db, "_cleanup_storage", tracked_cleanup)
    db.start_writer()
    try:
        deadline = time.monotonic() + 2
        while db.writer_stats()["last_cleanup_at"] is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cleanup_calls >= 1
        assert db.writer_stats()["last_cleanup_at"] is not None
    finally:
        assert db.stop_writer(timeout=5) is True


def test_sqlite_business_db_stores_single_admin_and_logs(tmp_path: Path) -> None:
    db = BusinessDB(str(tmp_path / "edugate.sqlite3"))
    db.init()
    db.seed_teacher(username="admin", password="edugate", display_name="Admin", role="admin")

    admin = db.authenticate_teacher("admin", "edugate")
    assert admin is not None
    assert admin["username"] == "admin"
    assert admin["is_active"] is True

    db.log_request(
        route="/chat",
        scenario_id="default",
        teacher_id="admin",
        model="deepseek-chat",
        knowledge_source_id=None,
        user_message_preview="hello",
        status_code=200,
        latency_ms=12,
        usage={"total_tokens": 3},
    )

    logs = db.list_logs()
    assert len(logs) == 1
    assert logs[0]["teacher_id"] == "admin"
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


def test_classroom_records_are_grouped_by_class_and_scoped_to_admin(tmp_path: Path) -> None:
    db = BusinessDB(str(tmp_path / "records.sqlite3"))
    db.init()
    db.seed_teacher(username="admin", password="secure-password", display_name="Admin", role="admin")

    first_run_id = db.record_classroom_turn(
        classroom_instance_id="classroom-one",
        teacher_username="admin",
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
        teacher_username="admin",
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
    assert first_run_id == second_turn_run_id
    records = db.list_classroom_records(teacher_username="admin")
    assert len(records) == 1
    assert records[0]["student_count"] == 2
    assert records[0]["chat_count"] == 1
    assert records[0]["python_count"] == 1
    assert len(db.list_classroom_records(teacher_username=None)) == 1

    detail = db.get_classroom_record(first_run_id, teacher_username="admin")
    assert detail is not None
    assert [turn["kind"] for turn in detail["turns"]] == ["chat", "python"]
    assert detail["turns"][0]["student_session_id"] == "student-session-1"
    assert detail["turns"][0]["computer_name"] == "LAB-PC-01"
    assert detail["turns"][0]["client_ip"] == "192.168.10.31"
    assert db.get_classroom_record(first_run_id, teacher_username="other") is None

    db.end_classroom_instance("classroom-one")
    ended = db.get_classroom_record(first_run_id, teacher_username="admin")
    assert ended is not None and ended["session"]["ended_at"] is not None
    assert db.delete_classroom_record(first_run_id, teacher_username="other") is False
    assert db.delete_classroom_record(first_run_id, teacher_username="admin") is True


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
    db.seed_teacher(username="admin", password="secure-password", display_name="Admin", role="admin")
    old_run = db.record_classroom_turn(
        classroom_instance_id="old-classroom",
        teacher_username="admin",
        student_session_id="student-old",
        kind="chat",
        input_content="x" * 800,
        output_content="y" * 800,
        status_code=200,
        latency_ms=1,
    )
    old_detail = db.get_classroom_record(old_run, teacher_username="admin")
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
        teacher_username="admin",
        student_session_id="student-current",
        kind="chat",
        input_content="current",
        output_content="current answer",
        status_code=200,
        latency_ms=1,
    )
    assert db.get_classroom_record(old_run, teacher_username="admin") is None
