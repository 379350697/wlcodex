# WLCodex ECC-Inspired Engineer Expertise Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ECC-inspired expert judgement profiles and route-specific `/auto` behavior while keeping WLCodex's small role set, tester-following mode, and stable Telegram cockpit.

**Architecture:** Extend the existing Adaptive Engineering Team overlay with a route classifier, role expert profiles, route-specific Gate A artifacts, context packet injection, and auditor anti-false-positive rules. Keep WLCodex as the control plane; Codex/Claude/ECC skills remain optional runtime enhancements.

**Tech Stack:** Python 3.12, SQLite ledger, existing WLCodex controller/event bridge/context packet/artifact modules, pytest, GitNexus MCP for impact analysis.

---

## Required Reading

- Spec: `docs/superpowers/specs/2026-05-26-wlcodex-ecc-inspired-engineer-expertise-design.md`
- Existing team design: `docs/superpowers/specs/2026-05-25-wlcodex-adaptive-engineering-team-design.md`
- Existing `/auto` design: `docs/superpowers/specs/2026-05-22-wlcodex-stage-gated-auto-workflow-design.md`
- Core files:
  - `wlcodex/team_roles.py`
  - `wlcodex/team_context.py`
  - `wlcodex/team_artifacts.py`
  - `wlcodex/auto_workflow.py`
  - `wlcodex/context_packets.py`
  - `wlcodex/controller.py`
  - `wlcodex/event_bridge.py`
  - `wlcodex/status.py`
  - `wlcodex/telegram_digest.py`
- ECC references:
  - `https://raw.githubusercontent.com/affaan-m/ECC/main/agents/architect.md`
  - `https://raw.githubusercontent.com/affaan-m/ECC/main/agents/code-reviewer.md`
  - `https://raw.githubusercontent.com/affaan-m/ECC/main/agents/build-error-resolver.md`
  - `https://raw.githubusercontent.com/affaan-m/ECC/main/agents/tdd-guide.md`
  - `https://raw.githubusercontent.com/affaan-m/ECC/main/.codex/agents/explorer.toml`

## File Structure

| File | Responsibility |
| --- | --- |
| `wlcodex/team_roles.py` | Add route kind, route classifier, and expert judgement profiles for Architect, Diagnostician, Implementer, Tester-following, and Auditor. |
| `wlcodex/team_context.py` | Include route kind and expert profile in TeamContextPacket rendering and JSON. |
| `wlcodex/team_artifacts.py` | Add `diagnosis_report` schema/payload/validator and Gate A helper for route-specific upstream artifacts. |
| `wlcodex/context_packets.py` | Add ECC-style instructions to analysis/final-plan/verification packets without exposing protocol to users. |
| `wlcodex/auto_workflow.py` | Add user-facing labels for feature-vs-bug first stage while preserving existing callback compatibility. |
| `wlcodex/controller.py` | Store route decision on TeamRun/artifacts, create Architect for feature route and Diagnostician for bug route, accept either upstream artifact before implementation, keep Tester-following mode. |
| `wlcodex/event_bridge.py` | Record `architecture_plan` or `diagnosis_report` depending on route and keep implementation/test/audit state transitions compatible. |
| `wlcodex/status.py` | Render route and role summaries in human wording. |
| `wlcodex/telegram_digest.py` | Keep protocol and JSON artifacts out of primary Telegram digests. |
| `tests/test_team_roles.py` | Route classifier and expert profile tests. |
| `tests/test_team_context.py` | Context packet route/profile tests. |
| `tests/test_team_artifacts.py` | Diagnosis report and route-specific Gate A tests. |
| `tests/test_controller_flow.py` | `/auto` feature-vs-bug routing and implementation handoff tests. |
| `tests/test_event_bridge.py` | Completion artifact recording tests for Architect and Diagnostician routes. |
| `tests/test_status.py` | Human-readable route/role status tests. |
| `tests/test_telegram_digest.py` | Protocol leakage regression tests. |

## Impact Baseline

Before modifying existing symbols, run these GitNexus checks:

