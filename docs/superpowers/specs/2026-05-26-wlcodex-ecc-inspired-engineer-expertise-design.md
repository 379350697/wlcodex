# WLCodex ECC-Inspired Engineer Expertise Design

## Status

Drafted on 2026-05-26 from the approved direction:

- do not copy ECC's role count or Claude-only assumptions;
- do copy ECC's expert judgement style;
- keep WLCodex stable across Codex, Claude, DeepSeek, GPT, Telegram, Cockpit,
  and terminal surfaces;
- route new or complex work differently from bug repair work.

This is a design/spec only. The paired implementation plan is
`docs/superpowers/plans/2026-05-26-wlcodex-ecc-inspired-engineer-expertise-implementation-plan.md`.

## Core Decision

WLCodex will adopt an **ECC-inspired expert judgement layer** for the existing
Adaptive Engineering Team, but WLCodex remains the control plane.

The product should not fully replicate ECC. ECC is a Claude Code capability
library with many agents, commands, hooks, rules, and skills. WLCodex is a
Telegram and Workbench cockpit that must stay understandable for personal use.
The optimal design is:

```text
WLCodex built-in engineer contracts
  + ECC-style expert judgement profiles
  + optional Codex/Claude local skills as runtime enhancement
```

This means the engineer roles keep a small set:

- Director, hidden from most user-facing copy, classifies and routes work.
- Architect, for new features and complex requirements.
- Diagnostician, for bugs, failures, regressions, and unclear bad behavior.
- Implementer, for code changes.
- Tester, following the implementer in the same practical execution loop.
- Auditor, for independent review and acceptance.

## External Reference Lessons

The design borrows from ECC's structure, not its full catalog size:

- `architect.md` shows how a role becomes a domain expert through required
  analysis shape: current state, requirements, proposal, tradeoffs, ADR, red
  flags, maintainability, scalability, security, and performance.
- `planner.md` and ECC longform workflows show that complex work benefits from
  a route like `RESEARCH -> PLAN -> IMPLEMENT -> REVIEW -> VERIFY`.
- `build-error-resolver.md` and Codex `explorer.toml` show that diagnostic work
  should be evidence-first, mostly read-only, and biased toward minimal repair.
- `tdd-guide.md` shows testing as an implementation discipline, not necessarily
  a separate model session.
- `code-reviewer.md` shows the audit rule WLCodex most needs: do not invent
  blockers. Block only with concrete evidence, concrete failure mode, and
  current diff relevance. A clean pass is allowed.

Reference sources:

- https://github.com/affaan-m/ECC/blob/main/README.zh-CN.md
- https://raw.githubusercontent.com/affaan-m/ECC/main/agents/architect.md
- https://raw.githubusercontent.com/affaan-m/ECC/main/agents/planner.md
- https://raw.githubusercontent.com/affaan-m/ECC/main/agents/build-error-resolver.md
- https://raw.githubusercontent.com/affaan-m/ECC/main/agents/tdd-guide.md
- https://raw.githubusercontent.com/affaan-m/ECC/main/agents/code-reviewer.md
- https://raw.githubusercontent.com/affaan-m/ECC/main/.codex/agents/explorer.toml

## Route Model

WLCodex will use two primary `/auto` routes.

### New Feature Or Complex Requirement

```text
Architect -> Implementer with Tester-following -> Auditor
```

The Architect is not a renamed diagnostician. It is a feature and systems
design expert.

The Architect:

- reads current system structure before proposing new design;
- separates must-have scope from out-of-scope work;
- identifies impacted modules and integration boundaries;
- offers the preferred solution and meaningful alternatives when a tradeoff
  matters;
- flags red flags such as data migration, compatibility, permissions, security,
  performance, concurrency, rollback, and user-facing workflow risk;
- outputs acceptance criteria clear enough for implementer and auditor.

### Bug, Failure, Regression, Or Suspicious Behavior

```text
Diagnostician -> Implementer with Tester-following -> Auditor
```

The Diagnostician is not a renamed architect. It is a bug and evidence expert.

The Diagnostician:

- treats symptoms and root cause as separate;
- gathers or requests evidence before claiming cause;
- uses logs, tests, runtime status, stack traces, diffs, and code paths;
- lists hypotheses with confidence when cause is not proven;
- chooses the smallest credible repair path;
- identifies regression tests or reproduction checks;
- refuses to produce a confident fix plan when evidence is insufficient.

### Ambiguous Work

Ambiguous tasks default to the Diagnostician route. This is conservative:
diagnosis is less likely than architecture to over-design a hidden bug.

