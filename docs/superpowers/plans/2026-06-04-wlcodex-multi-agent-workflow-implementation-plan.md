# WLCodex Multi-Agent Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first reusable multi-agent workflow foundation and ship "接棒执行" as the first workflow: preview a handoff prompt, let the user edit it, then start a new target provider session.

**Architecture:** Add a `wlcodex.collaboration` package above `NativeAgentRegistry`. The workflow layer owns workflow concepts, intent detection, prompt previews, persistent source-target links, and execution orchestration; provider implementations remain unaware and only receive normal native provider calls. The live UI adds a small handoff panel that calls workflow preview and execute routes.

**Tech Stack:** Python dataclasses, SQLite through existing `Ledger`, async route handlers in `wlcodex/live_stream/server.py`, vanilla HTML/CSS/JS embedded in the live page template, pytest, ruff, GitNexus CLI.

---

## Spec Source

Design spec:

```text
docs/superpowers/specs/2026-06-04-wlcodex-multi-agent-workflow-design.md
```

## Pre-Implementation Rules

Before editing an existing function, class, or method, run GitNexus impact for that symbol:

```bash
npx gitnexus impact WorkerLiveStreamServer --repo wlcodex --direction upstream --include-tests
npx gitnexus impact _handle_client --repo wlcodex --direction upstream --include-tests
npx gitnexus impact _handle_native_agent_route --repo wlcodex --direction upstream --include-tests
npx gitnexus impact _create_live_stream_components --repo wlcodex --direction upstream --include-tests
npx gitnexus impact Ledger.migrate --repo wlcodex --direction upstream --include-tests
```

Report any HIGH or CRITICAL blast radius before editing that symbol.

Before every commit, run:

```bash
npx gitnexus detect-changes -r wlcodex
```

## File Structure

Create:

- `wlcodex/collaboration/__init__.py`  
  Public exports for workflow models, prompt builder, store, and service.

- `wlcodex/collaboration/models.py`  
  Dataclasses and constants for workflow runs, steps, artifacts, preview input, and preview output.

- `wlcodex/collaboration/handoff_prompts.py`  
  Intent detection, artifact detection, transcript compaction, and provider-neutral prompt generation.

- `wlcodex/collaboration/workflow_store.py`  
  SQLite-backed workflow run, preview, and step persistence.

- `wlcodex/collaboration/workflow_service.py`  
  Async orchestration service that reads the source provider, builds previews, and starts the target provider session.

- `tests/test_collaboration_handoff_prompts.py`  
  Unit tests for intent detection and prompt generation.

- `tests/test_collaboration_workflow_store.py`  
  Unit tests for workflow persistence.

- `tests/test_collaboration_workflow_service.py`  
  Unit tests for provider-neutral preview and execution.

Modify:

- `wlcodex/db.py`  
  Add workflow tables in `Ledger.migrate()`.

- `wlcodex/live_stream/server.py`  
  Add workflow service injection, workflow routes, and live UI handoff panel.

- `wlcodex/main.py`  
  Wire `WorkflowRunStore` and `WorkflowService` when native providers are enabled.

- `tests/test_worker_live_stream_native_agent_routes.py`  
  Add workflow preview and execute route coverage.

- `tests/test_main_composition.py`  
  Confirm live stream composition wires workflow service.

## Task 1: Prompt Models And Handoff Prompt Builder

**Files:**

- Create: `wlcodex/collaboration/__init__.py`
- Create: `wlcodex/collaboration/models.py`
- Create: `wlcodex/collaboration/handoff_prompts.py`
- Test: `tests/test_collaboration_handoff_prompts.py`

- [ ] **Step 1: Write failing prompt builder tests**

Create `tests/test_collaboration_handoff_prompts.py`:

```python
from __future__ import annotations

from wlcodex.collaboration.handoff_prompts import (
    build_handoff_preview,
    detect_handoff_intent,
)
from wlcodex.collaboration.models import (
    HandoffArtifact,
    HandoffPreviewInput,
    HandoffIntent,
)


def test_detects_execute_plan_when_spec_and_plan_artifacts_exist() -> None:
    artifacts = [
        HandoffArtifact(kind="spec", path="docs/superpowers/specs/feature.md"),
        HandoffArtifact(kind="plan", path="docs/superpowers/plans/feature.md"),
    ]

    result = detect_handoff_intent(
        recent_user_text="继续",
        session_summary="Antigravity drafted the plan.",
        artifacts=artifacts,
    )

    assert result.intent == HandoffIntent.EXECUTE_PLAN
    assert result.confidence == "high"
    assert "spec" in result.reason.lower()
    assert "plan" in result.reason.lower()


def test_detects_fix_bug_from_error_context() -> None:
    result = detect_handoff_intent(
        recent_user_text="继续的时候出现 NotImplementedError",
        session_summary="The user reports a native provider regression.",
        artifacts=[],
    )

    assert result.intent == HandoffIntent.FIX_BUG
    assert result.confidence == "high"
    assert "bug" in result.reason.lower() or "error" in result.reason.lower()


def test_detects_implement_feature_without_formal_artifacts() -> None:
    result = detect_handoff_intent(
        recent_user_text="新增一个接棒执行按钮",
        session_summary="No spec or plan files were detected.",
        artifacts=[],
    )

    assert result.intent == HandoffIntent.IMPLEMENT_FEATURE
    assert result.confidence in {"medium", "high"}


def test_execute_plan_prompt_references_paths_and_says_execute_not_rewrite() -> None:
    preview = build_handoff_preview(
        HandoffPreviewInput(
            source_provider="antigravity",
            source_thread_id="source-session",
            target_provider="claude",
            cwd="/Users/wl/projects/wlcodex",
            recent_user_text="让 Claude 执行计划",
            session_summary="Antigravity produced a spec and implementation plan.",
            artifacts=[
                HandoffArtifact(
                    kind="spec",
                    path="docs/superpowers/specs/2026-06-04-feature.md",
                ),
                HandoffArtifact(
                    kind="plan",
                    path="docs/superpowers/plans/2026-06-04-feature.md",
                ),
            ],
        )
    )

    assert preview.intent == HandoffIntent.EXECUTE_PLAN
    assert "docs/superpowers/specs/2026-06-04-feature.md" in preview.prompt
    assert "docs/superpowers/plans/2026-06-04-feature.md" in preview.prompt
    assert "execute the plan" in preview.prompt.lower()
    assert "do not rewrite the plan" in preview.prompt.lower()
    assert "/Users/wl/projects/wlcodex" in preview.prompt


def test_bug_prompt_requires_evidence_before_fix() -> None:
    preview = build_handoff_preview(
        HandoffPreviewInput(
            source_provider="codex",
            source_thread_id="bug-source",
            target_provider="antigravity",
            cwd="/repo",
            recent_user_text="按钮状态还是不一致，有 bug",
            session_summary="The source session mentions a UI state mismatch.",
            artifacts=[],
        )
    )

    assert preview.intent == HandoffIntent.FIX_BUG
    assert "reproduce or inspect evidence first" in preview.prompt.lower()
    assert "root cause" in preview.prompt.lower()
    assert "preserve unrelated changes" in preview.prompt.lower()


def test_feature_prompt_keeps_scope_narrow() -> None:
    preview = build_handoff_preview(
        HandoffPreviewInput(
            source_provider="claude",
            source_thread_id="feature-source",
            target_provider="codex",
            cwd="/repo",
            recent_user_text="加一个工作流状态入口",
            session_summary="No formal documents exist.",
            artifacts=[],
        )
    )

    assert preview.intent == HandoffIntent.IMPLEMENT_FEATURE
    assert "inspect existing code patterns first" in preview.prompt.lower()
    assert "keep scope narrow" in preview.prompt.lower()


def test_prompt_builder_trims_raw_transcript_and_keeps_latest_request() -> None:
    long_transcript = "old assistant output " * 600
    preview = build_handoff_preview(
        HandoffPreviewInput(
            source_provider="antigravity",
            source_thread_id="source-session",
            target_provider="claude",
            cwd="/repo",
            recent_user_text="最新请求：只执行新内容",
            session_summary=long_transcript,
            artifacts=[],
        )
    )

    assert "最新请求：只执行新内容" in preview.prompt
    assert len(preview.prompt) < len(long_transcript)
    assert preview.prompt.count("old assistant output") < 40
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_collaboration_handoff_prompts.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'wlcodex.collaboration'`.

