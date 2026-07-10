from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from wlcodex.live_stream.native_templates.timeline_v2 import (
    render_timeline_v2_template,
)
from wlcodex.live_stream.server import (
    _marvis_relay_attachment_script,
    _marvis_relay_attachment_sheet_html,
    _live_page,
    _native_codex_page,
    _relay_blocked_inbox_page,
    _relay_chat_home_page,
    _relay_config_page,
    _relay_task_detail_page,
)
from wlcodex.relay.models import (
    RelayBoard,
    RelayPresentation,
    RelayRoleJob,
    RelayTask,
    RelayTaskDetail,
    RelayTaskSummary,
)


def _presentation(state: str, *, next_action: str = "查看任务状态。") -> RelayPresentation:
    return RelayPresentation(
        state=state,
        freshness={
            "source": "relay_lifecycle",
            "updated_at": "2026-07-10T00:00:00+00:00",
            "is_stale": state == "stale",
            "reason": "超过 30 分钟未收到新的 Relay 状态" if state == "stale" else "",
        },
        current_actor={"role": "director", "label": "总工程师", "status": "queued"},
        blocking_reason="需要用户确认。" if state.startswith("waiting_") else "",
        next_action=next_action,
        allowed_actions=["refresh"] if state == "stale" else ["add_input"],
    )


def _summary(task_id: int, state: str) -> RelayTaskSummary:
    return RelayTaskSummary(
        task_id=task_id,
        title=f"{state} task",
        workspace="/workspace",
        status=state,
        phase="execution",
        provider="codex",
        director_decision_summary="",
        latest_handoff_summary="",
        role_statuses={"director": "queued"},
        role_providers={"director": "codex"},
        last_activity_at="2026-07-10T00:00:00+00:00",
        presentation=_presentation(state, next_action=f"处理 {state}。"),
    )


def _detail() -> RelayTaskDetail:
    task = RelayTask(
        id=42,
        title="UX contract",
        prompt="Keep controls semantically distinct.",
        workspace="/workspace",
        provider="codex",
        status="running",
        phase="execution",
        created_at="2026-07-10T00:00:00+00:00",
        updated_at="2026-07-10T00:00:00+00:00",
    )
    return RelayTaskDetail(
        task=task,
        board=RelayBoard(
            task_id=task.id,
            current_goal=task.prompt,
            phase=task.phase,
            current_dispatch="director",
            next_step="等待总工程师处理。",
        ),
        role_jobs=[
            RelayRoleJob(
                id=1,
                task_id=task.id,
                role="director",
                status="streaming",
                provider="codex",
            )
        ],
        artifacts=[],
        latest_handoff=None,
        session_links=[],
        presentation=_presentation("running"),
    )


def _waiting_detail() -> RelayTaskDetail:
    detail = _detail()
    return replace(
        detail,
        task=replace(detail.task, status="waiting_user"),
        artifacts=[
            {
                "id": 99,
                "round_id": 1,
                "artifact_type": "architecture_plan",
                "status": "waiting",
                "summary": "先确认实施计划。",
                "open_questions": ["是否开始实施？"],
                "confirmation_source": "relay_prompt_fallback",
            }
        ],
    )


def _post_count(page: str) -> int:
    return len(re.findall(r'method:\s*"POST"', page))


def _idempotency_header_count(page: str) -> int:
    return page.count('"Idempotency-Key"')


def test_relay_mutation_surfaces_keep_idempotency_keys_and_interrupt_isolated() -> None:
    """A send, attachment action, or form submit must never share interrupt.

    This renders all current Relay mutation surfaces, rather than inspecting a
    source fragment, so f-string/template escaping cannot hide a missing UI
    header from the product page.
    """

    detail_page = _relay_task_detail_page(_detail(), access_token="token")
    pages = [
        _relay_chat_home_page(selected_workspace="/workspace", access_token="token"),
        _relay_config_page(
            providers=[{"provider": "codex", "provider_engine": "test"}],
            selected_workspace="/workspace",
            access_token="token",
        ),
        _relay_blocked_inbox_page(
            [_summary(1, "blocked")],
            selected_workspace="/workspace",
            access_token="token",
        ),
        detail_page,
    ]
    for page in pages:
        assert _post_count(page) > 0
        assert _post_count(page) == _idempotency_header_count(page)

    submit_start = detail_page.index('followupComposer?.addEventListener("submit"')
    interrupt_start = detail_page.index("followupInterruptButton?.addEventListener")
    submit_handler = detail_page[submit_start:interrupt_start]
    interrupt_handler = detail_page[interrupt_start:]
    assert "/interrupt" not in submit_handler
    assert "followupInterruptButton.dataset.interruptUrl" in interrupt_handler
    assert 'type="button" data-marvis-interrupt-button' in detail_page
    assert 'type="submit" aria-label="发送补充"' in detail_page

    attachment_script = _marvis_relay_attachment_script()
    assert "interrupt" not in attachment_script.lower()
    assert "fetch(" not in attachment_script


