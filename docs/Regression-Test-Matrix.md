# EduGate Standalone Regression Test Matrix

[中文](回归测试矩阵.md) · **English** · [Back to project home](../README.en.md)

This document records risks that appeared during development or were confirmed during design review so future changes do not reintroduce them.

## Automated tests

| Risk | Regression objective | Automated test |
|---|---|---|
| Separate Windows and web teacher controls appear together | The primary launcher has no Tkinter dependency, builds without a console, and scripts use `pythonw` | `test_teacher_launcher_is_web_only_and_windowless` |
| Restricted teacher installation network | Install and build scripts always use the Tsinghua PyPI mirror | `test_teacher_install_and_build_scripts_use_tsinghua_mirror` |
| Configuration is lost when the folder is copied | Default to `config`, `data`, and `runtime` beside the EXE and checkpoint on shutdown | `test_windows_bundle_is_a_copyable_portable_classroom_folder`, `test_pending_restore_replaces_configuration_and_knowledge` |
| API keys remain tied to the original Windows account | Store portable credentials that remain readable after copying the complete folder | `test_portable_secret_store_survives_copying_the_folder` |
| Teachers must find and enter a password for every class | Issue automatic teacher sessions only to loopback clients and reject LAN student devices | `test_portable_local_session_opens_teacher_console_without_password`, `test_portable_local_session_is_rejected_for_student_devices` |
| A single-teacher classroom still exposes school-style user management | Remove multi-teacher account and password controls from the teacher web console and make the brand mark prominent | `test_portable_teacher_console_omits_multi_teacher_management_and_uses_prominent_branding` |
| Ending a class accidentally stops the teacher service | Start and end only control student access; ending revokes old links and sessions while health checks remain online | `test_classroom_start_and_end_control_student_access_without_stopping_service`, `test_classroom_access_can_be_ended_and_started_with_a_fresh_token` |
| iPad controls are too small or the two-column layout becomes cramped | Use larger coarse-pointer targets, switch iPad portrait and landscape to one column, and keep classroom controls beside the student entry | `test_teacher_console_classroom_controls_and_ipad_touch_layout_are_distributed` |
| Existing standalone data disappears after upgrade | Import legacy data once only when the portable data directory is empty | `test_legacy_data_is_imported_into_portable_folder_once` |
| The configured port belongs to another service | The launcher accepts only health responses carrying the EduGate identity marker | `test_healthcheck_requires_edugate_identity_header` |
| A busy default port prevents class startup | Select the next available local port automatically | `test_first_available_port_skips_an_occupied_port` |
| Web restart invalidates sessions and reports repeated errors | System operations require the supervised launcher; old teacher sessions are intentionally invalidated | `test_system_management_requires_supervised_launcher`, `test_logout_revokes_session` |
| Backup restore overwrites arbitrary files | Reject path traversal, unknown entries, and malformed ZIP files; stage valid data before restart | `test_restore_rejects_unsafe_or_unknown_entries`, `test_pending_restore_rejects_path_traversal` |
| Knowledge files and configuration diverge after restore | Offline restore replaces configuration and the knowledge directory, then removes staged data | `test_pending_restore_replaces_configuration_and_knowledge` |
| An old model cannot be deleted because a scenario or teacher policy still references it | Atomically switch references to a selected replacement; return HTTP 409 when no replacement is supplied, while knowledge sources remain protected | `test_referenced_model_can_be_replaced_while_knowledge_source_stays_protected` |
| Every upstream model must be entered manually, a large catalog stretches the System page, or a saved key cannot be reused | Keep only source settings and imported models on the main page; fetch `/models` into a searchable modal with editable display names and batch selection, reusing a saved key for the same endpoint | `test_provider_models_can_be_discovered_and_batch_imported`, `test_upstream_models_can_be_discovered_selected_and_batch_imported` |
| A deleted legacy default model reappears after restart | Run legacy default migration only once and persist its completion in portable configuration | `test_completed_runtime_migration_does_not_restore_legacy_default` |
| 64 students bypass the upstream concurrency limit | 64 simulated requests remain bounded by the model semaphore | `test_model_concurrency_is_bounded` |
| A release or refactor silently restores the four-lane model default | Keep the benchmarked 16-lane default aligned across runtime configuration, System settings, and portable installation | `test_model_concurrency_default_matches_classroom_benchmark`, `test_python_pool_and_classroom_record_controls_are_distributed` |
| Students behind one proxy share one IP rate-limit bucket | Independent student sessions are limited separately; legacy classroom-token calls remain compatible | `test_student_sessions_rate_limit_students_independently_behind_one_ip` |
| Old students retain access after classroom-link rotation | Rotating the classroom token revokes all prior student sessions | `test_classroom_rotation_invalidates_student_session` |
| Students are asked to participate in recording, or conversation history and device identity disappear after refresh | Silently create and reuse a local device ID without an identity form; validate restored history and send only the latest 10 messages | `test_student_page_persists_history_and_joins_silently` |
| Python subprocesses can read model credentials | Child processes inherit only a minimal environment allowlist | `test_python_runner_does_not_inherit_application_secrets` |
| A packaged executable treats itself as a Python interpreter | Frozen builds require a separate interpreter and return an explicit HTTP 503 when unavailable | `test_frozen_bundle_requires_a_separate_python_executable`, `test_python_runner_unavailable_is_reported_as_503` |
| One Python lane serializes code execution for all 64 students | Use four isolated slots and a bounded queue of 64 tasks while enforcing the configured peak concurrency | `test_python_pool_runs_multiple_isolated_jobs_with_bounded_concurrency` |
| The Python queue grows without bounds or one student occupies multiple slots | Reject a full queue with HTTP 429 and allow only one queued or running task per student | `test_python_pool_rejects_queue_overflow_and_duplicate_student_jobs` |
| Python output appears only after the process exits | Deliver child-process stdout/stderr before completion and expose the full task lifecycle over SSE | `test_python_runner_emits_output_before_process_completion`, `test_python_pool_streams_queue_state_output_and_completion`, `test_python_runner_stream_reports_queue_output_and_result` |
| A teacher cannot identify which classroom device made a request | Derive a device label from the stable browser ID and store the request IP without student participation, while retaining teacher ownership isolation | `test_teacher_can_view_only_owned_identified_classroom_records`, `test_classroom_record_schema_adds_computer_identity_without_deleting_old_data` |
| Classroom content is retained indefinitely or individual entries are unbounded | Enforce retention, total-record, and content limits; support disabling recording and deleting a class | `test_classroom_record_retention_and_content_limits_are_enforced`, `test_classroom_content_recording_can_be_disabled` |
| A streaming response creates fragments or duplicate classroom turns | Merge one complete streamed answer into exactly one classroom turn | `test_streamed_chat_is_saved_as_one_classroom_turn` |
| The default DeepSeek model alias is retired | The default uses the current `deepseek-v4-flash` model | `test_current_deepseek_default_is_not_retired_alias` |
| Regular teachers can access system administration | The System tab and view remain administrator-only | `test_system_view_remains_admin_only` |
| The System view duplicates global Online state or exposes low-value internals | Put uptime beside the heading, hide duplicate technical state, retain LAN copying and fixed folder opening, and collapse System Logs immediately above Advanced Settings | `test_admin_can_open_fixed_application_directory`, `test_system_view_prioritizes_classroom_operations` |
| Knowledge sources can only be created or uploaded through the browser | Put edit, open-folder, incremental scan, and delete actions in the source-first list; use content hashes to add, update, and remove indexes | `test_source_folder_scan_adds_updates_and_removes_files`, `test_admin_can_open_fixed_knowledge_source_directory`, `test_admin_can_scan_knowledge_source_directory`, `test_resource_view_exposes_complete_folder_management` |
| iPad tabs are crowded or a tab switch lands midway through the next view | Use Chinese-only sticky tabs at touch widths, reset tab switches to the top, and stack the Records layout in portrait | `test_teacher_tabs_and_primary_status_copy_are_chinese_first` |
| Brand icons or bilingual documentation links disappear in a release | Web pages, the Windows bundle, and Chinese/English documentation retain shared assets and reciprocal links | `test_brand_assets_are_used_by_docs_web_and_windows_bundle`, `test_chinese_and_english_project_docs_link_to_each_other` |

