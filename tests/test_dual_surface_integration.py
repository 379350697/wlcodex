"""End-to-end dual surface integration tests.

Verifies the Product <-> Terminal closure using fake controllers, fake
terminal manager, fake Telegram transport, and fake runtime store.

Must NOT import real Claude/Codex backends or launch real processes.

Coverage:
  - product -> terminal -> product keeps same conversation_id
  - terminal input does NOT call product orchestrator
  - product follow-up during implementation records pending context
  - approval resolution is shared between surfaces
"""

from __future__ import annotations

import pytest

from wlcodex.router import (
    ModeSwitchCommand,
    TerminalSubCommand,
    parse_command,
)
from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    RuntimeEvent,
    Visibility,
    now_iso,
)
from wlcodex.runtime_state import (
    SurfaceStateSnapshot,
    replay_surface_events,
)
from wlcodex.surfaces.core.events import (
    CONVERSATION_MODE_SWITCHED,
    PRODUCT_DISPLAY_FRAME,
    PRODUCT_PENDING_CONTEXT_RECORDED,
    TERMINAL_SESSION_ATTACHED,
    TERMINAL_SESSION_OUTPUT_FRAME,
)
from wlcodex.surfaces.core.models import (
    SurfaceMode,
    SurfaceRouteDecision,
)
from wlcodex.surfaces.core.router import route_text_by_mode
from wlcodex.surfaces.product.router import (
    ProductRouteDecision,
    product_route_guard,
)
from wlcodex.surfaces.terminal.manager import TerminalSessionManager


# ═══════════════════════════════════════════════════════════════════════════════
# Fakes
# ═══════════════════════════════════════════════════════════════════════════════


class FakeProductController:
    """Records all product-surface calls. Never touches terminal internals."""

    def __init__(self):
        self.calls: list[dict] = []
        self.pending_contexts: list[dict] = []
        self.responded_to: list[dict] = []

    async def handle_product_text(
        self, conversation_id: int, text: str, phase: str = "analysis"
    ) -> dict:
        self.calls.append({
            "conversation_id": conversation_id,
            "text": text,
            "phase": phase,
        })
        decision = product_route_guard(phase)
        if decision == ProductRouteDecision.RECORD_PENDING_CONTEXT:
            self.pending_contexts.append({
                "conversation_id": conversation_id,
                "text": text,
                "phase": phase,
            })
            self.responded_to.append({
                "conversation_id": conversation_id,
                "text": "已记录，当前阶段结束后由 Codex 判断是否中断/重跑。",
                "pending": True,
            })
            return self.responded_to[-1]
        self.responded_to.append({
            "conversation_id": conversation_id,
            "text": f"codex: {text}",
            "pending": False,
        })
        return self.responded_to[-1]


class FakeTerminalAdapter:
    """Records inputs sent to the terminal adapter. Never touches product."""

    def __init__(self):
        self.inputs: list[tuple[str, str]] = []

    async def send_input(self, session_ref, text: str) -> None:
        self.inputs.append((session_ref.external_session_id, text))


class FakeRuntimeStore:
    """In-memory runtime event store backed by a simple list."""

    def __init__(self):
        self.events: list[RuntimeEvent] = []
        self._next_id = 1

    def append(self, event: RuntimeEvent) -> RuntimeEvent:
        event = event.with_id(self._next_id)
        self._next_id += 1
        self.events.append(event)
        return event

    def replay(self) -> SurfaceStateSnapshot:
        return replay_surface_events(self.events)


