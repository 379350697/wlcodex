"""Tests for the Agent Session Library projection (Repair Task 3).

The Session Library projects existing agent_runs ledger rows into
user-safe AgentSessionSummary cards that hide internal ids.
"""

from __future__ import annotations

from types import SimpleNamespace


from wlcodex.workbench.sessions import (
    AgentSessionLibrary,
    AgentSessionResumability,
)


class FakeLedger:
    """Returns pre-configured agent runs for a given conversation_id."""

    def __init__(self, runs=None):
        self._runs = runs or []

    def list_recent_agent_runs(self, conversation_id, limit=50):
        assert conversation_id == 42, "Session library must query by conversation_id"
        return self._runs[:limit]


def _make_run(**kw):
    defaults = dict(
        id=9,
        conversation_id=42,
        agent="claude",
        role="implementation",
        status="done",
        external_session_id="cl-secret-1",
        prompt_packet_summary="",
        completion_summary="修复 Telegram 接管逻辑",
        created_at="2026-05-20T11:08:00+08:00",
        updated_at="2026-05-20T11:12:00+08:00",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# --- Listing ---


def test_session_library_lists_user_safe_agent_sessions():
    runs = [
        _make_run(id=9, agent="claude", role="implementation",
                  completion_summary="修复 Telegram 接管逻辑",
                  external_session_id="cl-secret-1"),
        _make_run(id=8, agent="codex", role="verification",
                  completion_summary="验收 Workbench 语义",
                  external_session_id="cx-secret-1"),
    ]
    library = AgentSessionLibrary(FakeLedger(runs))
    sessions = library.list_for_workbench(42)

    assert [s.agent for s in sessions] == ["claude", "codex"]
    assert sessions[0].title == "修复 Telegram 接管逻辑"
    assert sessions[0].resumability is AgentSessionResumability.RESUMABLE
    assert "cl-secret-1" not in sessions[0].user_label
    assert "cx-secret-1" not in sessions[1].user_label


def test_session_library_returns_summary_only_when_no_resume_reference():
    runs = [
        _make_run(id=9, agent="claude",
                  completion_summary="修复菜单",
                  external_session_id=""),
    ]
    sessions = AgentSessionLibrary(FakeLedger(runs)).list_for_workbench(42)

    assert sessions[0].resumability is AgentSessionResumability.SUMMARY_ONLY


def test_session_library_newest_first():
    runs = [
        _make_run(id=12, agent="claude", created_at="2026-05-20T12:00:00+08:00"),
        _make_run(id=11, agent="codex", created_at="2026-05-20T11:00:00+08:00"),
    ]
    sessions = AgentSessionLibrary(FakeLedger(runs)).list_for_workbench(42)

    assert sessions[0].source_run_id == 12
    assert sessions[1].source_run_id == 11


def test_session_library_deduplicates_same_agent_same_internal_ref():
    runs = [
        _make_run(id=10, agent="claude", external_session_id="cl-1",
                  completion_summary="改菜单"),
        _make_run(id=9, agent="claude", external_session_id="cl-1",
                  completion_summary="改菜单 again"),
    ]
    sessions = AgentSessionLibrary(FakeLedger(runs)).list_for_workbench(42)

    assert len(sessions) == 1
    assert sessions[0].source_run_id == 10


def test_session_library_falls_back_to_role_when_no_completion_summary():
    runs = [
        _make_run(id=9, agent="claude", completion_summary="",
                  prompt_packet_summary="", role="implementation"),
    ]
    sessions = AgentSessionLibrary(FakeLedger(runs)).list_for_workbench(42)

    assert "implementation" in sessions[0].title.lower()


def test_session_library_renders_codex_json_summary_as_user_text():
    runs = [
        _make_run(
            id=9,
            agent="codex",
            completion_summary=(
                '{"summary":"default flow ok","needs_implementation":false,'
                '"files_to_touch":[],"implementation_steps":[]}'
            ),
            external_session_id="",
        ),
    ]

    sessions = AgentSessionLibrary(FakeLedger(runs)).list_for_workbench(42)

    assert sessions[0].title == "default flow ok"
    assert "needs_implementation" not in sessions[0].title


def test_session_library_falls_back_to_agent_name_when_everything_empty():
    runs = [
        _make_run(id=9, agent="codex", completion_summary="",
                  prompt_packet_summary="", role=""),
    ]
    sessions = AgentSessionLibrary(FakeLedger(runs)).list_for_workbench(42)

    assert "codex" in sessions[0].title.lower()


# --- Get single session ---


def test_get_for_workbench_returns_matching_session():
    runs = [
        _make_run(id=7, agent="codex", completion_summary="验收"),
        _make_run(id=9, agent="claude", completion_summary="修复"),
    ]
    library = AgentSessionLibrary(FakeLedger(runs))
    session = library.get_for_workbench(42, source_run_id=7)

    assert session is not None
    assert session.agent == "codex"
    assert session.source_run_id == 7


def test_get_for_workbench_returns_none_when_not_found():
    library = AgentSessionLibrary(FakeLedger([]))
    assert library.get_for_workbench(42, source_run_id=999) is None


# --- Resumability ---


def test_live_status_when_status_is_running():
    runs = [
        _make_run(id=9, agent="claude", status="running",
                  external_session_id="cl-1"),
    ]
    sessions = AgentSessionLibrary(FakeLedger(runs)).list_for_workbench(42)

    assert sessions[0].resumability is AgentSessionResumability.RESUMABLE


def test_resumable_when_done_with_session_id():
    runs = [
        _make_run(id=9, agent="claude", status="done",
                  external_session_id="cl-1"),
    ]
    sessions = AgentSessionLibrary(FakeLedger(runs)).list_for_workbench(42)

    assert sessions[0].resumability is AgentSessionResumability.RESUMABLE


def test_summary_only_when_done_without_session_id():
    runs = [
        _make_run(id=9, agent="claude", status="done",
                  external_session_id=""),
    ]
    sessions = AgentSessionLibrary(FakeLedger(runs)).list_for_workbench(42)

    assert sessions[0].resumability is AgentSessionResumability.SUMMARY_ONLY


def test_summary_only_when_failed_without_session_id():
    runs = [
        _make_run(id=9, agent="codex", status="failed",
                  external_session_id=""),
    ]
    sessions = AgentSessionLibrary(FakeLedger(runs)).list_for_workbench(42)

    assert sessions[0].resumability is AgentSessionResumability.SUMMARY_ONLY


# --- User label safety ---


def test_user_label_never_exposes_internal_ref():
    runs = [
        _make_run(id=9, agent="claude", external_session_id="cl-abc-123",
                  completion_summary="修复现场"),
    ]
    sessions = AgentSessionLibrary(FakeLedger(runs)).list_for_workbench(42)

    assert "cl-abc-123" not in sessions[0].user_label
    assert "external_session_id" not in sessions[0].user_label
    assert "thread id" not in sessions[0].user_label


def test_internal_ref_is_present_but_separate_from_user_label():
    runs = [
        _make_run(id=9, agent="claude", external_session_id="cl-abc-123"),
    ]
    sessions = AgentSessionLibrary(FakeLedger(runs)).list_for_workbench(42)

    assert sessions[0].internal_ref == "cl-abc-123"


# --- Filtering (agent_runs that are not codex/claude should not appear) ---


def test_non_codex_claude_agents_are_excluded():
    runs = [
        _make_run(id=9, agent="orchestrator", completion_summary="流程"),
        _make_run(id=8, agent="claude", completion_summary="修复"),
    ]
    sessions = AgentSessionLibrary(FakeLedger(runs)).list_for_workbench(42)

    assert len(sessions) == 1
    assert sessions[0].agent == "claude"


# --- Limit ---


def test_list_respects_limit():
    runs = [
        _make_run(id=i, agent="codex", completion_summary=f"run {i}",
                  external_session_id=f"cx-{i}")
        for i in range(20)
    ]
    sessions = AgentSessionLibrary(FakeLedger(runs)).list_for_workbench(42, limit=5)

    assert len(sessions) == 5


# --- Edge: empty ---


def test_empty_library_returns_empty_list():
    library = AgentSessionLibrary(FakeLedger([]))
    assert library.list_for_workbench(42) == []