- [ ] **Step 3: Implement models and prompt builder**

Create `wlcodex/collaboration/models.py` with these public names:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class HandoffIntent(StrEnum):
    AUTO = "auto"
    EXECUTE_PLAN = "execute_plan"
    FIX_BUG = "fix_bug"
    IMPLEMENT_FEATURE = "implement_feature"
    CONTINUE_WORK = "continue_work"
    CUSTOM = "custom"


@dataclass(frozen=True)
class HandoffArtifact:
    kind: str
    path: str
    title: str = ""
    source: str = ""
    confidence: str = "medium"

    def to_json_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "path": self.path,
            "title": self.title,
            "source": self.source,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class IntentDetectionResult:
    intent: HandoffIntent
    confidence: str
    reason: str


@dataclass(frozen=True)
class HandoffPreviewInput:
    source_provider: str
    source_thread_id: str
    target_provider: str
    cwd: str
    recent_user_text: str = ""
    session_summary: str = ""
    artifacts: list[HandoffArtifact] = field(default_factory=list)
    user_note: str = ""
    requested_intent: HandoffIntent = HandoffIntent.AUTO


@dataclass(frozen=True)
class HandoffPromptPreview:
    intent: HandoffIntent
    target_provider: str
    prompt: str
    artifacts: list[HandoffArtifact] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reason: str = ""

    def to_json_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent.value,
            "target_provider": self.target_provider,
            "prompt": self.prompt,
            "artifacts": [artifact.to_json_dict() for artifact in self.artifacts],
            "warnings": list(self.warnings),
            "reason": self.reason,
        }
```

Create `wlcodex/collaboration/handoff_prompts.py` with deterministic rule-based logic. Keep the implementation provider-neutral: no imports from `wlcodex.native_agents.*`.

The implementation must include:

```python
BUG_MARKERS = (
    "bug",
    "error",
    "failure",
    "failed",
    "regression",
    "unexpected",
    "traceback",
    "stack trace",
    "notimplementederror",
    "报错",
    "失败",
    "问题",
    "不一致",
)

FEATURE_MARKERS = (
    "add",
    "build",
    "implement",
    "change",
    "新增",
    "加一个",
    "实现",
    "改成",
)

MAX_SUMMARY_CHARS = 1200
```

Use this behavior:

```python
def detect_handoff_intent(
    *,
    recent_user_text: str,
    session_summary: str,
    artifacts: list[HandoffArtifact],
) -> IntentDetectionResult:
    kinds = {artifact.kind.lower() for artifact in artifacts}
    paths = " ".join(artifact.path.lower() for artifact in artifacts)
    text = f"{recent_user_text}\n{session_summary}".lower()
    if {"spec", "plan"}.issubset(kinds) or (
        "docs/superpowers/specs/" in paths and "docs/superpowers/plans/" in paths
    ):
        return IntentDetectionResult(
            HandoffIntent.EXECUTE_PLAN,
            "high",
            "Spec and plan artifacts were detected.",
        )
    if any(marker in text for marker in BUG_MARKERS):
        return IntentDetectionResult(
            HandoffIntent.FIX_BUG,
            "high",
            "Bug or error language was detected.",
        )
    if any(marker in text for marker in FEATURE_MARKERS):
        return IntentDetectionResult(
            HandoffIntent.IMPLEMENT_FEATURE,
            "medium",
            "Feature implementation language was detected.",
        )
    return IntentDetectionResult(
        HandoffIntent.CONTINUE_WORK,
        "low",
        "No specific plan, bug, or feature signal was detected.",
    )
```

Create separate private template functions for `execute_plan`, `fix_bug`, `implement_feature`, `continue_work`, and `custom`. Each template must include workspace, source provider/thread, target provider, newest user request, artifact paths, and a final-summary requirement.

Create `wlcodex/collaboration/__init__.py`:

```python
from wlcodex.collaboration.handoff_prompts import (
    build_handoff_preview,
    detect_handoff_intent,
)
from wlcodex.collaboration.models import (
    HandoffArtifact,
    HandoffIntent,
    HandoffPreviewInput,
    HandoffPromptPreview,
    IntentDetectionResult,
)

__all__ = [
    "HandoffArtifact",
    "HandoffIntent",
    "HandoffPreviewInput",
    "HandoffPromptPreview",
    "IntentDetectionResult",
    "build_handoff_preview",
    "detect_handoff_intent",
]
```

- [ ] **Step 4: Run prompt builder tests to verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_collaboration_handoff_prompts.py -q
```

