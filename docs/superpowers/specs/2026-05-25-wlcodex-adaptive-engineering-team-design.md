# WLCodex Adaptive Engineering Team Design

## Status

Drafted on 2026-05-25 from the approved multi-agent direction. Updated with
ECC-inspired capability-library and long-term-memory design on 2026-05-25.

This is a design/spec only. It does not authorize code changes by itself. The
paired implementation plan defines later code work.

## Core Decision

Build **Adaptive Engineering Team** as an incremental refinement of the current
WLCodex workflow, not a replacement for it.

The existing product loop stays canonical:

```text
Codex analysis / planning
  -> user confirms the implementation route
  -> Claude or Codex implementation
  -> Codex verification / audit
  -> user-facing closure
```

Adaptive Engineering Team makes each phase explicit as engineer roles:

- Each engineer role can bind its own model profile, tool capability set, and
  skills. Today the available model profiles are expected to be Codex GPT and
  Claude DeepSeek, but the role layer must not depend on that limited pool.
- Diagnosis, architecture, implementation, testing, and audit are separate
  role assignments. Current defaults can map most roles to one model profile
  and the Implementer role to two choices, but every role remains editable.
- Implementation is performed by a selected Implementer profile. The user can
  choose Claude DeepSeek Implementer, Codex GPT Implementer, or a configured
  future implementer after the plan is ready.
- Final verification is expressed as Auditor and, when needed, Tester. The
  first-release default may assign those roles to Codex GPT, but that is
  configuration, not a product rule.
- First release note: **Auditor performs tester duties in v1**. WLCodex still
  keeps `tester` in the role catalog and requires `test_report` evidence, but
  staged `/auto` does not create an independent Tester job until that later
  slice is implemented.
- First release note: **Architect performs investigator duties in v1**.
  WLCodex keeps `investigator` in the role catalog, but staged `/auto` records
  diagnosis/runtime evidence inside the Architect context packet and
  `architecture_plan` instead of creating a separate Investigator job. A
  standalone `diagnosis_report` gate is a later slice.

WLCodex becomes the durable engineering-team control plane. Codex, Claude, and
future model providers are execution runtimes. A task is handled by the current
single-agent/direct path when that is enough, or by a role-labeled staged
workflow when role separation, independent verification, or model/tool
specialization adds real value.

The team overlay model is:

```text
User
  -> WLCodex Director
      -> classifies task and selects the existing route shape
      -> creates role-labeled AgentJobs, gates, and context packets
      -> offers implementation profiles when user choice is required
      -> records events, artifacts, and evidence
      -> synthesizes the final answer
```

Roles are not models. A role defines work responsibility, skills, tool
capabilities, permissions, and expected output. A model profile defines which
backend and model performs a role for a run. The assignment policy is editable
for every role, not only for implementation.

The first release must preserve existing `/auto`, `/codex`, `/claude`,
`/verify`, Cockpit, Onsite, workspace locking, approvals, and staged-auto
buttons. It adds engineering-role semantics and artifacts around those flows.

## Rationale And External References

This design intentionally avoids both extremes: a single omnipotent agent with
too many tools, and a rigid swarm that adds coordination overhead to every
task.

- OpenAI's practical agent guide recommends maximizing a single agent first,
  then splitting when prompts, conditional logic, or tool selection become too
  complex. It also says models should be selected by task complexity, latency,
  and cost. Reference:
  https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/
- OpenAI Agents SDK documents the manager pattern: one manager keeps control,
  calls specialist agents, and combines their outputs under shared guardrails.
  It also documents code-orchestrated workflows, structured outputs, parallel
  runs, evaluator loops, and handoffs. Reference:
  https://openai.github.io/openai-agents-python/multi_agent/
- OpenAI Codex subagents can spawn specialized agents in parallel, define
  custom agents, inherit or override sandbox/model/MCP/skills settings, and
  collect results. They are useful execution primitives but are not a complete
  durable team ledger. Reference:
  https://developers.openai.com/codex/subagents
- Codex skills package reusable instructions, resources, scripts, and assets.
  They use progressive disclosure: Codex starts with skill metadata and loads
  full instructions only when selected. This supports role-specific skills
  without bloating every prompt. Reference:
  https://developers.openai.com/codex/skills
- ECC demonstrates a large Claude-oriented capability library with agents,
  skills, commands, rules, hooks, contexts, MCP metadata, and a continuous
  learning skill. The useful lesson for WLCodex is not the raw number of
  agents, but the layered packaging and event-triggered memory discipline.
  References:
  https://github.com/affaan-m/ECC/blob/main/README.zh-CN.md
  https://github.com/affaan-m/ECC/blob/main/.claude-plugin/plugin.json
  https://github.com/affaan-m/ECC/blob/main/skills/continuous-learning-v2/SKILL.md
  https://github.com/affaan-m/ECC/blob/main/scripts/hooks/session-start.js
