from __future__ import annotations

import pytest

from wlcodex.live_stream.collapse import (
    LiveTurnSummaryConfig,
    build_turn_collapse_summary,
    summarize_turn_with_sidecar,
)
from wlcodex.live_stream.models import WorkerStreamEvent


def _stream_event(
    event_id: int,
    kind: str,
    payload: dict,
) -> WorkerStreamEvent:
    return WorkerStreamEvent(
        id=event_id,
        type=kind,
        kind=kind,
        agent_run_id=1,
        conversation_id=1,
        occurred_at="2026-05-31T00:00:00+00:00",
        source="codex",
        actor="codex_native",
        visibility="user",
        payload=payload,
    )


def test_rule_summary_collapses_completed_old_turn() -> None:
    events = [
        _stream_event(
            1,
            "user_message",
            {
                "native_turn_id": "turn-1",
                "text": "改好了自己真实测一遍没问题了再回复",
            },
        ),
        _stream_event(
            2,
            "text_delta",
            {"native_turn_id": "turn-1", "delta": "改好了，这次我真实测过了。"},
        ),
        _stream_event(
            3,
            "completed",
            {"native_turn_id": "turn-1", "status": "completed"},
        ),
    ]

    summary = build_turn_collapse_summary(events, current_turn_id="")

    assert summary.native_turn_id == "turn-1"
    assert summary.event_count == 3
    assert summary.completed is True
    assert summary.should_collapse is True
    assert summary.title == "3 条消息：改好了自己真实测一遍没问题了再回复"
    assert summary.source == "rules"


def test_onsite_summary_never_collapses_completed_old_turn() -> None:
    events = [
        _stream_event(1, "user_message", {"native_turn_id": "turn-1", "text": "继续"}),
        _stream_event(2, "text_delta", {"native_turn_id": "turn-1", "delta": "完成"}),
        _stream_event(3, "completed", {"native_turn_id": "turn-1"}),
    ]

    summary = build_turn_collapse_summary(
        events,
        current_turn_id="",
        onsite=True,
    )

    assert summary.completed is True
    assert summary.should_collapse is False


def test_rule_summary_recognizes_native_turn_completed_activity() -> None:
    events = [
        _stream_event(
            1,
            "user_message",
            {"native_turn_id": "turn-native", "text": "旁路是不是更好"},
        ),
        _stream_event(
            2,
            "activity",
            {
                "native_turn_id": "turn-native",
                "action": "turn_completed",
                "status": "completed",
            },
        ),
    ]

    summary = build_turn_collapse_summary(events, current_turn_id="")

    assert summary.completed is True
    assert summary.should_collapse is True


def test_rule_summary_keeps_current_or_failed_turn_expanded() -> None:
    current = [
        _stream_event(1, "user_message", {"native_turn_id": "turn-live", "text": "继续"}),
        _stream_event(2, "text_delta", {"native_turn_id": "turn-live", "delta": "处理中"}),
    ]
    failed = [
        _stream_event(3, "user_message", {"native_turn_id": "turn-failed", "text": "部署"}),
        _stream_event(
            4,
            "failed",
            {"native_turn_id": "turn-failed", "text": "JsonRpcError"},
        ),
    ]

    assert (
        build_turn_collapse_summary(current, current_turn_id="turn-live").should_collapse
        is False
    )
    assert build_turn_collapse_summary(failed, current_turn_id="").should_collapse is False


def test_rule_summary_collapses_non_current_turn_without_completion_marker() -> None:
    events = [
        _stream_event(
            1,
            "user_message",
            {"native_turn_id": "turn-old", "text": "最新回复为什么没折叠"},
        ),
        _stream_event(
            2,
            "text_delta",
            {"native_turn_id": "turn-old", "delta": "因为缺少完成事件。"},
        ),
    ]

    old_summary = build_turn_collapse_summary(events, current_turn_id="turn-live")
    current_summary = build_turn_collapse_summary(events, current_turn_id="turn-old")

    assert old_summary.should_collapse is True
    assert current_summary.should_collapse is False


@pytest.mark.asyncio
async def test_sidecar_summary_is_optional_and_falls_back_to_rules() -> None:
    events = [
        _stream_event(1, "user_message", {"native_turn_id": "turn-1", "text": "修复实时发送"}),
        _stream_event(2, "completed", {"native_turn_id": "turn-1"}),
    ]

    async def failing_client(*, model: str, prompt: str, timeout_seconds: float) -> str:
        raise RuntimeError("sidecar down")

    summary = await summarize_turn_with_sidecar(
        events,
        config=LiveTurnSummaryConfig(enabled=True),
        client=failing_client,
    )

    assert summary.title == "2 条消息：修复实时发送"
    assert summary.source == "rules"


@pytest.mark.asyncio
async def test_sidecar_summary_can_replace_rule_title() -> None:
    events = [
        _stream_event(1, "user_message", {"native_turn_id": "turn-1", "text": "一大段排查"}),
        _stream_event(2, "text_delta", {"native_turn_id": "turn-1", "delta": "修复旧 turn 污染。"}),
        _stream_event(3, "completed", {"native_turn_id": "turn-1"}),
    ]

    async def client(*, model: str, prompt: str, timeout_seconds: float) -> str:
        return (
            '{"title":"执行摘要","primary_label":"结果","primary":"修复旧 active turn 污染并完成公网实测。",'
            '"evidence_label":"改动","evidence_items":["释放旧 turn 后再开新 turn。"],'
            '"risk_label":"验证","risk":"公网 events 和官方 thread/read 均通过。",'
            '"next_label":"下一步","next":"可以结束任务，或继续补充。"}'
        )

    summary = await summarize_turn_with_sidecar(
        events,
        config=LiveTurnSummaryConfig(enabled=True, max_title_chars=80),
        client=client,
    )

    assert summary.source == "deepseek"
    assert summary.title == "3 条消息：修复旧 active turn 污染并完成公网实测。"