Expected: all tests in `tests/test_collaboration_handoff_prompts.py` PASS.

- [ ] **Step 5: Run formatting check for created files**

Run:

```bash
.venv/bin/python -m ruff check wlcodex/collaboration tests/test_collaboration_handoff_prompts.py
```

Expected: PASS with no lint errors.

- [ ] **Step 6: Run GitNexus detect changes**

Run:

```bash
npx gitnexus detect-changes -r wlcodex
```

Expected: changes are limited to new collaboration prompt builder symbols. If GitNexus reports HIGH or CRITICAL risk, stop and report it.

- [ ] **Step 7: Commit Task 1**

Run:

```bash
git add wlcodex/collaboration/__init__.py wlcodex/collaboration/models.py wlcodex/collaboration/handoff_prompts.py tests/test_collaboration_handoff_prompts.py
git commit -m "feat: add handoff prompt builder"
```

## Task 2: Workflow Store And SQLite Schema

**Files:**

- Modify: `wlcodex/db.py`
- Create: `wlcodex/collaboration/workflow_store.py`
- Test: `tests/test_collaboration_workflow_store.py`

- [ ] **Step 1: Run GitNexus impact for database migration**

Run:

```bash
npx gitnexus impact Ledger.migrate --repo wlcodex --direction upstream --include-tests
```

Expected: review the blast radius. If risk is HIGH or CRITICAL, report it before editing `wlcodex/db.py`.

- [ ] **Step 2: Write failing workflow store tests**

Create `tests/test_collaboration_workflow_store.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_collaboration_workflow_store.py -q
```

Expected: FAIL because `wlcodex.collaboration.workflow_store` does not exist or the new tables do not exist.

- [ ] **Step 4: Add workflow tables to `Ledger.migrate()`**

Modify `wlcodex/db.py` inside the existing `executescript` migration block near `native_agent_sessions`. Add these tables and indexes:

```sql
CREATE TABLE IF NOT EXISTS collaboration_workflow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_run_id TEXT NOT NULL UNIQUE,
    workflow_type TEXT NOT NULL,
    status TEXT NOT NULL,
    source_provider TEXT NOT NULL,
    source_thread_id TEXT NOT NULL,
    source_turn_id TEXT NOT NULL DEFAULT '',
    target_provider TEXT NOT NULL DEFAULT '',
    target_thread_id TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_collaboration_workflow_runs_source
    ON collaboration_workflow_runs(source_provider, source_thread_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_collaboration_workflow_runs_target
    ON collaboration_workflow_runs(target_provider, target_thread_id, id DESC);

CREATE TABLE IF NOT EXISTS collaboration_workflow_previews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    preview_id TEXT NOT NULL UNIQUE,
    workflow_run_id TEXT NOT NULL,
    intent TEXT NOT NULL,
    target_provider TEXT NOT NULL,
    prompt TEXT NOT NULL,
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY(workflow_run_id) REFERENCES collaboration_workflow_runs(workflow_run_id)
);

CREATE INDEX IF NOT EXISTS idx_collaboration_workflow_previews_run
    ON collaboration_workflow_previews(workflow_run_id, id DESC);

CREATE TABLE IF NOT EXISTS collaboration_workflow_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    step_id TEXT NOT NULL UNIQUE,
    workflow_run_id TEXT NOT NULL,
    preview_id TEXT NOT NULL DEFAULT '',
    step_type TEXT NOT NULL,
    status TEXT NOT NULL,
    assigned_provider TEXT NOT NULL,
    target_thread_id TEXT NOT NULL DEFAULT '',
    target_agent_run_id INTEGER NOT NULL DEFAULT 0,
    submitted_prompt TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(workflow_run_id) REFERENCES collaboration_workflow_runs(workflow_run_id)
);

CREATE INDEX IF NOT EXISTS idx_collaboration_workflow_steps_run
    ON collaboration_workflow_steps(workflow_run_id, id);
```

- [ ] **Step 5: Implement workflow store**

Create `wlcodex/collaboration/workflow_store.py`. It must:

- accept `Ledger` in `WorkflowRunStore.__init__`;
- use `ledger._conn` like `NativeAgentSessionStore`;
- use `wlcodex.db._now`;
- generate ids with `uuid4().hex`;
- serialize artifacts with `artifact.to_json_dict()`;
- deserialize artifacts back to `HandoffArtifact`;
- provide `create_preview`, `get_preview`, `get_run`, `record_execution`, and `get_step`.

Use dataclasses in this file:

```python
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
```

- [ ] **Step 6: Run workflow store tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_collaboration_workflow_store.py -q
```

Expected: all workflow store tests PASS.

- [ ] **Step 7: Run migration-adjacent tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_native_agent_session_store.py tests/test_collaboration_workflow_store.py -q
```

Expected: both test files PASS.

- [ ] **Step 8: Run ruff**

Run:

```bash
.venv/bin/python -m ruff check wlcodex/db.py wlcodex/collaboration/workflow_store.py tests/test_collaboration_workflow_store.py
```

Expected: PASS with no lint errors.

- [ ] **Step 9: Run GitNexus detect changes**

Run:

```bash
npx gitnexus detect-changes -r wlcodex
```

Expected: changes include `Ledger.migrate` and the new workflow store only. If GitNexus reports HIGH or CRITICAL risk, stop and report it.

- [ ] **Step 10: Commit Task 2**

Run:

```bash
git add wlcodex/db.py wlcodex/collaboration/workflow_store.py tests/test_collaboration_workflow_store.py
git commit -m "feat: persist collaboration workflow handoffs"
```

## Task 3: Workflow Service

**Files:**

- Create: `wlcodex/collaboration/workflow_service.py`
- Modify: `wlcodex/collaboration/__init__.py`
- Test: `tests/test_collaboration_workflow_service.py`

- [ ] **Step 1: Write failing workflow service tests**

Create `tests/test_collaboration_workflow_service.py`:

