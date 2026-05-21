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


def test_runtime_settings_roundtrip(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    assert ledger.get_runtime_setting("claude.permission_mode") is None

    ledger.set_runtime_setting("claude.permission_mode", "acceptEdits")
    assert ledger.get_runtime_setting("claude.permission_mode") == "acceptEdits"

    ledger.set_runtime_setting("claude.permission_mode", "plan")
    assert ledger.get_runtime_setting("claude.permission_mode") == "plan"


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


# --- Usage events ---


def test_migration_creates_usage_events_table(tmp_path: Path) -> None:
    """Migration must create usage_events table with all expected columns."""
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    columns = ledger._table_columns("usage_events")
    expected = {
        "id", "created_at", "conversation_id", "orchestration_run_id",
        "agent_run_id", "task_id", "agent", "role", "phase", "request_kind",
        "request_index", "model", "external_thread_id", "external_turn_id",
        "external_session_id", "status", "source", "input_tokens",
        "cached_input_tokens", "output_tokens", "reasoning_output_tokens",
        "total_tokens", "workflow_overhead_input_tokens",
        "workflow_overhead_output_tokens", "latency_ms", "metadata_json",
    }
    missing = expected - columns
    assert not missing, f"missing columns: {missing}"


def test_usage_event_record_and_retrieve(tmp_path: Path) -> None:
    """Usage events can be inserted and retrieved."""
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    event = ledger.record_usage_event(
        agent="codex",
        role="analysis",
        phase="orchestration_analysis",
        request_kind="turn",
        request_index=1,
        model="gpt-5.5",
        source="exact",
        input_tokens=1500,
        cached_input_tokens=200,
        output_tokens=800,
        reasoning_output_tokens=300,
        status="completed",
        conversation_id=None,
        task_id=None,
        external_thread_id="thread-abc",
        external_turn_id="turn-1",
        metadata_json='{"protocol":"v2"}',
    )

    assert event.id > 0
    assert event.agent == "codex"
    assert event.source == "exact"
    assert event.input_tokens == 1500
    assert event.cached_input_tokens == 200
    assert event.output_tokens == 800
    assert event.reasoning_output_tokens == 300
    assert event.total_tokens == 2300  # 1500 + 800
    assert event.metadata_json == '{"protocol":"v2"}'

    retrieved = ledger.get_usage_event(event.id)
    assert retrieved.agent == "codex"
    assert retrieved.total_tokens == 2300


def test_usage_event_list_by_filters(tmp_path: Path) -> None:
    """Usage events can be filtered by conversation, task, agent."""
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    # Create a conversation for context
    convo = ledger.create_conversation(
        chat_id=111, user_id=1, title="test", mode="chief_engineer",
        workspace_alias="demo",
    )

    ledger.record_usage_event(
        agent="codex", role="analysis", input_tokens=100, output_tokens=50,
        conversation_id=convo.id, task_id=1,
    )
    ledger.record_usage_event(
        agent="claude", role="implementation", input_tokens=200, output_tokens=100,
        conversation_id=convo.id, task_id=1,
    )
    ledger.record_usage_event(
        agent="codex", role="verification", input_tokens=150, output_tokens=80,
        conversation_id=convo.id, task_id=1,
    )

    by_conv = ledger.list_usage_events(conversation_id=convo.id)
    assert len(by_conv) == 3

    by_agent = ledger.list_usage_events(conversation_id=convo.id, agent="codex")
    assert len(by_agent) == 2

    by_task = ledger.list_usage_events(task_id=1)
    assert len(by_task) == 3


def test_aggregate_usage_splits_by_agent(tmp_path: Path) -> None:
    """Aggregate returns usage split by agent with source breakdown."""
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    convo = ledger.create_conversation(
        chat_id=222, user_id=1, title="agg", mode="chief_engineer",
        workspace_alias="demo",
    )

    # Codex: 2 requests, one exact one estimated
    ledger.record_usage_event(
        agent="codex", role="analysis", input_tokens=100, output_tokens=50,
        source="exact", conversation_id=convo.id,
    )
    ledger.record_usage_event(
        agent="codex", role="verification", input_tokens=150, output_tokens=80,
        source="exact", conversation_id=convo.id,
    )

    # Claude: 1 request, estimated
    ledger.record_usage_event(
        agent="claude", role="implementation", input_tokens=200, output_tokens=100,
        source="estimated", conversation_id=convo.id,
    )

    # Workflow overhead
    ledger.record_usage_event(
        agent="workflow", role="overhead", phase="codex_analysis",
        workflow_overhead_input_tokens=300, conversation_id=convo.id,
    )

    agg = ledger.aggregate_usage(conversation_id=convo.id)

    assert agg["codex"]["requests"] == 2
    assert agg["codex"]["input_tokens"] == 250
    assert agg["codex"]["output_tokens"] == 130
    assert agg["codex"]["total_tokens"] == 380

    assert agg["claude"]["requests"] == 1
    assert agg["claude"]["input_tokens"] == 200

    assert agg["workflow"]["requests"] == 1
    assert agg["workflow"]["workflow_overhead_input_tokens"] == 300

    assert agg["totals"]["requests"] == 4
    assert agg["totals"]["total_tokens"] == 680
    assert agg["totals"]["workflow_overhead_input_tokens"] == 300


def test_render_usage_summary_string(tmp_path: Path) -> None:
    """Render usage summary produces readable output."""
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    convo = ledger.create_conversation(
        chat_id=333, user_id=1, title="summary", mode="chief_engineer",
        workspace_alias="demo",
    )

    ledger.record_usage_event(
        agent="codex", role="analysis", input_tokens=1000, output_tokens=500,
        source="exact", conversation_id=convo.id,
    )
    ledger.record_usage_event(
        agent="claude", role="implementation", input_tokens=2000, output_tokens=800,
        source="estimated", conversation_id=convo.id,
    )

    summary = ledger.render_usage_summary(conversation_id=convo.id)
    assert "Token 用量摘要" in summary
    assert "Codex" in summary
    assert "Claude" in summary
    assert "1,000" in summary
    assert "2000" in summary or "2,000" in summary


def test_usage_event_invalid_metadata_json_sanitized(tmp_path: Path) -> None:
    """Invalid metadata_json is sanitized to '{}'."""
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    event = ledger.record_usage_event(
        agent="codex", input_tokens=10, output_tokens=10,
        metadata_json="not json",
    )
    assert event.metadata_json == "{}"


def test_list_recent_agent_runs_returns_newest_first(tmp_path: Path) -> None:
    """list_recent_agent_runs returns runs in newest-first order."""
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    convo = ledger.create_conversation(
        chat_id=123, user_id=1, title="test", workspace_alias="demo", mode="product",
    )

    # Create 3 runs with distinct external_session_ids
    for i in range(1, 4):
        ledger.create_agent_run(
            conversation_id=convo.id,
            agent="claude",
            role="implementation",
            external_session_id=f"session_{i}",
        )

    recent = ledger.list_recent_agent_runs(convo.id, limit=10)
    assert len(recent) == 3
    assert recent[0].external_session_id == "session_3"
    assert recent[1].external_session_id == "session_2"
    assert recent[2].external_session_id == "session_1"

    # list_agent_runs still returns oldest-first (backward compat)
    oldest_first = ledger.list_agent_runs(convo.id, limit=10)
    assert oldest_first[0].external_session_id == "session_1"
    assert oldest_first[2].external_session_id == "session_3"


def test_list_recent_agent_runs_with_more_than_50_runs_finds_latest(tmp_path: Path) -> None:
    """When a conversation has >50 agent_runs, list_recent_agent_runs still
    finds the most recent external_session_id for each agent."""
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    convo = ledger.create_conversation(
        chat_id=123, user_id=1, title="test", workspace_alias="demo", mode="product",
    )

    # Create 60 runs: first 59 with old session ids, last one with target
    for i in range(1, 60):
        ledger.create_agent_run(
            conversation_id=convo.id,
            agent="claude",
            role="implementation",
            external_session_id=f"old_session_{i}",
        )
    # The 60th run has the session we want to find
    ledger.create_agent_run(
        conversation_id=convo.id,
        agent="claude",
        role="implementation",
        external_session_id="target_session_60",
    )

    recent = ledger.list_recent_agent_runs(convo.id, limit=50)
    assert len(recent) == 50
    # The newest run should be first
    assert recent[0].external_session_id == "target_session_60"
    # The 50th-oldest should NOT be in results (only 50 most recent)
    for r in recent:
        assert r.external_session_id != "old_session_1"


def test_list_conversations_by_chat_can_include_archived(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    first = ledger.create_conversation(
        chat_id=10,
        user_id=1,
        title="First",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    second = ledger.create_conversation(
        chat_id=10,
        user_id=1,
        title="Second",
        mode="chief_engineer",
        workspace_alias="lightfee",
    )
    ledger.archive_conversation(first.id)

    active_only = ledger.list_conversations_by_chat(10)
    with_archived = ledger.list_conversations_by_chat(10, include_archived=True)

    assert [c.id for c in active_only] == [second.id]
    assert {c.id for c in with_archived} == {first.id, second.id}


def test_restore_conversation_archives_other_active_workbench(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    old = ledger.create_conversation(
        chat_id=10,
        user_id=1,
        title="Old",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ledger.archive_conversation(old.id)
    current = ledger.create_conversation(
        chat_id=10,
        user_id=1,
        title="Current",
        mode="chief_engineer",
        workspace_alias="lightfee",
    )

    restored = ledger.restore_conversation(old.id)

    assert restored.id == old.id
    assert restored.archived_at is None
    assert ledger.get_conversation(current.id).archived_at is not None
    assert ledger.get_active_conversation(10).id == old.id