- Claude Code subagents are specialized assistants with their own context,
  tools, permissions, models, skills, MCP servers, memory, hooks, and optional
  worktree isolation. This validates role-level capability boundaries.
  Reference:
  https://code.claude.com/docs/en/sub-agents
- Claude Code agent teams are useful when teammates need direct communication,
  shared task lists, and independent contexts. Claude's docs also warn that
  teams add token and coordination cost, and single sessions or subagents are
  better for sequential tasks or same-file edits. Reference:
  https://code.claude.com/docs/en/agent-teams
- Agent Orchestrator, Stoneforge, and similar open-source projects converge on
  isolated worktrees, task queues, handoff notes, PR/test feedback loops, and
  dashboard observability. References:
  https://github.com/ComposioHQ/agent-orchestrator
  https://github.com/stoneforge-ai/stoneforge
- Agentic Project Management stores project state outside the agent context and
  uses self-contained task prompts plus structured handoffs to avoid context
  loss. Reference:
  https://github.com/sdi2200262/agentic-project-management
- Open Multi-Agent uses generated task DAGs, MCP/tool presets, structured
  output validation, progress events, traces, and dashboards. Reference:
  https://github.com/open-multi-agent/open-multi-agent
- The Codex issue requesting a shared subagent message bus highlights a current
  limitation: native subagents coordinate mostly through the parent. A
  task-local append-only board is a practical control-plane feature for
  WLCodex. Reference:
  https://github.com/openai/codex/issues/21027

## Terms

- **Director**: WLCodex's deterministic orchestration layer plus a configured
  manager model turn when judgement is needed. The Director owns routing, planning,
  role assignment, implementation-choice buttons, gates, and final synthesis.
- **Role**: a reusable engineer type such as architect, implementer, tester, or
  auditor. A role is backend-agnostic and carries default skills, tool
  capabilities, permissions, artifact contract, and handoff rules.
- **Model Profile**: a configured runtime target such as `codex_gpt`,
  `claude_deepseek`, or a future provider/model pair.
- **Assignment Policy**: the configuration that maps roles to model profiles by
  default, risk level, cost mode, workspace, and user override. Every role has
  an assignment, even when there is currently only one viable default.
- **TeamRun**: the role-aware overlay for one existing Workbench auto/direct
  execution. It links to existing `orchestration_runs`, `agent_runs`, and
  tasks instead of replacing them.
- **AgentJob**: one role execution inside a TeamRun.
- **Stage**: a role-labeled phase inside the existing Codex -> implementation
  -> Codex verification flow.
- **Wave**: an optional group of AgentJobs that can run concurrently because
  their inputs and write scopes do not conflict. This is not required for the
  first release.
- **Context Packet**: the minimal task packet sent to a role. It contains role
  identity, task goal, evidence references, allowed tools, forbidden actions,
  and required output schema.
- **Capability Library**: the local inventory of reusable role definitions,
  skills, commands, rules, context snippets, and tool presets. The library is
  indexed by metadata and activated per AgentJob, not fully injected.
- **Skill Catalog**: a searchable subset of the Capability Library containing
  skill id, summary, triggers, allowed roles, required tools, and token cost.
- **Capability Budget**: the per-AgentJob limit for active tools, active
  skills, MCP servers, memory snippets, and prompt tokens.
- **Instinct Memory**: a compact durable lesson extracted from evidence. It has
  trigger, action, scope, confidence, evidence refs, and freshness metadata.
  Instincts are suggestions to the current role, not higher-priority commands.
- **Observation**: a raw learning candidate emitted from runtime events,
  artifacts, command results, or audit findings before it becomes an Instinct.
- **Skill Activation**: the recorded decision that a skill/tool/memory snippet
  was included in a specific Context Packet.
- **Artifact**: a structured output from an AgentJob, such as a diagnosis
  report, architecture plan, diff summary, test report, or audit finding list.
- **Team Board**: an append-only shared coordination surface for one TeamRun.
  It stores status updates, findings, handoff notes, and accepted decisions.

## Problem

WLCodex currently supports Codex-only, Claude-only, and Codex -> Claude ->
Codex workflows. This is useful, but it still behaves like one or two broad
agents doing several jobs:

- diagnosis, architecture, implementation, testing, and audit can blur
  together;
- the same model often has to find, fix, and judge its own work;
- specialist skills are available globally instead of being attached to the
  roles that actually need them;
- multi-step context can live inside model conversations instead of a durable
  WLCodex ledger;
- parallel work is hard to observe and steer from the Cockpit.