```python
from __future__ import annotations

import pytest

from wlcodex.collaboration.workflow_service import WorkflowService
from wlcodex.collaboration.workflow_store import WorkflowRunStore
from wlcodex.db import Ledger
from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentStatus,
)
from wlcodex.native_agents.provider import NativeAgentRegistry


class ServiceFakeProvider:
    def __init__(
        self,
        provider: str,
        *,
        can_start_session: bool = True,
        session_payload: dict | None = None,
    ) -> None:
        self.provider = provider
        self.provider_engine = "fake"
        self.can_start_session = can_start_session
        self.session_payload = session_payload or {
            "turns": [
                {
                    "role": "user",
                    "content": "请实现这个小功能",
                },
                {
                    "role": "assistant",
                    "content": "Spec: docs/superpowers/specs/a.md\nPlan: docs/superpowers/plans/a.md",
                },
            ]
        }
        self.calls: list[tuple] = []

    async def status(self):
        return NativeAgentStatus(
            provider=self.provider,
            provider_engine=self.provider_engine,
            enabled=True,
            connected=True,
            status_code="ok",
        )

    def capabilities(self):
        self.calls.append(("capabilities",))
        return NativeAgentCapabilities(can_start_session=self.can_start_session)

    async def list_sessions(self, limit: int = 50):
        return []

    async def list_models(self):
        return []

    async def start_session(self, cwd: str, prompt: str, **kwargs):
        self.calls.append(("start_session", cwd, prompt, kwargs))
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=f"{self.provider}-target-session",
            agent_run_id=128,
            turn_id="turn-1",
            turn_running=True,
            status="started",
        )

    async def create_session(self, cwd: str, **kwargs):
        raise AssertionError("create_session should not be used for execute handoff")

    async def read_session(self, native_session_id: str):
        self.calls.append(("read_session", native_session_id))
        return self.session_payload

    async def attach_session(self, native_session_id: str):
        raise NotImplementedError

    async def sync_session(self, native_session_id: str):
        raise NotImplementedError

    async def continue_session(self, native_session_id: str, prompt: str, **kwargs):
        raise NotImplementedError

    async def steer_session(
        self,
        native_session_id: str,
        expected_turn_id: str,
        prompt: str,
        **kwargs,
    ):
        raise NotImplementedError

    async def interrupt_session(self, native_session_id: str, turn_id: str = ""):
        raise NotImplementedError

    async def resolve_approval(self, request_id: str, body: dict):
        raise NotImplementedError


def _service(tmp_path, providers):
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    return WorkflowService(
        registry=NativeAgentRegistry(providers),
        store=WorkflowRunStore(ledger),
        default_worker_id=42,
    )


@pytest.mark.asyncio
async def test_preview_reads_source_and_does_not_start_target(tmp_path) -> None:
    source = ServiceFakeProvider("antigravity")
    target = ServiceFakeProvider("claude")
    service = _service(tmp_path, [source, target])

    preview = await service.preview_handoff(
        source_provider="antigravity",
        source_thread_id="source-session",
        source_turn_id="",
        target_provider="claude",
        cwd="/repo",
        intent="auto",
        user_note="",
    )

    assert preview["intent"] == "execute_plan"
    assert preview["target_provider"] == "claude"
    assert preview["workflow_run_id"].startswith("wf_")
    assert preview["preview_id"].startswith("preview_")
    assert "docs/superpowers/specs/a.md" in preview["prompt"]
    assert ("read_session", "source-session") in source.calls
    assert not any(call[0] == "start_session" for call in target.calls)


@pytest.mark.asyncio
async def test_execute_handoff_uses_edited_prompt_and_returns_target_url(tmp_path) -> None:
    source = ServiceFakeProvider("codex")
    target = ServiceFakeProvider("claude")
    service = _service(tmp_path, [source, target])
    preview = await service.preview_handoff(
        source_provider="codex",
        source_thread_id="source-session",
        source_turn_id="",
        target_provider="claude",
        cwd="/repo",
        intent="auto",
        user_note="",
    )

    result = await service.execute_handoff(
        workflow_run_id=preview["workflow_run_id"],
        preview_id=preview["preview_id"],
        target_provider="claude",
        cwd="/repo",
        prompt="Edited handoff prompt.",
    )

    assert result["status"] == "running"
    assert result["target_provider"] == "claude"
    assert result["target_thread_id"] == "claude-target-session"
    assert result["target_url"] == (
        "/workers/128/live?native_provider=claude"
        "&native_thread_id=claude-target-session"
    )
    assert target.calls[-1][0:3] == ("start_session", "/repo", "Edited handoff prompt.")


@pytest.mark.asyncio
async def test_execute_rejects_target_without_start_capability(tmp_path) -> None:
    source = ServiceFakeProvider("codex")
    target = ServiceFakeProvider("antigravity", can_start_session=False)
    service = _service(tmp_path, [source, target])
    preview = await service.preview_handoff(
        source_provider="codex",
        source_thread_id="source-session",
        source_turn_id="",
        target_provider="antigravity",
        cwd="/repo",
        intent="auto",
        user_note="",
    )

    with pytest.raises(ValueError, match="cannot start sessions"):
        await service.execute_handoff(
            workflow_run_id=preview["workflow_run_id"],
            preview_id=preview["preview_id"],
            target_provider="antigravity",
            cwd="/repo",
            prompt=preview["prompt"],
        )
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_collaboration_workflow_service.py -q
```

Expected: FAIL because `wlcodex.collaboration.workflow_service` does not exist.

- [ ] **Step 3: Implement workflow service**

Create `wlcodex/collaboration/workflow_service.py`. It must:

- accept `NativeAgentRegistry`, `WorkflowRunStore`, and `default_worker_id`;
- validate source and target providers through `registry.get`;
- call `source.read_session(source_thread_id)` during preview;
- extract the newest user text and artifact paths from the normalized session payload;
- call `build_handoff_preview`;
- persist the preview via `WorkflowRunStore.create_preview`;
- return JSON-ready dicts;
- call `target.capabilities()` and require `can_start_session` during execute;
- call `target.start_session(cwd, prompt)`;
- persist execution with `WorkflowRunStore.record_execution`;
- build `target_url` from `agent_run_id`, `target_provider`, and `native_session_id`.

Use these helper rules:

```python
def _session_turns(session: dict[str, Any]) -> list[dict[str, Any]]:
    raw_turns = session.get("turns", [])
    return [turn for turn in raw_turns if isinstance(turn, dict)]


def _turn_text(turn: dict[str, Any]) -> str:
    content = turn.get("content", turn.get("text", ""))
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""
```

Artifact extraction must recognize paths matching:

```text
docs/superpowers/specs/
docs/superpowers/plans/
docs/bugs/
```

