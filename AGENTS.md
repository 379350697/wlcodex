<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **wlcodex** (12209 symbols, 27152 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Best Effort

- Prefer running impact analysis before editing a function, class, or method when GitNexus is available. Use `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- Prefer running `detect_changes()` before committing when GitNexus is available to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- Warn the user if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- If GitNexus is unavailable because of network, package registry, sandbox, or local installation failures, record the failure and continue with ordinary code review and test verification.
- When exploring unfamiliar code, use `query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.

## Never Do

- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/wlcodex/context` | Codebase overview, check index freshness |
| `gitnexus://repo/wlcodex/clusters` | All functional areas |
| `gitnexus://repo/wlcodex/processes` | All execution flows |
| `gitnexus://repo/wlcodex/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