```text
mcp__gitnexus__.impact({"repo":"wlcodex","target":"TeamRoleCatalog","file_path":"wlcodex/team_roles.py","direction":"upstream","maxDepth":3})
mcp__gitnexus__.impact({"repo":"wlcodex","target":"build_team_context_packet","file_path":"wlcodex/team_context.py","direction":"upstream","maxDepth":3})
mcp__gitnexus__.impact({"repo":"wlcodex","target":"validate_architecture_plan","file_path":"wlcodex/team_artifacts.py","direction":"upstream","maxDepth":3})
mcp__gitnexus__.impact({"repo":"wlcodex","target":"handle_auto_mode","file_path":"wlcodex/controller.py","direction":"upstream","maxDepth":3})
mcp__gitnexus__.impact({"repo":"wlcodex","target":"_handle_auto_send_to_claude","file_path":"wlcodex/controller.py","direction":"upstream","maxDepth":3})
mcp__gitnexus__.impact({"repo":"wlcodex","target":"_handle_auto_codex_verify","file_path":"wlcodex/controller.py","direction":"upstream","maxDepth":3})
mcp__gitnexus__.impact({"repo":"wlcodex","target":"_record_architecture_plan_artifact","file_path":"wlcodex/event_bridge.py","direction":"upstream","maxDepth":3})
```

Expected risk is MEDIUM or below for most pure helpers. Controller/event bridge
changes may be reported higher because they sit on `/auto`; stop and report if
GitNexus reports HIGH or CRITICAL from direct runtime consumers rather than
dirty-worktree noise.

---

## Task 1: Route Kind And Expert Profiles

**Files:**
- Modify: `wlcodex/team_roles.py`
- Test: `tests/test_team_roles.py`

- [ ] **Step 1: Write failing tests for route classification**

Add tests:

```python
from wlcodex.team_roles import (
    RoleId,
    TeamRouteKind,
    TeamRoleCatalog,
    classify_team_route,
)


def test_classify_team_route_sends_bug_language_to_diagnosis() -> None:
    route = classify_team_route("Telegram 验收一直失败，实际本地测试通过，帮我定位原因")

    assert route.kind == TeamRouteKind.BUG
    assert route.first_role == RoleId.INVESTIGATOR
    assert "失败" in route.matched_signals


def test_classify_team_route_sends_feature_language_to_architecture() -> None:
    route = classify_team_route("新增工程师专家判断模式，支持复杂需求走架构方案")

    assert route.kind == TeamRouteKind.FEATURE
    assert route.first_role == RoleId.ARCHITECT
    assert "新增" in route.matched_signals


def test_classify_team_route_defaults_ambiguous_work_to_diagnosis() -> None:
    route = classify_team_route("看看这个任务怎么处理")

    assert route.kind == TeamRouteKind.BUG
    assert route.first_role == RoleId.INVESTIGATOR
    assert route.reason == "ambiguous_defaults_to_diagnosis"
```

- [ ] **Step 2: Run the tests and confirm failure**

Run:

```bash
pytest tests/test_team_roles.py -q -k "classify_team_route"
```

Expected: fails because `TeamRouteKind` and `classify_team_route` do not exist.

- [ ] **Step 3: Implement route classification**

Add to `wlcodex/team_roles.py`:

```python
from dataclasses import dataclass
from enum import Enum


class TeamRouteKind(str, Enum):
    FEATURE = "feature"
    BUG = "bug"


@dataclass(frozen=True)
class TeamRouteDecision:
    kind: TeamRouteKind
    first_role: RoleId
    reason: str
    matched_signals: tuple[str, ...] = ()


BUG_ROUTE_SIGNALS: tuple[str, ...] = (
    "报错", "失败", "不对", "修复", "bug", "regression", "回归", "异常",
    "why", "为什么", "crash", "broken", "fail", "error", "stacktrace",
    "日志", "验收不过", "测试不过",
)

FEATURE_ROUTE_SIGNALS: tuple[str, ...] = (
    "新增", "实现一个", "支持", "设计", "复杂需求", "功能", "feature",
    "build", "add", "workflow redesign",
)


def classify_team_route(goal: str) -> TeamRouteDecision:
    text = goal.lower()
    bug_hits = tuple(signal for signal in BUG_ROUTE_SIGNALS if signal.lower() in text)
    feature_hits = tuple(
        signal for signal in FEATURE_ROUTE_SIGNALS if signal.lower() in text
    )
    if bug_hits and not feature_hits:
        return TeamRouteDecision(
            kind=TeamRouteKind.BUG,
            first_role=RoleId.INVESTIGATOR,
            reason="bug_signals",
            matched_signals=bug_hits,
        )
    if feature_hits and not bug_hits:
        return TeamRouteDecision(
            kind=TeamRouteKind.FEATURE,
            first_role=RoleId.ARCHITECT,
            reason="feature_signals",
            matched_signals=feature_hits,
        )
    if feature_hits and bug_hits:
        if any(signal in text for signal in ("失败", "报错", "error", "fail", "回归")):
            return TeamRouteDecision(
                kind=TeamRouteKind.BUG,
                first_role=RoleId.INVESTIGATOR,
                reason="mixed_signals_bug_first",
                matched_signals=bug_hits + feature_hits,
            )
        return TeamRouteDecision(
            kind=TeamRouteKind.FEATURE,
            first_role=RoleId.ARCHITECT,
            reason="mixed_signals_feature_first",
            matched_signals=feature_hits + bug_hits,
        )
    return TeamRouteDecision(
        kind=TeamRouteKind.BUG,
        first_role=RoleId.INVESTIGATOR,
        reason="ambiguous_defaults_to_diagnosis",
        matched_signals=(),
    )
```

