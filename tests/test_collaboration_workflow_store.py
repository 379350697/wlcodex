from __future__ import annotations

from wlcodex.collaboration.models import HandoffArtifact, HandoffIntent
from wlcodex.collaboration.workflow_store import WorkflowRunStore
from wlcodex.db import Ledger


def _store(tmp_path):
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    return WorkflowRunStore(ledger)


def test_create_preview_persists_source_target_and_prompt(tmp_path) -> None:
    store = _store(tmp_path)

    preview = store.create_preview(
        source_provider="antigravity",
        source_thread_id="source-1",
        source_turn_id="turn-1",
        target_provider="claude",
        cwd="/repo",
        intent=HandoffIntent.EXECUTE_PLAN,
        prompt="Read the plan and execute it.",
        artifacts=[
            HandoffArtifact(kind="spec", path="docs/superpowers/specs/a.md"),
            HandoffArtifact(kind="plan", path="docs/superpowers/plans/a.md"),
        ],
        warnings=[],
    )

    loaded = store.get_preview(preview.preview_id)

    assert loaded.workflow_run_id == preview.workflow_run_id
    assert loaded.source_provider == "antigravity"
    assert loaded.source_thread_id == "source-1"
    assert loaded.target_provider == "claude"
    assert loaded.intent == HandoffIntent.EXECUTE_PLAN
    assert loaded.prompt == "Read the plan and execute it."
    assert [artifact.kind for artifact in loaded.artifacts] == ["spec", "plan"]


def test_record_execution_links_target_session(tmp_path) -> None:
    store = _store(tmp_path)
    preview = store.create_preview(
        source_provider="codex",
        source_thread_id="source-2",
        source_turn_id="",
        target_provider="antigravity",
        cwd="/repo",
        intent=HandoffIntent.FIX_BUG,
        prompt="Fix the bug.",
        artifacts=[],
        warnings=["source turn is still running"],
    )

    step = store.record_execution(
        workflow_run_id=preview.workflow_run_id,
        preview_id=preview.preview_id,
        target_provider="antigravity",
        target_thread_id="target-2",
        target_agent_run_id=42,
        submitted_prompt="Edited bug prompt.",
        status="running",
    )

    loaded_step = store.get_step(step.step_id)
    loaded_run = store.get_run(preview.workflow_run_id)

    assert loaded_step.target_thread_id == "target-2"
    assert loaded_step.target_agent_run_id == 42
    assert loaded_step.submitted_prompt == "Edited bug prompt."
    assert loaded_run.status == "running"
    assert loaded_run.target_provider == "antigravity"
    assert loaded_run.target_thread_id == "target-2"
