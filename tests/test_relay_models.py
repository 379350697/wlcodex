from wlcodex.relay.models import (
    RELAY_ARTIFACT_TYPES,
    RELAY_ROLE_JOB_STATUSES,
    RELAY_ROLES,
    RELAY_TASK_STATUSES,
    RelayBoard,
    RelaySessionLink,
    RelayTask,
    RelayTaskDetail,
    RelayTaskSummary,
)


def test_relay_roles_match_design_spec() -> None:
    assert [role.role for role in RELAY_ROLES] == [
        "director",
        "architect",
        "implementer",
        "tester",
        "auditor",
    ]
    assert [role.display_name for role in RELAY_ROLES] == [
        "总工程师",
        "架构工程师",
        "开发工程师",
        "测试工程师",
        "审核工程师",
    ]


def test_relay_statuses_and_artifact_types_match_design_spec() -> None:
    assert RELAY_TASK_STATUSES == (
        "queued",
        "running",
        "waiting_user",
        "blocked",
        "failed",
        "completed",
        "interrupted",
    )
    assert RELAY_ROLE_JOB_STATUSES == (
        "idle",
        "queued",
        "streaming",
        "waiting",
        "passed",
        "failed",
        "blocked",
        "interrupted",
    )
    assert RELAY_ARTIFACT_TYPES == (
        "relay_board",
        "routing_decision",
        "role_dispatch_metadata",
        "architecture_plan",
        "implementation_report",
        "test_report",
        "audit_report",
        "handoff_packet",
        "final_summary",
    )


def test_relay_api_shapes_are_serializable() -> None:
    task = RelayTask(
        id=7,
        title="Large task",
        prompt="Build relay",
        workspace="/repo",
        provider="codex",
        status="running",
        phase="director",
        created_at="2026-06-14T00:00:00+00:00",
        updated_at="2026-06-14T00:01:00+00:00",
    )
    board = RelayBoard(
        task_id=7,
        current_goal="Build relay",
        phase="director",
        latest_user_input="Use provider registry",
        current_dispatch="director",
        next_step="dispatch architect",
    )
    summary = RelayTaskSummary.from_task(
        task,
        role_statuses={"director": "queued"},
        director_decision_summary="Director received the task.",
        latest_handoff_summary="No handoff yet.",
    )
    detail = RelayTaskDetail(
        task=task,
        board=board,
        role_jobs=[],
        artifacts=[],
        latest_handoff=None,
        session_links=[
            RelaySessionLink(
                role="director",
                provider="codex",
                native_session_id="native-1",
                url="/native/codex?thread=native-1",
            )
        ],
        routing_decision={
            "route": "director_only",
            "risk": "low",
            "complexity": "simple",
        },
    )

    assert summary.to_dict()["task_id"] == 7
    assert summary.to_dict()["role_statuses"]["director"] == "queued"
    assert detail.to_dict()["board"]["current_dispatch"] == "director"
    assert detail.to_dict()["session_links"][0]["url"].startswith("/native/")
    assert detail.to_dict()["routing_decision"]["route"] == "director_only"
