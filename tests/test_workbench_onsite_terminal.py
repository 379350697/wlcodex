"""Task 4: Onsite Terminal behaviours — open, start-card, tail, leave, pause.

Tests cover:
- open_for_conversation returns session when one is attached
- open_for_conversation returns start-card decision when no session exists
- tail returns latest recorded frames
- leave_view pauses delivery without aborting the session
- pause_delivery / resume_delivery toggles delivery state
"""

import pytest

from wlcodex.surfaces.terminal.models import TerminalFrame, TerminalSessionRef
from wlcodex.surfaces.terminal.manager import (
    TerminalSessionManager,
    OnsiteDecisionKind,
)
from wlcodex.surfaces.terminal.renderer import (
    render_terminal_frame,
    render_onsite_header,
    render_start_card,
)
from wlcodex.surfaces.terminal.router import (
    TerminalCommand,
    TerminalCommandKind,
    route_terminal_command,
)


# ── Fake adapter for manager tests ────────────────────────────────────────

class FakeTerminalAdapter:
    def __init__(self):
        self.inputs: list[tuple[str, str]] = []

    async def send_input(self, session_ref, text):
        self.inputs.append((session_ref.external_session_id, text))


# ═══════════════════════════════════════════════════════════════════════════
# Onsite decision: open_for_conversation
# ═══════════════════════════════════════════════════════════════════════════

def test_open_onsite_returns_auto_open_when_session_exists():
    """With an attached session, open_for_conversation auto-opens it."""
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})
    manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl_1",
    )

    decision = manager.open_for_conversation(42, preferred_agent="claude")

    assert decision.kind == OnsiteDecisionKind.AUTO_OPEN
    assert decision.session_ref is not None
    assert decision.session_ref.external_session_id == "cl_1"
    assert decision.session_ref.status == "attached"


def test_open_onsite_returns_start_card_when_no_session():
    """When no session exists, return a start-card decision — never a dead end."""
    manager = TerminalSessionManager(adapters={})

    decision = manager.open_for_conversation(42, preferred_agent="claude")

    assert decision.kind == OnsiteDecisionKind.START_CARD
    assert decision.session_ref is None
    # Start card must suggest available agents
    assert "claude" in decision.available_agents
    assert "codex" in decision.available_agents
    assert "回驾驶舱" in decision.return_action


def test_open_onsite_prefers_active_agent_when_multiple_sessions():
    """When both Claude and Codex are attached, prefer the given agent."""
    claude_adapter = FakeTerminalAdapter()
    codex_adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(
        adapters={"claude": claude_adapter, "codex": codex_adapter}
    )
    manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl_1",
    )
    manager.attach(
        conversation_id=42,
        agent="codex",
        strategy="app_server",
        external_session_id="cx_1",
    )

    decision = manager.open_for_conversation(42, preferred_agent="codex")

    assert decision.kind == OnsiteDecisionKind.AUTO_OPEN
    assert decision.session_ref.agent == "codex"


def test_open_onsite_falls_back_to_any_when_preferred_not_found():
    """When preferred agent has no session, fall back to other attached agent."""
    claude_adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": claude_adapter})
    manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl_1",
    )

    decision = manager.open_for_conversation(42, preferred_agent="codex")

    assert decision.kind == OnsiteDecisionKind.AUTO_OPEN
    assert decision.session_ref.agent == "claude"


def test_open_onsite_start_card_when_all_sessions_detached():
    """Detached sessions should not count — return start card."""
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})
    ref = manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl_1",
    )
    manager.detach(ref)

    decision = manager.open_for_conversation(42, preferred_agent="claude")

    assert decision.kind == OnsiteDecisionKind.START_CARD


# ═══════════════════════════════════════════════════════════════════════════
# Frame recording + tail
# ═══════════════════════════════════════════════════════════════════════════

def test_tail_returns_latest_recorded_frames():
    """record_frame stores frames; tail returns the latest N."""
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})
    ref = manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl_1",
    )

    for i in range(10):
        manager.record_frame(
            ref,
            TerminalFrame(
                conversation_id=42,
                agent="claude",
                phase="implementation",
                text=f"frame_{i}",
                sequence=i,
            ),
        )

    frames = manager.tail(ref, limit=3)

    assert len(frames) == 3
    assert frames[0].text == "frame_7"
    assert frames[-1].text == "frame_9"


