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


def test_windows_bundle_is_a_copyable_portable_classroom_folder() -> None:
    launcher = _read("desktop/edugate_standalone.py")
    build = _read("desktop/build_windows.bat")
    example = _read("backend/.env.example")
    page = _read("frontend/admin.html")

    assert "APP_DIR / \"data\"" in launcher
    assert "APP_DIR / \"config\" / \"edugate.env\"" in launcher
    assert "migrate_legacy_data()" in launcher
    assert "build_portable_runtime.py" in build
    assert "runtime\\python" in build
    assert "ADMIN_PASSWORD=edugate" in example
    assert "PORTABLE_AUTO_LOGIN=true" in example
    assert 'id="end-class"' in page
    assert "/auth/local-session" in page


def test_portable_teacher_console_omits_multi_teacher_management_and_uses_prominent_branding() -> None:
    page = _read("frontend/admin.html")
    student = _read("frontend/student.html")

    assert 'id="teacher-admin-section"' not in page
    assert "/admin/teachers" not in page
    assert "修改我的密码" not in page
    assert "width: 112px" in page
    assert "width: 72px" in student


def test_teacher_console_classroom_controls_and_ipad_touch_layout_are_distributed() -> None:
    page = _read("frontend/admin.html")

    assert page.index('<div class="control-grid">') < page.index('id="classroom-entry"')
    assert page.index('id="classroom-entry"') < page.index('id="class-controls"')
    assert 'id="class-controls"' not in page.split("</header>", 1)[0]
    assert 'id="start-class"' in page
    assert 'id="end-class"' in page
    assert 'request("/admin/classroom/start"' in page
    assert 'request("/admin/classroom/end"' in page
    assert 'el.endClass.addEventListener("click", () => requestSystemAction("shutdown"))' not in page
    assert "touch-action: manipulation" in page
    assert "@media (hover: none), (pointer: coarse)" in page
    assert "@media (max-width: 1100px)" in page


def test_current_deepseek_default_is_not_retired_alias() -> None:
    config = _read("backend/app/config.py")
    example = _read("backend/.env.example")

    assert '"deepseek-v4-flash"' in config
    assert "DEFAULT_MODEL=deepseek-v4-flash" in example
    assert "DEFAULT_MODEL=deepseek-chat" not in example


def test_default_join_budget_covers_reloads_for_64_students() -> None:
    example = _read("backend/.env.example")

    assert "STUDENT_JOIN_RATE_LIMIT_PER_5_MINUTES=256" in example


def test_student_page_persists_history_and_collects_computer_identity() -> None:
    page = _read("frontend/student.html")

    assert "POST" in page and "/classroom/join" in page
    assert '"X-Student-Token"' in page
    assert "localStorage.getItem(HISTORY_STORAGE_KEY)" in page
    assert "localStorage.setItem(" in page
    assert "messages.slice(-MAX_MESSAGES_TO_SEND)" in page
    assert "sessionStorage.setItem(SESSION_STORAGE_KEY" in page
    assert "COMPUTER_NAME_STORAGE_KEY" in page
    assert 'JSON.stringify({ computer_name: cleanComputerName })' in page
    assert 'id="computerNameInput"' in page
    assert 'id="identityClientIp"' in page


def test_system_view_remains_admin_only() -> None:
    page = _read("frontend/admin.html")

    assert 'data-tab="system" role="tab" aria-selected="false" aria-controls="system-view" data-admin-only' in page
    assert '<section id="system-view" hidden data-admin-only>' in page