- [ ] **Step 4: Add expert profile tests**

Add tests:

```python
def test_architect_and_diagnostician_have_different_expert_profiles() -> None:
    catalog = TeamRoleCatalog.default()

    architect = catalog.role(RoleId.ARCHITECT)
    diagnostician = catalog.role(RoleId.INVESTIGATOR)

    assert "tradeoff" in " ".join(architect.expert_priorities).lower()
    assert "root cause" in " ".join(diagnostician.expert_priorities).lower()
    assert "do not fix bugs without diagnosis" in " ".join(
        architect.anti_patterns
    ).lower()
    assert "do not turn bugs into redesigns" in " ".join(
        diagnostician.anti_patterns
    ).lower()
```

- [ ] **Step 5: Extend `TeamRole` with profile fields**

Add fields to the `TeamRole` dataclass:

```python
expert_stance: str = ""
expert_priorities: tuple[str, ...] = ()
required_checks: tuple[str, ...] = ()
anti_patterns: tuple[str, ...] = ()
handoff_focus: tuple[str, ...] = ()
```

Populate these fields in `TeamRoleCatalog.default()` using the spec language.

- [ ] **Step 6: Run tests**

Run:

```bash
pytest tests/test_team_roles.py -q
```

Expected: all tests pass.

---

## Task 2: Context Packets Carry Expert Judgement

**Files:**
- Modify: `wlcodex/team_context.py`
- Test: `tests/test_team_context.py`

- [ ] **Step 1: Write failing context packet tests**

Add tests:

```python
from wlcodex.team_roles import RoleId, TeamRouteKind, TeamRoleCatalog


def test_team_context_packet_includes_architect_expert_profile() -> None:
    role = TeamRoleCatalog.default().role(RoleId.ARCHITECT)
    packet = build_team_context_packet(TeamContextInput(
        team_run_id=7,
        agent_job_id=8,
        conversation_id=9,
        orchestration_run_id=10,
        role=role,
        model_profile="codex_gpt",
        user_goal="新增专家判断模式",
        workspace_alias="wlcodex",
        route_kind=TeamRouteKind.FEATURE.value,
        output_schema="architecture_plan",
    ))

    payload = packet.as_json()
    rendered = packet.render()

    assert payload["route_kind"] == "feature"
    assert payload["expert_profile"]["stance"]
    assert "tradeoff" in rendered.lower()
    assert "architecture_plan" in rendered


def test_team_context_packet_includes_diagnostician_expert_profile() -> None:
    role = TeamRoleCatalog.default().role(RoleId.INVESTIGATOR)
    packet = build_team_context_packet(TeamContextInput(
        team_run_id=7,
        agent_job_id=8,
        conversation_id=9,
        orchestration_run_id=10,
        role=role,
        model_profile="codex_gpt",
        user_goal="Telegram 验收失败",
        workspace_alias="wlcodex",
        route_kind=TeamRouteKind.BUG.value,
        output_schema="diagnosis_report",
    ))

    rendered = packet.render()

    assert "route_kind: bug" in rendered
    assert "root cause" in rendered.lower()
    assert "diagnosis_report" in rendered
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
pytest tests/test_team_context.py -q -k "expert_profile or route_kind"
```

