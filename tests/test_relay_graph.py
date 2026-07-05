import asyncio
import json

from wlcodex.db import Ledger
from wlcodex.native_agents.provider import NativeAgentRegistry
from wlcodex.relay.envelopes import parse_role_envelope
from wlcodex.relay.graph import (
    build_marvis_relay_state,
    transition_from_role_envelope,
    transition_from_role_parse_result,
)
from wlcodex.relay.service import RelayService
from wlcodex.relay.store import RelayStore

from tests.test_relay_service import FakeProvider


def _service(tmp_path):
    db_path = tmp_path / "wlcodex.sqlite3"
    ledger = Ledger.open(db_path)
    ledger.migrate()
    provider = FakeProvider()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([provider]),
        default_provider="claude",
    )
    return service, provider, db_path


def _envelope(**overrides):
    payload = {
        "status": "passed",
        "reason": "ready",
        "role": "implementer",
        "artifact_type": "implementation_report",
        "handoff_to": "tester",
        "summary": "Implementation is ready.",
        "evidence_refs": ["tests/test_relay_graph.py"],
        "open_questions": [],
        "next_action": "test it",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_role_parse_result_transition_models_handoff_interrupt_and_invalid() -> None:
    handoff = transition_from_role_parse_result(
        parse_role_envelope(_envelope()),
        role="implementer",
        round_id=3,
    )

    assert handoff.goto == "tester"
    assert handoff.terminal == ""
    assert handoff.interrupt is None
    assert handoff.update["role_statuses"] == {
        "implementer": "passed",
        "tester": "queued",
    }
    assert handoff.events[0]["event_type"] == "handoff.created"

    waiting = transition_from_role_parse_result(
        parse_role_envelope(
            _envelope(
                status="waiting",
                role="architect",
                artifact_type="architecture_plan",
                handoff_to="",
                open_questions=["Approve this plan?"],
                confirmation_options=[
                    {
                        "id": "approve",
                        "label": "Approve",
                        "instruction": "execute approved plan",
                    }
                ],
            )
        ),
        role="architect",
        round_id=3,
        artifact_id=42,
    )

    assert waiting.goto == "waiting_user"
    assert waiting.interrupt is not None
    assert waiting.interrupt.kind == "plan_approval"
    assert waiting.interrupt.role == "architect"
    assert waiting.interrupt.artifact_id == 42
    assert waiting.update["task_status"] == "waiting_user"
    assert waiting.events[0]["event_type"] == "task.waiting_user"

    invalid = transition_from_role_parse_result(
        parse_role_envelope("{not json"),
        role="director",
        round_id=3,
    )

    assert invalid.goto == "blocked"
    assert invalid.terminal == "blocked"
    assert invalid.update["role_statuses"] == {"director": "blocked"}
    assert "invalid json" in invalid.update["error"]


def test_role_transition_models_blocked_questions_terminals_and_rework_handoff() -> None:
    blocked_question = transition_from_role_parse_result(
        parse_role_envelope(
            _envelope(
                status="blocked",
                role="architect",
                artifact_type="architecture_plan",
                handoff_to="",
                open_questions=["Need browser target?"],
                next_action="wait for user",
            )
        ),
        role="architect",
        round_id=4,
        artifact_id=77,
    )

    assert blocked_question.goto == "waiting_user"
    assert blocked_question.terminal == ""
    assert blocked_question.interrupt is not None
    assert blocked_question.interrupt.kind == "blocked_question"
    assert blocked_question.interrupt.open_questions == ["Need browser target?"]
    assert blocked_question.update["task_status"] == "waiting_user"
    assert blocked_question.update["role_statuses"] == {"architect": "waiting"}

    blocked_terminal = transition_from_role_parse_result(
        parse_role_envelope(
            _envelope(
                status="blocked",
                role="tester",
                artifact_type="test_report",
                handoff_to="",
                open_questions=[],
            )
        ),
        role="tester",
        round_id=4,
    )
    assert blocked_terminal.goto == "blocked"
    assert blocked_terminal.terminal == "blocked"
    assert blocked_terminal.update["role_statuses"] == {"tester": "blocked"}

    failed_terminal = transition_from_role_parse_result(
        parse_role_envelope(
            _envelope(
                status="failed",
                role="implementer",
                artifact_type="implementation_report",
                handoff_to="",
                open_questions=[],
            )
        ),
        role="implementer",
        round_id=4,
    )
    assert failed_terminal.goto == "blocked"
    assert failed_terminal.terminal == "failed"
    assert failed_terminal.update["role_statuses"] == {"implementer": "failed"}

    completed = transition_from_role_parse_result(
        parse_role_envelope(
            _envelope(
                status="passed",
                role="director",
                artifact_type="final_summary",
                handoff_to="",
                summary="Done.",
                open_questions=[],
            )
        ),
        role="director",
        round_id=4,
    )
    assert completed.goto == "completed"
    assert completed.terminal == "completed"
    assert completed.update["task_status"] == "completed"

    rework_result = parse_role_envelope(
        _envelope(
            status="failed",
            role="auditor",
            artifact_type="audit_report",
            handoff_to="implementer",
            summary="Regression found.",
            open_questions=[],
            next_action="rework",
        )
    )
    assert rework_result.envelope is not None
    rework = transition_from_role_envelope(
        rework_result.envelope,
        role="auditor",
        round_id=4,
        next_role="implementer",
        prefer_handoff=True,
    )
    assert rework.goto == "implementer"
    assert rework.terminal == ""
    assert rework.update["task_status"] == "running"
    assert rework.update["role_statuses"] == {
        "auditor": "failed",
        "implementer": "queued",
    }
    assert rework.events[0]["event_type"] == "handoff.created"


def test_marvis_relay_state_rebuilds_waiting_round_from_existing_ledger(tmp_path) -> None:
    service, _provider, db_path = _service(tmp_path)
    task = service.create_task(
        title="Relay graph",
        prompt="Build a graph-state projection",
        workspace="/repo",
        provider="claude",
    )

    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            _envelope(
                role="director",
                artifact_type="routing_decision",
                handoff_to="architect",
                summary="Route through architect.",
                next_action="draft plan",
                complexity="medium",
                risk="medium",
                route="core_relay",
                required_roles=["director", "architect", "implementer", "auditor"],
                acceptance_criteria=["state projection exists"],
                stop_conditions=[],
                requires_user_approval=False,
            ),
            dispatch_next=False,
        )
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "architect",
            _envelope(
                status="waiting",
                role="architect",
                artifact_type="architecture_plan",
                handoff_to="",
                summary="Plan needs approval.",
                open_questions=["Approve implementation?"],
                next_action="wait for approval",
                confirmation_options=[
                    {
                        "id": "approve",
                        "label": "Approve",
                        "instruction": "execute approved plan",
                    }
                ],
            ),
            dispatch_next=False,
        )
    )

    state = build_marvis_relay_state(service.get_task(task.id))

    assert state.task_id == task.id
    assert state.round_id == 1
    assert state.current_node == "waiting_user"
    assert state.route == "core_relay"
    assert state.required_roles == ["director", "architect", "implementer", "auditor"]
    assert state.role_statuses["architect"] == "waiting"
    assert state.active_role == "architect"
    assert state.latest_user_input == "Build a graph-state projection"
    assert state.pending_interrupt is not None
    assert state.pending_interrupt.kind == "plan_approval"
    assert state.pending_interrupt.open_questions == ["Approve implementation?"]
    assert any(handoff["to_role"] == "architect" for handoff in state.handoffs)
    assert any(artifact["artifact_type"] == "architecture_plan" for artifact in state.artifacts)

    restarted_ledger = Ledger.open(db_path)
    restarted = RelayService(
        store=RelayStore(restarted_ledger),
        registry=NativeAgentRegistry([FakeProvider()]),
        default_provider="claude",
    )
    recovered = restarted.build_marvis_relay_state(task.id)

    assert recovered.to_json_dict() == state.to_json_dict()