The goal is not to create a bigger prompt. The goal is to let WLCodex run a
small engineering organization when the task justifies it, while keeping the
current staged workflow intact.

## Compatibility With Existing Workflow

The feature is an overlay on existing WLCodex concepts.

| Existing concept | Role-aware interpretation |
| --- | --- |
| Plain text | Director decides whether to stay read-only or suggest a role-aware flow |
| `/codex` | Direct run using the configured Codex model profile for the selected role |
| `/claude` | Direct run using the configured Claude model profile for the selected role, usually Implementer today |
| `/auto` context collection | Director plus optional Investigator/Architect analysis |
| `生成最终方案` | Architect/Director creates a plan and implementation packet |
| `交给 Claude 执行` | Start the Implementer assignment backed by `claude_deepseek` |
| New `交给 Codex 执行` | Start the Implementer assignment backed by `codex_gpt` |
| `Codex 验收` | Start the Auditor assignment, defaulting to `codex_gpt` in the first release |
| `orchestration_runs` | Still owns staged-auto status |
| `agent_runs` | Still owns backend session/run records |
| `runtime_events` | Source of truth for role artifacts and TeamRun projection |

The first release must not introduce a second top-level workflow that bypasses
staged-auto. It enriches staged-auto with roles, artifacts, assignment policy,
and implementation-choice buttons.

## Goals

### G1. Roles are model-agnostic

The user can configure which model profile, tools, and skills each role uses.
A role must not hard-code Codex, Claude, or any future provider.

### G2. Multi-agent is adaptive, not mandatory

WLCodex must choose the smallest execution shape that can meet the goal:

```text
simple question or small edit -> single agent
medium bug or feature -> current staged-auto with explicit engineer roles
high-risk or broad task -> staged-auto plus extra diagnosis/test/audit gates
```

### G3. Use specialists to reduce blind spots

Roles should exist only when they add a different lens, tool set, or evidence
requirement:

- diagnosis investigates runtime evidence and hypotheses;
- architecture defines boundaries and acceptance criteria;
- implementation changes code;
- testing proves behavior;
- audit challenges the result.

### G4. Preserve context without replaying history

AgentJobs must receive Context Packets, not full chat transcripts. Task state
must live in WLCodex events and artifacts so a later role can continue without
depending on another model's private conversation.

Context Packet completeness is a first-class requirement because a TeamRun may
cross models and conversations. A downstream engineer must be able to continue
from the packet, artifact references, and tools even when the upstream model's
session is unavailable.

### G5. Make handoffs mechanically complete

Each gate must require the fields needed by downstream roles. Missing required
artifacts block the next phase instead of relying on the next agent to infer
lost context.

### G6. Keep token cost bounded

The Director must compile short packets, pass evidence references instead of
large logs or diffs, and let agents fetch details on demand through tools.

### G7. Keep one user-facing owner

The user talks to WLCodex. The Director decides what to show in Cockpit and
what to expose in Onsite. Specialist chatter is evidence, not the primary UI.

### G8. Keep writes controlled

By default, only one AgentJob may write product code in a workspace at a time.
Parallel writers require isolated worktrees or explicit file ownership.

### G9. Keep capability growth outside the base prompt

WLCodex may accumulate many skills, commands, role profiles, and memory
records, but a role run activates only the small subset justified by the task,
role, tools, and token budget.

### G10. Learn without stale replay

Long-term memory must help future runs continue from project experience, but it
must never outrank the current user request, current repo state, or current
runtime evidence. Memory is evidence-linked, scoped, confidence-rated, and
eligible for expiry or deprecation.

## Non-Goals

- Building a general SaaS multi-agent platform.
- Replacing Codex or Claude native subagents.
- Replacing the current Codex -> implementation -> Codex verification workflow.
- Automatically using many agents for every request.
- Letting agents freely message each other without a durable event trail.
- Treating model conversation history as source-of-truth memory.
- Copying ECC's full agent/skill/command count as a product goal. WLCodex
  should borrow the packaging and learning patterns, not maximize catalog size.
- Automatically injecting every known skill or memory into every role prompt.
- Guaranteeing perfect model judgement. The design can guarantee handoff data
  completeness, not semantic correctness.
- Auto-deploying production changes without human approval.

## Role Catalog

The initial role catalog has six core roles.

Each role has default skills, tool capabilities, and permissions in the role
catalog. Each role also has a configurable model assignment in the assignment
policy. The current first-release defaults may assign Director, Investigator,
Architect, Tester, and Auditor to `codex_gpt`, while Implementer offers
`claude_deepseek` and `codex_gpt`; that default is editable and must not be
encoded into the role definitions.

### Director

Purpose:

