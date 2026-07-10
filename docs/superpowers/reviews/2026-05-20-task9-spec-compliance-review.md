# Spec Compliance Review — Task 9: Documentation And Config Alignment

> **SUPERSEDED — historical review only.** It is not a current release result;
> use [the current semantic contract](../../product-semantics.md) and current tests.

**Reviewer**: Spec Compliance Reviewer (independent)
**Date**: 2026-05-20
**Verdict**: **PASS**

---

## Plan Task 9 Steps: 3/3

| Step | Requirement | Status |
|------|-------------|--------|
| Step 1 | Update README product language: Remote workbench, Cockpit view, Onsite view, default Codex → Claude → Codex workflow, Codex-only direct mode, Claude-only direct mode | PASS |
| Step 2 | Update example config comments — keys stable, operator-facing comments, no config leak to normal user copy | PASS |
| Step 3 | `pytest tests/test_config.py tests/test_workbench_cockpit_menu.py -q` | 23 passed |

## Plan Final Verification Step 4: User-Facing Copy Audit

### Required Terms Present

| Term | README Hits | Location |
|------|-------------|----------|
| 驾驶舱 | 7 | Intro, terminology table, views table, view switching, smoke criteria |
| 接管现场 | 3 | Terminology (action), daily menu label, smoke criteria |
| 现场 (view name) | 5 | Intro, terminology, views table, view switching |
| 默认流程：Codex → Claude → Codex | 5+ | Terminology, execution modes, primary commands, smoke |
| 只问 Codex | 1 | Terminology table |
| 只叫 Claude | 1 | Terminology table |
| 让 Codex 验收 | 2 | Terminology table, execution modes prose, smoke |

### Forbidden Terms Absent (Normal User Copy)

| Term | README | Config |
|------|--------|--------|
| `terminal.enabled` | 0 (only in smoke item 15 as negative check) | 0 |
| `external_session_id` | 0 | 0 |
| `session id` (user-facing) | 0 (only in Cockpit Hides column) | 0 |
| `thread id` (user-facing) | 0 (only in smoke item 5 negative list) | 0 |
| `runtime_events` | 0 | 0 |
| `projection` | 0 | 0 |

## Spec Acceptance Criteria: 15/15

| AC | Description | README Coverage | Status |
|----|-------------|-----------------|--------|
| 1 | Ordinary text → Codex → Claude → Codex | Execution modes table + Primary commands + Smoke item 9 | PASS |
| 2 | `/codex` runs Codex-only, never calls Claude | Execution modes table + Primary commands + Smoke item 7 | PASS |
| 3 | `/claude` runs Claude-only, no auto-Codex | Execution modes table + Primary commands + Smoke item 8 | PASS |
| 4 | Claude-only offers "让 Codex 验收" | Terminology table + Execution modes prose + Smoke item 8 | PASS |
| 5 | `/terminal` never dead session | View switching bullet + Smoke item 12 | PASS |
| 6 | Onsite auto-attaches to active agent | View switching table `/terminal` description | PASS |
| 7 | Onsite start actions when no session | View switching bullet + Smoke item 12 | PASS |
| 8 | Onsite text → only live session | View switching bullet + Smoke item 13 | PASS |
| 9 | No raw terminal replay on Cockpit return | View switching bullet "stops Telegram output delivery" | PASS |
| 10 | Independent cursors | Recovery section "restores Cockpit and Onsite cursors" | PASS |
| 11 | Restart reconstructs workbench | Recovery section | PASS |
| 12 | Menu: daily phone actions | Daily menu table (6 commands, exact match) | PASS |
| 13 | Help: user language, no config keys | Smoke item 15 | PASS |
| 14 | Raw terminal redacted | Safety rules bullet "Onsite frames are redacted" | PASS |
| 15 | View switching ≠ restart work | Views section "Switching views does not restart work" + Smoke item 14 | PASS |

## Semantic Drift: NONE

Initial write had 3 semantic issues, all fixed during review cycle:

| Issue | Fix |
|-------|-----|
| "Onsite view → 接管现场" (action name used as view name) | Changed to "Onsite view → 现场" |
| `cockpit` profile described as "enables richer Cockpit view rendering" | Changed to "reserved; currently behaves like legacy" (verified against `profiles.py:63`) |
| `natural` described as "the default Cockpit chat surface" (conflated profile with view) | Changed to "recommended interaction style for the Cockpit view" |

All 9 spec name mappings (Internal → User-facing) verified against actual code output:

| Internal | User-facing | Code Evidence |
|----------|-------------|---------------|
| Product Surface | 驾驶舱 | `status.py:371` |
| Terminal Surface | 现场 | `renderer.py:18` |
| Conversation/session ids | Workbench | README terminology table |
| Attach/detach | 接管现场/回驾驶舱 | `status.py:379` / `telegram_app.py:845` |
| Direct agent mode | 只问 Codex / 只叫 Claude | `telegram_app.py:662` |
| Orchestration | 默认流程：Codex → Claude → Codex | `status.py:370` |

## Unauthorized Files: NONE

`git diff HEAD --name-only`: only `README.md` and `config/wlcodex.example.toml`.
All other modified files belong to Tasks 1-8 per plan ownership table.

## Config Keys Changed: NONE

6 comment additions/updates across 5 sections. Zero keys modified, zero keys added, zero keys removed.

## Required Fix (Applied)

Smoke criteria conclusion paragraph restored (was removed during rewrite):
```
The human smoke is considered passed only when the visible Telegram behavior and
the ledger state both match the criteria above. A green unit test run alone is
not enough evidence for this smoke.
```