Expected: fails because `route_kind` and `expert_profile` are missing.

- [ ] **Step 3: Add route kind to `TeamContextInput`**

Modify `TeamContextInput`:

```python
route_kind: str = ""
```

- [ ] **Step 4: Add expert profile JSON**

In `TeamContextPacket.as_json()`, add:

```python
"route_kind": self.data.route_kind,
"expert_profile": {
    "stance": role.expert_stance,
    "priorities": list(role.expert_priorities),
    "required_checks": list(role.required_checks),
    "anti_patterns": list(role.anti_patterns),
    "handoff_focus": list(role.handoff_focus),
},
```

- [ ] **Step 5: Render expert profile compactly**

In `TeamContextPacket.render()`, include lines after `role_mission`:

```python
if payload["route_kind"]:
    lines.append(f"route_kind: {payload['route_kind']}")
profile = payload["expert_profile"]
if profile["stance"]:
    lines.append(f"expert_stance: {profile['stance']}")
if profile["priorities"]:
    lines.append("expert_priorities:")
    for item in profile["priorities"]:
        lines.append(f"  - {item}")
if profile["required_checks"]:
    lines.append("required_checks:")
    for item in profile["required_checks"]:
        lines.append(f"  - {item}")
if profile["anti_patterns"]:
    lines.append("anti_patterns:")
    for item in profile["anti_patterns"]:
        lines.append(f"  - {item}")
```

Keep compact and ultra-compact rendering small; include only `route_kind` and
`expert_stance` when budget is tight.

- [ ] **Step 6: Run context tests**

Run:

```bash
pytest tests/test_team_context.py -q
```

Expected: pass.

---

## Task 3: Diagnosis Artifact And Route-Specific Gate A

**Files:**
- Modify: `wlcodex/team_artifacts.py`
- Test: `tests/test_team_artifacts.py`

- [ ] **Step 1: Write failing diagnosis report tests**

Add tests:

```python
from wlcodex.team_artifacts import (
    diagnosis_report_payload,
    validate_diagnosis_report,
    validate_gate_a_handoff,
)


def test_validate_diagnosis_report_requires_bug_evidence_and_fix_plan() -> None:
    result = validate_diagnosis_report({
        "summary": "Telegram audit fails while local validation passes.",
        "symptom": "Telegram blocks README-only task.",
        "expected_behavior": "Audit should pass task-scoped README change.",
        "evidence": ["local pytest passed", "Telegram audit blocked"],
        "root_cause": "Audit saw whole workspace instead of task diff.",
        "confidence": "high",
        "minimal_fix_plan": ["Scope audit packet to task diff."],
        "regression_tests": ["controller flow audit scope test"],
        "risk_level": "medium",
        "open_questions": ["None"],
    })

    assert result.passed is True


def test_validate_diagnosis_report_blocks_missing_root_cause() -> None:
    result = validate_diagnosis_report({
        "summary": "Something failed.",
        "symptom": "Audit failed.",
        "expected_behavior": "Audit passes.",
        "evidence": ["user report"],
        "minimal_fix_plan": ["Patch it."],
        "regression_tests": ["pytest"],
        "risk_level": "medium",
        "open_questions": ["Need logs"],
    })

    assert result.passed is False
    assert "root_cause" in result.missing_fields


def test_gate_a_accepts_architecture_for_feature_and_diagnosis_for_bug() -> None:
    assert validate_gate_a_handoff(
        route_kind="feature",
        artifact_type="architecture_plan",
        payload={
            "summary": "Add expert profile support.",
            "files_or_modules_in_scope": ["wlcodex/team_roles.py"],
            "files_or_modules_out_of_scope": ["unrelated"],
            "impact_notes": "Prompt/context only.",
            "risk_level": "medium",
            "implementation_steps": ["Add profiles"],
            "acceptance_criteria": ["Context packet includes profile"],
            "parallelization_policy": "single implementer",
        },
    ).passed is True

    assert validate_gate_a_handoff(
        route_kind="bug",
        artifact_type="diagnosis_report",
        payload=diagnosis_report_payload(
            summary="Fix audit false positive",
            symptom="Telegram blocks unrelated files",
            expected_behavior="Ignore unrelated dirty files",
            evidence=["task diff excludes unrelated files"],
            root_cause="Audit packet used workspace diff",
            confidence="high",
            minimal_fix_plan=["Use task scoped diff"],
            regression_tests=["controller_flow"],
            risk_level="medium",
        ),
    ).passed is True
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
pytest tests/test_team_artifacts.py -q -k "diagnosis_report or gate_a"
```