- classify the request;
- decide direct path vs staged auto;
- generate role-aware plan for the existing staged workflow;
- assign roles to model profiles;
- present implementation choices to the user when multiple implementers are
  configured;
- enforce gates;
- synthesize final output.

Default permissions:

- read ledger/artifacts;
- create TeamRuns, AgentJobs, Context Packets, and Team Board entries;
- no product code writes.

Outputs:

- `team_plan`
- `routing_decision`
- `final_synthesis`

### Investigator

Purpose:

- inspect symptoms, logs, status commands, failing tests, and runtime evidence;
- produce root-cause hypotheses with confidence and evidence references;
- identify missing observations.

Default permissions:

- read files;
- run approved read-only shell commands;
- inspect logs and runtime status;
- no product code writes.

Outputs:

- `diagnosis_report`

### Architect

Purpose:

- define fix boundary, impact surface, risk level, dependencies, and acceptance
  criteria;
- decide whether work is small enough for a single implementer or needs
  isolated work units;
- reject over-broad plans.

Default permissions:

- read files;
- use GitNexus impact/context tools;
- run read-only shell commands;
- no product code writes.

Outputs:

- `architecture_plan`
- `acceptance_criteria`

### Implementer

Purpose:

- apply the smallest defensible code change that satisfies the accepted plan;
- keep unrelated changes untouched;
- record changed files, commands, and notes.

Default permissions:

- read files;
- edit files;
- run targeted tests and formatters;
- no deployment actions.

Outputs:

- `implementation_report`
- `changed_files`
- `diff_summary`

Default first-release implementation profile examples:

- **`claude_deepseek` Implementer**: uses the Claude backend/session path and
  existing Claude permissions.
- **`codex_gpt` Implementer**: uses Codex direct or Codex subagent execution.
- Future implementer model profiles may be added without changing the
  Implementer role.

### Tester

Purpose:

- reproduce or validate behavior;
- add focused tests when the plan calls for them;
- run selected verification commands;
- report pass/fail with exact evidence.

Default permissions:

- read files;
- edit tests;
- run tests and read command output;
- no product code writes unless explicitly assigned by the Director.

Outputs:

- `test_report`

### Auditor

Purpose:

- independently review the final diff, test evidence, and risk areas;
- focus on P0/P1/P2 bugs, security, regressions, hidden coupling, and missing
  verification;
- block only when there is a concrete issue or missing evidence.

Default permissions:

- read files;
- read diff and artifacts;
- run read-only validation commands;
- no writes.

Outputs:

- `audit_report`
- `audit_decision`

## Model Profiles And Assignment Policy

Model profiles are configured independently from roles. The current local model
pool is expected to start with `codex_gpt` and `claude_deepseek`, but the
schema must allow new profiles without role changes.

Example conceptual configuration:

```yaml
model_profiles:
  codex_gpt:
    provider: codex
    model: gpt
    display_name: "Codex GPT"
    cost_tier: premium
  claude_deepseek:
    provider: claude
    model: deepseek-v4-pro
    display_name: "Claude DeepSeek"
    cost_tier: standard

assignments:
  director:
    default: codex_gpt
  investigator:
    default: codex_gpt
  architect:
    default: codex_gpt
  implementer:
    default_candidates: [claude_deepseek, codex_gpt]
    user_choice_required: true
  tester:
    default: codex_gpt
  auditor:
    default: codex_gpt
```

Rules:

- The user can override assignment policy, skills, and tool capability sets per
  role and workspace.
- For implementation after a final plan, the first release should present
  configured implementer choices instead of silently choosing one when more
  than one viable implementer exists.
- For non-implementation roles, the first release may start with one default
  assignment per role, but the UI/config model must allow changing them.
- Director may escalate a role to a stronger model when risk is high.
- Director may downgrade to a cheaper model for bounded, low-risk work.
- If a model profile is unavailable, WLCodex uses the configured fallback and
  records the substitution.
- Role prompts must not mention a fixed provider except where provider-specific
  tool behavior matters.

## Capability Library And Skill Activation

ECC's strongest reusable idea is the layered capability library:

```text
agents / roles
  -> skills
  -> commands / workflows
  -> rules / observers
  -> contexts / memory
  -> tool and MCP presets
```

WLCodex should implement this idea in a smaller, backend-agnostic form.

Design rules:

- Roles stay few and stable: Director, Investigator, Architect, Implementer,
  Tester, Auditor.
- The Capability Library may grow freely, but the Director activates only
  relevant capabilities for one AgentJob.
- Skill metadata is cheap: id, summary, triggers, allowed roles, required
  tools, contraindications, and estimated token cost.
- Skill bodies are loaded only when selected, matching Codex's progressive
  disclosure pattern and avoiding a huge universal system prompt.
