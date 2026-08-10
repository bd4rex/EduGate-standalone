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


def test_brand_assets_are_used_by_docs_web_and_windows_bundle() -> None:
    brand_dir = ROOT / "frontend" / "assets" / "brand"
    expected_assets = {
        "edugate-icon-mono.svg",
        "edugate-icon.svg",
        "edugate-logo-horizontal.svg",
        "edugate-icon-1024.png",
        "edugate-logo-horizontal.png",
    }

    assert expected_assets <= {path.name for path in brand_dir.iterdir()}
    assert (ROOT / "desktop" / "assets" / "edugate.ico").stat().st_size > 0
    for page_name in ("admin.html", "student.html", "teaching-embed-demo.html"):
        page = _read(f"frontend/{page_name}")
        assert 'rel="icon" href="assets/brand/edugate-icon.svg"' in page
        assert 'src="assets/brand/edugate-icon.svg"' in page
    assert 'icon=str(root / "desktop" / "assets" / "edugate.ico")' in _read(
        "desktop/edugate_standalone.spec"
    )


def test_chinese_and_english_project_docs_link_to_each_other() -> None:
    chinese = _read("README.md")
    english = _read("README.en.md")
    chinese_matrix = _read("docs/回归测试矩阵.md")
    english_matrix = _read("docs/Regression-Test-Matrix.md")
    chinese_design = _read("docs/并发执行与课堂记录设计.md")
    english_design = _read("docs/Execution-and-Classroom-Records-Design.md")

    assert "frontend/assets/brand/edugate-logo-horizontal.svg" in chinese
    assert "frontend/assets/brand/edugate-logo-horizontal.svg" in english
    assert 'href="README.en.md"' in chinese
    assert 'href="README.md"' in english
    assert "[English](Regression-Test-Matrix.md)" in chinese_matrix
    assert "[中文](回归测试矩阵.md)" in english_matrix
    assert "[English](Execution-and-Classroom-Records-Design.md)" in chinese_design
    assert "[中文](并发执行与课堂记录设计.md)" in english_design


def test_python_pool_and_classroom_record_controls_are_distributed() -> None:
    page = _read("frontend/admin.html")
    example = _read("backend/.env.example")

    assert 'data-tab="records"' in page
    assert 'id="records-view"' in page
    assert "/teacher/classroom-records" in page
    assert "PYTHON_RUNNER_MAX_CONCURRENCY" in page
    assert "CLASSROOM_RECORDING_ENABLED" in page
    assert "PYTHON_RUNNER_MAX_CONCURRENCY=4" in example
    assert "PYTHON_RUNNER_MAX_QUEUE=64" in example
    assert "CLASSROOM_RECORDING_ENABLED=true" in example