Expected: fails because new helpers do not exist.

- [ ] **Step 3: Implement diagnosis helpers**

Add:

```python
def validate_diagnosis_report(payload: Mapping[str, Any]) -> GateResult:
    missing = list(_missing(payload, (
        "summary",
        "symptom",
        "expected_behavior",
        "evidence",
        "root_cause",
        "confidence",
        "minimal_fix_plan",
        "regression_tests",
        "risk_level",
        "open_questions",
    )))
    if not _valid_non_placeholder_list(payload.get("evidence")):
        _append_missing_once(missing, "evidence")
    if not _valid_non_placeholder_list(payload.get("minimal_fix_plan")):
        _append_missing_once(missing, "minimal_fix_plan")
    if not _valid_non_placeholder_list(payload.get("regression_tests")):
        _append_missing_once(missing, "regression_tests")
    return _result(tuple(missing))


def diagnosis_report_payload(
    *,
    summary: str,
    symptom: str,
    expected_behavior: str,
    evidence: list[str],
    root_cause: str,
    confidence: str,
    minimal_fix_plan: list[str],
    regression_tests: list[str],
    risk_level: str = "medium",
    open_questions: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "summary": summary,
        "symptom": symptom,
        "expected_behavior": expected_behavior,
        "evidence": evidence,
        "root_cause": root_cause,
        "confidence": confidence,
        "minimal_fix_plan": minimal_fix_plan,
        "regression_tests": regression_tests,
        "risk_level": risk_level,
        "open_questions": open_questions or ["None"],
    }
```

- [ ] **Step 4: Implement route-specific Gate A helper**

Add:

```python
def validate_gate_a_handoff(
    *, route_kind: str, artifact_type: str, payload: Mapping[str, Any]
) -> GateResult:
    if route_kind == "bug":
        if artifact_type != "diagnosis_report":
            return _result(("diagnosis_report",))
        return validate_diagnosis_report(payload)
    if artifact_type != "architecture_plan":
        return _result(("architecture_plan",))
    return validate_architecture_plan(payload)
```

- [ ] **Step 5: Run artifact tests**

Run:

```bash
pytest tests/test_team_artifacts.py -q
```

Expected: pass.

---

## Task 4: `/auto` Creates Architect Or Diagnostician First Job

**Files:**
- Modify: `wlcodex/controller.py`
- Modify: `wlcodex/event_bridge.py`
- Test: `tests/test_controller_flow.py`
- Test: `tests/test_event_bridge.py`

- [ ] **Step 1: Write failing controller route tests**

Add tests:

```python
@pytest.mark.asyncio
async def test_auto_bug_route_creates_diagnostician_context_before_final_handoff(
    ctrl: CommandController,
) -> None:
    response = await ctrl.handle_auto_mode(
        chat_id=123,
        user_id=456,
        text="Telegram 验收失败，本地通过，定位原因",
        workspace_alias="wlcodex",
    )

    conversation = ctrl._ledger.get_active_conversation(123, 456)
    orch_run = ctrl._latest_active_auto_run(conversation.id)
    team_run = ctrl._ledger.get_team_run_for_orchestration(orch_run.id)
    jobs = ctrl._ledger.list_team_agent_jobs(team_run.id)

    assert "诊断工程师" in response.text or "诊断" in response.text
    assert jobs[0].role == "investigator"


@pytest.mark.asyncio
async def test_auto_feature_route_creates_architect_context_before_final_handoff(
    ctrl: CommandController,
) -> None:
    response = await ctrl.handle_auto_mode(
        chat_id=123,
        user_id=456,
        text="新增专家判断模式，支持复杂需求走架构方案",
        workspace_alias="wlcodex",
    )

    conversation = ctrl._ledger.get_active_conversation(123, 456)
    orch_run = ctrl._latest_active_auto_run(conversation.id)
    team_run = ctrl._ledger.get_team_run_for_orchestration(orch_run.id)
    jobs = ctrl._ledger.list_team_agent_jobs(team_run.id)

    assert "架构工程师" in response.text or "架构" in response.text
    assert jobs[0].role == "architect"
```