Map them to artifact kinds:

```text
spec
plan
bug_report
```

- [ ] **Step 4: Export workflow service names**

Modify `wlcodex/collaboration/__init__.py` to export:

```python
from wlcodex.collaboration.workflow_service import WorkflowService
from wlcodex.collaboration.workflow_store import WorkflowRunStore
```

Also add both names to `__all__`.

- [ ] **Step 5: Run workflow service tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_collaboration_workflow_service.py tests/test_collaboration_handoff_prompts.py tests/test_collaboration_workflow_store.py -q
```

Expected: all three test files PASS.

- [ ] **Step 6: Run ruff**

Run:

```bash
.venv/bin/python -m ruff check wlcodex/collaboration tests/test_collaboration_workflow_service.py
```

Expected: PASS with no lint errors.

- [ ] **Step 7: Run GitNexus detect changes**

Run:

```bash
npx gitnexus detect-changes -r wlcodex
```

Expected: changes are limited to the collaboration service and exports. If GitNexus reports HIGH or CRITICAL risk, stop and report it.

- [ ] **Step 8: Commit Task 3**

Run:

```bash
git add wlcodex/collaboration/__init__.py wlcodex/collaboration/workflow_service.py tests/test_collaboration_workflow_service.py
git commit -m "feat: add collaboration workflow service"
```

## Task 4: Workflow Routes

**Files:**

- Modify: `wlcodex/live_stream/server.py`
- Modify: `tests/test_worker_live_stream_native_agent_routes.py`

- [ ] **Step 1: Run GitNexus impact for route changes**

Run:

```bash
npx gitnexus impact WorkerLiveStreamServer --repo wlcodex --direction upstream --include-tests
npx gitnexus impact _handle_client --repo wlcodex --direction upstream --include-tests
npx gitnexus impact _handle_native_agent_route --repo wlcodex --direction upstream --include-tests
```

Expected: review blast radius. If risk is HIGH or CRITICAL, report it before editing `wlcodex/live_stream/server.py`.

- [ ] **Step 2: Add failing route tests**

Append these tests to `tests/test_worker_live_stream_native_agent_routes.py`:

```python
class FakeWorkflowService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def preview_handoff(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("preview_handoff", kwargs))
        return {
            "workflow_run_id": "wf_test",
            "preview_id": "preview_test",
            "intent": "execute_plan",
            "target_provider": kwargs["target_provider"],
            "prompt": "Read the plan and execute it.",
            "artifacts": [{"kind": "plan", "path": "docs/superpowers/plans/a.md"}],
            "warnings": [],
        }

    async def execute_handoff(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("execute_handoff", kwargs))
        return {
            "workflow_run_id": kwargs["workflow_run_id"],
            "step_id": "step_test",
            "target_provider": kwargs["target_provider"],
            "target_thread_id": "target-session",
            "target_url": (
                "/workers/128/live?native_provider=claude"
                "&native_thread_id=target-session"
            ),
            "status": "running",
        }


async def _request_native_agent_with_workflow(
    tmp_path: Path,
    request: str,
    *,
    workflow_service: FakeWorkflowService | None = None,
) -> tuple[str, FakeWorkflowService]:
    fake_workflow = workflow_service or FakeWorkflowService()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(_store(tmp_path)),
        native_registry=NativeAgentRegistry([FakeProvider()]),
        workflow_service=fake_workflow,
    )
    await server.start()
    try:
        response = await _read_response(server.host, server.port, request)
    finally:
        await server.stop()
    return response, fake_workflow