The Director may still upgrade a task to Architect when the evidence shows that
the user is asking for product design or feature planning rather than repair.

## User-Facing Stages

Users should not see backend protocol or model names as workflow concepts.

Feature route:

```text
架构工程师定方案 -> 开发测试中 -> 审计工程师验收 -> 完成/返工
```

Bug route:

```text
诊断工程师定位问题 -> 开发测试中 -> 审计工程师验收 -> 完成/返工
```

Model labels such as Codex, Claude, DeepSeek, and GPT are execution details and
may appear only in settings or explicit model-choice buttons.

## Engineer Profiles

Each profile has five stable parts:

- **stance**: how the role thinks;
- **priorities**: what the role optimizes for;
- **required checks**: things the role must inspect or reason about;
- **anti-patterns**: behavior the role must avoid;
- **handoff focus**: what downstream roles need.

### Architect Profile

Stance: systems designer for new or complex work.

Priorities:

- understand current architecture first;
- reduce ambiguity into executable scope;
- choose maintainable, small-enough design;
- preserve existing product conventions;
- expose tradeoffs instead of hiding them.

Required checks:

- current state and relevant flows;
- in-scope and out-of-scope files/modules;
- data, permissions, UI/API, runtime, and deployment impact where relevant;
- user-visible behavior;
- acceptance criteria.

Anti-patterns:

- fixing bugs without diagnosis;
- inventing a large framework for a narrow request;
- proposing changes without naming impacted files or modules;
- omitting rollback or verification concerns when risk is non-trivial.

Handoff focus:

- implementation boundaries;
- ordered steps;
- files/modules to touch and avoid;
- acceptance criteria;
- red flags and assumptions.

### Diagnostician Profile

Stance: evidence-first bug investigator.

Priorities:

- reproduce or verify the symptom;
- separate symptom, trigger, and root cause;
- prefer minimal repair;
- leave unrelated dirty work alone;
- make uncertainty explicit.

Required checks:

- user symptom and expected behavior;
- current logs/errors/tests/status when available;
- recent diff or baseline when relevant;
- code path that can explain the symptom;
- regression test or verification command.

Anti-patterns:

- guessing root cause without evidence;
- turning a bug into a redesign;
- blaming unrelated workspace diff without conflict evidence;
- claiming fixed before verification exists.

Handoff focus:

- root cause or top hypotheses with confidence;
- minimal fix path;
- exact evidence references;
- regression checks;
- risks if evidence is incomplete.

### Implementer Profile

Stance: focused builder.

Priorities:

- make the smallest change that satisfies the accepted plan;
- follow existing code patterns;
- keep unrelated files untouched;
- collect implementation and test evidence;
- stop when blocked rather than broadening scope silently.

Required checks:

- accepted Architect or Diagnostician handoff;
- task-scoped baseline/diff;
- commands run;
- tests attempted;
- known limitations.

Anti-patterns:

- doing new design during implementation without recording why;
- changing unrelated files because they are dirty;
- omitting commands or tests from the report;
- exposing JSON protocol to users.

Handoff focus:

- changed files;
- diff summary;
- commands run;
- tests attempted;
- limitations and follow-up risks.

### Tester-Following Profile

Stance: verification discipline inside the implementer loop.

The Tester is a role and evidence gate, not a separate model session in personal
default mode. Its model profile follows the current Implementer. It records
test evidence and returns failures to the Implementer before the user is asked
for audit.

Priorities:

- validate acceptance criteria;
- prefer focused tests first;
- record exact command evidence;
- block audit when tests are missing or failing;
- cap internal repair loops at 3 attempts.

Anti-patterns:

- starting an independent long model conversation for routine personal work;
- treating test evidence from a previous round as current;
- hiding repeated test failure from the user after the cap is reached.

### Auditor Profile

Stance: independent reviewer with anti-false-positive discipline.

Priorities:

- review current task diff, artifacts, and tests;
- verify the implementation matches the accepted handoff;
- block only on specific, current, evidence-backed risk;
- allow clean pass when evidence supports it.

Required checks:

- current task diff, not entire unrelated workspace noise;
- implementation report;
- test report;
- acceptance criteria coverage;
- security, regression, UX/API contract, and deployment risk when relevant.

Anti-patterns:

- blocking on vague concerns;
- blocking on unrelated dirty files unless they conflict with the task;
- rewriting backend protocol keys into user-facing text;
- treating missing optional polish as a correctness failure.

Handoff focus:

