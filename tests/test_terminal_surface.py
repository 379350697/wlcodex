"""Tests for wlcodex.surfaces.terminal — models, renderer, manager, router, adapters."""

import pytest

from wlcodex.surfaces.terminal.models import TerminalFrame, TerminalSessionRef
from wlcodex.surfaces.terminal.renderer import render_terminal_frame
from wlcodex.surfaces.terminal.manager import TerminalSessionManager
from wlcodex.surfaces.terminal.router import (
    TerminalCommand,
    TerminalCommandKind,
    route_terminal_command,
)


# ── Task 4: Models ─────────────────────────────────────────────────────────

def test_terminal_frame_renders_agent_phase_prefix():
    frame = TerminalFrame(
        conversation_id=42,
        agent="claude",
        phase="implementation",
        text="Running pytest -q",
        frame_kind="stdout",
        sequence=7,
    )
    rendered = render_terminal_frame(frame)
    assert rendered == "[claude:implementation] Running pytest -q"


def test_terminal_session_ref_keeps_strategy():
    ref = TerminalSessionRef(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="claude_1",
        status="attached",
    )
    assert ref.strategy == "stream_json"
    assert ref.status == "attached"


def test_terminal_frame_all_fields_preserved():
    frame = TerminalFrame(
        conversation_id=42,
        agent="codex",
        phase="analysis",
        text="diff --git a/x.py b/x.py\n+12 -3",
        frame_kind="diff",
        sequence=15,
    )
    assert frame.conversation_id == 42
    assert frame.agent == "codex"
    assert frame.phase == "analysis"
    assert frame.text == "diff --git a/x.py b/x.py\n+12 -3"
    assert frame.frame_kind == "diff"
    assert frame.sequence == 15


def test_terminal_session_ref_has_required_fields():
    ref = TerminalSessionRef(
        conversation_id=1,
        agent="codex",
        strategy="app_server",
        external_session_id="thr_abc",
        status="attached",
    )
    assert ref.conversation_id == 1
    assert ref.agent == "codex"
    assert ref.strategy == "app_server"
    assert ref.external_session_id == "thr_abc"
    assert ref.status == "attached"


def test_terminal_session_ref_default_status_is_detached():
    ref = TerminalSessionRef(
        conversation_id=7,
        agent="claude",
        strategy="stream_json",
        external_session_id="sess_xyz",
    )
    assert ref.status == "detached"


def test_terminal_frame_default_frame_kind_is_stdout():
    frame = TerminalFrame(
        conversation_id=1,
        agent="claude",
        phase="planning",
        text="hello world",
        sequence=1,
    )
    assert frame.frame_kind == "stdout"


def test_terminal_frame_default_sequence_is_zero():
    frame = TerminalFrame(
        conversation_id=1,
        agent="claude",
        phase="planning",
        text="first",
    )
    assert frame.sequence == 0


def test_render_terminal_frame_includes_text_verbatim():
    frame = TerminalFrame(
        conversation_id=1,
        agent="system",
        phase="startup",
        text="Session ready",
        frame_kind="system",
        sequence=0,
    )
    rendered = render_terminal_frame(frame)
    assert rendered.endswith(" Session ready")
    assert rendered.startswith("[system:startup]")


# ── Task 5: Session Manager ────────────────────────────────────────────────

class FakeTerminalAdapter:
    """Records all inputs for assertion; used by manager tests."""

    def __init__(self):
        self.inputs: list[tuple[str, str]] = []

    async def send_input(self, session_ref, text):
        self.inputs.append((session_ref.external_session_id, text))


class FakeDetachAdapter:
    """Minimal adapter that tracks detach calls."""

    def __init__(self):
        self.detached: list[str] = []

    async def detach(self, session_ref):
        self.detached.append(session_ref.external_session_id)


@pytest.mark.asyncio
async def test_terminal_manager_sends_input_to_selected_session():
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})
    ref = manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="claude_1",
    )

    await manager.send_input(ref, "continue")

    assert adapter.inputs == [("claude_1", "continue")]


@pytest.mark.asyncio
async def test_terminal_manager_attach_creates_session_ref():
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})
    ref = manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="claude_1",
    )

    assert ref.conversation_id == 42
    assert ref.agent == "claude"
    assert ref.strategy == "stream_json"
    assert ref.external_session_id == "claude_1"
    assert ref.status == "attached"


def test_terminal_manager_attach_raises_for_unknown_agent():
    manager = TerminalSessionManager(adapters={})
    with pytest.raises(ValueError, match="claude"):
        manager.attach(
            conversation_id=42,
            agent="claude",
            strategy="stream_json",
            external_session_id="x",
        )


