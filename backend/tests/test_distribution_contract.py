from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_teacher_launcher_is_web_only_and_windowless() -> None:
    launcher = _read("desktop/edugate_standalone.py").lower()
    spec = _read("desktop/edugate_standalone.spec")
    batch = _read("desktop/run_standalone.bat").lower()

    assert "tkinter" not in launcher
    assert "console=False" in spec
    assert "pythonw.exe" in batch
    assert 'start "" /b' in batch


def test_teacher_install_and_build_scripts_use_tsinghua_mirror() -> None:
    expected = "https://pypi.tuna.tsinghua.edu.cn/simple"

    assert expected in _read("desktop/install_backend_deps.bat")
    assert expected in _read("desktop/build_windows.bat")


def test_current_deepseek_default_is_not_retired_alias() -> None:
    config = _read("backend/app/config.py")
    example = _read("backend/.env.example")

    assert '"deepseek-v4-flash"' in config
    assert "DEFAULT_MODEL=deepseek-v4-flash" in example
    assert "DEFAULT_MODEL=deepseek-chat" not in example


def test_default_join_budget_covers_reloads_for_64_students() -> None:
    example = _read("backend/.env.example")

    assert "STUDENT_JOIN_RATE_LIMIT_PER_5_MINUTES=256" in example


def test_student_page_persists_history_and_uses_anonymous_session() -> None:
    page = _read("frontend/student.html")

    assert "POST" in page and "/classroom/join" in page
    assert '"X-Student-Token"' in page
    assert "localStorage.getItem(HISTORY_STORAGE_KEY)" in page
    assert "localStorage.setItem(" in page
    assert "messages.slice(-MAX_MESSAGES_TO_SEND)" in page
    assert "sessionStorage.setItem(SESSION_STORAGE_KEY" in page


def test_system_view_remains_admin_only() -> None:
    page = _read("frontend/admin.html")

    assert '<button class="tab" data-tab="system" data-admin-only>' in page
    assert '<section id="system-view" hidden data-admin-only>' in page
