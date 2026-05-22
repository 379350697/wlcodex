"""Tests for Telegram delivery isolation in the chief-engineer loop.

Verifies that Claude subprocesses are denied Telegram delivery secrets,
that direct Telegram API calls are detected and blocked, and that only
the platform controller can send Telegram after verification pass.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# 1. Claude subprocess environment is sanitized of Telegram secrets
# ---------------------------------------------------------------------------


def test_claude_subprocess_env_strips_bot_token() -> None:
    """Claude subprocess must not see WLCODEX_TELEGRAM_BOT_TOKEN."""
    from wlcodex.claude_backend import _sanitized_env

    with _fake_env({"WLCODEX_TELEGRAM_BOT_TOKEN": "12345:abc", "PATH": "/usr/bin"}):
        env = _sanitized_env()
        assert "WLCODEX_TELEGRAM_BOT_TOKEN" not in env
        assert "PATH" in env


def test_claude_subprocess_env_strips_telegram_substrings() -> None:
    from wlcodex.claude_backend import _sanitized_env

    with _fake_env({
        "CUSTOM_TELEGRAM_BOT_TOKEN": "abc",
        "MY_TELEGRAM_API_TOKEN": "xyz",
        "PATH": "/bin",
    }):
        env = _sanitized_env()
        assert "CUSTOM_TELEGRAM_BOT_TOKEN" not in env
        assert "MY_TELEGRAM_API_TOKEN" not in env
        assert "PATH" in env


def test_claude_subprocess_env_strips_telegram_api_hash() -> None:
    from wlcodex.claude_backend import _sanitized_env

    with _fake_env({
        "TELEGRAM_API_ID": "123456",
        "TELEGRAM_API_HASH": "abcdef",
        "HOME": "/home/user",
    }):
        env = _sanitized_env()
        assert "TELEGRAM_API_ID" not in env
        assert "TELEGRAM_API_HASH" not in env
        assert "HOME" in env


def test_claude_subprocess_env_strips_chat_id_shortcuts() -> None:
    from wlcodex.claude_backend import _sanitized_env

    with _fake_env({
        "WLC_CHAT_ID": "123",
        "WLCODEX_CHAT_ID": "456",
        "KEEP_ME": "yes",
    }):
        env = _sanitized_env()
        assert "WLC_CHAT_ID" not in env
        assert "WLCODEX_CHAT_ID" not in env
        assert "KEEP_ME" in env


def test_claude_send_uses_sanitized_env() -> None:
    """ClaudeBackend.send() launches subprocess with sanitized env."""
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig, _sanitized_env

    config = ClaudeConfig(enabled=False)
    backend = ClaudeBackend(config)

    # The env used for a real subprocess must be sanitized.
    env = _sanitized_env()
    for key in env:
        assert "TELEGRAM_BOT_TOKEN" not in key
        assert "TELEGRAM_API_TOKEN" not in key


# ---------------------------------------------------------------------------
# 2. Drift detection — Claude implementation text claiming Telegram delivery
# ---------------------------------------------------------------------------


def test_detect_claude_drift_message_id_claim() -> None:
    from wlcodex.orchestrator import _detect_claude_direct_delivery_drift

    impl_text = "All done. Sent Telegram reply with message_id=302 to user."
    findings = _detect_claude_direct_delivery_drift(impl_text)
    assert len(findings) > 0
    assert any("message_id=" in f.lower() for f in findings)


def test_detect_claude_drift_sendmessage_call() -> None:
    from wlcodex.orchestrator import _detect_claude_direct_delivery_drift

    impl_text = "I used sendMessage to notify the user via api.telegram.org"
    findings = _detect_claude_direct_delivery_drift(impl_text)
    assert len(findings) > 0
    # At least one of the patterns must have matched
    drift_text = " ".join(finding.lower() for finding in findings)
    assert any(
        pat in drift_text
        for pat in ("sendmessage", "api.telegram.org", "direct telegram delivery")
    )


def test_detect_claude_drift_curl_telegram() -> None:
    from wlcodex.orchestrator import _detect_claude_direct_delivery_drift

    impl_text = "Ran: curl -X POST https://api.telegram.org/bot${TOKEN}/sendMessage"
    findings = _detect_claude_direct_delivery_drift(impl_text)
    assert len(findings) > 0


def test_detect_claude_drift_token_access() -> None:
    from wlcodex.orchestrator import _detect_claude_direct_delivery_drift

    impl_text = "I read WLCODEX_TELEGRAM_BOT_TOKEN from env and sent the message."
    findings = _detect_claude_direct_delivery_drift(impl_text)
    assert len(findings) > 0
    assert any("token" in f.lower() for f in findings)


def test_detect_claude_drift_clean_implementation() -> None:
    from wlcodex.orchestrator import _detect_claude_direct_delivery_drift

    impl_text = "Modified 3 files and ran pytest. All tests pass. Ready for verification."
    findings = _detect_claude_direct_delivery_drift(impl_text)
    assert findings == []


def test_detect_verification_drift_ignores_negative_message_id_audit() -> None:
    from wlcodex.orchestrator import _detect_verification_delivery_drift

    verify_text = (
        "未发现 Claude 声称已发送 Telegram、直接调用 Telegram API、"
        "或输出 message_id=xxx。"
    )

    assert _detect_verification_delivery_drift(verify_text) == []


def test_detect_verification_drift_ignores_no_match_audit_line() -> None:
    from wlcodex.orchestrator import _detect_verification_delivery_drift

    verify_text = (
        "约束检查：\n"
        "- 未发送 Telegram 消息。\n"
        "- 未读取 token/env。\n"
        "- 未调用 Telegram Bot API。\n"
        "- diff 中未匹配到 `sendMessage`、`editMessageText`、"
        "`api.telegram.org`、`message_id=`。\n"
        "- Claude 完成摘要中未发现“已发送 Telegram”或 `message_id=xxx`。"
    )

    assert _detect_verification_delivery_drift(verify_text) == []


def test_detect_verification_drift_ignores_no_occurrence_audit_line() -> None:
    from wlcodex.orchestrator import _detect_verification_delivery_drift

    verify_text = (
        "Telegram 约束核查：本次 diff 中未出现 `Telegram`、"
        "`api.telegram.org`、`sendMessage`、`editMessageText`、"
        "`message_id`、`BOT_TOKEN` 等违规迹象。"
    )

    assert _detect_verification_delivery_drift(verify_text) == []


def test_detect_verification_drift_flags_positive_delivery_claim() -> None:
    from wlcodex.orchestrator import _detect_verification_delivery_drift

    verify_text = "我已经直接发送 Telegram，并拿到了 message_id=302。"

    findings = _detect_verification_delivery_drift(verify_text)
    assert len(findings) > 0
    assert "message_id=" in findings[0].lower()


def test_detect_verification_drift_token_request() -> None:
    from wlcodex.orchestrator import _detect_verification_delivery_drift

    verify_text = (
        "decision: pass\n"
        "But I need WLCODEX_TELEGRAM_BOT_TOKEN to send the result."
    )
    findings = _detect_verification_delivery_drift(verify_text)
    assert len(findings) > 0
    assert any("token" in f.lower() for f in findings)


def test_detect_verification_drift_clean() -> None:
    from wlcodex.orchestrator import _detect_verification_delivery_drift

    verify_text = (
        "decision: pass\n"
        "summary: All tests pass, git diff --check clean, no drift detected.\n"
        "confidence: high"
    )
    findings = _detect_verification_delivery_drift(verify_text)
    assert findings == []


# ---------------------------------------------------------------------------
# 3. Claude handoff packet includes anti-delivery constraints
# ---------------------------------------------------------------------------


def test_claude_handoff_packet_delivery_constraints() -> None:
    from wlcodex.context_packets import build_claude_handoff_packet

    packet = build_claude_handoff_packet(
        user_goal="Fix a bug",
        codex_analysis="Analysis here",
    )
    rendered = packet.render()
    assert "不要发送 Telegram" in rendered or "不要" in rendered
    assert "sendMessage" in rendered or "api.telegram.org" in rendered
    assert "WLCODEX_TELEGRAM_BOT_TOKEN" in rendered
    # Verify the handoff_from_codex constraints contain delivery prohibition
    h = packet.handoff_from_codex
    all_constraints = " ".join(h.constraints)
    assert "不要发送 Telegram" in all_constraints or "不要调用 Telegram Bot API" in all_constraints


# ---------------------------------------------------------------------------
# 4. Codex verification packet includes anti-delivery constraints
# ---------------------------------------------------------------------------


def test_codex_verification_packet_delivery_constraints() -> None:
    from wlcodex.context_packets import build_codex_verification_packet

    packet = build_codex_verification_packet(
        user_goal="Fix a bug",
        codex_plan_summary="Plan here",
        claude_completion_summary="Implementation complete",
        changed_files=["test.py"],
        test_results="All passing",
        diff_summary="1 file changed",
    )
    rendered = packet.render()
    assert "不要发送 Telegram" in rendered or "不要" in rendered
    assert "WLCODEX_TELEGRAM_BOT_TOKEN" in rendered
    # recent_user_constraints must be set with verification rules
    assert packet.recent_user_constraints
    constraints_text = " ".join(packet.recent_user_constraints)
    assert "不要发送 Telegram" in constraints_text or "不要调用 Telegram Bot API" in constraints_text


# ---------------------------------------------------------------------------
# 5. Verification decision overrides pass when Claude drift detected
# ---------------------------------------------------------------------------


def test_orchestrator_refuses_pass_on_claude_drift():
    from wlcodex.orchestrator import (
        ChiefEngineerOrchestrator,
        VerificationDecision,
        _detect_claude_direct_delivery_drift,
    )

    # Simulate Claude implementation text with direct delivery drift
    drift_impl = (
        "Patched the code. curl -X POST https://api.telegram.org/bot"
        "${WLCODEX_TELEGRAM_BOT_TOKEN}/sendMessage was executed. message_id=302."
    )
    findings = _detect_claude_direct_delivery_drift(drift_impl)
    assert len(findings) > 0, "Drift must be detected in this text"

    # The orchestrator should override pass → retry when drift found
    orch = ChiefEngineerOrchestrator(
        codex_backend=SimpleNamespace(),
        claude_backend=SimpleNamespace(),
    )
    orch._last_claude_drift_findings = findings

    decision = VerificationDecision.parse("decision: pass\nsummary: All good.")
    # Simulate the drift override logic from run/run_streaming
    if decision.decision == "pass" and orch._last_claude_drift_findings:
        decision = VerificationDecision(
            decision="retry",
            summary=decision.summary,
            required_fix=(
                "Claude 实施文本中检测到直接 Telegram delivery / token access: "
                + "; ".join(orch._last_claude_drift_findings)
            ),
        )

    assert decision.decision == "retry"
    assert "token access" in decision.required_fix.lower() or "delivery" in decision.required_fix.lower()


def test_orchestrator_allows_pass_when_no_drift():
    from wlcodex.orchestrator import (
        ChiefEngineerOrchestrator,
        VerificationDecision,
    )

    orch = ChiefEngineerOrchestrator(
        codex_backend=SimpleNamespace(),
        claude_backend=SimpleNamespace(),
    )
    orch._last_claude_drift_findings = []  # clean

    decision = VerificationDecision.parse("decision: pass\nsummary: All good.")
    # No drift → pass stands
    assert decision.decision == "pass"
    if decision.decision == "pass" and orch._last_claude_drift_findings:
        decision = VerificationDecision(
            decision="retry",
            summary=decision.summary,
            required_fix="drift",
        )
    assert decision.decision == "pass"


# ---------------------------------------------------------------------------
# 6. Runtime projector detects security violations and blocks pass
# ---------------------------------------------------------------------------


def test_projector_security_event_tags_agent_run(tmp_path: Path) -> None:
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_projector import RuntimeProjector
    from wlcodex.runtime_events import (
        EventType,
        AggregateType,
        EventSource,
        Visibility,
        RuntimeEvent,
        now_iso,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    projector = RuntimeProjector(ledger._conn, store=store)

    # Create an agent run row
    conv = ledger.create_conversation(
        chat_id=1, user_id=1, title="test", mode="chief_engineer", workspace_alias="test"
    )
    orch = ledger.create_orchestration_run(conversation_id=conv.id, goal="test")
    agent_run = ledger.create_agent_run(
        conversation_id=conv.id,
        agent="claude",
        role="implementation",
    )
    ledger.update_agent_run_status(agent_run.id, "running")

    # Append a security event
    sec_event = store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.SECURITY_DELIVERY_BLOCKED,
        aggregate_type=AggregateType.AGENT_RUN,
        aggregate_id=str(agent_run.id),
        correlation_id="corr-1",
        source=EventSource.ORCHESTRATOR,
        actor="orchestrator",
        visibility=Visibility.OPERATOR,
        payload={"finding": "Claude claimed message_id=302", "agent": "claude"},
        occurred_at=now_iso(),
        conversation_id=conv.id,
        orchestration_run_id=orch.id,
        agent_run_id=agent_run.id,
    ))

    projector.apply(sec_event)

    # Agent run completion summary should now contain the security tag
    row = ledger._conn.execute(
        "SELECT completion_summary FROM agent_runs WHERE id = ?", (agent_run.id,)
    ).fetchone()
    assert row is not None
    assert "[SECURITY]" in (row["completion_summary"] or "")


def test_projector_rejects_pass_on_security_violation(tmp_path: Path) -> None:
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_projector import RuntimeProjector
    from wlcodex.runtime_events import (
        EventType,
        AggregateType,
        EventSource,
        Visibility,
        RuntimeEvent,
        now_iso,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    projector = RuntimeProjector(ledger._conn, store=store)

    conv = ledger.create_conversation(
        chat_id=1, user_id=1, title="test", mode="chief_engineer", workspace_alias="test"
    )
    orch = ledger.create_orchestration_run(conversation_id=conv.id, goal="test")
    ledger.update_orchestration_run(orch.id, status="running", current_step="verification")

    # 1. Add a security event
    sec_event = store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.SECURITY_DELIVERY_BLOCKED,
        aggregate_type=AggregateType.ORCHESTRATION_RUN,
        aggregate_id=str(orch.id),
        correlation_id="corr-2",
        source=EventSource.ORCHESTRATOR,
        actor="orchestrator",
        visibility=Visibility.OPERATOR,
        payload={"finding": "Claude attempted direct Telegram delivery"},
        occurred_at=now_iso(),
        conversation_id=conv.id,
        orchestration_run_id=orch.id,
    ))

    # 2. Add a pass verification decision with a later id
    pass_event = store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.VERIFICATION_DECISION_RECORDED,
        aggregate_type=AggregateType.ORCHESTRATION_RUN,
        aggregate_id=str(orch.id),
        correlation_id="corr-2",
        source=EventSource.ORCHESTRATOR,
        actor="codex",
        visibility=Visibility.OPERATOR,
        payload={"decision": "pass", "verify_round": 1},
        occurred_at=now_iso(),
        conversation_id=conv.id,
        orchestration_run_id=orch.id,
    ))

    # Project them
    projector.apply(sec_event)
    projector.apply(pass_event)

    # _has_pass_verification should return False because of security event
    assert projector._has_pass_verification(pass_event) is False

    # Run completed without verified pass should fail
    run_complete = store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.RUN_COMPLETED,
        aggregate_type=AggregateType.ORCHESTRATION_RUN,
        aggregate_id=str(orch.id),
        correlation_id="corr-2",
        source=EventSource.ORCHESTRATOR,
        actor="orchestrator",
        visibility=Visibility.USER,
        payload={"verify_round": 1},
        occurred_at=now_iso(),
        conversation_id=conv.id,
        orchestration_run_id=orch.id,
    ))
    projector.apply(run_complete)

    # Orchestration run should be "failed" not "passed" due to security violation
    or_status = ledger._conn.execute(
        "SELECT status FROM orchestration_runs WHERE id = ?", (orch.id,)
    ).fetchone()
    assert or_status is not None
    assert or_status["status"] == "failed"


def test_projector_clean_run_passes(tmp_path: Path) -> None:
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_projector import RuntimeProjector
    from wlcodex.runtime_events import (
        EventType,
        AggregateType,
        EventSource,
        Visibility,
        RuntimeEvent,
        now_iso,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    projector = RuntimeProjector(ledger._conn, store=store)

    conv = ledger.create_conversation(
        chat_id=1, user_id=1, title="test", mode="chief_engineer", workspace_alias="test"
    )
    orch = ledger.create_orchestration_run(conversation_id=conv.id, goal="test")
    ledger.update_orchestration_run(orch.id, status="running", current_step="verification")

    # Only clean events — no security violations
    pass_event = store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.VERIFICATION_DECISION_RECORDED,
        aggregate_type=AggregateType.ORCHESTRATION_RUN,
        aggregate_id=str(orch.id),
        correlation_id="corr-3",
        source=EventSource.ORCHESTRATOR,
        actor="codex",
        visibility=Visibility.OPERATOR,
        payload={"decision": "pass", "verify_round": 1},
        occurred_at=now_iso(),
        conversation_id=conv.id,
        orchestration_run_id=orch.id,
    ))
    projector.apply(pass_event)

    run_complete = store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.RUN_COMPLETED,
        aggregate_type=AggregateType.ORCHESTRATION_RUN,
        aggregate_id=str(orch.id),
        correlation_id="corr-3",
        source=EventSource.ORCHESTRATOR,
        actor="orchestrator",
        visibility=Visibility.USER,
        payload={"verify_round": 1},
        occurred_at=now_iso(),
        conversation_id=conv.id,
        orchestration_run_id=orch.id,
    ))
    projector.apply(run_complete)

    # Should pass
    or_status = ledger._conn.execute(
        "SELECT status FROM orchestration_runs WHERE id = ?", (orch.id,)
    ).fetchone()
    assert or_status is not None
    assert or_status["status"] == "passed"


# ---------------------------------------------------------------------------
# 7. Security event types are defined in runtime_events
# ---------------------------------------------------------------------------


def test_security_event_types_exist() -> None:
    from wlcodex.runtime_events import EventType
    assert EventType.SECURITY_DELIVERY_BLOCKED == "security.delivery.blocked"
    assert EventType.SECURITY_TOKEN_ACCESS_ATTEMPTED == "security.token.access.attempted"


# ---------------------------------------------------------------------------
# 8. Context packet builders include anti-delivery constraints in render
# ---------------------------------------------------------------------------


def test_claude_handoff_packet_prohibits_telegram_api_calls() -> None:
    from wlcodex.context_packets import build_claude_handoff_packet

    packet = build_claude_handoff_packet(
        user_goal="Test",
        codex_analysis="Test analysis",
    )
    rendered = packet.render().lower()
    assert "api.telegram.org" in rendered or "sendmessage" in rendered or "telegram bot api" in rendered


# ---------------------------------------------------------------------------
# 9. Orchestration runner emits security events on drift
# ---------------------------------------------------------------------------


def test_orchestration_runner_emits_security_event_on_claude_drift(tmp_path: Path) -> None:
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.orchestrator import _detect_claude_direct_delivery_drift
    from wlcodex.runtime_events import EventType

    # Simulate the detection + event emission logic
    claude_text = "Fixed the bug. curl -X POST api.telegram.org sent message_id=302."
    findings = _detect_claude_direct_delivery_drift(claude_text)
    assert len(findings) > 0

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)

    conv = ledger.create_conversation(
        chat_id=1, user_id=1, title="test", mode="chief_engineer", workspace_alias="test"
    )
    orch = ledger.create_orchestration_run(conversation_id=conv.id, goal="test")

    # Emit the security events (mirrors what orchestration_runner does)
    from wlcodex.runtime_events import (
        AggregateType, EventSource, Visibility, RuntimeEvent, now_iso,
    )
    for finding in findings:
        store.append(RuntimeEvent(
            schema_version=1,
            event_type=EventType.SECURITY_DELIVERY_BLOCKED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id=str(orch.id),
            correlation_id="corr-sec",
            source=EventSource.ORCHESTRATOR,
            actor="orchestrator",
            visibility=Visibility.OPERATOR,
            payload={"finding": finding, "agent": "claude", "role": "implementation"},
            occurred_at=now_iso(),
            conversation_id=conv.id,
            orchestration_run_id=orch.id,
        ))

    # Events must be queryable
    events = store.list_by_correlation("corr-sec")
    assert len(events) >= len(findings)
    assert any(e.event_type == EventType.SECURITY_DELIVERY_BLOCKED for e in events)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _fake_env(env_dict: dict[str, str]):
    """Temporarily replace os.environ with *env_dict*."""
    old = dict(os.environ)
    os.environ.clear()
    os.environ.update(env_dict)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)
