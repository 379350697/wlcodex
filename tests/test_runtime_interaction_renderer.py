"""Tests for Lane E: Telegram Runtime Renderer."""

from __future__ import annotations

from dataclasses import replace

import pytest

from wlcodex.interaction.events import InteractionEvent
from wlcodex.interaction.renderer import InteractionRenderer
from wlcodex.interaction.runtime_renderer import (
    RuntimeProgressManager,
    RuntimeRenderer,
    RuntimeRunState,
)
from wlcodex.interaction.transport import TelegramTransport

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _state(**kw) -> RuntimeRunState:
    defaults: dict = {
        "phase": "running_implementation",
        "active_agent": "claude",
        "agent_status": "running",
    }
    defaults.update(kw)
    return RuntimeRunState(**defaults)


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.edited: list[tuple[int, int, str, object]] = []

    async def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text, buttons))
        return len(self.sent)

    async def edit(self, chat_id, message_id, text, buttons=None):
        self.edited.append((chat_id, message_id, text, buttons))

    async def typing(self, chat_id):
        return None


def _transport(fake: FakeTransport) -> TelegramTransport:
    return TelegramTransport(fake.send, fake.edit, fake.typing)


# ===================================================================
# RuntimeRenderer — pure text generation
# ===================================================================


class TestRuntimeRendererPure:
    """Tests for RuntimeRenderer — no I/O, text-only."""

    # -- verbosity 0 ----------------------------------------------------

    def test_v0_progress_is_empty(self) -> None:
        r = RuntimeRenderer(verbosity=0)
        state = _state(phase="running_implementation", active_agent="claude")
        assert r.progress_text(state) == ""

    def test_v0_heartbeat_is_empty(self) -> None:
        r = RuntimeRenderer(verbosity=0)
        assert r.heartbeat_text(_state()) == ""

    def test_v0_final_shows_result_without_detail(self) -> None:
        r = RuntimeRenderer(verbosity=0)
        state = _state(
            phase="completed",
            agent_status="completed",
            total_tokens=5000,
            retry_count=3,
            is_terminal=True,
        )
        text = r.final_text(state)
        assert "任务完成" in text
        assert "5000" not in text  # no token detail at v0

    def test_v0_final_failed(self) -> None:
        r = RuntimeRenderer(verbosity=0)
        state = _state(
            phase="failed",
            error_summary="Claude 超时",
            is_terminal=True,
        )
        text = r.final_text(state)
        assert "任务失败" in text
        assert "Claude 超时" in text

    # -- verbosity 1 ----------------------------------------------------

    def test_v1_phase_analysis(self) -> None:
        r = RuntimeRenderer(verbosity=1)
        state = _state(phase="running_analysis", active_agent="codex")
        assert "拆解需求" in r.progress_text(state)

    def test_v1_phase_implementation(self) -> None:
        r = RuntimeRenderer(verbosity=1)
        state = _state(phase="running_implementation", active_agent="claude")
        assert "开始实施" in r.progress_text(state)

    def test_v1_phase_verification(self) -> None:
        r = RuntimeRenderer(verbosity=1)
        state = _state(phase="running_verification", active_agent="codex")
        assert "验收" in r.progress_text(state)

    def test_v1_phase_retrying(self) -> None:
        r = RuntimeRenderer(verbosity=1)
        state = _state(phase="retrying_implementation", active_agent="claude")
        assert "重新实施" in r.progress_text(state)

    def test_v1_heartbeat_has_activity_time(self) -> None:
        r = RuntimeRenderer(verbosity=1)
        state = _state(last_activity_at="2026-05-18T10:00:00+00:00")
        text = r.heartbeat_text(state)
        assert "还在执行" in text
        assert "最近活动" in text

    def test_v1_final_shows_tokens(self) -> None:
        r = RuntimeRenderer(verbosity=1)
        state = _state(
            phase="completed",
            total_tokens=12000,
            is_terminal=True,
        )
        text = r.final_text(state)
        assert "任务完成" in text
        assert "12000" in text

    def test_v1_progress_empty_for_terminal(self) -> None:
        r = RuntimeRenderer(verbosity=1)
        state = _state(phase="completed", is_terminal=True)
        assert r.progress_text(state) == ""

    def test_v1_waiting_for_approval(self) -> None:
        r = RuntimeRenderer(verbosity=1)
        state = _state(
            phase="running_implementation",
            active_agent="claude",
            agent_status="waiting_for_approval",
        )
        text = r.progress_text(state)
        assert "等待审批" in text

    # -- verbosity 2 ----------------------------------------------------

    def test_v2_shows_file_names(self) -> None:
        r = RuntimeRenderer(verbosity=2)
        state = _state(changed_files=["src/main.py", "tests/test_main.py"])
        text = r.progress_text(state)
        assert "src/main.py" in text
        assert "修改文件" in text

    def test_v2_shows_command_names(self) -> None:
        r = RuntimeRenderer(verbosity=2)
        state = _state(commands=["pytest -q", "ruff check ."])
        text = r.progress_text(state)
        assert "pytest -q" in text
        assert "运行命令" in text

    def test_v2_shows_tool_names(self) -> None:
        r = RuntimeRenderer(verbosity=2)
        state = _state(tool_names=["Read", "Edit", "Bash"])
        text = r.progress_text(state)
        assert "Read" in text
        assert "使用工具" in text

    def test_v2_shows_retries(self) -> None:
        r = RuntimeRenderer(verbosity=2)
        state = _state(retry_count=4)
        text = r.progress_text(state)
        assert "重试" in text
        assert "4" in text

    def test_v2_shows_tokens(self) -> None:
        r = RuntimeRenderer(verbosity=2)
        state = _state(total_tokens=3500)
        text = r.progress_text(state)
        assert "3500" in text

    def test_v2_final_shows_retries(self) -> None:
        r = RuntimeRenderer(verbosity=2)
        state = _state(
            phase="completed",
            total_tokens=5000,
            retry_count=2,
            is_terminal=True,
        )
        text = r.final_text(state)
        assert "重试 2 次" in text

    def test_v2_detail_truncated_to_3_per_category(self) -> None:
        r = RuntimeRenderer(verbosity=2)
        state = _state(
            changed_files=["a.py", "b.py", "c.py", "d.py", "e.py"],
        )
        text = r.progress_text(state)
        # Only 3 most-recent files shown ([-3:] slice)
        assert "c.py" in text
        assert "d.py" in text
        assert "e.py" in text
        assert "a.py" not in text
        assert "b.py" not in text

    # -- approval -------------------------------------------------------

    def test_approval_card_preserves_semantics(self) -> None:
        r = RuntimeRenderer(verbosity=0)
        text = r.approval_text("command", "rm -rf /tmp/cache")
        assert "审批" in text
        assert "命令" in text
        assert "rm -rf /tmp/cache" in text

    def test_approval_card_file_change(self) -> None:
        r = RuntimeRenderer(verbosity=0)
        text = r.approval_text("file_change", "修改 src/main.py")
        assert "文件修改" in text

    # -- no raw chatter -------------------------------------------------

    def test_progress_never_contains_raw_english_chatter(self) -> None:
        """Raw Claude/Codex model text must never appear in progress."""
        r = RuntimeRenderer(verbosity=1)
        for phase in [
            "running_analysis", "running_implementation",
            "running_verification", "retrying_implementation",
        ]:
            state = _state(phase=phase)
            text = r.progress_text(state)
            # All output must be Chinese
            assert "analyzing" not in text.lower()
            assert "implementing" not in text.lower()
            assert "verifying" not in text.lower()
            assert "thinking" not in text.lower()

    def test_heartbeat_never_echoes_raw_text(self) -> None:
        r = RuntimeRenderer(verbosity=1)
        state = _state()
        text = r.heartbeat_text(state)
        assert "thinking" not in text.lower()
        assert "streaming" not in text.lower()

    # -- unknown phase --------------------------------------------------

    def test_unknown_phase_renders_as_is(self) -> None:
        r = RuntimeRenderer(verbosity=1)
        state = _state(phase="custom_phase")
        assert r.progress_text(state) == "custom_phase"