- Commands are workflow macros, not new roles. Examples: `/learn`,
  `/instinct-status`, `/role-audit`, `/team-compact`.
- Rules and hooks become WLCodex runtime observers instead of Claude-specific
  hook scripts. They listen to `runtime_events`, produce Observations, and never
  mutate product code.
- Tool presets are capability bundles such as `logs_readonly`,
  `gitnexus_impact`, `tests_write`, or `diff_audit`.

Skill activation algorithm:

1. Start from role defaults in the Role Catalog.
2. Add assignment-policy overrides from workspace config.
3. Match task text, route, risk, file scope, and prior artifacts against skill
   triggers.
4. Remove skills whose required tools are unavailable to the selected model
   profile.
5. Enforce Capability Budget.
6. Record every selected skill/tool/memory snippet as a Skill Activation event.

This lets WLCodex grow toward ECC-like breadth without forcing every task to
pay for that breadth.

Role Config Audit:

- WLCodex should inspect configured role capabilities before a TeamRun starts.
- Reviewer-style roles such as Auditor and Investigator must not silently gain
  write, deploy, secret-read, or destructive-command tools through config.
- Any high-risk capability assignment is surfaced to the Director and Cockpit
  before the role runs.
- This is the WLCodex equivalent of an AgentShield-style check: permissions,
  prompt-injection-like instructions, and over-broad tool grants are treated as
  configuration risk before model execution begins.

## Instinct Memory And TeamObserver

WLCodex should borrow ECC's memory idea, but store memory in the WLCodex ledger
instead of relying on one model provider's session hooks.

An Instinct Memory is a small record:

```json
{
  "id": "instinct:project:wlcodex:001",
  "scope": "project | workspace | global",
  "domain": "debugging | architecture | testing | audit | ops",
  "trigger": "When staged-auto verification starts after a non-Codex implementer",
  "action": "Include implementation summary, changed files, and missing evidence in the Auditor packet.",
  "confidence": 0.82,
  "evidence_refs": ["team_run=17", "audit_report=42"],
  "created_at": "2026-05-25T00:00:00Z",
  "last_validated_at": "2026-05-25T00:00:00Z",
  "status": "candidate | active | deprecated"
}
```

Memory lifecycle:

- `runtime_events` and artifacts produce Observations.
- TeamObserver groups Observations into candidate Instincts.
- Candidate Instincts become active only after repeated evidence, user
  acceptance, or Auditor confirmation.
- Active Instincts can be deprecated when contradicted by newer evidence.
- Instincts include evidence refs and confidence so later roles can inspect why
  the memory exists.

Memory selection for a Context Packet:

- match by workspace, role, task domain, current files/modules, route, and risk;
- prefer project/workspace instincts over global instincts;
- cap by token budget and confidence threshold;
- include only concise trigger/action text plus evidence refs;
- label all memories as historical advice, never current facts.

This design solves the practical "AI amnesia" problem without pretending that
old conversations are reliable source of truth.

## Execution Shapes

### Shape 1: Single Agent

Used for:

- read-only answers;
- small local edits;
- clear one-file fixes;
- low-risk docs or test updates.

Flow:

```text
Director routes through existing /codex or /claude direct path
  -> role runs
  -> optional verification
  -> final response
```

### Shape 2: Existing Staged Auto With Roles

Used for normal bug fixes and features that need planning, implementation, and
verification.

Flow:

```text
configured Director/Architect creates final plan
  -> user chooses `claude_deepseek` Implementer or `codex_gpt` Implementer
  -> selected Implementer executes
  -> configured Auditor verifies, defaulting to `codex_gpt` in first release
```

### Shape 3: Staged Auto With Diagnosis And Test/Audit Gates

Used for high-risk, unclear, runtime, or cross-layer tasks.

Flow:

```text
Investigator gathers runtime/code evidence
Architect creates accepted plan
Gate A: accepted plan and acceptance criteria
User chooses Implementer profile
Implementer executes
Gate B: changed files and diff summary
Tester validates, or Auditor explicitly performs tester duties
Gate C: test report
Auditor verifies
Gate D: audit decision
Director final synthesis
```

### Shape 4: Parallel Work Units

Used only when the architecture plan identifies independent file scopes or
worktree isolation. This shape is out of first-release scope unless the
existing workspace/worktree controls already make it safe.

Flow:

```text
Architect decomposes work units
Director creates one worktree or ownership scope per writer
Implementers run in parallel
Tester/Auditor integrate results
Director resolves merge or rejects unsafe parallelism
```

## Routing Policy

The Director classifies each request with a structured decision. The decision
must map to an existing WLCodex execution route unless a later release
explicitly adds a new route.