def test_terminal_manager_detach_changes_status():
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})
    ref = manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="claude_1",
    )

    detached = manager.detach(ref)

    assert detached.status == "detached"


@pytest.mark.asyncio
async def test_terminal_manager_send_input_raises_for_unknown_agent():
    manager = TerminalSessionManager(adapters={})
    ref = TerminalSessionRef(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="x",
        status="attached",
    )
    with pytest.raises(ValueError, match="claude"):
        await manager.send_input(ref, "hello")


def test_terminal_manager_active_for_conversation_returns_none_when_empty():
    manager = TerminalSessionManager(adapters={})
    assert manager.active_for_conversation(42) is None


def test_terminal_manager_active_for_conversation_returns_latest_attached():
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})
    ref1 = manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl_1",
    )
    ref2 = manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl_2",
    )
    manager.detach(ref1)

    active = manager.active_for_conversation(42)
    assert active is not None
    assert active.external_session_id == "cl_2"
    assert active.status == "attached"


def test_terminal_manager_active_for_conversation_returns_none_when_all_detached():
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})
    ref = manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl_1",
    )
    manager.detach(ref)

    assert manager.active_for_conversation(42) is None


def test_terminal_manager_active_for_conversation_scoped_per_conversation():
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})
    manager.attach(
        conversation_id=1,
        agent="claude",
        strategy="stream_json",
        external_session_id="a",
    )
    manager.attach(
        conversation_id=2,
        agent="claude",
        strategy="stream_json",
        external_session_id="b",
    )

    a = manager.active_for_conversation(1)
    b = manager.active_for_conversation(2)
    assert a is not None and a.external_session_id == "a"
    assert b is not None and b.external_session_id == "b"


@pytest.mark.asyncio
async def test_terminal_manager_uses_correct_adapter_per_agent():
    claude_adapter = FakeTerminalAdapter()
    codex_adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(
        adapters={"claude": claude_adapter, "codex": codex_adapter}
    )

    cl_ref = manager.attach(
        conversation_id=1,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl_1",
    )
    cx_ref = manager.attach(
        conversation_id=1,
        agent="codex",
        strategy="app_server",
        external_session_id="thr_1",
    )

    await manager.send_input(cl_ref, "to claude")
    await manager.send_input(cx_ref, "to codex")

    assert claude_adapter.inputs == [("cl_1", "to claude")]
    assert codex_adapter.inputs == [("thr_1", "to codex")]


# ── Task 5: Router ─────────────────────────────────────────────────────────

def test_route_terminal_agent_codex_command():
    cmd = route_terminal_command("/terminal agent codex")
    assert cmd.kind == TerminalCommandKind.SELECT_AGENT
    assert cmd.agent == "codex"


def test_route_terminal_agent_claude_command():
    cmd = route_terminal_command("/terminal agent claude")
    assert cmd.kind == TerminalCommandKind.SELECT_AGENT
    assert cmd.agent == "claude"


def test_route_terminal_product_switches_to_product_mode():
    cmd = route_terminal_command("/terminal product")
    assert cmd.kind == TerminalCommandKind.SWITCH_TO_PRODUCT


def test_route_terminal_detach():
    cmd = route_terminal_command("/terminal detach")
    assert cmd.kind == TerminalCommandKind.DETACH


def test_route_terminal_tail():
    cmd = route_terminal_command("/terminal tail")
    assert cmd.kind == TerminalCommandKind.TAIL


def test_route_terminal_pause():
    cmd = route_terminal_command("/terminal pause")
    assert cmd.kind == TerminalCommandKind.PAUSE


def test_route_terminal_bare_command_defaults_to_attach():
    cmd = route_terminal_command("/terminal")
    assert cmd.kind == TerminalCommandKind.SHOW_STATUS


def test_route_terminal_codex_shorthand_selects_codex():
    cmd = route_terminal_command("/terminal codex")
    assert cmd.kind == TerminalCommandKind.SELECT_AGENT
    assert cmd.agent == "codex"


def test_route_terminal_claude_shorthand_selects_claude():
    cmd = route_terminal_command("/terminal claude")
    assert cmd.kind == TerminalCommandKind.SELECT_AGENT
    assert cmd.agent == "claude"


def test_route_terminal_unknown_subcommand_falls_back_to_show_status():
    cmd = route_terminal_command("/terminal unknown_thing")
    assert cmd.kind == TerminalCommandKind.SHOW_STATUS


def test_route_terminal_command_is_dataclass():
    cmd = TerminalCommand(kind=TerminalCommandKind.SELECT_AGENT, agent="codex")
    assert cmd.kind == TerminalCommandKind.SELECT_AGENT
    assert cmd.agent == "codex"
    assert cmd.mode is None


# ── Task 6: Claude Terminal Adapter ────────────────────────────────────────