def test_tail_page_returns_frames_before_sequence():
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})
    ref = manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl_1",
    )
    for i in range(1, 8):
        manager.record_frame(
            ref,
            TerminalFrame(
                conversation_id=42,
                agent="claude",
                phase="implementation",
                text=f"frame_{i}",
                sequence=i,
            ),
        )

    frames = manager.tail_page(ref, limit=3, before_sequence=6)

    assert [frame.sequence for frame in frames] == [3, 4, 5]


def test_tail_returns_empty_list_when_no_frames_recorded():
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})
    ref = manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl_1",
    )

    frames = manager.tail(ref, limit=20)

    assert frames == []


def test_record_frame_is_scoped_per_session():
    """Frames recorded against one session must not leak to another."""
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter, "codex": adapter})
    cl_ref = manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl_1",
    )
    cx_ref = manager.attach(
        conversation_id=42,
        agent="codex",
        strategy="app_server",
        external_session_id="cx_1",
    )

    manager.record_frame(cl_ref, TerminalFrame(
        conversation_id=42, agent="claude", phase="implementation",
        text="claude_output", sequence=0,
    ))
    manager.record_frame(cx_ref, TerminalFrame(
        conversation_id=42, agent="codex", phase="analysis",
        text="codex_output", sequence=0,
    ))

    cl_tail = manager.tail(cl_ref, limit=10)
    cx_tail = manager.tail(cx_ref, limit=10)

    assert len(cl_tail) == 1
    assert cl_tail[0].text == "claude_output"
    assert len(cx_tail) == 1
    assert cx_tail[0].text == "codex_output"


# ═══════════════════════════════════════════════════════════════════════════
# Leave / pause delivery — never kill local work
# ═══════════════════════════════════════════════════════════════════════════

def test_leave_view_pauses_delivery_without_aborting_session():
    """leave_view pauses delivery; the session remains attached."""
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})
    ref = manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl_1",
    )

    manager.leave_view(ref)

    # Session must still exist — not killed
    assert ref.status == "attached"
    assert ref.external_session_id == "cl_1"
    # Delivery must be paused
    assert manager.is_delivery_paused(ref)


def test_pause_delivery_stops_push_but_keeps_session_alive():
    """pause_delivery stops phone push; session is NOT detached or killed."""
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})
    ref = manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl_1",
    )

    manager.pause_delivery(ref)

    assert manager.is_delivery_paused(ref)
    # Session still active — not detached
    active = manager.active_for_conversation(42)
    assert active is not None
    assert active.external_session_id == "cl_1"


def test_resume_delivery_restores_push():
    """After pause_delivery, resume_delivery re-enables push."""
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})
    ref = manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl_1",
    )

    manager.pause_delivery(ref)
    assert manager.is_delivery_paused(ref)

    manager.resume_delivery(ref)
    assert not manager.is_delivery_paused(ref)


def test_leave_then_reopen_returns_same_session():
    """After leave_view and re-open, the same session is still there."""
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})
    manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl_1",
    )

    active = manager.active_for_conversation(42)
    assert active is not None
    manager.leave_view(active)

    # Session is still attached (leave != detach)
    decision = manager.open_for_conversation(42, preferred_agent="claude")
    assert decision.kind == OnsiteDecisionKind.AUTO_OPEN
    assert decision.session_ref.external_session_id == "cl_1"


# ═══════════════════════════════════════════════════════════════════════════
# Router: /terminal leave
# ═══════════════════════════════════════════════════════════════════════════

def test_route_terminal_leave():
    cmd = route_terminal_command("/terminal leave")
    assert cmd.kind == TerminalCommandKind.LEAVE


def test_route_terminal_bare_is_still_show_status():
    """Bare /terminal still maps to SHOW_STATUS (attach logic handles open)."""
    cmd = route_terminal_command("/terminal")
    assert cmd.kind == TerminalCommandKind.SHOW_STATUS


def test_route_terminal_legacy_commands_still_work():
    """Existing compat commands are preserved."""
    for text, kind in [
        ("/terminal codex", TerminalCommandKind.SELECT_AGENT),
        ("/terminal claude", TerminalCommandKind.SELECT_AGENT),
        ("/terminal agent codex", TerminalCommandKind.SELECT_AGENT),
        ("/terminal tail", TerminalCommandKind.TAIL),
        ("/terminal pause", TerminalCommandKind.PAUSE),
        ("/terminal detach", TerminalCommandKind.DETACH),
        ("/terminal product", TerminalCommandKind.SWITCH_TO_PRODUCT),
    ]:
        cmd = route_terminal_command(text)
        assert cmd.kind == kind, f"Failed for: {text}"


