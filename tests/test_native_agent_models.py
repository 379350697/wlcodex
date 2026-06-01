from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentSession,
    NativeAgentStatus,
)


def test_status_serializes_provider_and_engine() -> None:
    status = NativeAgentStatus(
        provider="claude",
        provider_engine="sdk-deepseek",
        enabled=True,
        connected=False,
        status_code="missing_api_key",
        message="DEEPSEEK_API_KEY is not set.",
    )

    assert status.to_json_dict() == {
        "provider": "claude",
        "provider_engine": "sdk-deepseek",
        "enabled": True,
        "connected": False,
        "status_code": "missing_api_key",
        "message": "DEEPSEEK_API_KEY is not set.",
        "metadata": {},
    }


def test_capabilities_disable_controls_with_reasons() -> None:
    caps = NativeAgentCapabilities(
        can_list_sessions=True,
        can_continue_session=True,
        disabled_reasons={"can_steer_active_turn": "Claude SDK cannot steer an active turn."},
    )

    payload = caps.to_json_dict()

    assert payload["can_list_sessions"] is True
    assert payload["can_continue_session"] is True
    assert payload["can_steer_active_turn"] is False
    assert payload["disabled_reasons"]["can_steer_active_turn"] == (
        "Claude SDK cannot steer an active turn."
    )


def test_session_serializes_native_thread_id_for_existing_ui() -> None:
    session = NativeAgentSession(
        id=1,
        provider="antigravity",
        provider_engine="sdk",
        native_session_id="ag-1",
        agent_run_id=44,
        conversation_id=9,
        title="Fix UI",
        cwd="/Users/wl/projects/wlcodex",
        source_kind="antigravity_sdk",
        status="running",
        last_turn_id="turn-1",
        activity_at="2026-06-01T00:00:00Z",
        created_at="2026-06-01T00:00:00Z",
        updated_at="2026-06-01T00:01:00Z",
    )

    payload = session.to_json_dict()

    assert payload["provider"] == "antigravity"
    assert payload["provider_engine"] == "sdk"
    assert payload["native_session_id"] == "ag-1"
    assert payload["native_thread_id"] == "ag-1"


def test_control_result_preserves_existing_thread_field() -> None:
    result = NativeAgentControlResult(
        provider="claude",
        provider_engine="cli-local",
        native_session_id="claude-session-1",
        agent_run_id=45,
        status="started",
    )

    payload = result.to_json_dict()

    assert payload["native_session_id"] == "claude-session-1"
    assert payload["native_thread_id"] == "claude-session-1"
    assert payload["status"] == "started"
