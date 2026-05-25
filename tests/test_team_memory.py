from datetime import UTC, datetime

from wlcodex.team_memory import InstinctMemory, select_relevant_instincts


def test_select_relevant_instincts_prefers_scope_role_and_confidence() -> None:
    now = datetime(2026, 5, 25, tzinfo=UTC)
    instincts = (
        InstinctMemory(
            instinct_id="project-audit",
            scope="project",
            workspace_alias="wlcodex",
            role="auditor",
            domain="audit",
            trigger="verification after implementation diff",
            action="Check changed files and missing test evidence before passing.",
            confidence=0.84,
            evidence_refs=("audit_report=12",),
            status="active",
            created_at=now,
            last_validated_at=now,
        ),
        InstinctMemory(
            instinct_id="global-debug",
            scope="global",
            workspace_alias=None,
            role="investigator",
            domain="debugging",
            trigger="logs show crash loop",
            action="Collect process status before editing code.",
            confidence=0.92,
            evidence_refs=("observation=9",),
            status="active",
            created_at=now,
            last_validated_at=now,
        ),
        InstinctMemory(
            instinct_id="weak-audit",
            scope="project",
            workspace_alias="wlcodex",
            role="auditor",
            domain="audit",
            trigger="diff review",
            action="Ask for broad manual review.",
            confidence=0.2,
            evidence_refs=("observation=2",),
            status="active",
            created_at=now,
            last_validated_at=now,
        ),
    )

    selected = select_relevant_instincts(
        instincts,
        workspace_alias="wlcodex",
        role="auditor",
        task_text="verification after implementation diff",
        limit=2,
        min_confidence=0.6,
    )

    assert [instinct.instinct_id for instinct in selected] == ["project-audit"]


def test_select_relevant_instincts_marks_memory_as_historical_advice() -> None:
    now = datetime(2026, 5, 25, tzinfo=UTC)
    instinct = InstinctMemory(
        instinct_id="project-context",
        scope="project",
        workspace_alias="wlcodex",
        role="architect",
        domain="architecture",
        trigger="staged auto plan",
        action="Keep staged-auto compatibility columns populated.",
        confidence=0.77,
        evidence_refs=("spec=adaptive-team",),
        status="active",
        created_at=now,
        last_validated_at=now,
    )
    selected = select_relevant_instincts(
        (instinct,),
        workspace_alias="wlcodex",
        role="architect",
        task_text="staged auto plan",
        limit=1,
        min_confidence=0.6,
    )

    assert (
        selected[0].as_packet_item()["precedence"]
        == "historical_advice_current_evidence_wins"
    )
