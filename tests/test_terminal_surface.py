"""Tests for wlcodex.surfaces.terminal — models, renderer, manager, router, adapters."""

import pytest

from wlcodex.surfaces.terminal.models import TerminalFrame, TerminalSessionRef
from wlcodex.surfaces.terminal.renderer import render_terminal_frame
from wlcodex.surfaces.terminal.renderer import (
    render_onsite_header,
    render_start_card,
    render_terminal_frames_append,
    render_tail_output,
    render_pause_confirmation,
    render_resume_confirmation,
    render_detach_confirmation,
    render_return_to_cockpit,
    render_no_session_hint,
    render_busy_selector,
)
from wlcodex.surfaces.terminal.redaction import redact_terminal_text, redact_and_cap_frame
from wlcodex.surfaces.terminal.manager import TerminalSessionManager
from wlcodex.surfaces.core.models import TerminalPolicy
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
    assert rendered == "[DeepSeek 开发工程师:implementation] Running pytest -q"


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


def test_render_terminal_frames_append_keeps_multiple_frames():
    frames = [
        TerminalFrame(1, "codex", "implementer", "a", sequence=1),
        TerminalFrame(1, "codex", "implementer", "b", sequence=2),
    ]

    rendered = render_terminal_frames_append(frames)

    assert "a" in rendered
    assert "b" in rendered
    assert rendered.index("a") < rendered.index("b")


# ── Task 5: Session Manager ────────────────────────────────────────────────

class FakeTerminalAdapter:
    """Records all inputs for assertion; used by manager tests."""

    def __init__(self):
        self.inputs: list[tuple[str, str]] = []

    async def send_input(self, session_ref, text):
        self.inputs.append((session_ref.external_session_id, text))


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


def test_route_terminal_tail_page():
    cmd = route_terminal_command("/terminal tail before 12 5")

    assert cmd.kind == TerminalCommandKind.TAIL
    assert cmd.before_sequence == 12
    assert cmd.limit == 5


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


# ── Terminal Renderer: onsite header, start card, tail ──────────────────────


def test_render_onsite_header():
    assert "现场" in render_onsite_header("claude", "implementation")
    assert "DeepSeek 开发工程师" in render_onsite_header("claude", "implementation")
    assert "implementation" in render_onsite_header("claude", "implementation")


def test_render_onsite_header_codex():
    text = render_onsite_header("codex", "analysis")
    assert "GPT 开发工程师" in text
    assert "analysis" in text


def test_render_start_card_default():
    text = render_start_card()
    assert "启动 DeepSeek 开发工程师 现场" in text
    assert "启动 GPT 开发工程师 现场" in text
    assert "回驾驶舱" in text


def test_render_start_card_custom_agents():
    text = render_start_card(("claude",))
    assert "启动 DeepSeek 开发工程师 现场" in text
    assert "GPT 开发工程师" not in text


def test_render_start_card_no_dead_end():
    text = render_start_card()
    # Must always offer next steps
    assert "启动" in text or "你可以" in text


def test_render_tail_output_empty():
    assert "暂无" in render_tail_output([])


def test_render_tail_output_with_frames():
    frames = [
        TerminalFrame(conversation_id=1, agent="claude", phase="impl", text="line 1"),
        TerminalFrame(conversation_id=1, agent="claude", phase="impl", text="line 2"),
    ]
    text = render_tail_output(frames)
    assert "[DeepSeek 开发工程师:impl]" in text
    assert "line 1" in text
    assert "line 2" in text


def test_render_tail_output_limit():
    frames = [
        TerminalFrame(conversation_id=1, agent="codex", phase="analysis", text=f"output {i}")
        for i in range(30)
    ]
    text = render_tail_output(frames, limit=5)
    # Should only include last 5 frames
    assert "output 29" in text
    assert "output 25" in text
    assert "output 24" not in text


