"""Tests for /recent command — router, DB query, controller response, boundaries."""

from pathlib import Path

import pytest

from wlcodex.codex_backend import FakeCodexBackend
from wlcodex.config import WorkspaceConfig
from wlcodex.controller import CommandController
from wlcodex.db import Ledger
from wlcodex.inspection import TaskInspector
from wlcodex.router import RecentCommand, ParseError, parse_command
from wlcodex.status import (
    render_recent_list,
    ConversationSession,
    AgentRun,
    OrchestrationRun,
)
from wlcodex.task_service import TaskService


# --- Router tests ---


def test_parse_recent_default() -> None:
    cmd = parse_command("/recent")
    assert cmd == RecentCommand(n=5)


def test_parse_recent_with_n() -> None:
    cmd = parse_command("/recent 3")
    assert cmd == RecentCommand(n=3)


def test_parse_recent_n_1() -> None:
    cmd = parse_command("/recent 1")
    assert cmd == RecentCommand(n=1)


def test_parse_recent_n_20() -> None:
    cmd = parse_command("/recent 20")
    assert cmd == RecentCommand(n=20)


def test_parse_recent_n_zero_rejected() -> None:
    with pytest.raises(ParseError, match="1-20"):
        parse_command("/recent 0")


def test_parse_recent_n_21_rejected() -> None:
    with pytest.raises(ParseError, match="1-20"):
        parse_command("/recent 21")


def test_parse_recent_non_digit_rejected() -> None:
    with pytest.raises(ParseError, match="用法"):
        parse_command("/recent abc")


def test_parse_recent_extra_args_rejected() -> None:
    # Non-numeric arg after /recent is rejected
    with pytest.raises(ParseError, match="用法"):
        parse_command("/recent 5 extra stuff")


# --- DB query tests ---


def make_conversation(ledger: Ledger, chat_id: int, title: str, mode: str = "chief_engineer") -> int:
    c = ledger.create_conversation(
        chat_id=chat_id, user_id=200, title=title,
        mode=mode, workspace_alias="wlcodex",
    )
    return c.id


def make_agent_run(ledger: Ledger, convo_id: int, agent: str = "codex", role: str = "analysis", status: str = "done") -> int:
    r = ledger.create_agent_run(conversation_id=convo_id, agent=agent, role=role)
    ledger.update_agent_run_status(r.id, status)
    return r.id


def make_orch_run(ledger: Ledger, convo_id: int, status: str = "passed") -> int:
    r = ledger.create_orchestration_run(conversation_id=convo_id, goal="test")
    ledger.update_orchestration_run(r.id, status=status)
    return r.id