Adjust helper names to match existing test fixtures if necessary. Do not change
product behavior to satisfy test helper assumptions; update tests to the
existing controller entrypoints.

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
pytest tests/test_controller_flow.py -q -m integration -k "auto_bug_route or auto_feature_route"
```

Expected: fails because `/auto` does not persist route-specific first role.

- [ ] **Step 3: Store route decision in TeamRun creation**

In the `/auto` start path, call:

```python
route_decision = classify_team_route(goal)
```

When creating the first `team_agent_job`, use:

```python
first_role = route_decision.first_role.value
output_schema = (
    "architecture_plan"
    if route_decision.kind == TeamRouteKind.FEATURE
    else "diagnosis_report"
)
```

Pass `route_kind=route_decision.kind.value` into `TeamContextInput`.

If `TeamRun` does not have a route-kind column, store it first in the first
context packet and a `routing_decision` artifact. Do not add a migration unless
needed by existing queries.

- [ ] **Step 4: Update event bridge artifact recording**

When the first-role Codex analysis/final plan completes:

- feature route records `architecture_plan`;
- bug route records `diagnosis_report`;
- both mark the first role job done.

Use the artifact helper from Task 3 for fallback payloads.

- [ ] **Step 5: Run route tests**

Run:

```bash
pytest tests/test_controller_flow.py tests/test_event_bridge.py -q -m "slow or integration" -k "auto_bug_route or auto_feature_route or final_plan_completion"
```

Expected: pass.

---

## Task 5: Gate A Accepts Route-Specific Handoff Before Implementation

**Files:**
- Modify: `wlcodex/controller.py`
- Test: `tests/test_controller_flow.py`

- [ ] **Step 1: Write failing Gate A tests**

Add tests:

```python
@pytest.mark.asyncio
async def test_bug_route_implementation_requires_diagnosis_report_not_architecture_plan(
    ctrl: CommandController,
) -> None:
    conversation, orch_run, team_run = _create_auto_run_with_route(
        ctrl,
        route_kind="bug",
        current_step=AUTO_DRAFT_READY,
        last_codex_analysis="诊断：需要修复 audit diff scope。",
    )

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation.id, AUTO_SEND_TO_CLAUDE)
    )

    assert "Gate A" in response.text
    assert "diagnosis_report" in response.text


@pytest.mark.asyncio
async def test_bug_route_implementation_accepts_valid_diagnosis_report(
    ctrl: CommandController,
) -> None:
    conversation, orch_run, team_run = _create_auto_run_with_route(
        ctrl,
        route_kind="bug",
        current_step=AUTO_DRAFT_READY,
        last_codex_analysis="诊断：需要修复 audit diff scope。",
    )
    _record_valid_diagnosis_report(ctrl._ledger, team_run_id=team_run.id)

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation.id, AUTO_SEND_TO_CLAUDE)
    )

    assert "开发工程师开始执行" in response.text
```

Implement test helpers in the test file using existing ledger helpers. Keep
them local to the test file.

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
pytest tests/test_controller_flow.py -q -m integration -k "bug_route_implementation"
```

Expected: fails because implementation only checks `architecture_plan`.

- [ ] **Step 3: Replace fixed Gate A check with route-aware validation**

In `_handle_auto_send_to_claude`, `_handle_auto_send_to_codex`, and repair
entrypoints if they recheck Gate A:

```python
route_kind = self._team_route_kind(team_run) or "feature"
artifact_type = "diagnosis_report" if route_kind == "bug" else "architecture_plan"
validator = validate_diagnosis_report if route_kind == "bug" else validate_architecture_plan
```

Keep error text human-readable:

```text
Gate A 阻断：缺少诊断工程师交接报告。
```

or:

```text
Gate A 阻断：缺少架构工程师方案。
```

Avoid exposing only raw schema field names in the primary sentence.

- [ ] **Step 4: Run implementation gate tests**

Run:

```bash
pytest tests/test_controller_flow.py -q -m integration -k "implementation_requires or implementation_accepts"
```

Expected: pass.

---

## Task 6: Implementer And Tester-Following Context Hardening

**Files:**
- Modify: `wlcodex/team_context.py`
- Modify: `wlcodex/controller.py`
- Modify: `wlcodex/event_bridge.py`
- Test: `tests/test_controller_flow.py`
- Test: `tests/test_event_bridge.py`