# ═══════════════════════════════════════════════════════════════════════════
# Renderer: Onsite language
# ═══════════════════════════════════════════════════════════════════════════

def test_render_onsite_header_shows_live_worksite_language():
    header = render_onsite_header(agent="claude", phase="implementation")
    assert "现场" in header
    assert "DeepSeek 开发工程师" in header
    assert "implementation" in header.lower()


def test_render_start_card_has_next_actions():
    card = render_start_card()
    assert "没有可接管的现场" in card or "现场" in card
    assert "DeepSeek 开发工程师" in card
    assert "GPT 开发工程师" in card
    assert "驾驶舱" in card


def test_render_start_card_never_says_dead_or_error():
    """The start card must suggest actions, not terminate."""
    card = render_start_card()
    assert "错误" not in card
    assert "失败" not in card
    assert "不可用" not in card


def test_render_onsite_header_includes_running_status():
    header = render_onsite_header(agent="codex", phase="verification")
    assert "running" in header or "运行" in header


def test_render_start_card_respects_available_agents():
    """Parameterized start card only lists the passed agents."""
    card = render_start_card(available_agents=("claude",))
    assert "启动 DeepSeek 开发工程师 现场" in card
    assert "启动 GPT 开发工程师 现场" not in card
    assert "回驾驶舱" in card

    card2 = render_start_card(available_agents=("codex",))
    assert "启动 GPT 开发工程师 现场" in card2
    assert "启动 DeepSeek 开发工程师 现场" not in card2


def test_render_terminal_frame_still_works_for_raw_output():
    """Existing render_terminal_frame preserves raw terminal style."""
    frame = TerminalFrame(
        conversation_id=1,
        agent="claude",
        phase="implementation",
        text="$ pytest -q",
        sequence=1,
    )
    rendered = render_terminal_frame(frame)
    # Agent labels are user-facing role names.
    assert rendered.startswith("[DeepSeek 开发工程师:implementation]")
    assert "$ pytest -q" in rendered


# ═══════════════════════════════════════════════════════════════════════════
# Closed-loop integration: no external deps, full manager lifecycle
# ═══════════════════════════════════════════════════════════════════════════

def test_closed_loop_no_session_shows_start_card():
    """User without any session gets a start card with next actions."""
    manager = TerminalSessionManager(adapters={"claude": FakeTerminalAdapter()})

    decision = manager.open_for_conversation(42, preferred_agent="claude")

    assert decision.kind == OnsiteDecisionKind.START_CARD
    assert "claude" in decision.available_agents
    card = render_start_card()
    assert "启动 DeepSeek 开发工程师 现场" in card


def test_closed_loop_attach_open_tail_leave_reopen():
    """Full lifecycle: attach → open → run frames → tail → leave → reopen."""
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})

    # 1. Attach a Claude session (simulating orchestrator)
    ref = manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl_sess_1",
    )

    # 2. User opens Onsite → auto open
    d1 = manager.open_for_conversation(42, preferred_agent="claude")
    assert d1.kind == OnsiteDecisionKind.AUTO_OPEN
    assert d1.session_ref.external_session_id == "cl_sess_1"
    header = render_onsite_header(d1.session_ref.agent, "implementation")
    assert "现场" in header

    # 3. Frames arrive → record
    for i in range(5):
        manager.record_frame(ref, TerminalFrame(
            conversation_id=42, agent="claude", phase="implementation",
            text=f"log line {i}", frame_kind="stdout", sequence=i,
        ))

    # 4. User taps "查看尾部" → tail
    last_frames = manager.tail(ref, limit=3)
    assert len(last_frames) == 3
    assert last_frames[-1].text == "log line 4"

    # 5. User taps "暂停推送" → pause delivery
    manager.pause_delivery(ref)
    assert manager.is_delivery_paused(ref)
    # Session still active
    assert manager.active_for_conversation(42) is not None

    # 6. User taps "离开" → leave
    manager.leave_view(ref)
    # Session still attached (leave != detach)
    active = manager.active_for_conversation(42)
    assert active is not None
    assert active.status == "attached"

    # 7. User comes back → auto re-open
    d2 = manager.open_for_conversation(42, preferred_agent="claude")
    assert d2.kind == OnsiteDecisionKind.AUTO_OPEN
    assert d2.session_ref.external_session_id == "cl_sess_1"


