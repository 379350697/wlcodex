# Spec Compliance Review — Task 2: Cockpit Menu And Help UX

> Superseded on 2026-05-20 by
> `docs/superpowers/reviews/2026-05-20-deep-repair-spec-compliance-review.md`.
> This review used the older `/new` = "新任务" and `/sessions` legacy-menu
> framing. Current product semantics are Workbench-first: `/new` creates a new
> Workbench, and `/sessions` lists historical agent sessions for the active
> Workbench.

**Reviewer:** Spec Compliance Reviewer
**Date:** 2026-05-20
**Verdict: PASS** (1 notation)

## Blocking Issues

None.

## Plan Step Coverage

| Plan Step | Requirement | Status |
|-----------|-------------|--------|
| Step 1 | Impact analysis on `build_bot_commands`, `render_conversation_help` | LOW risk confirmed |
| Step 2 | Failing menu tests: exact 6 commands, 11 hidden | Done |
| Step 3 | Failing help-copy tests: required phrases, forbidden phrases | Done |
| Step 4 | Confirm test failure before implementation | Done |
| Step 5 | Update menu/help copy, keep legacy handlers intact | Done |
| Step 6 | Focused tests pass | 25 passed |

## Menu Design (Spec §Menu Design) — Line-by-line

| Spec Requirement | Implementation | Match |
|-----------------|----------------|-------|
| `/new` → 新任务 | `/new` → 新任务 | Exact |
| `/status` → 状态 | `/status` → 状态 | Exact |
| `/terminal` → 接管现场 | `/terminal` → 接管现场 | Exact |
| `/diff` → 变更 | `/diff` → 变更 | Exact |
| `/settings` → 设置 | `/settings` → 设置 | Exact |
| `/help` → 帮助 | `/help` → 帮助 | Exact |

Typed commands remain available: `/codex`, `/claude`, `/auto`, `/model`, `/claude_mode`, `/sessions`, `/switch`, `/health`, `/files` — all in legacy menu, hidden from natural, still typeable.

Legacy diagnostic commands hidden: `/task`, `/continue`, `/steer`, `/tail`, `/events`, `/pause`, `/abort`, `/archive`, `/fork` — not in either menu profile.

## Startup And First Use (Spec §Startup And First Use) — Line-by-line

| Spec Template | Implementation | Match |
|--------------|----------------|-------|
| WLCodex 已连接 | WLCodex 已连接 | Exact |
| 默认流程：Codex → Claude → Codex | 默认流程：Codex -> Claude -> Codex | Exact (arrow glyph difference only) |
| 当前视图：驾驶舱 | 当前视图：驾驶舱 | Exact |
| 工作区：wlcodex | 工作区：当前项目 | Notation 1 |
| Codex：可用 | Codex：可用 | Exact |
| Claude：可用 | Claude：可用 | Exact |
| 现场接管：可用 | 现场接管：可用 | Exact |
| 直接发消息开始。 | 直接发消息开始。 | Exact |
| [新任务] [接管现场] [设置] | [新任务] [接管现场] [设置] | Exact |

## Acceptance Criteria — Task 2 Relevant

| AC# | Requirement | Status |
|-----|-------------|--------|
| 12 | Menu contains only daily phone actions | 6 items match spec exactly |
| 13 | Help explains product in user language, not config keys | No config keys in any help variant |

AC 1–11, 14–15 are outside Task 2 scope.

## Forbidden Term Audit

Terms verified absent from all three help variants (natural, legacy, render_help):

- `terminal.enabled` — absent
- `external_session_id` — absent
- `session id` — absent
- `thread id` — absent
- `双面模式` — absent
- `产品模式` — absent
- `手机端模式` — absent
- `远程终端模式` — absent
- `终端模式` (as primary product language) — absent

## Remote Workbench Model Compliance

- One workbench, Cockpit/Onsite two views — "当前视图：驾驶舱", "接管现场"
- Default Codex → Claude → Codex preserved — all three help variants state it
- Codex-only / Claude-only remain explicit — `/codex`, `/claude` preserved as typed commands
- Execution mode ≠ View mode — menu controls view navigation; execution routing is Task 5
- Old "双面模式" language fully removed — "驾驶舱与现场" used instead
- "终端"/"terminal" replaced with "现场"/"onsite" in all descriptions

## Unauthorized Files

Task 2 write ownership: `wlcodex/menu.py`, `wlcodex/status.py`, `tests/test_workbench_cockpit_menu.py`.

Only these files were modified. Other files in `git diff --stat` belong to parallel tasks (1, 3–7, 9).

## Known Breakage (outside Task 2 scope)

`tests/test_telegram_conversation_handlers.py::test_natural_bot_commands_are_compact` — asserts old natural menu shape. Belongs to Task 7's write scope. Not a Task 2 defect.

## Notation 1: Workspace Placeholder

- **Location:** `wlcodex/status.py` line 372, `"工作区：当前项目"`
- **Spec:** `工作区：wlcodex` (example workspace alias)
- **Assessment:** `render_conversation_help` is a static function with no access to runtime workspace state. Hardcoding `wlcodex` would be more misleading in non-wlcodex workspaces. `当前项目` is a reasonable static placeholder.
- **Severity:** Minor — architecturally justified. Dynamic workspace display belongs in Task 5/7 controller layer.
