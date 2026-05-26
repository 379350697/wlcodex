# README Documentation Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a small README documentation map so contributors can quickly find the existing manual, protocol, smoke, and Superpowers documents.

**Architecture:** This is a documentation-only change. Keep the repository behavior unchanged and limit edits to this plan file and `README.md`.

**Tech Stack:** Markdown.

---

### Task 1: Add README Documentation Map

**Files:**
- Modify: `README.md`
- Verify: `git status --short`
- Verify: `git diff -- README.md docs/`

- [x] **Step 1: Insert a documentation map section**

Add this section after the opening product description and before `## Remote Workbench`:

```markdown
## Documentation Map

| Area | Start here | Use when |
|------|------------|----------|
| End-to-end manual | `docs/manual-aet-e2e.md` | Running or checking the adaptive engineering team flow |
| Protocol notes | `docs/protocol/codex-app-server-spike.md` | Reviewing Codex app-server protocol experiments |
| Smoke checks | `docs/smoke/` | Replaying focused manual checks for interaction modes |
| Design specs | `docs/superpowers/specs/` | Reading approved feature designs before implementation |
| Implementation plans | `docs/superpowers/plans/` | Executing task-by-task plans with agentic workers |
| Reviews and reports | `docs/superpowers/reviews/`, `docs/superpowers/reports/` | Checking review findings, acceptance notes, and analysis artifacts |
```

- [x] **Step 2: Verify documentation-only scope**

Run: `git status --short`

Expected: only `README.md` and `docs/superpowers/plans/2026-05-26-readme-documentation-map-docs-only-plan.md`.

- [x] **Step 3: Review the exact documentation diff**

Run: `git diff -- README.md docs/`

Expected: the diff only adds this plan and the README documentation map.