def test_closed_loop_leave_never_detaches():
    """leave does NOT change session status to detached — it stays alive."""
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})
    ref = manager.attach(
        conversation_id=42, agent="claude",
        strategy="stream_json", external_session_id="cl_x",
    )

    manager.leave_view(ref)

    # detach is a stronger operation that makes sessions invisible
    # leave is softer — it only pauses push
    active = manager.active_for_conversation(42)
    assert active is not None
    assert active.status == "attached"


def test_closed_loop_detach_is_stronger_than_leave():
    """detach makes session invisible; leave keeps it visible."""
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})
    ref = manager.attach(
        conversation_id=42, agent="claude",
        strategy="stream_json", external_session_id="cl_x",
    )

    manager.detach(ref)

    # After detach, no active session for conversation
    assert manager.active_for_conversation(42) is None
    # open_for_conversation returns start card
    decision = manager.open_for_conversation(42, preferred_agent="claude")
    assert decision.kind == OnsiteDecisionKind.START_CARD


def test_closed_loop_cockpit_to_onsite_to_cockpit():
    """Simulate the full Cockpit→Onsite→Cockpit transition."""
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter, "codex": FakeTerminalAdapter()})

    # Workbench says: active_agent=claude, active_phase=implementation
    # User taps "接管现场" → Task 7 calls:
    decision = manager.open_for_conversation(42, preferred_agent="claude")
    assert decision.kind == OnsiteDecisionKind.START_CARD  # no session yet
    card = render_start_card()
    assert "当前没有可接管的现场" in card

    # Later, orchestrator attaches Claude session
    ref = manager.attach(
        conversation_id=42, agent="claude",
        strategy="stream_json", external_session_id="cl_abc",
    )

    # User taps "接管现场" again → auto open
    decision = manager.open_for_conversation(42, preferred_agent="claude")
    assert decision.kind == OnsiteDecisionKind.AUTO_OPEN
    assert decision.session_ref.agent == "claude"

    # User sends text in Onsite → routed to ONSITE_INPUT (routing layer)
    # Onsite text must never call Cockpit controller
    from wlcodex.workbench.models import WorkbenchRoute, ViewMode, WorkbenchState
    from wlcodex.workbench.routing import route_plain_text
    state = WorkbenchState(
        conversation_id=42, chat_id=100, workspace_alias="wlcodex",
        view=ViewMode.ONSITE, active_agent="claude", active_phase="implementation",
    )
    assert route_plain_text(state, "任意文本") is WorkbenchRoute.ONSITE_INPUT
    assert route_plain_text(state, "任意文本") is not WorkbenchRoute.ORCHESTRATED_COCKPIT

    # User taps "回驾驶舱" → Task 7 calls leave_view
    manager.leave_view(ref)
    assert manager.is_delivery_paused(ref)

    # Workbench state transitions back to COCKPIT
    state.view = ViewMode.COCKPIT
    assert route_plain_text(state, "帮我做XX") is WorkbenchRoute.ORCHESTRATED_COCKPIT

    # Session is still alive — local work continues
    assert manager.active_for_conversation(42) is not None


def test_closed_loop_no_session_is_never_dead_end():
    """Every path out of open_for_conversation leads to user action."""
    # Scenarios that should never dead-end
    scenarios = [
        # (adapters, preferred_agent, desc)
        ({}, "claude", "no adapters at all"),
        ({"claude": FakeTerminalAdapter()}, "claude", "has adapter but no session"),
        ({"claude": FakeTerminalAdapter()}, "codex", "wrong preferred agent"),
    ]

    for adapters, pref, desc in scenarios:
        manager = TerminalSessionManager(adapters=adapters)
        decision = manager.open_for_conversation(42, preferred_agent=pref)
        assert decision.kind == OnsiteDecisionKind.START_CARD, (
            f"Dead end for: {desc}"
        )
        # Start card must have actionable suggestions
        assert len(decision.available_agents) > 0 or decision.return_action, (
            f"No next action for: {desc}"
        )
        card = render_start_card()
        assert "错误" not in card
        assert "失败" not in card
