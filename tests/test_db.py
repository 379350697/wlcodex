from pathlib import Path


from wlcodex.db import Ledger
from wlcodex.models import ApprovalKind, ApprovalStatus, TaskStatus


def test_ledger_creates_and_lists_tasks(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    task = ledger.create_task(
        workspace_alias="demo",
        workspace_path="/tmp/demo",
        title="Fix a bug",
        codex_thread_id="thread-1",
        parent_task_id=None,
        telegram_chat_id=12345,
    )
    ledger.add_event(task.id, "task_created", {"title": "Fix a bug"})

    tasks = ledger.list_tasks(limit=10)

    assert tasks[0].id == task.id
    assert tasks[0].status == TaskStatus.QUEUED
    assert tasks[0].telegram_chat_id == 12345
    assert tasks[0].active_turn_id is None
    assert ledger.list_events(task.id)[0].event_type == "task_created"


def test_ledger_updates_status_and_summary(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Run tests", "thread-2", None)

    ledger.set_task_status(task.id, TaskStatus.RUNNING, phase="running tests", summary="pytest")
    loaded = ledger.get_task(task.id)

    assert loaded.status == TaskStatus.RUNNING
    assert loaded.last_phase == "running tests"
    assert loaded.last_summary == "pytest"


def test_ledger_set_active_turn(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)

    ledger.set_active_turn(task.id, "turn-42")
    loaded = ledger.get_task(task.id)
    assert loaded.active_turn_id == "turn-42"

    ledger.clear_active_turn(task.id)
    loaded = ledger.get_task(task.id)
    assert loaded.active_turn_id is None


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    ledger.migrate()  # second call must not crash
    ledger.migrate()


def test_approval_create_and_resolve(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)

    approval = ledger.create_approval(
        task_id=task.id,
        codex_request_id="req-1",
        codex_item_id="item-1",
        codex_turn_id="turn-1",
        kind=ApprovalKind.COMMAND,
        summary="Run: rm -rf /",
        command_json='{"command":"rm -rf /"}',
    )
    assert approval.status == ApprovalStatus.PENDING
    assert approval.kind == ApprovalKind.COMMAND

    resolved = ledger.resolve_approval(approval.id, ApprovalStatus.APPROVED, "approved_once")
    assert resolved.status == ApprovalStatus.APPROVED
    assert resolved.resolution == "approved_once"

    # Duplicate resolution returns the already-resolved state
    again = ledger.resolve_approval(approval.id, ApprovalStatus.APPROVED, "approved_once")
    assert again.status == ApprovalStatus.APPROVED


def test_approval_duplicate_insert_ignored(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)

    a1 = ledger.create_approval(task.id, "req-dup", None, None, ApprovalKind.COMMAND, "test")
    a2 = ledger.create_approval(task.id, "req-dup", None, None, ApprovalKind.COMMAND, "test")
    # INSERT OR IGNORE means second create uses same row id
    assert a1.id == a2.id


def test_approval_codex_request_id_is_task_scoped(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    first = ledger.create_task("demo", "/tmp/demo", "First", "thread-1", None)
    second = ledger.create_task("demo", "/tmp/demo", "Second", "thread-2", None)

    a1 = ledger.create_approval(first.id, "1", None, None, ApprovalKind.COMMAND, "first")
    a2 = ledger.create_approval(second.id, "1", None, None, ApprovalKind.COMMAND, "second")

    assert a1.id != a2.id
    assert ledger.get_approval_by_codex_id("1", task_id=first.id).id == a1.id
    assert ledger.get_approval_by_codex_id("1", task_id=second.id).id == a2.id


def test_touched_files_dedup(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)

    ledger.record_touched_file(task.id, "src/main.py", "modified")
    ledger.record_touched_file(task.id, "src/main.py", "modified")  # dupe
    ledger.record_touched_file(task.id, "src/test.py", "added")

    files = ledger.list_touched_files(task.id)
    assert len(files) == 2


def test_mark_active_tasks_recovery_paused(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    t1 = ledger.create_task("a", "/tmp/a", "Running task", "th-1", None)
    ledger.set_task_status(t1.id, TaskStatus.RUNNING)
    t2 = ledger.create_task("b", "/tmp/b", "Queued task", "th-2", None)
    t3 = ledger.create_task("c", "/tmp/c", "Waiting approval", "th-3", None)
    ledger.set_task_status(t3.id, TaskStatus.WAITING_APPROVAL)
    t4 = ledger.create_task("d", "/tmp/d", "Done task", "th-4", None)
    ledger.set_task_status(t4.id, TaskStatus.DONE)

    paused_ids = ledger.mark_active_tasks_recovery_paused()

    assert t1.id in paused_ids  # running -> paused
    assert t2.id in paused_ids  # queued -> paused
    assert t3.id in paused_ids  # waiting_approval -> paused
    assert t4.id not in paused_ids  # done stays done

    assert ledger.get_task(t1.id).status == TaskStatus.PAUSED
    assert ledger.get_task(t2.id).status == TaskStatus.PAUSED
    assert ledger.get_task(t3.id).status == TaskStatus.PAUSED
    assert ledger.get_task(t4.id).status == TaskStatus.DONE

    events = ledger.list_events(t1.id)
    recovery_events = [e for e in events if e.event_type == "recovery_paused"]
    assert len(recovery_events) >= 1


def test_migrate_upgrades_legacy_tasks_table(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_alias TEXT NOT NULL,
            workspace_path TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            codex_thread_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    ledger = Ledger.open(db_path)
    ledger.migrate()

    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)
    assert task.pending_approval_count == 0
    assert task.telegram_status_message_id is None


def test_list_tasks_excludes_archived_by_default(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    t1 = ledger.create_task("a", "/tmp/a", "Active", "th-1", None)
    t2 = ledger.create_task("b", "/tmp/b", "Archived", "th-2", None)
    ledger.set_task_status(t2.id, TaskStatus.ARCHIVED)

    tasks = ledger.list_tasks()
    assert any(t.id == t1.id for t in tasks)
    assert not any(t.id == t2.id for t in tasks)

    all_tasks = ledger.list_tasks(include_archived=True)
    assert any(t.id == t2.id for t in all_tasks)


def test_list_active_tasks_returns_write_lock_statuses(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    queued = ledger.create_task("demo", "/tmp/demo", "Queued", "q", None)
    running = ledger.create_task("demo", "/tmp/demo", "Running", "r", None)
    ledger.set_task_status(running.id, TaskStatus.RUNNING)
    waiting = ledger.create_task("demo", "/tmp/demo", "Waiting", "w", None)
    ledger.set_task_status(waiting.id, TaskStatus.WAITING_APPROVAL)
    paused = ledger.create_task("demo", "/tmp/demo", "Paused", "p", None)
    ledger.set_task_status(paused.id, TaskStatus.PAUSED)
    done = ledger.create_task("demo", "/tmp/demo", "Done", "d", None)
    ledger.set_task_status(done.id, TaskStatus.DONE)

    ids = {task.id for task in ledger.list_active_tasks()}

    assert ids == {queued.id, running.id, waiting.id, paused.id}


def test_mark_task_timeout_records_failure_event(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Running", "thread-1", None)
    ledger.set_task_status(task.id, TaskStatus.RUNNING)

    updated = ledger.mark_task_timeout(
        task.id,
        status=TaskStatus.RUNNING,
        age_seconds=8000,
        threshold_seconds=7200,
    )

    assert updated.status == TaskStatus.FAILED
    assert "timed out" in updated.last_error
    events = ledger.list_events(task.id)
    assert events[-1].event_type == "task_timeout"
    assert events[-1].payload["age_seconds"] == 8000


def test_mark_backend_dead_records_failure_event(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Running", "thread-1", None)
    ledger.set_task_status(task.id, TaskStatus.RUNNING)

    updated = ledger.mark_backend_dead(task.id, "Backend unhealthy: process dead")

    assert updated.status == TaskStatus.FAILED
    assert "Backend unhealthy" in updated.last_error
    events = ledger.list_events(task.id)
    assert events[-1].event_type == "backend_dead"