```json
{
  "route": "codex_direct | claude_direct | staged_auto | staged_auto_with_extra_gates",
  "risk": "low | medium | high | critical",
  "reason": "short explanation",
  "roles": ["investigator", "architect", "implementer", "tester", "auditor"],
  "assignments": {
    "investigator": "codex_gpt",
    "architect": "codex_gpt",
    "implementer": ["claude_deepseek", "codex_gpt"],
    "tester": "codex_gpt",
    "auditor": "codex_gpt"
  },
  "requires_user_approval": false
}
```

Critical risk includes production deploys, destructive commands, credential
changes, database migrations, security policy changes, broad refactors, and
unclear work that can affect user data.

## Context Packet Contract

Every AgentJob receives a Context Packet. It is the only required input for the
role run. This packet is the handoff contract across models, sessions, and
conversations.

Required fields:

| Field | Meaning |
| --- | --- |
| `team_run_id` | Durable TeamRun id |
| `agent_job_id` | Durable AgentJob id |
| `role` | Role id |
| `model_profile` | Assigned profile id |
| `user_goal` | Current user goal |
| `role_mission` | One concise mission for this role |
| `workspace_alias` | Active workspace |
| `conversation_id` | Current WLCodex Workbench id |
| `orchestration_run_id` | Existing staged-auto run id when available |
| `allowed_tools` | Tool/capability names |
| `forbidden_actions` | Explicit restrictions |
| `skills` | Role-specific skill ids to load or mention |
| `inputs` | Prior artifacts and concise summaries |
| `evidence_refs` | References to events, files, diffs, logs, tests |
| `resume_state` | What has already happened and what remains next |
| `open_questions` | Explicit unknowns that must not be silently assumed |
| `relevant_instincts` | Short historical lessons selected by scope, confidence, and relevance |
| `capability_budget` | Active skill/tool/MCP/memory/token limits for this role run |
| `skill_activations` | Skill and tool ids selected for this packet, with source policy |
| `historical_context_policy` | Statement that old memory is advisory and current evidence wins |
| `source_refs` | Source event/artifact/config ids used to compile the packet |
| `output_schema` | Required structured output |
| `handoff_rules` | What downstream roles need |

Packet rules:

- Do not include full transcripts.
- Do not inline long logs, raw diffs, or full files.
- Include paths and event ids so agents can fetch details on demand.
- Include the current user message and acceptance criteria.
- Include only role-relevant skills. Skills are static role context, not copied
  from every other role.
- Include a precedence statement: current user goal and role mission outrank
  historical artifacts.
- Include a stale-replay guard: historical memories and prior artifacts are
  advisory only unless confirmed by current evidence.
- Include enough resume state for a fresh model session to continue without the
  upstream chat history.
- Include output validation rules so WLCodex can reject incomplete handoffs
  before starting the next role.
- Include compact artifact summaries plus evidence references. The receiving
  role should fetch details through tools instead of receiving everything in
  prompt text.
- Include relevant Instincts only when their scope, confidence, and triggers
  match the current role run.
- Include a Capability Budget so the backend knows which tools and skills are
  active and which ones are intentionally unavailable.

The packet must have two renderings:

- a machine JSON form stored in artifacts/events for deterministic recovery;
- a compact prompt form sent to the selected model profile.

The JSON form is canonical. Prompt text is a lossy rendering of that JSON plus
role instructions.

## Artifact Contracts

Artifacts are structured JSON plus a short human summary. The JSON is used for
gates and downstream packets. The summary is used for Cockpit.

### Diagnosis Report

Required fields:

- `summary`
- `hypotheses`
- `evidence_refs`
- `commands_run`
- `recommended_next_steps`
- `confidence`

### Architecture Plan

Required fields:

- `summary`
- `files_or_modules_in_scope`
- `files_or_modules_out_of_scope`
- `impact_notes`
- `risk_level`
- `implementation_steps`
- `acceptance_criteria`
- `parallelization_policy`

### Implementation Report

Required fields:

- `summary`
- `changed_files`
- `diff_summary`
- `commands_run`
- `tests_attempted`
- `known_limitations`

### Test Report

Required fields:

- `summary`
- `commands_run`
- `passed`
- `failed`
- `coverage_of_acceptance_criteria`
- `failure_evidence`

### Audit Report

Required fields:

- `decision` with value `pass`, `block`, or `needs_user`
- `summary`
- `findings`
- `missing_evidence`
- `risk_level`
- `recommended_next_action`

## Gates

Gates are deterministic checks run by WLCodex before starting the next wave.

### Gate A: Plan Ready

Requires:

- architecture plan exists;
- risk level exists;
- acceptance criteria are non-empty;
- write scope is explicit;
- high or critical risk has user approval before implementation.