@pytest.mark.asyncio
async def test_workflow_handoff_preview_route(tmp_path: Path) -> None:
    body = json.dumps(
        {
            "source_provider": "antigravity",
            "source_thread_id": "source-session",
            "target_provider": "claude",
            "cwd": "/repo",
            "intent": "auto",
            "user_note": "",
        }
    )

    response, workflow = await _request_native_agent_with_workflow(
        tmp_path,
        "POST /api/native/workflows/handoffs/preview HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["workflow_run_id"] == "wf_test"
    assert payload["intent"] == "execute_plan"
    assert payload["target_provider"] == "claude"
    assert workflow.calls == [
        (
            "preview_handoff",
            {
                "source_provider": "antigravity",
                "source_thread_id": "source-session",
                "source_turn_id": "",
                "target_provider": "claude",
                "cwd": "/repo",
                "intent": "auto",
                "user_note": "",
            },
        )
    ]


@pytest.mark.asyncio
async def test_workflow_handoff_execute_route(tmp_path: Path) -> None:
    body = json.dumps(
        {
            "workflow_run_id": "wf_test",
            "preview_id": "preview_test",
            "target_provider": "claude",
            "cwd": "/repo",
            "prompt": "Edited prompt.",
        }
    )

    response, workflow = await _request_native_agent_with_workflow(
        tmp_path,
        "POST /api/native/workflows/handoffs/execute HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["target_thread_id"] == "target-session"
    assert payload["target_url"].endswith("native_thread_id=target-session")
    assert workflow.calls == [
        (
            "execute_handoff",
            {
                "workflow_run_id": "wf_test",
                "preview_id": "preview_test",
                "target_provider": "claude",
                "cwd": "/repo",
                "prompt": "Edited prompt.",
            },
        )
    ]
```

- [ ] **Step 3: Run route tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_live_stream_native_agent_routes.py::test_workflow_handoff_preview_route tests/test_worker_live_stream_native_agent_routes.py::test_workflow_handoff_execute_route -q
```

Expected: FAIL because `WorkerLiveStreamServer.__init__` does not accept `workflow_service` or the routes return 404.

- [ ] **Step 4: Add workflow service injection to server**

Modify `WorkerLiveStreamServer.__init__` in `wlcodex/live_stream/server.py`:

```python
def __init__(
    self,
    *,
    host: str,
    port: int,
    hub: WorkerLiveStreamHub,
    native_controller: Any = None,
    native_registry: Any = None,
    workflow_service: Any = None,
    access_token: str | None = None,
    allow_unauthenticated_loopback: bool = True,
    turn_summary_config: LiveTurnSummaryConfig | None = None,
    turn_summary_client: DigestClient | None = None,
    native_transcript_mirror: Any = None,
) -> None:
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError(f"Worker live stream server is loopback-only, got {host!r}")
    self._workflow_service = workflow_service
```

- [ ] **Step 5: Route `/api/native/workflows/...` before provider routes**

In `_handle_client`, place this check before the existing `if parsed.path.startswith("/api/native/")` provider route:

```python
if parsed.path.startswith("/api/native/workflows/"):
    await self._handle_workflow_route(
        reader,
        writer,
        method,
        parsed.path,
        headers,
        query,
    )
    return
```

This ordering matters because `/api/native/workflows/...` also starts with `/api/native/`.

- [ ] **Step 6: Implement `_handle_workflow_route`**

Add a method near `_handle_native_agent_route`:

```python
async def _handle_workflow_route(
    self,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    method: str,
    path: str,
    headers: dict[str, str],
    query: dict[str, list[str]],
) -> None:
    if self._workflow_service is None:
        await self._send_json(writer, 503, {"error": "workflow service unavailable"})
        return
    if not self._is_authorized(
        writer,
        headers,
        query,
        require_token=self._native_registry is not None,
    ):
        await self._send_json(writer, 401, {"error": "unauthorized"})
        return
    route = path.removeprefix("/api/native/workflows")
    if route == "/handoffs/preview" and method == "POST":
        body = await self._read_request_json(writer, reader, headers)
        if body is None:
            return
        result = await self._workflow_service.preview_handoff(
            source_provider=str(body.get("source_provider") or ""),
            source_thread_id=str(body.get("source_thread_id") or ""),
            source_turn_id=str(body.get("source_turn_id") or ""),
            target_provider=str(body.get("target_provider") or ""),
            cwd=str(body.get("cwd") or ""),
            intent=str(body.get("intent") or "auto"),
            user_note=str(body.get("user_note") or ""),
        )
        await self._send_json(writer, 200, _json_object(result))
        return
    if route == "/handoffs/execute" and method == "POST":
        body = await self._read_request_json(writer, reader, headers)
        if body is None:
            return
        result = await self._workflow_service.execute_handoff(
            workflow_run_id=str(body.get("workflow_run_id") or ""),
            preview_id=str(body.get("preview_id") or ""),
            target_provider=str(body.get("target_provider") or ""),
            cwd=str(body.get("cwd") or ""),
            prompt=str(body.get("prompt") or ""),
        )
        await self._send_json(writer, 200, _json_object(result))
        return
    if route in ("/handoffs/preview", "/handoffs/execute"):
        await self._send_json(writer, 405, {"error": "method not allowed"})
        return
    await self._send_json(writer, 404, {"error": "not found"})
```

If service methods raise `KeyError`, return 404. If they raise `ValueError`, return 409. If a provider start operation fails with a runtime exception, let the top-level handler return 500 for this task; Task 6 will refine user-facing error text after the full path exists.

- [ ] **Step 7: Run route tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_live_stream_native_agent_routes.py::test_workflow_handoff_preview_route tests/test_worker_live_stream_native_agent_routes.py::test_workflow_handoff_execute_route -q
```

Expected: both tests PASS.

- [ ] **Step 8: Run native route regression tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_live_stream_native_agent_routes.py -q
```

Expected: full native agent route test file PASS.

- [ ] **Step 9: Run ruff**

Run:

```bash
.venv/bin/python -m ruff check wlcodex/live_stream/server.py tests/test_worker_live_stream_native_agent_routes.py
```

Expected: PASS with no lint errors.

- [ ] **Step 10: Run GitNexus detect changes**

Run:

```bash
npx gitnexus detect-changes -r wlcodex
```

Expected: changes include `WorkerLiveStreamServer` route flow. If GitNexus reports HIGH or CRITICAL risk, stop and report it.

- [ ] **Step 11: Commit Task 4**

Run:

```bash
git add wlcodex/live_stream/server.py tests/test_worker_live_stream_native_agent_routes.py
git commit -m "feat: add native workflow handoff routes"
```

## Task 5: Main Composition

**Files:**

- Modify: `wlcodex/main.py`
- Modify: `tests/test_main_composition.py`

- [ ] **Step 1: Run GitNexus impact**

Run:

```bash
npx gitnexus impact _create_live_stream_components --repo wlcodex --direction upstream --include-tests
```

Expected: review blast radius. If risk is HIGH or CRITICAL, report it before editing `wlcodex/main.py`.

- [ ] **Step 2: Add failing composition test**

Add this assertion to an existing native-agents composition test such as `test_create_live_stream_components_wires_antigravity_cli_engine`:

```python
assert components.server._workflow_service is components.workflow_service
assert components.workflow_service is not None
```

If that test only configures one provider, also assert:

```python
assert components.workflow_service._registry is components.native_registry
```

- [ ] **Step 3: Run composition test to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_main_composition.py::test_create_live_stream_components_wires_antigravity_cli_engine -q
```

Expected: FAIL because `components.workflow_service` does not exist or server `_workflow_service` is `None`.

- [ ] **Step 4: Wire workflow store and service**

Modify `_create_live_stream_components` in `wlcodex/main.py` after `native_registry` is created:

```python
workflow_service = None
if native_registry is not None and ledger is not None:
    from wlcodex.collaboration.workflow_service import WorkflowService
    from wlcodex.collaboration.workflow_store import WorkflowRunStore

    workflow_service = WorkflowService(
        registry=native_registry,
        store=WorkflowRunStore(ledger),
    )
```

Pass `workflow_service=workflow_service` into `WorkerLiveStreamServer`.

Return it from the `SimpleNamespace`:

```python
return SimpleNamespace(
    hub=hub,
    server=server,
    native_client=native_client,
    native_controller=native_controller,
    native_registry=native_registry,
    workflow_service=workflow_service,
)
```

- [ ] **Step 5: Run composition tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_main_composition.py::test_create_live_stream_components_wires_antigravity_cli_engine tests/test_main_composition.py::test_create_live_stream_components_wires_single_claude_cli_engine -q
```

Expected: both tests PASS.

- [ ] **Step 6: Run native route and collaboration regression tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_collaboration_handoff_prompts.py tests/test_collaboration_workflow_store.py tests/test_collaboration_workflow_service.py tests/test_worker_live_stream_native_agent_routes.py -q
```

Expected: all listed tests PASS.

- [ ] **Step 7: Run ruff**

Run:

```bash
.venv/bin/python -m ruff check wlcodex/main.py tests/test_main_composition.py
```

Expected: PASS with no lint errors.

- [ ] **Step 8: Run GitNexus detect changes**

Run:

```bash
npx gitnexus detect-changes -r wlcodex
```

Expected: changes include `_create_live_stream_components`. If GitNexus reports HIGH or CRITICAL risk, stop and report it.

- [ ] **Step 9: Commit Task 5**

Run:

```bash
git add wlcodex/main.py tests/test_main_composition.py
git commit -m "feat: wire collaboration workflow service"
```

## Task 6: Live UI Handoff Preview Panel

**Files:**

- Modify: `wlcodex/live_stream/server.py`
- Modify: `tests/test_worker_live_stream_native_routes.py`

- [ ] **Step 1: Run GitNexus impact**

Run:

```bash
npx gitnexus impact _live_page --repo wlcodex --direction upstream --include-tests
```

Expected: review blast radius. If risk is HIGH or CRITICAL, report it before editing `wlcodex/live_stream/server.py`.

- [ ] **Step 2: Add failing live-page UI test**

Add this test to `tests/test_worker_live_stream_native_routes.py`:

```python
def test_live_page_contains_handoff_execution_preview_panel() -> None:
    response = _live_page(42, native_provider="antigravity")

    assert 'id="handoffButton"' in response
    assert "接棒执行" in response
    assert 'id="handoffPanel"' in response
    assert 'id="handoffTargetProvider"' in response
    assert 'value="codex"' in response
    assert 'value="claude"' in response
    assert 'value="antigravity"' in response
    assert 'id="handoffIntent"' in response
    assert 'id="handoffPromptPreview"' in response
    assert "async function previewHandoff()" in response
    assert "async function executeHandoff()" in response
    assert '"/api/native/workflows/handoffs/preview"' in response
    assert '"/api/native/workflows/handoffs/execute"' in response
```

- [ ] **Step 3: Run UI test to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_live_stream_native_routes.py::test_live_page_contains_handoff_execution_preview_panel -q
```

Expected: FAIL because the live page does not yet include the handoff panel.

- [ ] **Step 4: Add handoff button near default permissions**

In `_live_page` template, inside `.composer-settings`, place the button directly after the existing permission button:

```html
<button class="setting-pill" id="handoffButton" type="button">接棒执行</button>
```

Keep the existing "默认权限" button unchanged.

- [ ] **Step 5: Add handoff panel markup**

Add the panel under the model popover or before the attachment button:

```html
<div class="handoff-panel" id="handoffPanel" hidden>
  <div class="handoff-grid">
    <label>
      目标
      <select id="handoffTargetProvider">
        <option value="codex">Codex</option>
        <option value="claude">Claude</option>
        <option value="antigravity">Antigravity</option>
      </select>
    </label>
    <label>
      类型
      <select id="handoffIntent">
        <option value="auto">自动判断</option>
        <option value="execute_plan">执行计划</option>
        <option value="fix_bug">修复 bug</option>
        <option value="implement_feature">实现功能</option>
        <option value="continue_work">继续工作</option>
        <option value="custom">自定义</option>
      </select>
    </label>
  </div>
  <div class="handoff-meta" id="handoffMeta"></div>
  <textarea id="handoffPromptPreview" rows="9" placeholder="接棒提示词预览"></textarea>
  <div class="handoff-actions">
    <button class="secondary" id="handoffPreviewButton" type="button">生成预览</button>
    <button id="handoffExecuteButton" type="button">确认执行</button>
  </div>
</div>
```

- [ ] **Step 6: Add minimal CSS**

Add compact styles scoped to the live page:

```css
.handoff-panel {
  border: 1px solid #30333a;
  border-radius: 10px;
  background: #111217;
  padding: 10px;
  display: grid;
  gap: 10px;
}
.handoff-panel[hidden] { display: none; }
.handoff-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.handoff-grid label { display: grid; gap: 4px; color: #d4d7de; font-size: 12px; }
.handoff-grid select {
  width: 100%;
  min-height: 38px;
  border-radius: 8px;
  border: 1px solid #3f4550;
  background: #12151d;
  color: #f4f4f5;
}
.handoff-meta { color: #9ca3af; font-size: 12px; line-height: 1.45; }
#handoffPromptPreview {
  width: 100%;
  min-height: 156px;
  resize: vertical;
  border-radius: 10px;
  border: 1px solid #3f4550;
  background: #12151d;
  color: #f4f4f5;
  padding: 10px;
  font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.handoff-actions { display: flex; gap: 8px; justify-content: flex-end; }
```

- [ ] **Step 7: Add JavaScript wiring**

Add DOM references:

```javascript
const handoffButton = document.getElementById("handoffButton");
const handoffPanel = document.getElementById("handoffPanel");
const handoffTargetProvider = document.getElementById("handoffTargetProvider");
const handoffIntent = document.getElementById("handoffIntent");
const handoffMeta = document.getElementById("handoffMeta");
const handoffPromptPreview = document.getElementById("handoffPromptPreview");
const handoffPreviewButton = document.getElementById("handoffPreviewButton");
const handoffExecuteButton = document.getElementById("handoffExecuteButton");
let handoffPreviewState = null;
```

Add functions:

```javascript
function workflowUrl(path) {
  const suffix = token ? `?token=${encodeURIComponent(token)}` : "";
  return path + suffix;
}

async function previewHandoff() {
  if (!nativeThreadId) {
    setSendStatus("当前会话未连接", "error");
    return;
  }
  handoffPreviewButton.disabled = true;
  handoffMeta.textContent = "生成接棒预览中";
  try {
    const body = {
      source_provider: PROVIDER,
      source_thread_id: nativeThreadId,
      source_turn_id: nativeTurnId,
      target_provider: handoffTargetProvider.value,
      cwd: "",
      intent: handoffIntent.value,
      user_note: promptInput.value
    };
    const preview = await api(workflowUrl("/api/native/workflows/handoffs/preview"), {
      method: "POST",
      headers: {"Content-Type": "application/json", ...authHeaders},
      body: JSON.stringify(body)
    });
    handoffPreviewState = preview;
    handoffPromptPreview.value = preview.prompt || "";
    const artifactCount = Array.isArray(preview.artifacts) ? preview.artifacts.length : 0;
    handoffMeta.textContent = `${preview.intent || "auto"} · ${artifactCount} 个引用`;
  } catch (error) {
    handoffMeta.textContent = error.message || String(error);
    setSendStatus(error.message || "接棒预览失败", "error");
  } finally {
    handoffPreviewButton.disabled = false;
  }
}

async function executeHandoff() {
  if (!handoffPreviewState) {
    await previewHandoff();
    if (!handoffPreviewState) return;
  }
  const prompt = handoffPromptPreview.value;
  if (!prompt.trim()) {
    setSendStatus("接棒提示词为空", "error");
    return;
  }
  handoffExecuteButton.disabled = true;
  handoffMeta.textContent = "启动目标智能体中";
  try {
    const result = await api(workflowUrl("/api/native/workflows/handoffs/execute"), {
      method: "POST",
      headers: {"Content-Type": "application/json", ...authHeaders},
      body: JSON.stringify({
        workflow_run_id: handoffPreviewState.workflow_run_id,
        preview_id: handoffPreviewState.preview_id,
        target_provider: handoffTargetProvider.value,
        cwd: "",
        prompt
      })
    });
    if (result.target_url) {
      location.href = token
        ? `${result.target_url}&token=${encodeURIComponent(token)}`
        : result.target_url;
    }
  } catch (error) {
    handoffMeta.textContent = error.message || String(error);
    setSendStatus(error.message || "接棒执行失败", "error");
  } finally {
    handoffExecuteButton.disabled = false;
  }
}
```

Add event handlers:

```javascript
handoffButton.onclick = () => {
  handoffPanel.hidden = !handoffPanel.hidden;
};
handoffPreviewButton.onclick = previewHandoff;
handoffExecuteButton.onclick = executeHandoff;
handoffTargetProvider.onchange = () => {
  handoffPreviewState = null;
};
handoffIntent.onchange = () => {
  handoffPreviewState = null;
};
```

- [ ] **Step 8: Run UI test**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_live_stream_native_routes.py::test_live_page_contains_handoff_execution_preview_panel -q
```

Expected: PASS.

- [ ] **Step 9: Run live page regression tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_live_stream_native_routes.py tests/test_worker_live_stream_native_agent_routes.py -q
```

Expected: both files PASS.

- [ ] **Step 10: Run ruff**

Run:

```bash
.venv/bin/python -m ruff check wlcodex/live_stream/server.py tests/test_worker_live_stream_native_routes.py
```

Expected: PASS with no lint errors.

- [ ] **Step 11: Run GitNexus detect changes**

Run:

```bash
npx gitnexus detect-changes -r wlcodex
```

Expected: changes include `_live_page` UI template. If GitNexus reports HIGH or CRITICAL risk, stop and report it.

- [ ] **Step 12: Commit Task 6**

Run:

```bash
git add wlcodex/live_stream/server.py tests/test_worker_live_stream_native_routes.py
git commit -m "feat: add live handoff execution panel"
```

## Task 7: End-To-End Verification And Local Smoke

**Files:**

- No new source files.
- Verify changed files from Tasks 1-6.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_collaboration_handoff_prompts.py \
  tests/test_collaboration_workflow_store.py \
  tests/test_collaboration_workflow_service.py \
  tests/test_worker_live_stream_native_agent_routes.py \
  tests/test_worker_live_stream_native_routes.py \
  tests/test_main_composition.py -q
```

Expected: all listed tests PASS.

- [ ] **Step 2: Run ruff on touched Python files**

Run:

```bash
.venv/bin/python -m ruff check \
  wlcodex/collaboration \
  wlcodex/db.py \
  wlcodex/live_stream/server.py \
  wlcodex/main.py \
  tests/test_collaboration_handoff_prompts.py \
  tests/test_collaboration_workflow_store.py \
  tests/test_collaboration_workflow_service.py \
  tests/test_worker_live_stream_native_agent_routes.py \
  tests/test_worker_live_stream_native_routes.py \
  tests/test_main_composition.py
```

Expected: PASS with no lint errors.

- [ ] **Step 3: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no output and exit 0.

- [ ] **Step 4: Run GitNexus detect changes**

Run:

```bash
npx gitnexus detect-changes -r wlcodex
```

Expected: affected scope matches the collaboration workflow, live route, live UI, main composition, and database migration changes. If HIGH or CRITICAL risk appears, summarize the blast radius and wait for user confirmation before final integration.

- [ ] **Step 5: Start or restart local service for manual smoke**

Use the existing local deployment flow for this repo. If the service is already running on port `18731`, restart it through the same mechanism used by prior WLCodex local deployments.

Then verify:

```bash
curl -sS http://127.0.0.1:18731/health
```

Expected:

```json
{"status":"ok","service":"worker-live-stream"}
```

- [ ] **Step 6: Manual smoke the UI**

Open:

```text
http://127.0.0.1:18731/workers/128/live?native_provider=antigravity&native_thread_id=d9f0b219-9559-4c0c-aa3b-228229be933b
```

Verify:

- the "接棒执行" button appears next to "默认权限";
- clicking it opens the preview panel;
- selecting `claude` and clicking "生成预览" fills the editable prompt;
- clicking "确认执行" starts a new Claude native session or returns a clear provider error;
- the source Antigravity session is not mutated.

- [ ] **Step 7: Final commit if Task 7 produced changes**

Only commit if verification required source changes. If no files changed, do not create an empty commit.

Run when needed:

```bash
git add wlcodex/collaboration wlcodex/db.py wlcodex/live_stream/server.py wlcodex/main.py tests/test_collaboration_handoff_prompts.py tests/test_collaboration_workflow_store.py tests/test_collaboration_workflow_service.py tests/test_worker_live_stream_native_agent_routes.py tests/test_worker_live_stream_native_routes.py tests/test_main_composition.py
git commit -m "fix: stabilize collaboration workflow smoke"
```

## Final Acceptance Checklist

- [ ] `wlcodex.collaboration` exists and has no imports from provider implementation modules.
- [ ] `HandoffIntent` supports `execute_plan`, `fix_bug`, `implement_feature`, `continue_work`, and `custom`.
- [ ] Preview route does not start a target provider session.
- [ ] Execute route starts a new target provider session through `NativeAgentRegistry`.
- [ ] Workflow store links source provider/thread to target provider/thread.
- [ ] Live UI has "接棒执行" next to "默认权限".
- [ ] Preview prompt is editable before execution.
- [ ] Existing `/api/native/{provider}/...` routes still work.
- [ ] Focused tests pass.
- [ ] Ruff passes.
- [ ] `git diff --check` passes.
- [ ] `npx gitnexus detect-changes -r wlcodex` has been reviewed.