def test_system_view_prioritizes_classroom_operations() -> None:
    page = _read("frontend/admin.html")
    resources = page.split('<section id="settings-view" hidden>', 1)[1].split(
        '<section id="system-view" hidden data-admin-only>', 1
    )[0]
    system = page.split('<section id="system-view" hidden data-admin-only>', 1)[1]

    shutdown = page.index('id="shutdown-system"')
    open_directory = page.index('id="open-app-directory"')
    assert shutdown < open_directory
    assert 'request("/admin/system/open-app-dir"' in page
    assert 'id="open-app-directory" type="button">打开目录</button>' in page
    assert 'id="copy-lan-url"' in page
    assert '["数据目录", system.data_dir' not in page
    assert '["剩余磁盘",' not in page
    assert '["进程 ID",' not in page
    assert '["监督模式",' not in page
    assert '["服务状态",' not in page
    assert '["运行时间",' not in page
    assert 'id="system-uptime"' in page
    assert '已运行 ${formatDuration(system.uptime_seconds)}' in page
    assert 'id="model-api-panel"' not in resources
    assert 'id="model-api-panel"' in system
    assert system.index('id="dashboard-metrics"') < system.index('id="model-api-panel"')
    assert system.index('id="model-api-panel"') < system.index('id="system-log-panel"')
    assert 'id="model-api-panel" open' not in page
    assert "模型与供应商管理" not in page
    assert "下游 API 密钥" in page
    system_log = page.index('id="system-log-panel"')
    model_api = page.index('id="model-api-panel"')
    advanced = page.index('id="advanced-settings-panel"')
    assert model_api < system_log < advanced
    assert '<details class="collapsible-panel system-log-panel" id="system-log-panel">' in page
    assert '<details class="collapsible-panel advanced-settings-panel" id="advanced-settings-panel">' in page
    assert 'id="system-log-panel" open' not in page
    assert 'id="advanced-settings-panel" open' not in page
    assert '<details class="collapsible-panel" id="backup-panel">' in page
    assert 'id="backup-panel" open' not in page
    backup = page.split('id="backup-panel"', 1)[1].split("</details>", 1)[0]
    backup_actions = backup.split('<div class="actions" style="justify-content:flex-start">', 1)[1].split(
        "</div>", 1
    )[0]
    assert 'id="download-backup"' in backup_actions
    assert 'id="restore-backup"' in backup_actions


def test_resource_view_exposes_complete_folder_management() -> None:
    page = _read("frontend/admin.html")
    resources = page.split('<section id="settings-view" hidden>', 1)[1].split(
        '<section id="system-view" hidden data-admin-only>', 1
    )[0]

    assert "知识库管理" in resources
    assert 'id="source-editor-panel"' in resources
    assert 'id="source-editor-panel" open' not in resources
    assert 'data-source-open=' in page
    assert 'data-source-scan=' in page
    assert 'data-source-edit=' in page
    assert 'data-source-delete=' in page
    assert '/open-folder`' in page
    assert '/scan`' in page
    assert "默认知识库，不可删除" in page


def test_teacher_tabs_and_primary_status_copy_are_chinese_first() -> None:
    page = _read("frontend/admin.html")

    for label in ("控制", "记录", "资源", "系统"):
        assert f">{label}</button>" in page
    assert "(Control)" not in page
    assert "(Records)" not in page
    assert "(Resources)" not in page
    assert "(System)" not in page
    assert "Current Teacher Policy" not in page
    assert 'setStatus("运行中", true)' in page
    assert "window.scrollTo({ top: 0 });" in page
    assert '.tabs { position: sticky; top: 0; z-index: 5; }' in page
    assert 'role="status" aria-live="polite"' in page