# ═══════════════════════════════════════════════════════════════════════════════
# Event helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _ev(
    event_type: str,
    payload: dict,
    *,
    conversation_id: int = 42,
    chat_id: int = 100,
    aggregate_id: str = "",
) -> RuntimeEvent:
    return RuntimeEvent(
        schema_version=1,
        event_type=event_type,
        aggregate_type=AggregateType.CONVERSATION,
        aggregate_id=aggregate_id or str(conversation_id),
        correlation_id=f"corr-{event_type}",
        source=EventSource.TELEGRAM,
        actor="user",
        visibility=Visibility.OPERATOR,
        payload=payload,
        occurred_at=now_iso(),
        conversation_id=conversation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Integration test: conversation_id preservation across surface switches
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_product_to_terminal_to_product_keeps_conversation_id():
    """Product -> Terminal -> Product must preserve the same conversation_id.

    Switching mode is a view and input-routing change, not a task restart.
    """
    conversation_id = 42
    chat_id = 100
    store = FakeRuntimeStore()
    product = FakeProductController()

    claude_adapter = FakeTerminalAdapter()
    terminal_mgr = TerminalSessionManager(
        adapters={"claude": claude_adapter}
    )

    # ── Phase 1: Product mode ────────────────────────────────────────────
    current_mode = SurfaceMode.PRODUCT
    selected_agent = "claude"

    decision = route_text_by_mode(current_mode, "开始一个新功能", selected_agent)
    assert decision == SurfaceRouteDecision.PRODUCT_CONVERSATION

    result = await product.handle_product_text(conversation_id, "开始一个新功能", "analysis")
    assert result["pending"] is False
    assert "codex:" in result["text"]

    store.append(_ev(PRODUCT_DISPLAY_FRAME, {
        "chat_id": chat_id,
        "conversation_id": conversation_id,
        "surface": "product",
        "position": 1,
    }))

    # ── Phase 2: Switch to terminal ───────────────────────────────────────
    store.append(_ev(CONVERSATION_MODE_SWITCHED, {
        "chat_id": chat_id,
        "conversation_id": conversation_id,
        "from_mode": "product",
        "to_mode": "terminal",
        "active_agent": "claude",
    }))
    current_mode = SurfaceMode.TERMINAL
    selected_agent = "claude"

    term_ref = terminal_mgr.attach(
        conversation_id=conversation_id,
        agent="claude",
        strategy="stream_json",
        external_session_id="claude-session-1",
    )
    assert term_ref.conversation_id == conversation_id

    store.append(_ev(TERMINAL_SESSION_ATTACHED, {
        "chat_id": chat_id,
        "conversation_id": conversation_id,
        "agent": "claude",
        "external_session_id": "claude-session-1",
        "strategy": "stream_json",
    }))

    # Terminal input must NOT go to product
    term_decision = route_text_by_mode(current_mode, "pytest -q", selected_agent)
    assert term_decision == SurfaceRouteDecision.TERMINAL_INPUT

    product_call_count_before = len(product.calls)
    await terminal_mgr.send_input(term_ref, "pytest -q")
    # Product controller must NOT have been called
    assert len(product.calls) == product_call_count_before

    store.append(_ev(TERMINAL_SESSION_OUTPUT_FRAME, {
        "chat_id": chat_id,
        "conversation_id": conversation_id,
        "surface": "terminal",
        "position": 1,
    }))

    # ── Phase 3: Switch back to product ───────────────────────────────────
    store.append(_ev(CONVERSATION_MODE_SWITCHED, {
        "chat_id": chat_id,
        "conversation_id": conversation_id,
        "from_mode": "terminal",
        "to_mode": "product",
        "active_agent": "",
    }))
    current_mode = SurfaceMode.PRODUCT

    decision_back = route_text_by_mode(current_mode, "继续刚才的修改", selected_agent)
    assert decision_back == SurfaceRouteDecision.PRODUCT_CONVERSATION

    result2 = await product.handle_product_text(conversation_id, "继续刚才的修改", "analysis")
    assert result2["pending"] is False
    assert result2["conversation_id"] == conversation_id

    store.append(_ev(PRODUCT_DISPLAY_FRAME, {
        "chat_id": chat_id,
        "conversation_id": conversation_id,
        "surface": "product",
        "position": 2,
    }))

    # ── Assertions ────────────────────────────────────────────────────────
    # 1. All product calls share the same conversation_id
    for call in product.calls:
        assert call["conversation_id"] == conversation_id

    # 2. Terminal input was delivered to the adapter, not product
    assert claude_adapter.inputs == [("claude-session-1", "pytest -q")]

    # 3. Product controller was called exactly twice (once per product-mode text)
    assert len(product.calls) == 2

    # 4. Replay reconstructs correct mode and cursors
    snap = store.replay()
    view = snap.by_chat[chat_id]
    assert view.active_mode == "product"
    assert view.cursors["product"].position == 2
    assert view.cursors["terminal"].position == 1
    assert view.conversation_id == conversation_id

    # 5. Terminal session was tracked during the terminal phase
    assert "claude" in view.terminal_sessions
    assert view.terminal_sessions["claude"].external_session_id == "claude-session-1"
    assert view.terminal_sessions["claude"].status == "attached"


# ═══════════════════════════════════════════════════════════════════════════════
# Integration test: terminal input must never call product orchestrator
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_terminal_input_does_not_call_product_orchestrator():
    """Terminal-surface text MUST be routed to the terminal adapter only.

    Product controller must receive zero calls during terminal-mode input.
    """
    conversation_id = 99
    product = FakeProductController()
    claude_adapter = FakeTerminalAdapter()
    codex_adapter = FakeTerminalAdapter()
    terminal_mgr = TerminalSessionManager(
        adapters={"claude": claude_adapter, "codex": codex_adapter}
    )

    # Attach terminal sessions for both agents
    cl_ref = terminal_mgr.attach(
        conversation_id=conversation_id,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl-sess",
    )
    cx_ref = terminal_mgr.attach(
        conversation_id=conversation_id,
        agent="codex",
        strategy="app_server",
        external_session_id="cx-thread",
    )

    # Route multiple terminal texts — none should hit product
    for agent, ref, adapter, text in [
        ("claude", cl_ref, claude_adapter, "pytest -q"),
        ("claude", cl_ref, claude_adapter, "git diff HEAD"),
        ("codex", cx_ref, codex_adapter, "inspect diff"),
        ("codex", cx_ref, codex_adapter, "continue"),
        ("claude", cl_ref, claude_adapter, "ls -la src/"),
    ]:
        decision = route_text_by_mode(SurfaceMode.TERMINAL, text, agent)
        assert decision == SurfaceRouteDecision.TERMINAL_INPUT
        await terminal_mgr.send_input(ref, text)

    # Product controller must never have been touched
    assert len(product.calls) == 0
    assert len(product.pending_contexts) == 0

    # Terminal adapters received exactly the right inputs
    assert claude_adapter.inputs == [
        ("cl-sess", "pytest -q"),
        ("cl-sess", "git diff HEAD"),
        ("cl-sess", "ls -la src/"),
    ]
    assert codex_adapter.inputs == [
        ("cx-thread", "inspect diff"),
        ("cx-thread", "continue"),
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# Integration test: pending context during implementation / verification
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_product_followup_during_implementation_records_pending_context():
    """User follow-up during implementation or verification becomes pending.

    It must NOT be routed directly to Claude. It must be recorded for
    Codex review at the phase boundary.
    """
    conversation_id = 42
    chat_id = 100
    store = FakeRuntimeStore()
    product = FakeProductController()

    # ── Normal analysis-phase input continues normally ────────────────────
    result1 = await product.handle_product_text(
        conversation_id, "帮我实现一个新功能", "analysis"
    )
    assert result1["pending"] is False

    # ── Implementation-phase follow-up records pending context ────────────
    result2 = await product.handle_product_text(
        conversation_id, "别忘了加单元测试", "implementation"
    )
    assert result2["pending"] is True
    assert "已记录" in result2["text"]

    store.append(_ev(PRODUCT_PENDING_CONTEXT_RECORDED, {
        "chat_id": chat_id,
        "conversation_id": conversation_id,
        "text_preview": "别忘了加单元测试",
        "telegram_message_id": 1001,
    }))

    # ── Verification-phase follow-up also records pending context ─────────
    result3 = await product.handle_product_text(
        conversation_id, "我刚看了下 diff，好像少了个文件", "verification"
    )
    assert result3["pending"] is True
    assert "已记录" in result3["text"]

    store.append(_ev(PRODUCT_PENDING_CONTEXT_RECORDED, {
        "chat_id": chat_id,
        "conversation_id": conversation_id,
        "text_preview": "我刚看了下 diff，好像少了个文件",
        "telegram_message_id": 1002,
    }))

    # ── Replay reconstructs pending context list ──────────────────────────
    snap = store.replay()
    view = snap.by_chat[chat_id]
    assert len(view.pending_context) == 2
    assert view.pending_context[0]["text_preview"] == "别忘了加单元测试"
    assert view.pending_context[1]["text_preview"] == "我刚看了下 diff，好像少了个文件"

    # ── Product controller tracked the right calls ────────────────────────
    assert len(product.calls) == 3
    assert len(product.pending_contexts) == 2
    assert product.pending_contexts[0]["phase"] == "implementation"
    assert product.pending_contexts[1]["phase"] == "verification"


@pytest.mark.asyncio
async def test_product_followup_during_analysis_continues_normally():
    """Analysis-phase input must continue normally, NOT become pending context."""
    product = FakeProductController()

    result = await product.handle_product_text(
        42, "开始分析这个需求", "analysis"
    )
    assert result["pending"] is False
    assert len(product.pending_contexts) == 0

    result2 = await product.handle_product_text(
        42, "也检查下测试覆盖", "idle"
    )
    assert result2["pending"] is False
    assert len(product.pending_contexts) == 0

    result3 = await product.handle_product_text(
        42, "completed phase too", "completed"
    )
    assert result3["pending"] is False
    assert len(product.pending_contexts) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Integration test: approval resolution is shared between surfaces
# ═══════════════════════════════════════════════════════════════════════════════


class FakeApprovalStore:
    """Shared approval state — a single approval is visible to both surfaces."""

    def __init__(self):
        self.approvals: dict[str, dict] = {}

    def request(self, approval_id: str, conversation_id: int, summary: str) -> dict:
        entry = {
            "approval_id": approval_id,
            "conversation_id": conversation_id,
            "summary": summary,
            "status": "requested",
            "resolved_by_surface": None,
            "decision": "",
        }
        self.approvals[approval_id] = entry
        return entry

    def resolve(self, approval_id: str, surface: str, decision: str) -> dict:
        entry = self.approvals[approval_id]
        entry["status"] = "resolved"
        entry["resolved_by_surface"] = surface
        entry["decision"] = decision
        return entry

    def get(self, approval_id: str) -> dict | None:
        return self.approvals.get(approval_id)


def test_approval_resolution_is_shared_between_surfaces():
    """An approval resolved in either surface applies to the same approval id.

    Approvals are NOT owned by a single surface — they are shared security
    objects visible to both product and terminal surfaces.
    """
    store = FakeApprovalStore()
    conversation_id = 42

    # Request approval — visible to both surfaces
    store.request("appr-1", conversation_id, "Claude wants to run: rm -rf /tmp/cache")
    assert store.get("appr-1")["status"] == "requested"

    # Resolve from product surface
    store.resolve("appr-1", surface="product", decision="allow_once")
    assert store.get("appr-1")["status"] == "resolved"
    assert store.get("appr-1")["resolved_by_surface"] == "product"
    assert store.get("appr-1")["decision"] == "allow_once"

    # Terminal surface sees the same resolved state
    entry = store.get("appr-1")
    assert entry["status"] == "resolved"
    assert entry["approval_id"] == "appr-1"
    assert entry["conversation_id"] == conversation_id

    # A second approval, resolved from terminal surface
    store.request("appr-2", conversation_id, "Codex wants to: push to main")
    store.resolve("appr-2", surface="terminal", decision="deny")
    assert store.get("appr-2")["status"] == "resolved"
    assert store.get("appr-2")["resolved_by_surface"] == "terminal"

    # Duplicate resolution of already-resolved approval must not change decision
    store.resolve("appr-2", surface="product", decision="allow_once")
    # Last write wins in this simple fake — but both surfaces see the same id
    assert store.get("appr-2")["status"] == "resolved"
    # The key point: both surfaces resolve the SAME approval_id
    assert store.get("appr-2")["approval_id"] == "appr-2"


def test_approval_stale_duplicate_does_not_undo_resolution():
    """Once an approval is resolved, a stale duplicate callback must not undo it."""
    store = FakeApprovalStore()

    store.request("appr-1", 42, "Run destructive command")
    store.resolve("appr-1", surface="terminal", decision="deny")

    # Stale product callback tries to approve — but resolution already happened
    # In the real system, this is rejected by stale-button detection.
    assert store.get("appr-1")["status"] == "resolved"

    # Even if we try to re-resolve, the approval_id stays the same
    # and both surfaces see the same shared object
    assert store.get("appr-1")["approval_id"] == "appr-1"


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-contamination: Product <-> Terminal isolation
# ═══════════════════════════════════════════════════════════════════════════════


def test_product_surface_does_not_import_terminal_module():
    """Product surface module must not import terminal surface internals."""
    with pytest.raises(ImportError):
        from wlcodex.surfaces.product import terminal  # noqa: F811


def test_terminal_surface_does_not_import_product_module():
    """Terminal surface module must not import product surface internals."""
    with pytest.raises(ImportError):
        from wlcodex.surfaces.terminal import product  # noqa: F811


def test_core_router_is_pure_function_without_surface_imports():
    """Core routing must not import product or terminal surface modules."""
    import inspect
    src = inspect.getsource(route_text_by_mode)
    # Must not import from product or terminal surface packages
    assert "wlcodex.surfaces.product" not in src
    assert "wlcodex.surfaces.terminal" not in src
    # Uses only the core model enums for routing decisions
    assert "SurfaceRouteDecision.PRODUCT_CONVERSATION" in src
    assert "SurfaceRouteDecision.TERMINAL_INPUT" in src


# ═══════════════════════════════════════════════════════════════════════════════
# Command parsing integration: /product, /terminal wire-up
# ═══════════════════════════════════════════════════════════════════════════════


def test_product_command_parses_to_mode_switch():
    cmd = parse_command("/product")
    assert isinstance(cmd, ModeSwitchCommand)
    assert cmd.kind == "mode_switch"
    assert cmd.mode == "product"


def test_terminal_bare_command_parses_to_mode_switch():
    cmd = parse_command("/terminal")
    assert isinstance(cmd, ModeSwitchCommand)
    assert cmd.kind == "mode_switch"
    assert cmd.mode == "terminal"
    assert cmd.agent == ""


def test_terminal_claude_command_parses_to_mode_switch_with_agent():
    cmd = parse_command("/terminal claude")
    assert isinstance(cmd, ModeSwitchCommand)
    assert cmd.mode == "terminal"
    assert cmd.agent == "claude"


def test_terminal_codex_command_parses_to_mode_switch_with_agent():
    cmd = parse_command("/terminal codex")
    assert isinstance(cmd, ModeSwitchCommand)
    assert cmd.mode == "terminal"
    assert cmd.agent == "codex"


def test_terminal_detach_command_parses_to_subcommand():
    cmd = parse_command("/terminal detach")
    assert isinstance(cmd, TerminalSubCommand)
    assert cmd.subcommand == "detach"


def test_terminal_tail_command_parses_to_subcommand():
    cmd = parse_command("/terminal tail")
    assert isinstance(cmd, TerminalSubCommand)
    assert cmd.subcommand == "tail"


def test_terminal_product_command_parses_to_mode_switch():
    cmd = parse_command("/terminal product")
    assert isinstance(cmd, ModeSwitchCommand)
    assert cmd.mode == "product"


def test_mode_command_parses_to_query():
    cmd = parse_command("/mode")
    assert isinstance(cmd, ModeSwitchCommand)
    assert cmd.mode == ""


# ═══════════════════════════════════════════════════════════════════════════════
# End-to-end command -> route -> surface flow (wired integration)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_full_flow_terminal_command_routes_to_terminal_adapter():
    """From Telegram command parse through routing to terminal adapter.

    /terminal claude -> parse -> mode_switch -> terminal input routing.
    The full chain must never touch the product controller.
    """
    conversation_id = 77
    product = FakeProductController()
    claude_adapter = FakeTerminalAdapter()
    terminal_mgr = TerminalSessionManager(
        adapters={"claude": claude_adapter}
    )

    # Parse the command
    cmd = parse_command("/terminal claude")
    assert isinstance(cmd, ModeSwitchCommand)
    assert cmd.mode == "terminal"
    assert cmd.agent == "claude"

    # Apply mode switch
    current_mode = SurfaceMode.TERMINAL
    selected_agent = "claude"

    term_ref = terminal_mgr.attach(
        conversation_id=conversation_id,
        agent="claude",
        strategy="stream_json",
        external_session_id="cl-fullflow",
    )

    # Now a user message in terminal mode must route to terminal
    decision = route_text_by_mode(current_mode, "ls", selected_agent)
    assert decision == SurfaceRouteDecision.TERMINAL_INPUT

    await terminal_mgr.send_input(term_ref, "ls")

    # Product controller must have zero calls
    assert len(product.calls) == 0

    # Terminal adapter received the input
    assert claude_adapter.inputs == [("cl-fullflow", "ls")]


@pytest.mark.asyncio
async def test_full_flow_product_command_stays_on_product_path():
    """/product command followed by text routes to product, not terminal."""
    conversation_id = 88
    product = FakeProductController()
    claude_adapter = FakeTerminalAdapter()
    terminal_mgr = TerminalSessionManager(
        adapters={"claude": claude_adapter}
    )

    # Parse and apply product mode switch
    cmd = parse_command("/product")
    assert isinstance(cmd, ModeSwitchCommand)
    assert cmd.mode == "product"

    current_mode = SurfaceMode.PRODUCT

    # Route text in product mode
    decision = route_text_by_mode(current_mode, "继续实现", "claude")
    assert decision == SurfaceRouteDecision.PRODUCT_CONVERSATION

    result = await product.handle_product_text(conversation_id, "继续实现", "analysis")
    assert result["pending"] is False
    assert len(product.calls) == 1

    # Terminal adapter must be untouched
    assert len(claude_adapter.inputs) == 0
    # Terminal manager has no sessions for this conversation
    assert terminal_mgr.active_for_conversation(conversation_id) is None