class FakeClaudeBackend:
    def __init__(self):
        self.received: list[tuple[str, str]] = []

    async def send_terminal_input(self, session_id: str, text: str) -> None:
        self.received.append((session_id, text))


@pytest.mark.asyncio
async def test_claude_terminal_adapter_delegates_input():
    from wlcodex.surfaces.terminal.claude_remote import ClaudeTerminalAdapter

    backend = FakeClaudeBackend()
    adapter = ClaudeTerminalAdapter(backend)

    await adapter.send_input_by_session_id("claude_1", "next")

    assert backend.received == [("claude_1", "next")]


@pytest.mark.asyncio
async def test_claude_terminal_adapter_has_send_input_method_for_manager():
    from wlcodex.surfaces.terminal.claude_remote import ClaudeTerminalAdapter

    backend = FakeClaudeBackend()
    adapter = ClaudeTerminalAdapter(backend)

    ref = TerminalSessionRef(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="claude_1",
        status="attached",
    )
    await adapter.send_input(ref, "ls -la")

    assert backend.received == [("claude_1", "ls -la")]


@pytest.mark.asyncio
async def test_claude_terminal_adapter_supports_multiple_inputs():
    from wlcodex.surfaces.terminal.claude_remote import ClaudeTerminalAdapter

    backend = FakeClaudeBackend()
    adapter = ClaudeTerminalAdapter(backend)

    await adapter.send_input_by_session_id("sess_a", "first")
    await adapter.send_input_by_session_id("sess_a", "second")
    await adapter.send_input_by_session_id("sess_a", "third")

    assert len(backend.received) == 3
    assert backend.received[0] == ("sess_a", "first")
    assert backend.received[2] == ("sess_a", "third")


# ── Task 6: Codex Terminal Adapter ─────────────────────────────────────────

class FakeCodexBackend:
    def __init__(self):
        self.received: list[tuple[str, str]] = []

    async def steer_thread(self, thread_id: str, text: str) -> None:
        self.received.append((thread_id, text))


@pytest.mark.asyncio
async def test_codex_terminal_adapter_delegates_input():
    from wlcodex.surfaces.terminal.codex_terminal import CodexTerminalAdapter

    backend = FakeCodexBackend()
    adapter = CodexTerminalAdapter(backend)

    await adapter.send_input_by_thread_id("thr_1", "inspect diff")

    assert backend.received == [("thr_1", "inspect diff")]


@pytest.mark.asyncio
async def test_codex_terminal_adapter_has_send_input_method_for_manager():
    from wlcodex.surfaces.terminal.codex_terminal import CodexTerminalAdapter

    backend = FakeCodexBackend()
    adapter = CodexTerminalAdapter(backend)

    ref = TerminalSessionRef(
        conversation_id=42,
        agent="codex",
        strategy="app_server",
        external_session_id="thr_1",
        status="attached",
    )
    await adapter.send_input(ref, "continue")

    assert backend.received == [("thr_1", "continue")]


@pytest.mark.asyncio
async def test_codex_terminal_adapter_supports_multiple_inputs():
    from wlcodex.surfaces.terminal.codex_terminal import CodexTerminalAdapter

    backend = FakeCodexBackend()
    adapter = CodexTerminalAdapter(backend)

    await adapter.send_input_by_thread_id("thr_x", "step 1")
    await adapter.send_input_by_thread_id("thr_x", "step 2")

    assert len(backend.received) == 2
    assert backend.received == [("thr_x", "step 1"), ("thr_x", "step 2")]


# ── Task 6: Manager + Adapter Integration ──────────────────────────────────

@pytest.mark.asyncio
async def test_manager_integrates_with_claude_terminal_adapter():
    from wlcodex.surfaces.terminal.claude_remote import ClaudeTerminalAdapter

    backend = FakeClaudeBackend()
    adapter = ClaudeTerminalAdapter(backend)
    manager = TerminalSessionManager(adapters={"claude": adapter})

    ref = manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl_session",
    )
    await manager.send_input(ref, "pytest -q")

    assert backend.received == [("cl_session", "pytest -q")]


@pytest.mark.asyncio
async def test_manager_integrates_with_codex_terminal_adapter():
    from wlcodex.surfaces.terminal.codex_terminal import CodexTerminalAdapter

    backend = FakeCodexBackend()
    adapter = CodexTerminalAdapter(backend)
    manager = TerminalSessionManager(adapters={"codex": adapter})

    ref = manager.attach(
        conversation_id=42,
        agent="codex",
        strategy="app_server",
        external_session_id="codex_thread",
    )
    await manager.send_input(ref, "show diff")

    assert backend.received == [("codex_thread", "show diff")]