def test_teacher_console_uses_compact_brand_tokens_and_touch_safe_controls() -> None:
    page = _read("frontend/admin.html")

    assert "--brand: #2563eb" in page
    assert "--accent: #0aa9bd" in page
    assert ".session-card" in page and "border-left: 4px solid var(--accent)" in page
    assert "linear-gradient(135deg, #654ff0, #4f3de0)" not in page
    assert "关闭后，学生将暂时无法获得 AI 回答。" in page
    assert "点击开关会调用 <code>/config/model</code>" not in page
    assert "@media (max-width: 900px)" in page
    assert ".system-grid, .setting-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }" in page
    assert 'class="system-action-group critical"' in page
    assert 'h2 { margin-bottom: 0; font-size: 20px; font-weight: 900; line-height: 1.35;' in page
    assert 'font-size: 16px;\n      font-weight: 900;' in page
    assert '.tab { min-height: 58px; font-size: 14px; }' in page
    assert '.row-title { margin: 0; font-size: 16px; font-weight: 900; line-height: 1.45; }' in page
    assert '.tab.active:focus-visible { box-shadow: inset 0 -5px 0' in page
    assert '.checkbox-row input[type="checkbox"] { flex: 0 0 22px; width: 22px; height: 22px;' in page
    assert 'input[type="range"]::-webkit-slider-thumb { width: 24px; height: 24px;' in page
    assert '.upload-card { width: 100%; padding: 16px; }' in page
    assert 'id="refresh-source-admin" type="button">刷新知识库</button>' in page
    assert '.control-grid > div > .section:last-child { margin-bottom: 0; }' in page
    assert '.classroom-entry { margin-top: 26px; margin-bottom: 0;' in page
    assert '["请求数", dashboard.total_requests ?? 0]' in page
    assert '["Tokens", dashboard.total_tokens ?? 0]' in page
    assert '["总请求", dashboard.total_requests ?? 0]' not in page
    assert '["总 Tokens", dashboard.total_tokens ?? 0]' not in page
    assert 'class="brand-title-row"' in page
    assert page.index('EduGate 教师端控制台</h1>') < page.index('id="system-status"') < page.index('教师控制 · 本地 AI 教学网关')
    assert '.system-metric-value { display: flex; min-height: 34px; align-items: center;' in page
    assert '.system-metric-value { min-height: 44px; }' in page
    assert page.count('<div class="system-metric-value">') == 1


def test_teacher_console_replaces_native_visible_file_inputs_and_compacts_source_actions() -> None:
    page = _read("frontend/admin.html")

    assert 'class="file-input-hidden" id="knowledge-file"' in page
    assert 'id="knowledge-file-name">未选择文件</span>' in page
    assert 'class="file-input-hidden" id="restore-backup-file"' in page
    assert 'id="restore-backup-file-name">未选择文件</span>' in page
    assert 'id="knowledge-file-trigger" type="button">选择文件</button>' in page
    assert 'id="restore-backup-file-trigger" type="button">选择备份</button>' in page
    assert 'aria-hidden="true" tabindex="-1"' in page
    assert "updateFilePicker(el.knowledgeFile, el.knowledgeFileName)" in page
    assert "updateFilePicker(el.restoreBackupFile, el.restoreBackupFileName)" in page
    assert '<details class="action-menu">' in page
    assert "<summary>更多操作</summary>" in page


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
    chinese_benchmark = _read("docs/模型并发基准报告-2026-08-10.md")
    english_benchmark = _read("docs/Model-Concurrency-Benchmark-2026-08-10.md")

    assert "frontend/assets/brand/edugate-logo-horizontal.svg" in chinese
    assert "frontend/assets/brand/edugate-logo-horizontal.svg" in english
    assert 'href="README.en.md"' in chinese
    assert 'href="README.md"' in english
    assert "[English](Regression-Test-Matrix.md)" in chinese_matrix
    assert "[中文](回归测试矩阵.md)" in english_matrix
    assert "[English](Execution-and-Classroom-Records-Design.md)" in chinese_design
    assert "[中文](并发执行与课堂记录设计.md)" in english_design
    assert "[English](Model-Concurrency-Benchmark-2026-08-10.md)" in chinese_benchmark
    assert "[中文](模型并发基准报告-2026-08-10.md)" in english_benchmark


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
    assert "MODEL_MAX_CONCURRENCY=16" in example
    assert "CLASSROOM_RECORDING_ENABLED=true" in example
    assert "DB_WRITE_QUEUE_SIZE=4096" in example
    assert "DB_WRITE_BATCH_SIZE=100" in example
    assert "DB_WRITE_FLUSH_INTERVAL_MS=20" in example
    assert "DB_CLEANUP_INTERVAL_SECONDS=300" in example
    assert "turn.computer_name" in page
    assert "turn.client_ip" in page
    assert "匿名学生" not in page