- [ ] **Step 1: Add regression tests for Tester-following**

Keep or add tests asserting:

```python
assert tester_job.model_profile == implementer_job.model_profile
assert tester_assignment.selected_by == "follow_implementer"
```

Use existing tests around:

- `auto_send_to_claude_creates_implementer_context`
- `auto_implementation_completion_creates_tester_job_before_audit`

- [ ] **Step 2: Add context rule**

When building Implementer context, include handoff rule:

```text
Tester follows this developer session. Record commands_run and tests_attempted;
missing or failing current-round tests return to the developer before audit.
```

- [ ] **Step 3: Preserve 3-attempt cap**

Verify both controller and event bridge keep:

```python
MAX_INTERNAL_TEST_ATTEMPTS = 3
```

The cap message should remain human-readable and should not expose backend
field names as the main text.

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_controller_flow.py tests/test_event_bridge.py -q -m "slow or integration" -k "tester or implementation_completion or claude_completion_records"
```

Expected: pass.

---

## Task 7: ECC-Style Auditor Anti-False-Positive Rules

**Files:**
- Modify: `wlcodex/team_roles.py`
- Modify: `wlcodex/context_packets.py`
- Modify: `wlcodex/codex_backend.py`
- Modify: `wlcodex/telegram_digest.py`
- Test: `tests/test_context_packets.py`
- Test: `tests/test_telegram_digest.py`
- Test: `tests/test_controller_flow.py`

- [ ] **Step 1: Write failing auditor prompt tests**

Add:

```python
def test_auditor_packet_requires_concrete_failure_mode_before_blocking() -> None:
    packet = build_auto_verification_packet(
        user_goal="README 增加一行测试说明",
        codex_plan_summary="Only edit README.",
        claude_completion_summary="README updated.",
        changed_files=["README.md"],
        unrelated_changed_files=["wlcodex/controller.py"],
        test_results="pytest passed",
        diff_summary="README one-line diff",
        workspace="wlcodex",
        verify_round=1,
    )

    text = packet.render()

    assert "concrete failure mode" in text.lower()
    assert "unrelated dirty files" in text.lower()
    assert "do not block" in text.lower()
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
pytest tests/test_context_packets.py -q -k "auditor_packet"
```

Expected: fails because anti-false-positive text is missing or too weak.

- [ ] **Step 3: Add auditor rules to verification packet**

In `build_auto_verification_packet`, add constraints:

```text
Auditor anti-false-positive rules:
- Block only with current task evidence, exact file/path when possible, and a concrete failure mode.
- Do not block on unrelated dirty files unless they conflict with this task or make verification impossible.
- A clean PASS is valid when diff, tests, and acceptance criteria are sufficient.
- Missing optional polish is not a blocker unless it violates the accepted goal.
```

- [ ] **Step 4: Add digest sanitization tests**

Add regression test:

```python
def test_audit_digest_does_not_show_raw_protocol_json() -> None:
    text = render_auto_verification_digest("""
```json
{"audit_report":{"decision":"pass","summary":"Looks good","findings":[]}}
```
""")

    assert "audit_report" not in text
    assert '"decision"' not in text
    assert "Looks good" in text or "通过" in text
```

Use the existing digest function name. If the project uses a different
verification digest function, test that function.

- [ ] **Step 5: Run audit/digest tests**

Run:

```bash
pytest tests/test_context_packets.py tests/test_telegram_digest.py tests/test_controller_flow.py -q -k "auditor or audit or verification_filters_workspace_diff"
```

Expected: pass.

---

## Task 8: Human-Readable Role Status And Settings

**Files:**
- Modify: `wlcodex/auto_workflow.py`
- Modify: `wlcodex/status.py`
- Modify: `wlcodex/controller.py`
- Test: `tests/test_auto_workflow.py`
- Test: `tests/test_status.py`
- Test: `tests/test_controller_flow.py`

- [ ] **Step 1: Write status tests**

Add:

```python
def test_auto_stage_label_uses_architect_or_diagnostician_language() -> None:
    assert "开发" not in auto_stage_label("collecting_context")
    assert "工程师" in auto_stage_label("collecting_context")


def test_engineer_model_settings_shows_tester_following_implementer(ctrl) -> None:
    response = ctrl.render_engineer_model_settings()

    assert "测试工程师：跟随开发工程师" in response.text
    assert "settings:engineer_models:tester" not in str(response.buttons)