# ===================================================================
# RuntimeProgressManager — throttled Telegram editing
# ===================================================================


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def time(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class TestRuntimeProgressManager:
    """Tests for RuntimeProgressManager — I/O with throttling."""

    @pytest.mark.asyncio
    async def test_first_progress_sends_new_message(self) -> None:
        fake = FakeTransport()
        mgr = RuntimeProgressManager(_transport(fake), verbosity=1)
        state = _state(phase="running_implementation")

        await mgr.update_progress(state, chat_id=123)

        assert len(fake.sent) == 1
        assert "开始实施" in fake.sent[0][1]
        assert fake.edited == []

    @pytest.mark.asyncio
    async def test_second_identical_progress_is_skipped(self) -> None:
        fake = FakeTransport()
        clock = FakeClock()
        mgr = RuntimeProgressManager(
            _transport(fake), verbosity=1, min_edit_interval=0.0, clock=clock,
        )
        state = _state(phase="running_implementation")

        await mgr.update_progress(state, chat_id=123)
        assert len(fake.sent) == 1

        # Same state — no change
        await mgr.update_progress(state, chat_id=123)
        assert len(fake.sent) == 1  # no new send
        assert fake.edited == []     # no empty edit

    @pytest.mark.asyncio
    async def test_different_state_edits_existing_message(self) -> None:
        fake = FakeTransport()
        clock = FakeClock()
        mgr = RuntimeProgressManager(
            _transport(fake), verbosity=1, min_edit_interval=0.0, clock=clock,
        )

        await mgr.update_progress(
            _state(phase="running_implementation"), chat_id=123,
        )
        assert len(fake.sent) == 1

        clock.advance(5.0)
        await mgr.update_progress(
            _state(phase="running_verification", active_agent="codex"), chat_id=123,
        )

        assert len(fake.edited) == 1
        assert "验收" in fake.edited[0][2]

    @pytest.mark.asyncio
    async def test_throttling_respected(self) -> None:
        fake = FakeTransport()
        clock = FakeClock()
        mgr = RuntimeProgressManager(
            _transport(fake), verbosity=1, min_edit_interval=5.0, clock=clock,
        )

        await mgr.update_progress(
            _state(phase="running_implementation"), chat_id=123,
        )
        assert len(fake.sent) == 1

        # Immediately change state — should be throttled
        await mgr.update_progress(
            _state(phase="running_verification", active_agent="codex"), chat_id=123,
        )
        assert len(fake.edited) == 0  # throttled

        # After interval — should go through
        clock.advance(6.0)
        await mgr.update_progress(
            _state(phase="running_verification", active_agent="codex"), chat_id=123,
        )
        assert len(fake.edited) == 1

    @pytest.mark.asyncio
    async def test_v0_sends_no_progress(self) -> None:
        fake = FakeTransport()
        mgr = RuntimeProgressManager(_transport(fake), verbosity=0)

        await mgr.update_progress(
            _state(phase="running_implementation"), chat_id=123,
        )
        assert fake.sent == []
        assert fake.edited == []

    @pytest.mark.asyncio
    async def test_v0_approval_still_sent(self) -> None:
        fake = FakeTransport()
        mgr = RuntimeProgressManager(_transport(fake), verbosity=0)

        await mgr.show_approval("command", "rm -rf /tmp", chat_id=123)
        assert len(fake.sent) == 1
        assert "审批" in fake.sent[0][1]

    @pytest.mark.asyncio
    async def test_finish_edits_progress_to_final(self) -> None:
        fake = FakeTransport()
        mgr = RuntimeProgressManager(
            _transport(fake), verbosity=1, min_edit_interval=0.0,
        )

        await mgr.update_progress(
            _state(phase="running_implementation"), chat_id=123,
        )
        await mgr.finish(
            _state(phase="completed", total_tokens=5000, is_terminal=True),
            chat_id=123,
        )

        assert len(fake.edited) == 1
        assert "任务完成" in fake.edited[0][2]
        assert "5000" in fake.edited[0][2]

    @pytest.mark.asyncio
    async def test_finish_sends_if_no_progress_yet(self) -> None:
        fake = FakeTransport()
        mgr = RuntimeProgressManager(
            _transport(fake), verbosity=1, min_edit_interval=0.0,
        )

        await mgr.finish(
            _state(phase="completed", is_terminal=True), chat_id=123,
        )

        assert len(fake.sent) == 1
        assert "任务完成" in fake.sent[0][1]

    @pytest.mark.asyncio
    async def test_finish_not_called_twice(self) -> None:
        fake = FakeTransport()
        mgr = RuntimeProgressManager(
            _transport(fake), verbosity=1, min_edit_interval=0.0,
        )

        await mgr.update_progress(
            _state(phase="running_implementation"), chat_id=123,
        )
        await mgr.finish(
            _state(phase="completed", is_terminal=True), chat_id=123,
        )
        edit_count_before = len(fake.edited)
        sent_count_before = len(fake.sent)

        # Second finish should be a no-op
        await mgr.finish(
            _state(phase="completed", is_terminal=True), chat_id=123,
        )
        assert len(fake.edited) == edit_count_before
        assert len(fake.sent) == sent_count_before

    @pytest.mark.asyncio
    async def test_edit_fallback_to_send_on_failure(self) -> None:
        """If edit fails, fall back to sending a new message."""

        class EditFailsTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[int, str, object]] = []
                self.edits = 0

            async def send(self, chat_id, text, buttons=None):
                self.sent.append((chat_id, text, buttons))
                return len(self.sent)

            async def edit(self, chat_id, message_id, text, buttons=None):
                self.edits += 1
                raise RuntimeError("edit failed")

            async def typing(self, chat_id):
                return None

        fake = EditFailsTransport()
        mgr = RuntimeProgressManager(
            TelegramTransport(fake.send, fake.edit, fake.typing),
            verbosity=1,
            min_edit_interval=0.0,
        )

        # First: send progress (gets message_id=1)
        await mgr.update_progress(
            _state(phase="running_implementation"), chat_id=123,
        )
        assert len(fake.sent) == 1
        sent_before = len(fake.sent)

        # New phase: edit fails, should fall back to send
        await mgr.update_progress(
            _state(phase="running_verification", active_agent="codex"), chat_id=123,
        )
        assert fake.edits >= 1
        assert len(fake.sent) > sent_before  # fallback send

    @pytest.mark.asyncio
    async def test_outbox_placeholder_message_id_is_not_edited(self) -> None:
        """Outbox queued sends return -1, which is not a real editable message id."""

        class OutboxLikeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[int, str, object]] = []
                self.edits = 0

            async def send(self, chat_id, text, buttons=None):
                self.sent.append((chat_id, text, buttons))
                return -1

            async def edit(self, chat_id, message_id, text, buttons=None):
                self.edits += 1
                raise AssertionError("placeholder message id must not be edited")

            async def typing(self, chat_id):
                return None

        fake = OutboxLikeTransport()
        mgr = RuntimeProgressManager(
            TelegramTransport(fake.send, fake.edit, fake.typing),
            verbosity=1,
            min_edit_interval=0.0,
        )

        await mgr.update_progress(
            _state(phase="running_implementation"), chat_id=123,
        )
        await mgr.update_progress(
            _state(phase="running_verification", active_agent="codex"), chat_id=123,
        )

        assert fake.edits == 0
        assert len(fake.sent) == 2


