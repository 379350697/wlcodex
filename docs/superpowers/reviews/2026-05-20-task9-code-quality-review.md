# Code Quality Review — Task 9: Documentation And Config Alignment

> Superseded on 2026-05-21 by the Remote Workbench deep repair documentation
> cleanup. This review was written before `/new` copy changed from the old
> task-led label to the current Workbench-first label. Current product docs and
> config comments treat `/new` as "新工作台" and live smoke evidence as
> Workbench/runtime-event evidence.

**Reviewer**: Code Quality Reviewer (independent)
**Date**: 2026-05-20
**Verdict**: **PASS**

---

## Quality Blockers: NONE

## Non-blocking Notes

### NB-1: Smoke criteria conclusion restored (post Spec Review)

The paragraph establishing dual-verification standard (visual + ledger) for smoke testing was removed during the product-language rewrite. Restored before `## Live Telegram Smoke`. No other text was deleted without justification.

### NB-2: Config `[claude]` comment "falls back to Codex-only" is slightly imprecise

```toml
# When disabled, the default Codex -> Claude -> Codex workflow falls back to
# Codex-only; the user can still use 只问 Codex.
```

The code (`controller.py:1489-1492`) informs the user and offers Codex-only as an explicit option rather than silently falling back. The spec (line 414-422) also describes it as informing + offering. The comment is adequate for operator orientation.

**Severity**: Low. No fix required.

### NB-3: README arrow style variation is domain-correct

Terminology table uses `->` (ASCII) to match actual Telegram user-facing text (`status.py:370`). English prose tables use `→` (Unicode) for typographic quality. Not an inconsistency — the terminology column documents exact user-facing text.

---

## Change Quality Assessment

### Precision ✓

- 134 insertions, 84 deletions across exactly 2 owned files (README.md, config/wlcodex.example.toml)
- Zero config keys modified
- Every deletion justified by spec alignment (old product language → new)
- Two unrelated double-space → single-space fixes in Task Liveness section — minimal, non-destructive

### No Over-Abstraction ✓

- Product terminology table is a glossary, not a premature abstraction layer
- No new conceptual framework — all terms directly from spec
- Execution modes and Views kept as separate tables (matching spec's explicit separation of "who does work" vs "how you see work")

### No Pattern Breakage ✓

- Command tables maintain existing column structure
- Section hierarchy preserved (`##`, `###`, `####`)
- Code blocks and lists follow existing conventions
- New Recovery section follows existing section style

### No Duplication ✓

- Terminology table maps internal→user concepts
- Execution modes table maps modes→triggers→behavior
- Views table maps views→purpose→what's shown/hidden
- Each serves a distinct semantic purpose

### Unauthorized File Touches ✓

`git diff HEAD --name-only` confirms only README.md and config/wlcodex.example.toml modified.

---

## Drift Risk: NONE

Every documentation claim cross-checked against actual code:

| README claim | Code evidence | Match |
|-------------|---------------|-------|
| Daily menu = 6 commands | `menu.py`: `_NATURAL_COMMANDS` has exactly 6 entries | ✓ |
| Cockpit hides session IDs | `status.py`: `render_conversation_help` doesn't expose session IDs | ✓ |
| Onsite = 现场 | `renderer.py:18`: `f"现场 · {agent} · {phase} · running"` | ✓ |
| Start card on no session | `manager.py:28`: `START_CARD = "start_card"` | ✓ |
| Recovery replays events | `runtime_state.py:982-999`: workbench state fields | ✓ |
| Execution modes: Codex/Claude direct | `controller.py:1181,1278`: `CODEX_DIRECT` / `CLAUDE_DIRECT` | ✓ |
| Cockpit profile = reserved → legacy | `profiles.py:63`: `normalized in {"legacy", "cockpit"}` → `LegacyProfile()` | ✓ |
| Redaction on Onsite frames | `redaction.py`: `redact_terminal_text()` | ✓ |
| Help uses 驾驶舱/接管现场 | `status.py:371,379` | ✓ |
| "只问 Codex" button | `telegram_app.py:662` | ✓ |

---

## Semantic Consistency: Config Comments → Code

| Config Comment | Code Behavior | Match |
|----------------|---------------|-------|
| `chief_engineer = default Codex -> Claude -> Codex` | `controller.py` routes plain text to orchestrated mode | ✓ |
| `Controls the default Codex -> Claude -> Codex workflow` | `orchestration.enabled` gates orchestration runner | ✓ |
| `Claude backend for default orchestrated + Claude-only direct` | Claude used in both orchestration step + `handle_claude_direct` | ✓ |
| `cockpit = reserved; currently behaves like legacy` | `profile_from_name("cockpit")` returns `LegacyProfile()` | ✓ |
| historical `Register daily-menu` comment | Superseded; current config/docs use Workbench-first menu copy | historical only |
| `natural = quiet chat + same-run streaming (recommended for Cockpit view)` | `NaturalChatProfile` returns empty `started_text` | ✓ |
| `Claude permission mode (operator setting)` | `/claude_mode` routes to permission mode switch | ✓ |