```

Update exact expectations to match existing helper names.

- [ ] **Step 2: Ensure tester remains non-configurable**

If this is already implemented, keep the tests and do not rework code. The
expected behavior is:

```text
测试工程师：跟随开发工程师
```

and no independent tester model picker button.

- [ ] **Step 3: Route-specific first-stage labels**

If route kind is available in status rendering, display:

- feature route: `架构工程师正在定方案`
- bug route: `诊断工程师正在定位问题`

If route kind is not available in a generic helper, keep the generic label:

```text
工程师正在分析中
```

Do not add fragile global state just to improve the generic label.

- [ ] **Step 4: Run UI/status tests**

Run:

```bash
pytest tests/test_auto_workflow.py tests/test_status.py tests/test_controller_flow.py -q -k "stage_label or engineer_model_settings or team_summary"
```

Expected: pass.

---

## Task 9: End-To-End `/auto` Route Regression Tests

**Files:**
- Test: `tests/test_controller_flow.py`
- Test: `tests/test_event_bridge.py`

- [ ] **Step 1: Add feature-route E2E test**

Add a test that covers:

```text
feature goal
-> Architect job/context packet
-> architecture_plan artifact
-> Implementer job
-> Tester-following test_report
-> Auditor gate can start
```

Assertions:

```python
assert first_job.role == "architect"
assert architecture_artifact.artifact_type == "architecture_plan"
assert implementer_job.role == "implementer"
assert tester_job.model_profile == implementer_job.model_profile
assert auditor_job.role == "auditor"
```

- [ ] **Step 2: Add bug-route E2E test**

Add a test that covers:

```text
bug goal
-> Diagnostician job/context packet
-> diagnosis_report artifact
-> Implementer job
-> Tester-following test_report
-> Auditor gate can start
```

Assertions:

```python
assert first_job.role == "investigator"
assert diagnosis_artifact.artifact_type == "diagnosis_report"
assert "root_cause" in diagnosis_artifact.payload
assert implementer_job.role == "implementer"
assert tester_job.model_profile == implementer_job.model_profile
```

- [ ] **Step 3: Run targeted E2E tests**

Run:

```bash
pytest tests/test_controller_flow.py tests/test_event_bridge.py -q -m "slow or integration" -k "feature_route or bug_route or auto_verification"
```

Expected: pass.

---

## Task 10: Final Validation And Deployment

**Files:**
- No code edits unless tests reveal a defect.

- [ ] **Step 1: Run focused suites**

Run:

```bash
pytest tests/test_team_roles.py tests/test_team_context.py tests/test_team_artifacts.py -q
```

Expected: pass.

- [ ] **Step 2: Run `/auto` controller/event bridge suites**

Run:

```bash
pytest tests/test_controller_flow.py -q -m integration -k "auto or engineer_model_settings or verification"
pytest tests/test_event_bridge.py -q -m "slow or integration" -k "auto or verification"
```

Expected: pass.

- [ ] **Step 3: Compile**

Run:

```bash
python3 -m compileall wlcodex
```

Expected: no syntax errors.

- [ ] **Step 4: Detect changes**

Run:

```text
mcp__gitnexus__.detect_changes({"repo":"wlcodex","scope":"all"})
```

Expected: the report may mention broad dirty worktree noise, but changed flows
should match team roles, context packets, artifacts, controller, event bridge,
status, and digest work.

- [ ] **Step 5: Restart local service**

Run:

```bash
rtk systemctl --user restart wlcodex.service
rtk systemctl --user status wlcodex.service
```

Expected: service is `active (running)`.

## Self-Review Checklist

- Every spec acceptance criterion maps to at least one task:
  - route split: Tasks 1, 4, 9;
  - expert profiles: Tasks 1, 2;
  - Gate A route artifacts: Tasks 3, 5;
  - tester-following: Tasks 6, 8, 9;
  - auditor anti-false-positive: Task 7;
  - human-readable UI: Tasks 7, 8;
  - validation/deploy: Task 10.
- No task requires ECC installation.
- No task introduces new visible roles beyond Architect, Diagnostician,
  Implementer, Tester-following, Auditor.
- No task makes Tester an independent model session.
- No task replaces existing staged `/auto` callbacks.