def test_round_control_continue_records_resume_transition_and_clears_interrupt(tmp_path) -> None:
    service, _provider, _db_path = _service(tmp_path)
    task = service.create_task(
        title="Relay resume",
        prompt="Approve then resume",
        workspace="/repo",
        provider="claude",
    )

    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            _envelope(
                role="director",
                artifact_type="routing_decision",
                handoff_to="architect",
                summary="Route through architect.",
                next_action="draft plan",
                complexity="medium",
                risk="medium",
                route="core_relay",
                required_roles=["director", "architect", "implementer"],
                acceptance_criteria=["resume transition exists"],
                stop_conditions=[],
                requires_user_approval=False,
            ),
            dispatch_next=False,
        )
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "architect",
            _envelope(
                status="waiting",
                role="architect",
                artifact_type="architecture_plan",
                handoff_to="",
                summary="Plan needs approval.",
                open_questions=["Approve implementation?"],
                next_action="wait for approval",
            ),
            dispatch_next=False,
        )
    )

    control = asyncio.run(
        service.apply_round_control(
            task.id,
            1,
            decision="continue",
            comment="Approved.",
            dispatch_next=False,
        )
    )

    assert control["relay_transition"]["goto"] == "architect"
    assert control["relay_transition"]["update"]["task_status"] == "running"
    assert control["relay_transition"]["update"]["role_statuses"] == {"architect": "queued"}
    round_control_event = [
        event for event in service.events_for_task(task.id) if event.event_type == "round.control"
    ][-1]
    assert round_control_event.payload["relay_transition"] == control["relay_transition"]

    state = service.build_marvis_relay_state(task.id)
    assert state.pending_interrupt is None
    assert state.current_node == "architect"
    assert control["marvis_relay_state"]["current_node"] == "architect"
    assert control["marvis_relay_state"]["pending_interrupt"] is None