### Gate B: Implementation Ready For Test

Requires:

- implementation report exists;
- changed files are listed;
- diff summary exists;
- product code was written by an allowed writer;
- no unapproved destructive command was run.

### Gate C: Test Evidence Ready

Requires:

- test report exists;
- every acceptance criterion is marked covered or uncovered;
- each command has command text, exit status, and summary;
- failing tests are preserved as evidence.

### Gate D: Audit Ready For Closure

Requires:

- audit report exists;
- audit decision is explicit;
- block findings include exact file/path/evidence references;
- pass decisions mention the test evidence reviewed.

## Team Board

The Team Board is append-only and task-local.

First-release scope note: the first release does not add a
`team_board_entries` projection table or peer-to-peer board workflow. Board
entries are a later feature. Until that later slice lands, role coordination is
represented by `runtime_events`, `team_artifacts`, Context Packets, and Cockpit
summaries.

Entries:

- `status`
- `finding`
- `handoff_note`
- `decision_proposed`
- `decision_accepted`
- `question_for_user`

Rules:

- Agents append; they do not overwrite another agent's notes.
- The Director is the only writer of accepted decisions.
- The Director decides what enters downstream Context Packets.
- Board entries are stored as runtime events and can be shown in Onsite.
- A later release may add a `team_board_entries` projection for faster board
  rendering; that table is not required for this release.

## Runtime Events

Add event families on top of the existing runtime event system.

Team run:

- `team.run.requested`
- `team.run.routed`
- `team.run.started`
- `team.run.completed`
- `team.run.failed`
- `team.run.cancelled` (later scope)

Agent job:

- `team.agent_job.queued`
- `team.agent_job.started`
- `team.agent_job.completed`
- `team.agent_job.failed`
- `team.agent_job.blocked` (later scope)

Wave:

- `team.wave.started` (later scope)
- `team.wave.completed` (later scope)
- `team.wave.failed` (later scope)

Artifacts and gates:

- `team.context_packet.recorded`
- `team.artifact.recorded`
- `team.gate.started` (later scope)
- `team.gate.passed`
- `team.gate.failed`

Board:

- `team.board.entry_appended` (later scope)
- `team.board.decision_accepted` (later scope)

Assignment:

- `team.assignment.selected`
- `team.assignment.fallback_used`

Capability and memory:

- `team.skill_activated`
- `team.capability_budget.applied`
- `team.observation.recorded`
- `team.instinct.proposed`
- `team.instinct.promoted`
- `team.instinct.deprecated`
- `team.instinct.selected`

## Persistence Model

The runtime event log remains the source of truth. Projection tables make
queries fast and simple.

Projected tables:

- `team_runs`
- `team_agent_jobs`
- `team_context_packets`
- `team_artifacts`
- `team_assignments`
- `team_skill_activations`
- `team_observations`
- `team_instincts`

Existing `agent_runs`, `orchestration_runs`, and `tasks` remain compatibility
surfaces. Each AgentJob may link to one or more existing `agent_runs`.

First-release persistence rule:

- `team_runs` may be a projection/metadata table keyed to an existing
  `orchestration_run_id` for `/auto`.
- `team_agent_jobs` link to existing `agent_runs` whenever a backend turn
  exists.
- each AgentJob has exactly one canonical Context Packet JSON recorded before
  the backend run starts; prompt text is a rendering of that JSON.
- role artifacts are recorded in `team_artifacts` and emitted as runtime
  events.
- role-specific skill/tool/memory selections are recorded in
  `team_skill_activations`.
- TeamObserver writes raw learning candidates to `team_observations`; promoted
  lessons live in `team_instincts`.
- existing staged-auto columns such as `last_codex_analysis`,
  `last_claude_summary`, and `last_verification_result` remain populated for
  backward compatibility.

## Recovery

First-release recovery is limited to durable projection persistence: TeamRuns,
AgentJobs, Context Packets, Artifacts, assignments, skill activations,
Observations, and Instincts survive ledger reopen and remain visible. Startup
rebuild of active TeamRuns from runtime events is a later recovery slice.

Later startup recovery rebuilds active TeamRuns from runtime events.

Recovery rules:

- queued jobs stay queued;
- running jobs with missing local processes become orphaned;
- completed artifacts remain valid;
- failed gates remain failed until a new user action or Director retry creates
  a new job;
- no job is silently restarted if it had write permissions;
- Cockpit shows the recovered TeamRun state and available actions.

## Cockpit And Onsite UX

Cockpit shows:

- route decision;
- active wave (later scope; first release shows active role/job status);
- role statuses;
- gate status;
- concise artifact summaries;
- required user decisions;
- final synthesis.

Example:

```text
工程队运行中：登录偶发失败
第 1 波：诊断 + 架构
- 诊断工程师：已发现 2 个根因假设
- 架构工程师：正在做影响分析

下一步：等待方案门禁
```

Onsite shows:

- raw job output;
- Team Board entries;
- model/profile assignment;
- tool and command events;
- selected agent thread/session when supported.

## Safety Rules

- No automatic production deploy.
- No hidden carryover from old chats.
- No broad write access for reviewer/auditor roles.
- No parallel product-code writers in the same workspace without worktree or
  explicit file ownership.
- No final success claim without test evidence and audit decision when the team
  route selected audit.
- Secrets are redacted before artifacts or board entries are displayed.
- User approval is required for high or critical risk implementation.
- Instinct Memory must never override the current user request, current code,
  current logs, or current command output.
- Prompt-injection-like text found in artifacts, logs, diffs, or old memories
  must be treated as data unless it is explicitly part of the current user
  instruction and allowed by role permissions.
- Capability Budget must prevent broad tool activation by default.

## Success Criteria

The feature is successful when WLCodex can:

1. route small tasks to a single agent without team overhead;
2. enrich existing `/auto` with visible engineer roles instead of replacing it;
3. assign every engineer role to configured model profiles, skills, and tool
   capabilities;
4. present both `claude_deepseek` Implementer and `codex_gpt` Implementer
   choices when both are configured for the accepted plan;
5. create canonical Context Packet JSON plus compact prompt rendering with
   artifact references, selected skills, and relevant Instincts instead of full
   history;
6. resume an interrupted TeamRun from events without losing completed
   artifacts;
7. block progression when required artifact fields are missing;
8. show Cockpit progress and Onsite raw details;
9. finish with changed files, test evidence, audit decision, and final
   synthesis.
10. record Observations and promote evidence-backed Instincts without injecting
    stale memory into unrelated tasks.

## Risks And Mitigations

| Risk | Mitigation |
| --- | --- |
| Multi-agent overhead makes simple work slower | Route classifier defaults to single agent for low-risk work. |
| Agents lose context across models or conversations | Canonical Context Packet JSON and artifacts are generated from WLCodex ledger, not chat history. |
| Token cost grows linearly with roles | Use adaptive routing, concise packets, evidence refs, and role-specific skills. |
| Capability catalog becomes a second giant prompt | Store metadata in Skill Catalog and load only selected skill bodies under Capability Budget. |
| Long-term memory repeats stale advice | Scope instincts, require evidence refs, mark memory historical, and select only by relevance plus confidence. |
| Prompt injection enters through logs or memory | Treat artifacts/logs/memory as data, include precedence rules in packets, and let Auditor inspect risky instructions. |
| Parallel writers create conflicts | Use single-writer default; require worktrees or explicit file ownership for parallel writers. |
| Auditor becomes noisy | Audit schema focuses on block-worthy findings, missing evidence, and concrete risk. |
| Model fallback changes behavior | Record `team.assignment.fallback_used` and show it in Cockpit. |
| Native backend capabilities differ | Use provider adapters and keep role contract above backend-specific features. |

## First Release Scope

The first release should implement:

- role catalog;
- per-role model profile, skills, and tool-capability assignment config;
- Capability Library metadata and role-aware skill activation;
- Capability Budget enforcement for selected tools, skills, memory, and prompt
  tokens;
- Role Config Audit for over-broad reviewer permissions and prompt-injection
  risk in configured instructions;
- TeamRun and AgentJob projections linked to existing staged-auto runs;
- canonical Context Packet compiler with JSON storage and compact prompt
  rendering, relevant Instincts, skill activations, and stale-replay guard;
- TeamObserver, Observations, and Instinct Memory tables with conservative
  promotion rules;
- role-aware `/auto` context/final-plan/implementation/verification stages;
- `claude_deepseek` Implementer and `codex_gpt` Implementer implementation
  choices;
- single writer enforcement;
- artifact schemas and gates;
- Cockpit summaries;
- tests for routing, context minimization, gate blocking, and projection
  persistence across ledger reopen.
- tests for skill selection, capability-budget trimming, memory selection, and
  stale-memory precedence.

The first release should not implement:

- fully autonomous PR shepherding;
- wave/parallel execution beyond single-writer staged-auto;
- `team_board_entries` projection table or Team Board append/decision workflow;
- startup rebuild of active TeamRuns from runtime events;
- general external issue tracker integration;
- peer-to-peer agent messaging;
- ECC-scale agent/skill/command migration;
- automatic memory injection without relevance and confidence filters;
- automatic rewriting of role assignments based on memory alone;
- a replacement orchestrator that bypasses staged-auto;
- auto-merge;
- production deploy actions.