## Manual release verification

1. Run the first-time installer in a clean Windows profile and confirm dependencies are downloaded from the Tsinghua mirror.
2. Start both the script and packaged executable and confirm that no persistent command-line or desktop control window appears.
3. Complete administrator setup; verify the composed endpoint, fetch a large provider catalog, test modal search, display-name edits, and batch import, then switch references and delete an old model. Continue through class start, student-link access, streaming responses, generation cancellation, token rotation, and class end; confirm ending the class does not stop the teacher console.
4. Use two browser profiles behind the same proxy and confirm they receive different `student_session_id` values.
5. Refresh the student page repeatedly and confirm current-class history is restored; rotate the classroom link and confirm the new class does not load old history.
6. Run a 30-minute load test with 64 clients and 24 to 32 upstream requests, monitoring HTTP 429 responses, time to first token, memory, and SQLite lock waits.
7. Download a backup, modify configuration and knowledge files, restore the backup, and confirm the restored state after restart.
8. Use the bundle's `runtime\python` and verify 4–8 concurrent slots, queue saturation, duplicate-student rejection, live output, forbidden imports, timeout, truncation, and windowless execution.
9. Confirm the student link opens without an identity prompt. Open Records as both a regular teacher and an administrator; verify ownership isolation, generated-device-label/IP and activity filters, rotation finalization, and deletion.
10. Check iPad Safari or equivalent 768×1024 and 1024×768 touch viewports for the classroom controls beside the bottom student entry, four tabs, forms, and switches; verify there is no overlap or horizontal scrolling.
11. In System, confirm uptime sits beside the heading, copy the LAN address, and open the program folder; confirm System Logs sits immediately above Advanced Settings with both collapsed, and duplicate service state, data path, free disk space, PID, and supervisor mode are absent.
12. In Resources, open a source folder; add, modify, and remove a supported file, scanning after each change, and verify indexes are added, updated, and removed. Confirm the default source is clearly marked as non-deletable.

## Run locally

```powershell
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r backend\requirements-dev.txt
python -m pytest -q --basetemp=.pytest-tmp
python -m compileall -q backend desktop
```

GitHub Actions runs the same suite on Windows with Python 3.9 and 3.12.