def test_relay_attachment_dialog_has_standard_keyboard_focus_and_inert_contract() -> None:
    sheet = _marvis_relay_attachment_sheet_html()
    script = _marvis_relay_attachment_script()

    assert 'role="dialog"' in sheet
    assert 'aria-modal="true"' in sheet
    assert "let previouslyFocused = null;" in script
    assert "child.inert = isOpen;" in script
    assert 'event.key === "Escape"' in script
    assert 'event.key !== "Tab"' in script
    assert "closeButton?.focus();" in script
    assert "previouslyFocused instanceof HTMLElement" in script
    assert "previouslyFocused.focus();" in script


def test_relay_work_log_and_confirmation_pages_preserve_modal_a11y_contract() -> None:
    page = _relay_task_detail_page(_waiting_detail(), access_token="token")

    # The work log is a non-modal desktop aside, but starts hidden in the
    # mobile document and becomes a real dialog only when opened there.
    assert 'data-marvis-work-log-max-event-id="0" aria-label="工作日志" hidden' in page
    assert "function updateMarvisWorkLogSemantics()" in page
    assert 'marvisWorkLog.setAttribute("role", "dialog");' in page
    assert 'marvisWorkLog.removeAttribute("role");' in page
    assert "function closeMarvisWorkLog()" in page
    assert "finishMarvisWorkLogClose();" in page
    assert 'event.key === "Escape"' in page
    assert "trapMarvisDialogFocus(marvisWorkLog, event);" in page
    assert "focusMarvisDialog(marvisWorkLog);" in page
    assert "setMarvisModalBackground(marvisWorkLog, true);" in page
    assert "setMarvisModalBackground(marvisWorkLog, false);" in page

    # The confirmation surface is re-parented outside the app shell before
    # that shell becomes inert, so no modal lives inside its own inert tree.
    assert '<section class="marvis-relay-confirmation-page"' in page
    assert 'role="dialog" aria-modal="true" aria-label="计划等待确认" hidden' in page
    assert "moveMarvisConfirmationPagesToDocumentRoot();" in page
    assert "function openMarvisConfirmationPage(page, trigger)" in page
    assert "function closeMarvisConfirmationPage(page, options = {})" in page
    assert "document.body.appendChild(page);" in page
    assert "setMarvisModalBackground(page, true);" in page
    assert "setMarvisModalBackground(page, false);" in page
    assert "page.addEventListener(\"keydown\"" in page
    assert "trapMarvisDialogFocus(page, event);" in page


def test_native_plan_dialog_has_standard_keyboard_focus_and_inert_contract() -> None:
    native_page = _live_page(42, native_provider="codex")

    assert 'role="dialog"' in native_page
    assert 'aria-modal="true"' in native_page
    assert "let planPagePreviouslyFocused = null;" in native_page
    assert "nativeAppShell.inert = true;" in native_page
    assert "nativeAppShell.inert = false;" in native_page
    assert "planPageClose?.focus()" in native_page
    assert "event.key !== \"Tab\"" in native_page
    assert "event.key !== \"Escape\"" in native_page
    assert "planPagePreviouslyFocused.focus({preventScroll: true});" in native_page


def test_native_session_page_surfaces_presentation_freshness_and_explicit_recovery() -> None:
    native_page = _live_page(42, native_provider="codex")

    # A failed or cached provider read must be visible as such.  The retry is
    # the only deliberate mutation; opening/refreshing the page remains a
    # pure read path.
    assert 'id="nativePresentationNotice"' in native_page
    assert 'role="status" aria-live="polite"' in native_page
    assert 'id="nativePresentationRetry"' in native_page
    assert 'id="contextPresentationStateValue"' in native_page
    assert 'id="contextFreshnessValue"' in native_page
    assert 'id="contextNextActionValue"' in native_page
    assert "function applyNativeSessionPresentation(session)" in native_page
    assert "function nativePresentationSourceLabel(value)" in native_page
    assert "freshness.source" in native_page
    assert 'source: "unavailable"' in native_page
    assert "nativePresentationRetry.addEventListener(\"click\"" in native_page
    assert "await syncNativeTranscript();" in native_page
    assert "await loadNativeSessionInfo();" in native_page


def test_relay_blocked_inbox_groups_by_presentation_and_exposes_one_next_action() -> None:
    summaries = [
        _summary(1, "waiting_user"),
        _summary(2, "waiting_approval"),
        _summary(3, "running"),
        _summary(4, "blocked"),
        _summary(5, "failed"),
        _summary(6, "interrupted"),
        _summary(7, "stale"),
        _summary(8, "completed"),
    ]
    page = _relay_blocked_inbox_page(
        summaries,
        selected_workspace="/workspace",
        access_token="token",
    )

    for label in ("等待我", "等待系统", "需要恢复", "已陈旧"):
        assert label in page
    for state in (
        "waiting_user",
        "waiting_approval",
        "running",
        "blocked",
        "failed",
        "interrupted",
        "stale",
    ):
        assert f"{state} task" in page
    assert "completed task" not in page
    assert page.count("唯一下一步：") == 7
    assert 'data-relay-inbox-action="resume"' in page
    assert 'data-relay-inbox-action="refresh"' in page
    assert 'data-relay-inbox-action="archive"' in page
    assert 'role="status"' in page
    assert '"Idempotency-Key"' in page