def test_render_pause_confirmation():
    text = render_pause_confirmation()
    assert "暂停" in text
    assert "tail" in text


def test_render_resume_confirmation():
    assert "恢复" in render_resume_confirmation()


def test_render_detach_confirmation():
    text = render_detach_confirmation()
    assert "离开" in text
    assert "terminal" in text


def test_render_return_to_cockpit():
    assert "驾驶舱" in render_return_to_cockpit()


def test_render_no_session_hint():
    assert "没有" in render_no_session_hint()


def test_render_busy_selector_with_agent():
    text = render_busy_selector("codex")
    assert "GPT 开发工程师" in text


def test_render_busy_selector_no_agent():
    text = render_busy_selector(None)
    assert "运行" in text


# ── Terminal Redaction and Capping ────────────────────────────────────────


def test_redact_and_cap_frame_default():
    text = "normal output"
    assert redact_and_cap_frame(text) == "normal output"


def test_redact_and_cap_frame_redacts_secrets():
    text = "WLCODEX_TELEGRAM_BOT_TOKEN=abc123 ANTHROPIC_API_KEY=sk-xxx"
    result = redact_and_cap_frame(text)
    assert "abc123" not in result
    assert "sk-xxx" not in result
    assert "<redacted>" in result


def test_redact_and_cap_frame_caps_oversized():
    text = "x" * 5000
    result = redact_and_cap_frame(text, max_chars=3900)
    assert len(result) <= 3900
    assert "截断" in result
    assert "/terminal tail" in result


def test_redact_and_cap_frame_preserves_short_text():
    text = "short output"
    result = redact_and_cap_frame(text, max_chars=3900)
    assert result == "short output"


def test_redact_and_cap_frame_redaction_can_be_disabled():
    text = "TELEGRAM_BOT_TOKEN=secret123"
    result = redact_and_cap_frame(text, redaction_enabled=False)
    assert "secret123" in result


def test_redact_terminal_text_from_renderer():
    """Verify render_terminal_frame applies redaction by default."""
    frame = TerminalFrame(
        conversation_id=1,
        agent="codex",
        phase="analysis",
        text="ANTHROPIC_API_KEY=sk-test-key output here",
    )
    result = render_terminal_frame(frame)
    assert "sk-test-key" not in result
    assert "<redacted>" in result


def test_render_terminal_frame_caps_long_output():
    """Verify render_terminal_frame caps output to max_frame_chars."""
    frame = TerminalFrame(
        conversation_id=1,
        agent="codex",
        phase="analysis",
        text="x" * 5000,
    )
    policy = TerminalPolicy(max_frame_chars=3500, redaction_enabled=True)
    result = render_terminal_frame(frame, policy=policy)
    # Prefix like [Codex:analysis] adds ~20 chars
    assert len(result) <= 3500 + 30  # margin for prefix
    assert "截断" in result


# ── Tail output total cap (blocking issue 2) ────────────────────────────────


def test_render_tail_output_caps_total_to_max_total_chars():
    """render_tail_output must cap total output to max_total_chars (Telegram limit)."""
    # Build 20 frames each near 400 chars — total would be ~8000+
    frames = [
        TerminalFrame(
            conversation_id=1, agent="codex", phase="analysis",
            text="x" * 400,
        )
        for _ in range(20)
    ]
    text = render_tail_output(frames, max_total_chars=3900)
    assert len(text) <= 3900, f"tail output len={len(text)} exceeds cap"


def test_render_tail_output_keeps_recent_frames_when_capped():
    """When total exceeds cap, older frames are dropped first."""
    frames = [
        TerminalFrame(
            conversation_id=1, agent="claude", phase="impl",
            text=f"frame_{i:04d}",
        )
        for i in range(50)
    ]
    text = render_tail_output(frames, max_total_chars=500)
    # Most recent frame must be present
    assert "frame_0049" in text
    # Much older frame should be dropped
    assert "frame_0000" not in text
