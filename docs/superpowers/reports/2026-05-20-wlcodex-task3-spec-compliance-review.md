# Spec Compliance Review — Task 3: Workbench Command Parsing

**Date**: 2026-05-20
**Reviewer**: Claude Opus 4.7
**Reviewed files**: `wlcodex/router.py` (+8 lines), `tests/test_workbench_commands.py` (new)
**Verdict**: PASS

---

## Plan Step Compliance

| Plan Step | Requirement | Status |
|---|---|---|
| Step 1 | Impact analysis (parse_command + ModeSwitchCommand) | LOW risk, executed |
| Step 2 | Write failing tests for /terminal, /product, /settings, /codex, /claude, /auto + compatibility subcommands | 15 tests, full coverage |
| Step 3 | Red phase: only /settings tests fail (3), compatibility tests pass (69) | Verified |
| Step 4 | Add SettingsCommand dataclass, parse /settings | Done |
| Step 5 | Do not remove existing dataclasses, do not change existing parser outputs | 30+ compatibility assertions all PASS |
| Step 6 | Green phase: all tests pass | 55 parser-scope tests PASS |

## Spec Acceptance Criteria

| # | Criterion | Scope | Status |
|---|---|---|---|
| 1 | Ordinary text starts default Codex→Claude→Codex | Not parser (Task 5) | N/A |
| 2 | /codex runs Codex-only | Parser → CodexDirectCommand | PASS |
| 3 | /claude runs Claude-only | Parser → ClaudeDirectCommand | PASS |
| 4 | Claude-only offers verify action | Not parser (Task 5) | N/A |
| 5 | /terminal never dead session | Parser output correct; handler responsibility (Task 4/7) | PASS (parser layer) |
| 6-15 | Various view/onsite/recovery criteria | Not parser scope | N/A |

## Semantic Drift

None. Three command categories are cleanly separated in the parser:

| Dimension | Commands | Parser Output |
|---|---|---|
| View switch | /terminal, /product | ModeSwitchCommand(mode="terminal"/"product") |
| Execution mode | /codex, /claude, /auto | CodexDirectCommand, ClaudeDirectCommand, AutoModeCommand |
| Menu entry | /settings | SettingsCommand() |

No user-facing config keys (`terminal.enabled`, `external_session_id`, `thread id`, `session id`, `runtime_events`) appear in parser output.

## Unauthorized Files

None. Plan authorizes `wlcodex/router.py` + `tests/test_workbench_commands.py`. Only these two files were modified.

## Blocking Issues

None.