def test_relay_inbox_never_offers_resume_for_uncertain_native_approval() -> None:
    summary = replace(
        _summary(42, "blocked"),
        presentation=RelayPresentation(
            state="blocked",
            freshness={
                "source": "relay_lifecycle",
                "updated_at": "2026-07-10T00:00:00+00:00",
                "is_stale": False,
                "reason": "",
                "recovery_required": True,
                "recovery_state": "needs_recovery",
            },
            current_actor={"role": "director", "label": "总工程师", "status": "blocked"},
            blocking_reason="原生审批操作尚未获得可验证回执。",
            next_action="等待系统恢复审批回执。",
            allowed_actions=["refresh"],
        ),
    )

    page = _relay_blocked_inbox_page([summary], selected_workspace="/workspace")

    assert "检查审批回执" in page
    assert 'data-relay-inbox-action="refresh"' in page
    assert 'data-relay-inbox-action="resume"' not in page


def test_relay_and_native_pages_allow_zoom_and_keep_touch_high_contrast_support() -> None:
    relay_page = _relay_chat_home_page(selected_workspace="/workspace")
    native_home = _native_codex_page("codex")
    native_page = _live_page(42, native_provider="codex")
    timeline_v2 = render_timeline_v2_template("codex", {"initial_events": []})
    base_css = Path("wlcodex/live_stream/static/base.css").read_text(encoding="utf-8")
    relay_css = Path("wlcodex/live_stream/static/relay_marvis.css").read_text(
        encoding="utf-8"
    )

    for page in (relay_page, native_home, native_page, timeline_v2):
        assert "user-scalable=no" not in page
        assert "maximum-scale=1" not in page
        assert 'content="width=device-width, initial-scale=1' in page
    assert "--control-touch-target: 44px;" in base_css
    assert "@media (forced-colors: active)" in base_css
    assert "forced-color-adjust: auto;" in base_css
    assert ".marvis-relay-interrupt[data-marvis-interrupt-button]" in relay_css
    assert "min-height: 44px;" in relay_css


def test_sse_connection_owns_live_updates_and_hidden_pages_suspend_polling() -> None:
    detail_page = _relay_task_detail_page(_detail())
    native_home = _native_codex_page("codex")
    native_page = _live_page(42, native_provider="codex")

    # Relay closes its EventSource while hidden and reconnects from the event
    # cursor.  There is no second-level status poll in the rendered task page.
    assert 'document.visibilityState === "hidden"' in detail_page
    assert "closeRelayEventSource();" in detail_page
    assert "scheduleRelayEventsReconnect" in detail_page
    assert "relayEventsAfter" in detail_page
    assert not re.search(r"setInterval\([^)]*,\s*(?:1000|2000)\)", detail_page)

    # Native keeps polling only as a visible-page, 30-second recovery path;
    # an open EventSource cancels it and a hidden page closes the stream.
    assert "NATIVE_TRANSCRIPT_FALLBACK_INTERVAL_MS = 30000" in native_page
    assert "stopNativeTranscriptFallback();" in native_page
    assert "startNativeTranscriptFallback();" in native_page
    assert "document.visibilityState === \"hidden\"" in native_page
    assert "closeLiveEventSource();" in native_page
    assert "source.onopen = () => {" in native_page
    assert not re.search(r"setInterval\(pollEvents,\s*1000\)", native_page)
    assert "WLCodexSurfaceRuntime.createConditionalScroller" in native_page
    assert "const timelineScroller" in native_page
    assert "function isNearTimelineBottom()" not in native_page
    # The Native session index follows the same rule: no polling while its
    # session EventSource is connected, and no stream while the page is hidden.
    assert "sessionsFallbackPollTimer" in native_home
    assert "stopSessionsFallbackPoll();" in native_home
    assert "startSessionsFallbackPoll();" in native_home
    assert "resumeSessionsLiveConnection" in native_home
    assert "document.visibilityState === \"hidden\"" in native_home
    assert "sessionsEventSource || sessionsFallbackPollTimer" in native_home
    assert "source.onopen = () => {\n          stopSessionsFallbackPoll();" in native_home


def test_relay_sse_snapshot_uses_the_same_presentation_contract_as_the_task_page() -> None:
    detail_page = _relay_task_detail_page(_detail())

    assert 'addRelayEventListener("presentation.snapshot"' in detail_page
    assert "const presentation = payload.presentation;" in detail_page
    assert "presentation.current_actor" in detail_page
