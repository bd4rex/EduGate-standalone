# EduGate Standalone Regression Test Matrix

[中文](回归测试矩阵.md) · **English** · [Back to project home](../README.en.md)

This document records risks that appeared during development or were confirmed during design review so future changes do not reintroduce them.

## Automated tests

| Risk | Regression objective | Automated test |
|---|---|---|
| Separate Windows and web teacher controls appear together | The primary launcher has no Tkinter dependency, builds without a console, and scripts use `pythonw` | `test_teacher_launcher_is_web_only_and_windowless` |
| Restricted teacher installation network | Install and build scripts always use the Tsinghua PyPI mirror | `test_teacher_install_and_build_scripts_use_tsinghua_mirror` |
| The configured port belongs to another service | The launcher accepts only health responses carrying the EduGate identity marker | `test_healthcheck_requires_edugate_identity_header` |
| Web restart invalidates sessions and reports repeated errors | System operations require the supervised launcher; old teacher sessions are intentionally invalidated | `test_system_management_requires_supervised_launcher`, `test_logout_revokes_session` |
| Backup restore overwrites arbitrary files | Reject path traversal, unknown entries, and malformed ZIP files; stage valid data before restart | `test_restore_rejects_unsafe_or_unknown_entries`, `test_pending_restore_rejects_path_traversal` |
| Knowledge files and configuration diverge after restore | Offline restore replaces configuration and the knowledge directory, then removes staged data | `test_pending_restore_replaces_configuration_and_knowledge` |
| A model or knowledge base is deleted while referenced by a class | Return HTTP 409 and preserve resources that are still in use | `test_referenced_model_and_knowledge_source_cannot_be_deleted` |
| 64 students bypass the upstream concurrency limit | 64 simulated requests remain bounded by the model semaphore | `test_model_concurrency_is_bounded` |
| Students behind one proxy share one IP rate-limit bucket | Anonymous student sessions are limited independently; legacy classroom-token calls remain compatible | `test_student_sessions_rate_limit_students_independently_behind_one_ip` |
| Old students retain access after classroom-link rotation | Rotating the classroom token revokes all prior student sessions | `test_classroom_rotation_invalidates_student_session` |
| Student conversation history disappears after refresh | Validate and restore local history, while sending only the latest 10 messages | `test_student_page_persists_history_and_uses_anonymous_session` |
| Python subprocesses can read model credentials | Child processes inherit only a minimal environment allowlist | `test_python_runner_does_not_inherit_application_secrets` |
| A packaged executable treats itself as a Python interpreter | Frozen builds require a separate interpreter and return an explicit HTTP 503 when unavailable | `test_frozen_bundle_requires_a_separate_python_executable`, `test_python_runner_unavailable_is_reported_as_503` |
| The default DeepSeek model alias is retired | The default uses the current `deepseek-v4-flash` model | `test_current_deepseek_default_is_not_retired_alias` |
| Regular teachers can access system administration | The System tab and view remain administrator-only | `test_system_view_remains_admin_only` |
| Brand icons or bilingual documentation links disappear in a release | Web pages, the Windows bundle, and Chinese/English documentation retain shared assets and reciprocal links | `test_brand_assets_are_used_by_docs_web_and_windows_bundle`, `test_chinese_and_english_project_docs_link_to_each_other` |

## Manual release verification

1. Run the first-time installer in a clean Windows profile and confirm dependencies are downloaded from the Tsinghua mirror.
2. Start both the script and packaged executable and confirm that no persistent command-line or desktop control window appears.
3. Complete administrator setup, model API probing, student-link access, streaming responses, generation cancellation, and classroom-token rotation.
4. Use two browser profiles behind the same proxy and confirm they receive different `student_session_id` values.
5. Refresh the student page repeatedly and confirm current-class history is restored; rotate the classroom link and confirm the new class does not load old history.
6. Run a 30-minute load test with 64 clients and 24 to 32 upstream requests, monitoring HTTP 429 responses, time to first token, memory, and SQLite lock waits.
7. Download a backup, modify configuration and knowledge files, restore the backup, and confirm the restored state after restart.
8. Configure a separate interpreter before enabling the Python runner and verify basic code, forbidden imports, timeout, output truncation, and windowless execution.

## Run locally

```powershell
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r backend\requirements-dev.txt
python -m pytest -q --basetemp=.pytest-tmp
python -m compileall -q backend desktop
```

GitHub Actions runs the same suite on Windows with Python 3.9 and 3.12.
