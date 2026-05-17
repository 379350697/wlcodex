"""Tests for conversation state persistence."""

from pathlib import Path

from wlcodex.db import Ledger


def test_ledger_creates_and_updates_conversation(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    convo = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="登录 bug",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )

    assert convo.id > 0
    assert convo.chat_id == 100
    assert convo.mode == "chief_engineer"

    updated = ledger.update_conversation_summary(
        convo.id,
        "用户要修复登录空指针，要求 Codex 验收。",
    )

    assert updated.conversation_summary.startswith("用户要修复")


def test_ledger_active_conversation_per_chat(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    ledger.create_conversation(chat_id=100, user_id=200, title="A", mode="codex_direct", workspace_alias="wlcodex")
    ledger.create_conversation(chat_id=100, user_id=200, title="B", mode="chief_engineer", workspace_alias="wlcodex")

    active = ledger.get_active_conversation(chat_id=100)
    assert active is not None
    assert active.title == "B"


def test_ledger_archive_conversation(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    convo = ledger.create_conversation(chat_id=100, user_id=200, title="old", mode="codex_direct", workspace_alias="wlcodex")
    archived = ledger.archive_conversation(convo.id)
    assert archived.archived_at is not None

    active = ledger.get_active_conversation(chat_id=100)
    assert active is None


def test_ledger_sets_conversation_mode_and_workspace(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    convo = ledger.create_conversation(chat_id=100, user_id=200, title="test", mode="codex_direct", workspace_alias="wlcodex")

    updated = ledger.set_active_conversation_mode(convo.id, "claude_direct")
    assert updated.mode == "claude_direct"

    updated2 = ledger.set_conversation_workspace(convo.id, "other")
    assert updated2.workspace_alias == "other"


def test_ledger_creates_agent_run(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    convo = ledger.create_conversation(chat_id=100, user_id=200, title="test", mode="chief_engineer", workspace_alias="wlcodex")

    run = ledger.create_agent_run(
        conversation_id=convo.id,
        agent="codex",
        role="analyze",
        hidden_task_id=42,
        prompt_packet_summary="分析登录bug",
    )

    assert run.id > 0
    assert run.agent == "codex"
    assert run.status == "queued"
    assert run.hidden_task_id == 42

    updated = ledger.update_agent_run_status(run.id, "done", token_input=500, token_output=300)
    assert updated.status == "done"
    assert updated.token_input == 500
    assert updated.token_output == 300


def test_ledger_creates_orchestration_run_and_decisions(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    convo = ledger.create_conversation(chat_id=100, user_id=200, title="test", mode="chief_engineer", workspace_alias="wlcodex")

    orch_run = ledger.create_orchestration_run(
        conversation_id=convo.id,
        goal="修复登录bug",
    )

    assert orch_run.id > 0
    assert orch_run.status == "running"
    assert orch_run.verify_round == 0

    # Update status
    updated = ledger.update_orchestration_run(
        orch_run.id,
        status="passed",
        verify_round=1,
        last_codex_analysis="根因在auth.py",
        last_claude_summary="修改了2个文件",
        last_verification_result="pass: 验收通过",
    )
    assert updated.status == "passed"
    assert updated.verify_round == 1
    assert updated.last_codex_analysis == "根因在auth.py"

    # Record a decision
    decision = ledger.record_orchestration_decision(
        run_id=orch_run.id,
        decision="delegate_to_claude",
        reason="需要修改代码",
        next_agent="claude",
    )
    assert decision.decision == "delegate_to_claude"


def test_ledger_lists_conversations(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    ledger.create_conversation(chat_id=100, user_id=200, title="A", mode="codex_direct", workspace_alias="wlcodex")
    ledger.create_conversation(chat_id=200, user_id=300, title="B", mode="chief_engineer", workspace_alias="wlcodex")
    ledger.create_conversation(chat_id=100, user_id=200, title="C", mode="claude_direct", workspace_alias="wlcodex")

    all_convos = ledger.list_conversations(limit=50)
    assert len(all_convos) == 3

    chat_convos = ledger.list_conversations_by_chat(chat_id=100, limit=50)
    assert len(chat_convos) == 2