# ===================================================================
# InteractionRenderer with runtime events
# ===================================================================


class TestInteractionRendererRuntimeEvents:
    """Tests for InteractionRenderer handling runtime_progress and runtime_heartbeat."""

    @pytest.mark.asyncio
    async def test_runtime_progress_delegates_to_manager(self) -> None:
        fake = FakeTransport()
        mgr = RuntimeProgressManager(
            _transport(fake), verbosity=1, min_edit_interval=0.0,
        )
        renderer = InteractionRenderer(
            transport=_transport(fake),
            profile=_fake_profile(),
            runtime_progress=mgr,
        )
        state = _state(phase="running_analysis", active_agent="codex")

        await renderer.handle(InteractionEvent(
            event_type="runtime_progress",
            chat_id=123,
            conversation_id=1,
            metadata={"runtime_state": state},
        ))

        assert len(fake.sent) == 1
        assert "拆解需求" in fake.sent[0][1]

    @pytest.mark.asyncio
    async def test_runtime_progress_noop_when_manager_none(self) -> None:
        fake = FakeTransport()
        renderer = InteractionRenderer(
            transport=_transport(fake),
            profile=_fake_profile(),
            runtime_progress=None,
        )

        # Must not crash
        await renderer.handle(InteractionEvent(
            event_type="runtime_progress",
            chat_id=123,
            metadata={"runtime_state": _state()},
        ))
        assert fake.sent == []

    @pytest.mark.asyncio
    async def test_runtime_progress_noop_when_state_missing(self) -> None:
        fake = FakeTransport()
        mgr = RuntimeProgressManager(
            _transport(fake), verbosity=1, min_edit_interval=0.0,
        )
        renderer = InteractionRenderer(
            transport=_transport(fake),
            profile=_fake_profile(),
            runtime_progress=mgr,
        )

        # metadata without runtime_state
        await renderer.handle(InteractionEvent(
            event_type="runtime_progress",
            chat_id=123,
            metadata={},
        ))
        assert fake.sent == []

    @pytest.mark.asyncio
    async def test_runtime_heartbeat_delegates_to_manager(self) -> None:
        fake = FakeTransport()
        mgr = RuntimeProgressManager(
            _transport(fake), verbosity=1, min_edit_interval=0.0,
        )
        renderer = InteractionRenderer(
            transport=_transport(fake),
            profile=_fake_profile(),
            runtime_progress=mgr,
        )
        state = _state(
            phase="running_implementation",
            last_activity_at="2026-05-18T10:00:00+00:00",
        )

        await renderer.handle(InteractionEvent(
            event_type="runtime_heartbeat",
            chat_id=123,
            metadata={"runtime_state": state},
        ))

        assert len(fake.sent) == 1
        assert "开始实施" in fake.sent[0][1]

    @pytest.mark.asyncio
    async def test_existing_events_still_work_with_runtime_manager(self) -> None:
        """run_started, text_delta, run_completed still function correctly."""
        fake = FakeTransport()
        mgr = RuntimeProgressManager(
            _transport(fake), verbosity=1, min_edit_interval=0.0,
        )
        from wlcodex.interaction.profiles import NaturalChatProfile

        renderer = InteractionRenderer(
            transport=_transport(fake),
            profile=NaturalChatProfile(),
            min_interval_seconds=0.0,
            runtime_progress=mgr,
        )

        # Normal text_delta still streams
        await renderer.handle(InteractionEvent(
            event_type="run_started", chat_id=1,
        ))
        await renderer.handle(InteractionEvent(
            event_type="text_delta", chat_id=1, task_id=10, text="hello",
        ))
        assert fake.sent[-1][1] == "hello"  # last send is the text_delta

        # Runtime event doesn't interfere
        await renderer.handle(InteractionEvent(
            event_type="runtime_progress",
            chat_id=1,
            conversation_id=1,
            metadata={"runtime_state": _state(phase="running_analysis")},
        ))
        assert len(fake.sent) >= 2  # progress sent separately

    @pytest.mark.asyncio
    async def test_exception_in_runtime_handler_preserves_typing_cancel(self) -> None:
        """Exception in runtime handler still cancels typing (resilience)."""
        fake = FakeTransport()

        class CrashManager:
            async def update_progress(self, state, *, chat_id, conversation_id=0):
                raise RuntimeError("boom")

        mgr = CrashManager()
        renderer = InteractionRenderer(
            transport=_transport(fake),
            profile=_fake_profile(),
            runtime_progress=mgr,
        )

        with pytest.raises(RuntimeError, match="boom"):
            await renderer.handle(InteractionEvent(
                event_type="runtime_progress",
                chat_id=123,
                metadata={"runtime_state": _state()},
            ))

    # -- completion finalizes runtime progress -------------------------

    @pytest.mark.asyncio
    async def test_run_completed_finishes_runtime_progress(self) -> None:
        """_handle_completed also calls RuntimeProgressManager.finish()."""
        fake = FakeTransport()
        mgr = RuntimeProgressManager(
            _transport(fake), verbosity=1, min_edit_interval=0.0,
        )
        renderer = InteractionRenderer(
            transport=_transport(fake),
            profile=_fake_profile(),
            runtime_progress=mgr,
        )

        # Start a progress message
        await renderer.handle(InteractionEvent(
            event_type="runtime_progress",
            chat_id=123,
            conversation_id=1,
            metadata={"runtime_state": _state(phase="running_implementation")},
        ))
        assert len(fake.sent) == 1

        # run_completed with runtime_state finalizes
        await renderer.handle(InteractionEvent(
            event_type="run_completed",
            chat_id=123,
            conversation_id=1,
            metadata={
                "runtime_state": _state(
                    phase="completed", total_tokens=3000, is_terminal=True,
                ),
            },
        ))

        # Progress message was edited to final
        assert len(fake.edited) == 1
        assert "任务完成" in fake.edited[0][2]

    @pytest.mark.asyncio
    async def test_run_completed_finishes_progress_even_without_stream_session(self) -> None:
        """Runtime progress finalizes on run_completed even with no text_delta session."""
        fake = FakeTransport()
        mgr = RuntimeProgressManager(
            _transport(fake), verbosity=1, min_edit_interval=0.0,
        )
        renderer = InteractionRenderer(
            transport=_transport(fake),
            profile=_fake_profile(),
            runtime_progress=mgr,
        )

        # No text_delta session created — only runtime progress
        await renderer.handle(InteractionEvent(
            event_type="runtime_progress",
            chat_id=123,
            conversation_id=1,
            metadata={"runtime_state": _state(phase="running_implementation")},
        ))

        # run_completed without any prior text_delta session
        await renderer.handle(InteractionEvent(
            event_type="run_completed",
            chat_id=123,
            conversation_id=1,
            metadata={
                "runtime_state": _state(phase="completed", is_terminal=True),
            },
        ))

        # Progress was sent then finished via edit
        assert len(fake.sent) == 1  # initial progress
        assert len(fake.edited) == 1
        assert "任务完成" in fake.edited[0][2]

    @pytest.mark.asyncio
    async def test_run_failed_finishes_runtime_progress(self) -> None:
        """_handle_failed also calls RuntimeProgressManager.finish()."""
        fake = FakeTransport()
        mgr = RuntimeProgressManager(
            _transport(fake), verbosity=1, min_edit_interval=0.0,
        )
        renderer = InteractionRenderer(
            transport=_transport(fake),
            profile=_fake_profile(),
            runtime_progress=mgr,
        )

        await renderer.handle(InteractionEvent(
            event_type="runtime_progress",
            chat_id=123,
            conversation_id=1,
            metadata={"runtime_state": _state(phase="running_implementation")},
        ))

        await renderer.handle(InteractionEvent(
            event_type="run_failed",
            chat_id=123,
            conversation_id=1,
            metadata={
                "runtime_state": _state(
                    phase="failed", error_summary="timeout", is_terminal=True,
                ),
            },
        ))

        assert len(fake.edited) == 1
        assert "任务失败" in fake.edited[0][2]
        assert "timeout" in fake.edited[0][2]

    @pytest.mark.asyncio
    async def test_runtime_final_event_works(self) -> None:
        """Explicit runtime_final event finalizes progress."""
        fake = FakeTransport()
        mgr = RuntimeProgressManager(
            _transport(fake), verbosity=1, min_edit_interval=0.0,
        )
        renderer = InteractionRenderer(
            transport=_transport(fake),
            profile=_fake_profile(),
            runtime_progress=mgr,
        )

        await renderer.handle(InteractionEvent(
            event_type="runtime_progress",
            chat_id=123,
            conversation_id=1,
            metadata={"runtime_state": _state(phase="running_verification", active_agent="codex")},
        ))

        await renderer.handle(InteractionEvent(
            event_type="runtime_final",
            chat_id=123,
            conversation_id=1,
            metadata={
                "runtime_state": _state(phase="completed", total_tokens=999, is_terminal=True),
            },
        ))

        assert len(fake.edited) == 1
        assert "任务完成" in fake.edited[0][2]
        assert "999" in fake.edited[0][2]

    @pytest.mark.asyncio
    async def test_runtime_final_passes_buttons(self) -> None:
        """runtime_final passes completion buttons through."""
        fake = FakeTransport()
        mgr = RuntimeProgressManager(
            _transport(fake), verbosity=1, min_edit_interval=0.0,
        )
        renderer = InteractionRenderer(
            transport=_transport(fake),
            profile=_fake_profile(),
            runtime_progress=mgr,
        )

        # No prior progress — finish sends new message with buttons
        await renderer.handle(InteractionEvent(
            event_type="runtime_final",
            chat_id=123,
            conversation_id=1,
            buttons=[[{"text": "查看 diff", "callback_data": "diff"}]],
            metadata={
                "runtime_state": _state(phase="completed", is_terminal=True),
            },
        ))

        assert len(fake.sent) == 1
        assert fake.sent[0][2] is not None
        assert fake.sent[0][2][0][0]["text"] == "查看 diff"

    @pytest.mark.asyncio
    async def test_runtime_final_noop_when_manager_none(self) -> None:
        """runtime_final is a no-op when no RuntimeProgressManager."""
        fake = FakeTransport()
        renderer = InteractionRenderer(
            transport=_transport(fake),
            profile=_fake_profile(),
            runtime_progress=None,
        )

        # Must not crash
        await renderer.handle(InteractionEvent(
            event_type="runtime_final",
            chat_id=123,
            metadata={"runtime_state": _state()},
        ))
        assert fake.sent == []


# ===================================================================
# RuntimeRunState — projected state contract
# ===================================================================


class TestRuntimeRunState:
    def test_defaults_are_empty(self) -> None:
        state = RuntimeRunState()
        assert state.phase == ""
        assert state.active_agent == ""
        assert state.tool_names == []
        assert state.changed_files == []
        assert state.commands == []
        assert state.retry_count == 0
        assert state.total_tokens == 0

    def test_immutable_lists_are_instance_independent(self) -> None:
        """Default lists must not be shared across instances."""
        a = RuntimeRunState()
        b = RuntimeRunState()
        a.tool_names.append("Bash")
        assert b.tool_names == []

    def test_replace_via_dataclass(self) -> None:
        a = _state(phase="running_analysis")
        b = replace(a, phase="running_implementation", active_agent="claude")
        assert b.phase == "running_implementation"
        assert b.active_agent == "claude"
        assert a.phase == "running_analysis"  # original unchanged


# ===================================================================
# Helpers
# ===================================================================


def _fake_profile():
    from wlcodex.interaction.profiles import InteractionProfile
    return InteractionProfile(name="test")