def test_db_empty_returns_empty(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    result = ledger.list_recent_conversation_summaries(chat_id=100, limit=5)
    assert result == []


def test_db_single_conversation_no_runs(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    make_conversation(ledger, 100, "测试对话")
    result = ledger.list_recent_conversation_summaries(chat_id=100, limit=5)
    assert len(result) == 1
    convo, agent_run, orch_run = result[0]
    assert convo.title == "测试对话"
    assert agent_run is None
    assert orch_run is None


def test_db_conversation_with_runs(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    cid = make_conversation(ledger, 100, "有运行的对话")
    make_agent_run(ledger, cid, agent="claude", role="implement", status="done")
    make_orch_run(ledger, cid, status="passed")

    result = ledger.list_recent_conversation_summaries(chat_id=100, limit=5)
    assert len(result) == 1
    convo, agent_run, orch_run = result[0]
    assert agent_run is not None
    assert agent_run.agent == "claude"
    assert agent_run.role == "implement"
    assert agent_run.status == "done"
    assert orch_run is not None
    assert orch_run.status == "passed"


def test_db_multiple_conversations_picks_latest_run(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    cid = make_conversation(ledger, 100, "多轮对话")
    make_agent_run(ledger, cid, agent="codex", role="analyze", status="done")
    make_agent_run(ledger, cid, agent="claude", role="implement", status="failed")
    make_orch_run(ledger, cid, status="running")
    make_orch_run(ledger, cid, status="passed")

    result = ledger.list_recent_conversation_summaries(chat_id=100, limit=5)
    assert len(result) == 1
    _, agent_run, orch_run = result[0]
    assert agent_run is not None
    # Should pick the latest (highest id) agent run
    assert agent_run.agent == "claude"
    assert orch_run is not None
    assert orch_run.status == "passed"


def test_db_respects_limit(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    for i in range(5):
        make_conversation(ledger, 100, f"对话 {i}")

    result = ledger.list_recent_conversation_summaries(chat_id=100, limit=3)
    assert len(result) == 3


def test_db_ignores_archived(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    make_conversation(ledger, 100, "活跃")
    c2 = make_conversation(ledger, 100, "已归档")
    ledger.archive_conversation(c2)

    result = ledger.list_recent_conversation_summaries(chat_id=100, limit=5)
    assert len(result) == 1
    assert result[0][0].title == "活跃"


def test_db_only_returns_requested_chat(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    make_conversation(ledger, 100, "Chat A")
    make_conversation(ledger, 200, "Chat B")

    result = ledger.list_recent_conversation_summaries(chat_id=100, limit=5)
    assert len(result) == 1
    assert result[0][0].title == "Chat A"


# --- Controller tests ---


@pytest.fixture
def ctrl(tmp_path: Path) -> CommandController:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (WorkspaceConfig("demo", Path("/tmp/demo"), True),))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    return CommandController(service, backend, inspector, ledger=ledger)


@pytest.mark.asyncio
async def test_controller_recent_empty(ctrl: CommandController) -> None:
    response = await ctrl.handle("/recent", {"chat_id": 123})
    assert "暂无历史对话" in response.text


@pytest.mark.asyncio
async def test_controller_recent_with_data(ctrl: CommandController) -> None:
    ledger = ctrl._ledger
    cid = ledger.create_conversation(
        chat_id=123, user_id=456, title="测试",
        mode="chief_engineer", workspace_alias="wlcodex",
    )
    r = ledger.create_agent_run(conversation_id=cid.id, agent="codex", role="analyze")
    ledger.update_agent_run_status(r.id, "done")
    o = ledger.create_orchestration_run(conversation_id=cid.id, goal="test")
    ledger.update_orchestration_run(o.id, status="passed")

    response = await ctrl.handle("/recent", {"chat_id": 123})
    assert "最近 1 条对话" in response.text
    assert "测试" in response.text
    assert "Codex" in response.text
    assert "analyze" in response.text
    assert "done" in response.text
    assert "已通过" in response.text


@pytest.mark.asyncio
async def test_controller_recent_n_1(ctrl: CommandController) -> None:
    response = await ctrl.handle("/recent 1", {"chat_id": 123})
    assert "暂无历史对话" in response.text


@pytest.mark.asyncio
async def test_controller_recent_n_out_of_range(ctrl: CommandController) -> None:
    response = await ctrl.handle("/recent 21", {"chat_id": 123})
    assert "1-20" in response.text


@pytest.mark.asyncio
async def test_controller_recent_no_ledger(tmp_path: Path) -> None:
    backend = FakeCodexBackend()
    service = TaskService(
        Ledger.open(tmp_path / "db2.sqlite3"),
        (WorkspaceConfig("demo", Path("/tmp/demo"), True),),
    )
    inspector = TaskInspector(service._ledger, tmp_path / "logs")
    ctrl_no_ledger = CommandController(service, backend, inspector, ledger=None)
    response = await ctrl_no_ledger.handle("/recent", {"chat_id": 123})
    assert "未完全初始化" in response.text


# --- Renderer tests ---


def _fake_session(id: int = 1, title: str = "test", mode: str = "chief_engineer") -> ConversationSession:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return ConversationSession(
        id=id, chat_id=100, user_id=200, title=title, mode=mode,
        workspace_alias="wlcodex", active_codex_task_id=None,
        active_claude_run_id=None, conversation_summary="",
        current_model="", created_at=now, updated_at=now, archived_at=None,
    )


def _fake_agent_run(agent: str = "codex", role: str = "analyze", status: str = "done") -> AgentRun:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return AgentRun(
        id=1, conversation_id=1, agent=agent, role=role, status=status,
        hidden_task_id=None, external_session_id=None,
        prompt_packet_summary="", completion_summary="",
        token_input=0, token_output=0, created_at=now, updated_at=now,
    )


def _fake_orch_run(status: str = "passed") -> OrchestrationRun:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return OrchestrationRun(
        id=1, conversation_id=1, goal="test", status=status,
        current_step="", verify_round=0, max_verify_rounds=3,
        last_codex_analysis="", last_claude_summary="",
        last_verification_result="", created_at=now, updated_at=now,
    )


def test_render_empty_state() -> None:
    text = render_recent_list([], limit=5)
    assert "暂无历史对话" in text


def test_render_single_with_runs() -> None:
    summaries = [
        (_fake_session(), _fake_agent_run(agent="claude", role="implement"), _fake_orch_run(status="running")),
    ]
    text = render_recent_list(summaries, limit=5)
    assert "最近 1 条对话" in text
    assert "Claude" in text
    assert "implement" in text
    assert "运行中" in text


def test_render_single_no_runs() -> None:
    summaries = [(_fake_session(), None, None)]
    text = render_recent_list(summaries, limit=5)
    assert "无运行记录" in text
    assert "无记录" in text


def test_render_does_not_leak_tokens() -> None:
    """Ensure token/prompt/completion details are not rendered."""
    summaries = [
        (_fake_session(), _fake_agent_run(), _fake_orch_run()),
    ]
    text = render_recent_list(summaries, limit=5)
    assert "token" not in text.lower()
    assert "prompt_packet" not in text.lower()
    assert "completion_summary" not in text.lower()


def test_render_multiple_conversations() -> None:
    summaries = [
        (_fake_session(id=1, title="A"), _fake_agent_run(), _fake_orch_run()),
        (_fake_session(id=2, title="B"), None, None),
    ]
    text = render_recent_list(summaries, limit=5)
    assert "最近 2 条对话" in text
    assert "A" in text
    assert "B" in text


def test_render_obeys_limit_label() -> None:
    summaries = [(_fake_session(), None, None)]
    text = render_recent_list(summaries, limit=20)
    assert "最多 20 条" in text


def test_recentcommand_default() -> None:
    cmd = RecentCommand()
    assert cmd.n == 5


def test_recentcommand_custom_n() -> None:
    cmd = RecentCommand(n=10)
    assert cmd.n == 10
