from wlcodex.relay.context import build_relay_board, build_role_context_packet
from wlcodex.relay.models import HandoffPacket, RelayTask


def _task() -> RelayTask:
    return RelayTask(
        id=11,
        title="Relay task",
        prompt="Ship the relay workspace",
        workspace="/repo",
        provider="claude",
        status="running",
        phase="architecture",
        created_at="2026-06-14T00:00:00+00:00",
        updated_at="2026-06-14T00:00:00+00:00",
    )


def test_role_context_packet_excludes_full_transcript_text() -> None:
    transcript_text = "FULL TRANSCRIPT SECRET: do not share complete history"
    board = build_relay_board(
        _task(),
        latest_user_input="Latest instruction",
        confirmed_facts=["Fact A"],
        artifacts=[{"artifact_type": "relay_board", "summary": transcript_text}],
        transcript=[{"role": "user", "text": transcript_text}],
    )

    packet = build_role_context_packet(
        task=_task(),
        role="architect",
        board=board,
        handoffs=[],
        artifacts=[{"artifact_type": "relay_board", "summary": transcript_text}],
        transcript=[{"role": "assistant", "text": transcript_text}],
    )

    serialized = packet.to_json_dict()
    assert transcript_text not in str(serialized)
    assert serialized["latest_user_input"] == "Latest instruction"


def test_latest_user_input_wins_over_stale_handoff_summaries() -> None:
    stale_handoff = HandoffPacket(
        from_role="architect",
        to_role="implementer",
        summary="Old permission: deploy automatically",
        confirmed_facts=["Old plan"],
        open_questions=[],
        evidence_refs=[],
        next_action="Deploy",
    )
    board = build_relay_board(
        _task(),
        latest_user_input="Do not commit or deploy in v1",
        confirmed_facts=["Relay Task is not a native session"],
        handoffs=[stale_handoff],
    )

    packet = build_role_context_packet(
        task=_task(),
        role="implementer",
        board=board,
        handoffs=[stale_handoff],
        artifacts=[],
    )

    assert packet.latest_user_input == "Do not commit or deploy in v1"
    assert packet.handoff_summaries == ["Old permission: deploy automatically"]
    assert "Latest user input has priority" in " ".join(packet.constraints)
    assert "No role inherits old permissions" in " ".join(packet.constraints)


def test_director_first_context_requires_routing_decision() -> None:
    board = build_relay_board(
        _task(),
        latest_user_input="测试接力流程：删除一个文件",
    )

    packet = build_role_context_packet(
        task=_task(),
        role="director",
        board=board,
        handoffs=[],
        artifacts=[],
    )

    constraints = " ".join(packet.constraints)
    assert "Director first action must be a routing_decision" in constraints
    assert "Do not inspect, edit, delete, test, commit, or deploy" in constraints
    assert packet.expected_output_envelope["artifact_type"] == "routing_decision"
    assert packet.expected_output_envelope["route"] == (
        "director_only|core_relay|full_relay|audit_first|waiting_user|blocked"
    )
    assert packet.expected_output_envelope["required_roles"] == ["director"]