def test_marvis_relay_state_rebuilds_historical_superseded_round(tmp_path) -> None:
    service, _provider, _db_path = _service(tmp_path)
    task = service.create_task(
        title="Relay rounds",
        prompt="Initial round",
        workspace="/repo",
        provider="claude",
    )

    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            _envelope(
                role="director",
                artifact_type="routing_decision",
                handoff_to="architect",
                summary="Route initial round.",
                next_action="draft plan",
                complexity="medium",
                risk="medium",
                route="core_relay",
                required_roles=["director", "architect", "implementer"],
                acceptance_criteria=["initial route is preserved"],
                stop_conditions=[],
                requires_user_approval=False,
            ),
            dispatch_next=False,
        )
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "architect",
            _envelope(
                status="waiting",
                role="architect",
                artifact_type="architecture_plan",
                handoff_to="",
                summary="Initial plan needs approval.",
                open_questions=["Approve initial plan?"],
                next_action="wait for approval",
            ),
            dispatch_next=False,
        )
    )
    asyncio.run(service.add_user_message(task.id, "Follow-up round"))

    assert service.get_task(task.id).current_round_id == 2
    historical = service.build_marvis_relay_state(task.id, round_id=1)
    current = service.build_marvis_relay_state(task.id)

    assert historical.round_id == 1
    assert historical.route == "core_relay"
    assert historical.required_roles == ["director", "architect", "implementer", "auditor"]
    assert historical.latest_user_input == "Initial round"
    assert historical.terminal_status == "superseded"
    assert historical.pending_interrupt is None
    assert all(int(artifact["round_id"]) == 1 for artifact in historical.artifacts)
    assert any(handoff["to_role"] == "architect" for handoff in historical.handoffs)
    assert current.round_id == 2
    assert current.latest_user_input == "Follow-up round"


def test_manual_interrupt_emits_transition_and_rebuildable_state(tmp_path) -> None:
    service, _provider, _db_path = _service(tmp_path)
    task = service.create_task(
        title="Relay interrupt",
        prompt="Stop me cleanly",
        workspace="/repo",
        provider="claude",
    )

    asyncio.run(service.interrupt(task.id))

    interrupted = [
        event for event in service.events_for_task(task.id) if event.event_type == "task.interrupted"
    ][-1]
    assert interrupted.payload["round_id"] == 1
    assert interrupted.payload["relay_transition"]["terminal"] == "interrupted"
    assert interrupted.payload["marvis_relay_state"]["terminal_status"] == "interrupted"
    assert service.build_marvis_relay_state(task.id).current_node == "interrupted"