- decision: pass, block, or needs_user;
- concrete findings with file/path evidence;
- missing evidence;
- recommended next action.

## Artifact Contracts

The first upstream artifact differs by route:

- Feature route produces `architecture_plan`.
- Bug route produces `diagnosis_report`.

Both are valid Gate A inputs for implementation. The Implementer does not care
whether the upstream expert was Architect or Diagnostician; it receives a
canonical execution handoff.

### architecture_plan

Required fields:

- `summary`
- `current_state`
- `requirements`
- `files_or_modules_in_scope`
- `files_or_modules_out_of_scope`
- `design_proposal`
- `tradeoffs`
- `red_flags`
- `implementation_steps`
- `acceptance_criteria`
- `risk_level`

### diagnosis_report

Required fields:

- `summary`
- `symptom`
- `expected_behavior`
- `evidence`
- `root_cause`
- `confidence`
- `minimal_fix_plan`
- `regression_tests`
- `risk_level`
- `open_questions`

### implementation_report

Required fields remain:

- `summary`
- `changed_files`
- `diff_summary`
- `commands_run`
- `tests_attempted`
- `known_limitations`

### test_report

The Tester-following role records:

- `summary`
- `commands_run`
- `passed`
- `failed`
- `coverage_of_acceptance_criteria`
- `failure_evidence`

The `model_profile` for this job follows the Implementer for the same round.

### audit_report

Required fields remain:

- `decision`
- `summary`
- `findings`
- `missing_evidence`
- `risk_level`
- `recommended_next_action`
- `test_evidence_refs` when passing

Audit validation must prefer semantic synonyms in parser normalization, but the
stored canonical artifact remains stable.

## Skill Strategy

WLCodex should not require ECC skills to be installed into Codex or Claude.

The stable layer is WLCodex built-in expert contracts. Runtime skills are
optional enhancement:

- Codex skills such as GitNexus impact analysis and focused validation may be
  activated when available.
- Claude/ECC skills may be referenced as methodology when available.
- Missing external skills must not break `/auto`.

This prevents Telegram behavior from drifting because one local model runtime
has a different plugin set than another.

## Context Packet Rules

Every role receives a compact context packet containing:

- route kind: `feature` or `bug`;
- role id and display name;
- expert profile summary;
- user goal;
- current evidence references;
- artifacts from prior roles;
- allowed and forbidden actions;
- output schema;
- handoff rules.

The packet must not rely on full chat history. It must say that current code,
current evidence, and current user goal override historical memory.

## Routing Heuristics

Initial deterministic route classifier:

Bug route terms:

- 报错, 失败, 不对, 修复, bug, regression, 回归, 异常, why, 为什么, crash,
  broken, fail, error, stacktrace, 日志, 验收不过, 测试不过

Feature route terms:

- 新增, 实现一个, 支持, 设计, 复杂需求, 功能, feature, build, add,
  refactor when user asks for planned redesign, workflow redesign

When both groups match:

- route to bug if the user describes current broken behavior;
- route to feature if the user asks for new capability design;
- route to bug when uncertain.

The route decision should be recorded in the TeamRun and visible in team
evidence, but not over-explained to the user.

## UI Rules

- Do not show "Adaptive Engineering Team" as user-facing primary text.
- Prefer "开发团队", "架构工程师", "诊断工程师", "开发工程师",
  "审计工程师".
- Tester-following should be visible in settings as "跟随开发工程师"; it should
  not present an independent model picker.
- Buttons should be role actions, not backend protocol names.
- Artifact JSON, field names, and protocol keys should stay in team evidence
  views, not the main user-facing digest.

## Non-Goals

- Do not add more visible roles such as product manager, security engineer,
  release engineer, or DBA in this slice.
- Do not require ECC installation.
- Do not make Tester an independent model session in personal default mode.
- Do not introduce parallel role waves.
- Do not replace existing `/auto` callbacks with a new workflow engine.

## Acceptance Criteria

- `/auto` can route feature-like work to Architect and bug-like work to
  Diagnostician.
- Architect and Diagnostician context packets contain different expert profiles
  and output contracts.
- Gate A accepts the correct upstream artifact for the selected route.
- Implementer receives a canonical handoff from either route.
- Tester-following records current-round test evidence and caps internal repair
  at 3 attempts.
- Auditor receives ECC-style anti-false-positive instructions and cannot pass
  without current-round test evidence.
- Settings show Tester as following Implementer, not as an independent model
  picker.
- User-facing Telegram and Cockpit copy stays human-readable and avoids raw
  backend protocol language.
