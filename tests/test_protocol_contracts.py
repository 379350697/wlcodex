"""Contract tests locking the Codex app-server JSON-RPC payload shapes."""

from wlcodex.codex_backend import (
    build_legacy_approval_response,
    build_thread_start_params,
    build_turn_start_params,
    build_turn_steer_params,
    parse_thread_start_response,
    parse_turn_response,
    parse_turn_notification_ids,
    build_approval_response,
)
from wlcodex.models import ApprovalKind


def test_turn_start_uses_input_not_items() -> None:
    params = build_turn_start_params("thread-1", "hello")
    assert params == {
        "threadId": "thread-1",
        "input": [{"type": "text", "text": "hello", "text_elements": []}],
    }
    assert "items" not in params


def test_turn_steer_uses_input_and_expected_turn_id() -> None:
    params = build_turn_steer_params("thread-1", "turn-1", "stop")
    assert params == {
        "threadId": "thread-1",
        "expectedTurnId": "turn-1",
        "input": [{"type": "text", "text": "stop", "text_elements": []}],
    }


def test_thread_start_includes_policy_and_sandbox() -> None:
    params = build_thread_start_params("/tmp/work", "on-request", "workspace-write")
    assert params["cwd"] == "/tmp/work"
    assert params["approvalPolicy"] == "on-request"
    assert params["sandbox"] == "workspace-write"


def test_thread_start_supports_planning_overrides() -> None:
    params = build_thread_start_params(
        "/tmp/work",
        "on-request",
        "workspace-write",
        developer_instructions="Chief engineer boundaries.",
        config={"model_reasoning_effort": "xhigh"},
        model="gpt-5.5",
        personality="pragmatic",
    )

    assert params["approvalPolicy"] == "on-request"
    assert params["sandbox"] == "workspace-write"
    assert params["developerInstructions"] == "Chief engineer boundaries."
    assert params["config"] == {"model_reasoning_effort": "xhigh"}
    assert params["model"] == "gpt-5.5"
    assert params["personality"] == "pragmatic"


def test_turn_start_supports_planning_overrides() -> None:
    output_schema = {
        "type": "object",
        "required": ["summary"],
        "properties": {"summary": {"type": "string"}},
    }
    params = build_turn_start_params(
        "thread-1",
        "hello",
        effort="xhigh",
        approval_policy="on-request",
        sandbox_policy={
            "type": "workspaceWrite",
            "networkAccess": False,
            "writableRoots": [],
        },
        output_schema=output_schema,
        model="gpt-5.5",
        summary="none",
        personality="pragmatic",
    )

    assert params["threadId"] == "thread-1"
    assert params["input"] == [{"type": "text", "text": "hello", "text_elements": []}]
    assert params["effort"] == "xhigh"
    assert params["approvalPolicy"] == "on-request"
    assert params["sandboxPolicy"] == {
        "type": "workspaceWrite",
        "networkAccess": False,
        "writableRoots": [],
    }
    assert params["outputSchema"] == output_schema
    assert params["model"] == "gpt-5.5"
    assert params["summary"] == "none"
    assert params["personality"] == "pragmatic"


def test_turn_start_supports_native_model_and_images() -> None:
    params = build_turn_start_params(
        "thread-1",
        "describe this",
        model="gpt-5.5",
        images=[
            {
                "url": "data:image/png;base64,abc",
                "filename": "screen.png",
            }
        ],
        collaboration_mode={
            "mode": "default",
            "settings": {
                "model": "gpt-5.5",
                "reasoning_effort": "medium",
                "developer_instructions": None,
            },
        },
    )

    assert params == {
        "threadId": "thread-1",
        "input": [
            {"type": "text", "text": "describe this", "text_elements": []},
            {"type": "image", "url": "data:image/png;base64,abc"},
        ],
        "model": "gpt-5.5",
        "collaborationMode": {
            "mode": "default",
            "settings": {
                "model": "gpt-5.5",
                "reasoning_effort": "medium",
                "developer_instructions": None,
            },
        },
    }


def test_turn_steer_supports_image_input_blocks() -> None:
    params = build_turn_steer_params(
        "thread-1",
        "turn-1",
        "use this image",
        images=[{"data_url": "data:image/jpeg;base64,abc"}],
    )

    assert params == {
        "threadId": "thread-1",
        "expectedTurnId": "turn-1",
        "input": [
            {"type": "text", "text": "use this image", "text_elements": []},
            {"type": "image", "url": "data:image/jpeg;base64,abc"},
        ],
    }


def test_parse_nested_thread_and_turn_responses() -> None:
    assert parse_thread_start_response({"thread": {"id": "thread-1"}}) == "thread-1"
    assert parse_turn_response({"turn": {"id": "turn-1"}}) == "turn-1"


def test_parse_turn_notification_ids_from_nested_turn() -> None:
    assert parse_turn_notification_ids(
        {"threadId": "thread-1", "turn": {"id": "turn-1"}}
    ) == ("thread-1", "turn-1")


def test_command_approval_response_shape() -> None:
    assert build_approval_response(
        kind=ApprovalKind.COMMAND,
        action="approve_once",
        requested_permissions={},
        allow_session=True,
    ) == {"decision": "accept"}


def test_permissions_approval_response_shape() -> None:
    assert build_approval_response(
        kind=ApprovalKind.PERMISSIONS,
        action="approve_session",
        requested_permissions={"network": {"enabled": True}},
        allow_session=True,
    ) == {"permissions": {"network": {"enabled": True}}, "scope": "session"}


def test_permissions_deny_returns_empty_permission_profile() -> None:
    assert build_approval_response(
        kind=ApprovalKind.PERMISSIONS,
        action="deny",
        requested_permissions={"network": {"enabled": True}},
        allow_session=True,
    ) == {"permissions": {}, "scope": "turn"}


def test_legacy_approval_response_shape() -> None:
    assert build_legacy_approval_response(
        action="approve_once",
        allow_session=True,
    ) == {"decision": "approved"}
    assert build_legacy_approval_response(
        action="approve_session",
        allow_session=True,
    ) == {"decision": "approved_for_session"}
    assert build_legacy_approval_response(
        action="deny",
        allow_session=True,
    ) == {"decision": "denied"}
    assert build_legacy_approval_response(
        action="cancel",
        allow_session=True,
    ) == {"decision": "abort"}
