from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from uuid import uuid4

from wlcodex.collaboration.models import HandoffArtifact, HandoffIntent
from wlcodex.db import Ledger, _now


@dataclass(frozen=True)
class StoredWorkflowRun:
    workflow_run_id: str
    workflow_type: str
    status: str
    source_provider: str
    source_thread_id: str
    source_turn_id: str
    target_provider: str
    target_thread_id: str
    cwd: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class StoredHandoffPreview:
    preview_id: str
    workflow_run_id: str
    source_provider: str
    source_thread_id: str
    source_turn_id: str
    target_provider: str
    cwd: str
    intent: HandoffIntent
    prompt: str
    artifacts: list[HandoffArtifact]
    warnings: list[str]
    created_at: str


@dataclass(frozen=True)
class StoredWorkflowStep:
    step_id: str
    workflow_run_id: str
    preview_id: str
    step_type: str
    status: str
    assigned_provider: str
    target_thread_id: str
    target_agent_run_id: int
    submitted_prompt: str
    output_summary: str
    created_at: str
    updated_at: str


class WorkflowRunStore:
    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self._conn = ledger._conn

    def create_preview(
        self,
        *,
        source_provider: str,
        source_thread_id: str,
        source_turn_id: str,
        target_provider: str,
        cwd: str,
        intent: HandoffIntent,
        prompt: str,
        artifacts: list[HandoffArtifact],
        warnings: list[str],
    ) -> StoredHandoffPreview:
        now = _now()
        workflow_run_id = _new_id("wf")
        preview_id = _new_id("preview")
        self._conn.execute(
            """
            INSERT INTO collaboration_workflow_runs (
                workflow_run_id, workflow_type, status, source_provider,
                source_thread_id, source_turn_id, target_provider,
                target_thread_id, cwd, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow_run_id,
                "handoff_execution",
                "previewed",
                source_provider,
                source_thread_id,
                source_turn_id,
                target_provider,
                "",
                cwd,
                "{}",
                now,
                now,
            ),
        )
        self._conn.execute(
            """
            INSERT INTO collaboration_workflow_previews (
                preview_id, workflow_run_id, intent, target_provider, prompt,
                artifacts_json, warnings_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                preview_id,
                workflow_run_id,
                intent.value,
                target_provider,
                prompt,
                _artifacts_json(artifacts),
                json.dumps(warnings, ensure_ascii=False),
                now,
            ),
        )
        self._conn.commit()
        return self.get_preview(preview_id)

    def get_preview(self, preview_id: str) -> StoredHandoffPreview:
        row = self._conn.execute(
            """
            SELECT
                preview.preview_id,
                preview.workflow_run_id,
                run.source_provider,
                run.source_thread_id,
                run.source_turn_id,
                preview.target_provider,
                run.cwd,
                preview.intent,
                preview.prompt,
                preview.artifacts_json,
                preview.warnings_json,
                preview.created_at
            FROM collaboration_workflow_previews AS preview
            JOIN collaboration_workflow_runs AS run
                ON run.workflow_run_id = preview.workflow_run_id
            WHERE preview.preview_id = ?
            """,
            (preview_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown handoff preview id: {preview_id}")
        return StoredHandoffPreview(
            preview_id=str(row["preview_id"]),
            workflow_run_id=str(row["workflow_run_id"]),
            source_provider=str(row["source_provider"]),
            source_thread_id=str(row["source_thread_id"]),
            source_turn_id=str(row["source_turn_id"]),
            target_provider=str(row["target_provider"]),
            cwd=str(row["cwd"]),
            intent=HandoffIntent(str(row["intent"])),
            prompt=str(row["prompt"]),
            artifacts=_load_artifacts(row["artifacts_json"]),
            warnings=_load_string_list(row["warnings_json"]),
            created_at=str(row["created_at"]),
        )

    def get_run(self, workflow_run_id: str) -> StoredWorkflowRun:
        row = self._conn.execute(
            """
            SELECT * FROM collaboration_workflow_runs
            WHERE workflow_run_id = ?
            """,
            (workflow_run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown workflow run id: {workflow_run_id}")
        return _run(row)

    def record_execution(
        self,
        *,
        workflow_run_id: str,
        preview_id: str,
        target_provider: str,
        target_thread_id: str,
        target_agent_run_id: int,
        submitted_prompt: str,
        status: str,
    ) -> StoredWorkflowStep:
        self.get_run(workflow_run_id)
        self._verify_preview_belongs_to_run(
            preview_id=preview_id,
            workflow_run_id=workflow_run_id,
        )
        now = _now()
        step_id = _new_id("step")
        self._conn.execute(
            """
            UPDATE collaboration_workflow_runs
            SET status = ?, target_provider = ?, target_thread_id = ?, updated_at = ?
            WHERE workflow_run_id = ?
            """,
            (status, target_provider, target_thread_id, now, workflow_run_id),
        )
        self._conn.execute(
            """
            INSERT INTO collaboration_workflow_steps (
                step_id, workflow_run_id, preview_id, step_type, status,
                assigned_provider, target_thread_id, target_agent_run_id,
                submitted_prompt, output_summary, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                step_id,
                workflow_run_id,
                preview_id,
                "execute_handoff",
                status,
                target_provider,
                target_thread_id,
                target_agent_run_id,
                submitted_prompt,
                "",
                now,
                now,
            ),
        )
        self._conn.commit()
        return self.get_step(step_id)

    def get_step(self, step_id: str) -> StoredWorkflowStep:
        row = self._conn.execute(
            """
            SELECT * FROM collaboration_workflow_steps
            WHERE step_id = ?
            """,
            (step_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown workflow step id: {step_id}")
        return _step(row)

    def _verify_preview_belongs_to_run(
        self,
        *,
        preview_id: str,
        workflow_run_id: str,
    ) -> None:
        row = self._conn.execute(
            """
            SELECT workflow_run_id FROM collaboration_workflow_previews
            WHERE preview_id = ?
            """,
            (preview_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown handoff preview id: {preview_id}")
        preview_run_id = str(row["workflow_run_id"])
        if preview_run_id != workflow_run_id:
            raise ValueError(
                f"handoff preview {preview_id} does not belong to workflow run "
                f"{workflow_run_id}"
            )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _artifacts_json(artifacts: list[HandoffArtifact]) -> str:
    return json.dumps(
        [artifact.to_json_dict() for artifact in artifacts],
        ensure_ascii=False,
        sort_keys=True,
    )


def _load_artifacts(raw: object) -> list[HandoffArtifact]:
    loaded = json.loads(str(raw or "[]"))
    if not isinstance(loaded, list):
        return []
    artifacts = []
    for item in loaded:
        if not isinstance(item, dict):
            continue
        artifacts.append(
            HandoffArtifact(
                kind=str(item.get("kind") or ""),
                path=str(item.get("path") or ""),
                title=str(item.get("title") or ""),
                source=str(item.get("source") or ""),
                confidence=str(item.get("confidence") or "medium"),
            )
        )
    return artifacts


def _load_string_list(raw: object) -> list[str]:
    loaded = json.loads(str(raw or "[]"))
    if not isinstance(loaded, list):
        return []
    return [str(item) for item in loaded]


def _run(row: Any) -> StoredWorkflowRun:
    return StoredWorkflowRun(
        workflow_run_id=str(row["workflow_run_id"]),
        workflow_type=str(row["workflow_type"]),
        status=str(row["status"]),
        source_provider=str(row["source_provider"]),
        source_thread_id=str(row["source_thread_id"]),
        source_turn_id=str(row["source_turn_id"]),
        target_provider=str(row["target_provider"]),
        target_thread_id=str(row["target_thread_id"]),
        cwd=str(row["cwd"]),
        metadata=json.loads(str(row["metadata_json"] or "{}")),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _step(row: Any) -> StoredWorkflowStep:
    return StoredWorkflowStep(
        step_id=str(row["step_id"]),
        workflow_run_id=str(row["workflow_run_id"]),
        preview_id=str(row["preview_id"]),
        step_type=str(row["step_type"]),
        status=str(row["status"]),
        assigned_provider=str(row["assigned_provider"]),
        target_thread_id=str(row["target_thread_id"]),
        target_agent_run_id=int(row["target_agent_run_id"]),
        submitted_prompt=str(row["submitted_prompt"]),
        output_summary=str(row["output_summary"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
