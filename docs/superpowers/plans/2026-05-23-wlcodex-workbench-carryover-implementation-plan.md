# WLCodex Workbench Carryover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit Workbench-level carryover so a new Workbench can inherit a concise, user-visible Continuity Brief from a historical Workbench without inheriting old execution state or dirty full context.

**Architecture:** Add a small carryover domain layer around existing `conversation_sessions`, `agent_runs`, and `orchestration_runs`. `/carry` lists/searches source Workbenches, prepares a cached Continuity Brief, and the next non-command user message creates a clean target Workbench with the brief injected as advisory context.

**Tech Stack:** Python 3.12, SQLite ledger, existing Telegram command/callback protocol, existing controller/router/status modules, pytest, GitNexus MCP for impact checks.

---

## Required Reading

- Spec: `docs/superpowers/specs/2026-05-23-wlcodex-workbench-carryover-design.md`
- Existing Workbench history spec: `docs/superpowers/specs/2026-05-21-workbench-history-and-workspace-switching-design.md`
- Existing staged `/auto` spec: `docs/superpowers/specs/2026-05-22-wlcodex-stage-gated-auto-workflow-design.md`
- Router: `wlcodex/router.py`
- Ledger schema and methods: `wlcodex/db.py`
- Conversation models: `wlcodex/models.py`
- Controller command/callback flow: `wlcodex/controller.py`
- Telegram handlers: `wlcodex/telegram_app.py`
- Status renderers: `wlcodex/status.py`
- Context packet policy: `wlcodex/context_packets.py`
- Callback protocol: `wlcodex/conversation_callback.py`

## Non-Negotiable Product Rules

- `/new` stays clean. It must not inherit carryover.
- `/carry 36` means Workbench/conversation id `36`, not task id.
- Carryover must be explicit: no automatic similarity-based inheritance.
- The Continuity Brief is advisory context, not a system instruction.
- Current user input in the target Workbench wins over historical carryover.
- Do not copy old active tasks, Claude runs, approvals, terminal mode, runtime leases, or permissions.
- Do not inline code blocks, long logs, raw diffs, or full transcripts into the brief.
- Do not start Codex, Claude, `/auto`, shell commands, or implementation from `接棒开新工作台`.
- Source evidence remains traceable through ids and summaries.
- Secrets must be redacted before saving or displaying a brief.

## Impact Baseline Commands

Run these immediately before editing the matching existing symbols. If any risk is HIGH or CRITICAL, stop and report before editing.

```text
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "parse_command",
  "file_path": "wlcodex/router.py",
  "kind": "Function",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "migrate",
  "file_path": "wlcodex/db.py",
  "kind": "Method",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "handle",
  "file_path": "wlcodex/controller.py",
  "kind": "Method",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "handle_conversation_text",
  "file_path": "wlcodex/controller.py",
  "kind": "Method",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "handle_conversation_callback",
  "file_path": "wlcodex/controller.py",
  "kind": "Method",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "render_workbench_history",
  "file_path": "wlcodex/status.py",
  "kind": "Function",
  "direction": "upstream"
})
```

Expected risk: LOW or MEDIUM.

## File Structure

| File | Responsibility |
| --- | --- |
| `wlcodex/carryover.py` | New pure helpers for Continuity Brief source assembly, redaction, trimming, preview generation, and prompt injection text. |
| `wlcodex/models.py` | Add `WorkbenchCarryover` dataclass. |
| `wlcodex/db.py` | Add `workbench_carryovers` table and ledger methods. |
| `wlcodex/router.py` | Add `CarryWorkbenchCommand` and parse `/carry`, `/carry <id>`, `/carry <query>`. |
| `wlcodex/conversation_callback.py` | Add carryover callback constants using existing `conv:{conversation_id}:{action}` protocol. |
| `wlcodex/status.py` | Render carryover candidate list, prepared carryover confirmation, and full brief view. |
| `wlcodex/controller.py` | Handle `/carry`, carryover callbacks, pending carryover consumption, and clean target Workbench creation. |
| `wlcodex/telegram_app.py` | Register `/carry` handler if not already routed generically. |
| `tests/test_carryover.py` | Pure helper tests for brief shape, redaction, trimming, and source selection. |
| `tests/test_db.py` | Ledger tests for carryover records and pending consumption. |
| `tests/test_router.py` | Parser tests for `/carry`. |
| `tests/test_status.py` | Renderer tests for carryover cards. |
| `tests/test_controller_flow.py` | End-to-end controller tests for list/search/prepare/consume/cancel. |
| `tests/test_telegram_handlers.py` | Telegram command registration and callback routing tests if existing coverage requires it. |

---

## Task 1: Add Pure Carryover Helpers

**Files:**
- Create: `wlcodex/carryover.py`
- Test: `tests/test_carryover.py`

- [ ] **Step 1: Write failing helper tests**

Create `tests/test_carryover.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from wlcodex.carryover import (
    CarryoverSource,
    build_continuity_brief,
    build_carryover_preview,
    redact_sensitive_text,
)


def test_continuity_brief_is_delimited_advisory_and_short() -> None:
    source = CarryoverSource(
        source_conversation_id=36,
        title="云上部署核验",
        workspace_alias="lightfeev2",
        generated_at=datetime(2026, 5, 23, 16, 40, tzinfo=timezone.utc),
        conversation_summary="围绕云上部署和交易所状态异常展开。",
        latest_codex_summary=(
            "最新版部署后服务可运行，但业务状态异常。"
            "真实交易所无非零持仓，本地状态残留 ALTUSDT open position。"
        ),
        latest_claude_summary="",
        latest_verification_result="Binance reduce-only 400 body 尚未完整确认。",
        evidence_refs=["latest_auto_run=58", "latest_codex_analysis_run=80"],
    )

    brief = build_continuity_brief(source)

    assert brief.startswith("<carryover_context>")
    assert brief.endswith("</carryover_context>")
    assert "历史背景，仅供参考" in brief
    assert "当前用户最新输入优先" in brief
    assert "source_conversation_id=36" in brief
    assert "ALTUSDT" in brief
    assert len(brief) <= 2200


def test_carryover_redacts_credentials_and_strips_code_blocks() -> None:
    source = CarryoverSource(
        source_conversation_id=9,
        title="Sensitive",
        workspace_alias="demo",
        conversation_summary=(
            "password: secret-password\n"
            "```python\nprint('do not include code')\n```\n"
            "API key sk-test-1234567890abcdef"
        ),
        latest_codex_summary="确认问题仍未闭环。",
        evidence_refs=[],
    )

    brief = build_continuity_brief(source)

    assert "secret-password" not in brief
    assert "sk-test" not in brief
    assert "print(" not in brief
    assert "```" not in brief
    assert "[已隐藏敏感信息]" in brief


def test_carryover_preview_is_compact() -> None:
    source = CarryoverSource(
        source_conversation_id=12,
        title="Telegram 摘要优化",
        workspace_alias="wlcodex",
        conversation_summary="已实现短摘要，但下一步动作仍需更明确。",
        latest_codex_summary="需要展示 Claude 要做什么，而不是只写交给 Claude。",
    )

    preview = build_carryover_preview(source)

    assert "短摘要" in preview
    assert "Claude" in preview
    assert len(preview) <= 220


def test_redact_sensitive_text_masks_common_secret_shapes() -> None:
    text = "ssh password: abc123\nOPENAI_API_KEY=sk-abcdef1234567890\n普通结论保留。"

    redacted = redact_sensitive_text(text)

    assert "abc123" not in redacted
    assert "sk-abcdef" not in redacted
    assert "普通结论保留" in redacted
```

- [ ] **Step 2: Run helper tests and confirm failure**

Run:

```bash
rtk pytest tests/test_carryover.py -q
```

Expected: import fails because `wlcodex.carryover` does not exist.

- [ ] **Step 3: Implement pure helper module**

Create `wlcodex/carryover.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re

CARRYOVER_BRIEF_MAX_CHARS = 2200
CARRYOVER_PREVIEW_MAX_CHARS = 220
REDACTION = "[已隐藏敏感信息]"


@dataclass(frozen=True)
class CarryoverSource:
    source_conversation_id: int
    title: str
    workspace_alias: str
    generated_at: datetime | None = None
    conversation_summary: str = ""
    latest_codex_summary: str = ""
    latest_claude_summary: str = ""
    latest_verification_result: str = ""
    evidence_refs: list[str] = field(default_factory=list)


def redact_sensitive_text(text: str) -> str:
    patterns = [
        r"(?i)(password|passwd|pwd)\s*[:=]\s*\S+",
        r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*\S+",
        r"sk-[A-Za-z0-9_\-]{12,}",
        r"(?i)ssh\s+password\s*[:=]\s*\S+",
    ]
    redacted = text
    for pattern in patterns:
        redacted = re.sub(pattern, REDACTION, redacted)
    return redacted


def strip_code_blocks(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def clean_carryover_text(text: str, *, max_chars: int = 360) -> str:
    text = strip_code_blocks(redact_sensitive_text(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def build_carryover_preview(source: CarryoverSource) -> str:
    candidates = [
        source.latest_verification_result,
        source.latest_codex_summary,
        source.conversation_summary,
        source.latest_claude_summary,
    ]
    text = next((clean_carryover_text(item, max_chars=CARRYOVER_PREVIEW_MAX_CHARS) for item in candidates if item.strip()), "")
    return text or "暂无摘要，请先刷新接棒摘要。"


def build_continuity_brief(source: CarryoverSource) -> str:
    generated = source.generated_at or datetime.now(timezone.utc)
    generated_text = generated.astimezone().strftime("%Y-%m-%d %H:%M")
    summary = clean_carryover_text(source.conversation_summary)
    codex = clean_carryover_text(source.latest_codex_summary)
    claude = clean_carryover_text(source.latest_claude_summary)
    verification = clean_carryover_text(source.latest_verification_result)
    evidence = source.evidence_refs or []

    lines = [
        "<carryover_context>",
        f"来源：工作台 #{source.source_conversation_id}「{clean_carryover_text(source.title, max_chars=80)}」",
        f"工作区：{source.workspace_alias}",
        f"生成时间：{generated_text}",
        "",
        "使用规则：",
        "- 这是历史背景，仅供参考。",
        "- 当前用户最新输入优先。",
        "- 不要自动继续旧任务，不要继承旧权限或旧执行状态。",
        "- 需要证据时，根据证据索引回查，不要猜。",
        "",
        "背景：",
        summary or "来源工作台没有稳定摘要，请结合证据索引回查。",
        "",
        "已确认：",
        f"- {codex}" if codex else "- 暂无明确已确认结论。",
        f"- Claude 执行摘要：{claude}" if claude else "- 暂无 Claude 执行摘要。",
        "",
        "未闭环：",
        f"- {verification}" if verification else "- 暂无明确未闭环项。",
        "",
        "关键约束：",
        "- 不要把历史摘要当成当前任务指令。",
        "- 不要跳过与当前目标相关的真实核验。",
        "",
        "建议切入点：",
        "先基于当前用户目标核对历史未闭环项，再决定是否进入 /auto 或交给 Claude。",
        "",
        "证据索引：",
        f"- source_conversation_id={source.source_conversation_id}",
        f"- workspace={source.workspace_alias}",
    ]
    lines.extend(f"- {clean_carryover_text(ref, max_chars=120)}" for ref in evidence)
    lines.append("</carryover_context>")
    brief = "\n".join(line for line in lines if line is not None)
    if len(brief) <= CARRYOVER_BRIEF_MAX_CHARS:
        return brief
    return brief[: CARRYOVER_BRIEF_MAX_CHARS - len("\n</carryover_context>")] + "\n</carryover_context>"
```

- [ ] **Step 4: Run helper tests and confirm pass**

Run:

```bash
rtk pytest tests/test_carryover.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit helper module**

```bash
git add wlcodex/carryover.py tests/test_carryover.py
git commit -m "feat: add workbench carryover brief helpers"
```

---

## Task 2: Add Carryover Persistence

**Files:**
- Modify: `wlcodex/models.py`
- Modify: `wlcodex/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write failing ledger tests**

Add to `tests/test_db.py`:

```python
def test_create_and_get_workbench_carryover(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    source = ledger.create_conversation(
        chat_id=10,
        user_id=1,
        title="Source",
        mode="chief_engineer",
        workspace_alias="lightfeev2",
    )

    carryover = ledger.create_workbench_carryover(
        chat_id=10,
        source_conversation_id=source.id,
        workspace_alias="lightfeev2",
        brief_text="<carryover_context>brief</carryover_context>",
        preview_text="brief",
        source_fingerprint="agent_runs=1",
        status="ready",
    )

    loaded = ledger.get_workbench_carryover(carryover.id)
    assert loaded.id == carryover.id
    assert loaded.source_conversation_id == source.id
    assert loaded.target_conversation_id is None
    assert loaded.brief_text.startswith("<carryover_context>")


def test_latest_prepared_carryover_by_chat(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    source = ledger.create_conversation(
        chat_id=10,
        user_id=1,
        title="Source",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ledger.create_workbench_carryover(
        chat_id=10,
        source_conversation_id=source.id,
        workspace_alias="wlcodex",
        brief_text="old",
        preview_text="old",
        source_fingerprint="old",
        status="cancelled",
    )
    prepared = ledger.create_workbench_carryover(
        chat_id=10,
        source_conversation_id=source.id,
        workspace_alias="wlcodex",
        brief_text="new",
        preview_text="new",
        source_fingerprint="new",
        status="prepared",
    )

    loaded = ledger.get_latest_prepared_carryover(10)
    assert loaded.id == prepared.id


def test_mark_carryover_used_links_target(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    source = ledger.create_conversation(
        chat_id=10,
        user_id=1,
        title="Source",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    target = ledger.create_conversation(
        chat_id=10,
        user_id=1,
        title="Target",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    carryover = ledger.create_workbench_carryover(
        chat_id=10,
        source_conversation_id=source.id,
        workspace_alias="wlcodex",
        brief_text="brief",
        preview_text="brief",
        source_fingerprint="fp",
        status="prepared",
    )

    used = ledger.mark_workbench_carryover_used(carryover.id, target.id)

    assert used.status == "used"
    assert used.target_conversation_id == target.id
    assert used.used_at is not None
```

- [ ] **Step 2: Run ledger tests and confirm failure**

Run:

```bash
rtk pytest tests/test_db.py::test_create_and_get_workbench_carryover tests/test_db.py::test_latest_prepared_carryover_by_chat tests/test_db.py::test_mark_carryover_used_links_target -q
```

Expected: missing model/table/method failures.

- [ ] **Step 3: Add `WorkbenchCarryover` dataclass**

In `wlcodex/models.py`, add near conversation models:

```python
@dataclass(frozen=True)
class WorkbenchCarryover:
    id: int
    chat_id: int
    source_conversation_id: int
    target_conversation_id: int | None
    workspace_alias: str
    brief_text: str
    preview_text: str
    source_fingerprint: str
    status: str
    created_at: datetime
    updated_at: datetime
    used_at: datetime | None
```

- [ ] **Step 4: Add migration table**

In `Ledger.migrate`, add:

```python
CREATE TABLE IF NOT EXISTS workbench_carryovers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    source_conversation_id INTEGER NOT NULL,
    target_conversation_id INTEGER,
    workspace_alias TEXT NOT NULL,
    brief_text TEXT NOT NULL DEFAULT '',
    preview_text TEXT NOT NULL DEFAULT '',
    source_fingerprint TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ready',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    used_at TEXT,
    FOREIGN KEY(source_conversation_id) REFERENCES conversation_sessions(id),
    FOREIGN KEY(target_conversation_id) REFERENCES conversation_sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_workbench_carryovers_chat_status
    ON workbench_carryovers(chat_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_workbench_carryovers_source
    ON workbench_carryovers(source_conversation_id, updated_at DESC);
```

- [ ] **Step 5: Add row mapper and ledger methods**

Add a private mapper in `wlcodex/db.py`:

```python
def _workbench_carryover(row: sqlite3.Row) -> WorkbenchCarryover:
    return WorkbenchCarryover(
        id=int(row["id"]),
        chat_id=int(row["chat_id"]),
        source_conversation_id=int(row["source_conversation_id"]),
        target_conversation_id=(
            int(row["target_conversation_id"])
            if row["target_conversation_id"] is not None else None
        ),
        workspace_alias=str(row["workspace_alias"] or ""),
        brief_text=str(row["brief_text"] or ""),
        preview_text=str(row["preview_text"] or ""),
        source_fingerprint=str(row["source_fingerprint"] or ""),
        status=str(row["status"] or ""),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
        used_at=_parse_dt(row["used_at"]) if row["used_at"] else None,
    )
```

Add methods:

```python
def create_workbench_carryover(
    self,
    *,
    chat_id: int,
    source_conversation_id: int,
    workspace_alias: str,
    brief_text: str,
    preview_text: str,
    source_fingerprint: str,
    status: str = "ready",
) -> WorkbenchCarryover:
    now = _now()
    cur = self._conn.execute(
        """
        INSERT INTO workbench_carryovers (
            chat_id, source_conversation_id, workspace_alias, brief_text,
            preview_text, source_fingerprint, status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chat_id, source_conversation_id, workspace_alias, brief_text,
            preview_text, source_fingerprint, status, now, now,
        ),
    )
    self._conn.commit()
    return self.get_workbench_carryover(int(cur.lastrowid))


def get_workbench_carryover(self, carryover_id: int) -> WorkbenchCarryover:
    row = self._conn.execute(
        "SELECT * FROM workbench_carryovers WHERE id = ?",
        (carryover_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown workbench carryover id: {carryover_id}")
    return _workbench_carryover(row)


def get_latest_prepared_carryover(self, chat_id: int) -> WorkbenchCarryover | None:
    row = self._conn.execute(
        """
        SELECT * FROM workbench_carryovers
        WHERE chat_id = ? AND status = 'prepared'
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (chat_id,),
    ).fetchone()
    return _workbench_carryover(row) if row else None


def mark_workbench_carryover_used(
    self, carryover_id: int, target_conversation_id: int
) -> WorkbenchCarryover:
    now = _now()
    self._conn.execute(
        """
        UPDATE workbench_carryovers
        SET status = 'used',
            target_conversation_id = ?,
            used_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (target_conversation_id, now, now, carryover_id),
    )
    self._conn.commit()
    return self.get_workbench_carryover(carryover_id)


def update_workbench_carryover_status(
    self, carryover_id: int, status: str
) -> WorkbenchCarryover:
    self._conn.execute(
        "UPDATE workbench_carryovers SET status = ?, updated_at = ? WHERE id = ?",
        (status, _now(), carryover_id),
    )
    self._conn.commit()
    return self.get_workbench_carryover(carryover_id)
```

- [ ] **Step 6: Run ledger tests and confirm pass**

Run:

```bash
rtk pytest tests/test_db.py::test_create_and_get_workbench_carryover tests/test_db.py::test_latest_prepared_carryover_by_chat tests/test_db.py::test_mark_carryover_used_links_target -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit persistence**

```bash
git add wlcodex/models.py wlcodex/db.py tests/test_db.py
git commit -m "feat: persist workbench carryovers"
```

---

## Task 3: Parse `/carry` And Add Callback Constants

**Files:**
- Modify: `wlcodex/router.py`
- Modify: `wlcodex/conversation_callback.py`
- Test: `tests/test_router.py`
- Test: `tests/test_controller_flow.py`

- [ ] **Step 1: Add failing parser tests**

Add to `tests/test_router.py`:

```python
def test_parse_carry_command_without_query() -> None:
    from wlcodex.router import CarryWorkbenchCommand, parse_command

    cmd = parse_command("/carry")

    assert isinstance(cmd, CarryWorkbenchCommand)
    assert cmd.query == ""


def test_parse_carry_command_with_workbench_id_or_query() -> None:
    from wlcodex.router import CarryWorkbenchCommand, parse_command

    by_id = parse_command("/carry 36")
    by_query = parse_command("/carry lightfeev2 状态收敛")

    assert isinstance(by_id, CarryWorkbenchCommand)
    assert by_id.query == "36"
    assert isinstance(by_query, CarryWorkbenchCommand)
    assert by_query.query == "lightfeev2 状态收敛"
```

- [ ] **Step 2: Run parser tests and confirm failure**

Run:

```bash
rtk pytest tests/test_router.py::test_parse_carry_command_without_query tests/test_router.py::test_parse_carry_command_with_workbench_id_or_query -q
```

Expected: `CarryWorkbenchCommand` missing.

- [ ] **Step 3: Add router command**

In `wlcodex/router.py`, add:

```python
@dataclass(frozen=True)
class CarryWorkbenchCommand:
    query: str = ""
```

Add it to the `ParsedCommand` union.

Add parse logic near Workbench history commands:

```python
    if stripped == "/carry":
        return CarryWorkbenchCommand()
    if stripped.startswith("/carry "):
        return CarryWorkbenchCommand(query=stripped.split(maxsplit=1)[1].strip())
```

- [ ] **Step 4: Add callback constants**

In `wlcodex/conversation_callback.py`, add:

```python
CARRY_START = "carry_start"
CARRY_SHOW = "carry_show"
CARRY_REFRESH = "carry_refresh"
CARRY_CANCEL = "carry_cancel"
```

These actions use the existing source Workbench id in `conv:{id}:{action}`.

- [ ] **Step 5: Run parser and callback smoke tests**

Run:

```bash
rtk pytest tests/test_router.py::test_parse_carry_command_without_query tests/test_router.py::test_parse_carry_command_with_workbench_id_or_query tests/test_controller_flow.py::test_encode_decode_conversation_callback_roundtrip -q
```

Expected: tests pass.

- [ ] **Step 6: Commit parser/callback changes**

```bash
git add wlcodex/router.py wlcodex/conversation_callback.py tests/test_router.py
git commit -m "feat: parse workbench carryover commands"
```

---

## Task 4: Render Carryover Lists And Brief Views

**Files:**
- Modify: `wlcodex/status.py`
- Test: `tests/test_status.py`

- [ ] **Step 1: Add failing renderer tests**

Add to `tests/test_status.py`:

```python
def test_render_carryover_candidates_is_human_readable() -> None:
    from datetime import datetime, timezone
    from wlcodex.models import ConversationSession
    from wlcodex.status import render_carryover_candidates

    session = ConversationSession(
        id=36,
        chat_id=100,
        user_id=7,
        title="云上部署核验",
        mode="chief_engineer",
        workspace_alias="lightfeev2",
        active_codex_task_id=None,
        active_claude_run_id=None,
        conversation_summary="已确认部署运行，但状态收敛未闭环。",
        current_model="",
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 23, 12, 35, tzinfo=timezone.utc),
        archived_at=datetime(2026, 5, 23, 13, 0, tzinfo=timezone.utc),
    )

    text = render_carryover_candidates([(session, "ALTUSDT 状态收敛未闭环")])

    assert "可接棒历史工作台" in text
    assert "#36" in text
    assert "lightfeev2" in text
    assert "ALTUSDT 状态收敛未闭环" in text


def test_render_prepared_carryover_mentions_next_user_goal() -> None:
    from wlcodex.status import render_prepared_carryover

    text = render_prepared_carryover(
        source_conversation_id=36,
        source_title="云上部署核验",
        workspace_alias="lightfeev2",
        preview="状态收敛未闭环。",
    )

    assert "准备从工作台 #36 接棒" in text
    assert "请发送新任务目标" in text
    assert "不会启动 Claude" in text
```

- [ ] **Step 2: Run renderer tests and confirm failure**

Run:

```bash
rtk pytest tests/test_status.py::test_render_carryover_candidates_is_human_readable tests/test_status.py::test_render_prepared_carryover_mentions_next_user_goal -q
```

Expected: missing renderer functions.

- [ ] **Step 3: Add renderer functions**

In `wlcodex/status.py`, add:

```python
def render_carryover_candidates(
    items: Sequence[tuple[ConversationSession, str]]
) -> str:
    if not items:
        return "没有找到可接棒的历史工作台。可以换个关键词，或用 /new 开始干净工作台。"
    lines = ["可接棒历史工作台", ""]
    for session, preview in items:
        lines.append(
            f"#{session.id} {_trim(session.title, 36)} · {session.workspace_alias} · {_format_dt(session.updated_at)}"
        )
        lines.append(f"摘要：{_trim(preview, 120)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_prepared_carryover(
    *,
    source_conversation_id: int,
    source_title: str,
    workspace_alias: str,
    preview: str,
) -> str:
    return "\n".join([
        f"准备从工作台 #{source_conversation_id} 接棒",
        f"来源：{_trim(source_title, 60)}",
        f"工作区：{workspace_alias}",
        "",
        "接棒摘要：",
        _trim(preview, 220),
        "",
        "请发送新任务目标。",
        "",
        "说明：新工作台只继承接棒摘要，不继承旧会话全文、旧执行状态、旧权限或旧终端现场，也不会启动 Claude。",
    ])


def render_carryover_brief_view(
    *, source_conversation_id: int, brief_text: str
) -> str:
    return f"接棒摘要 · 来源工作台 #{source_conversation_id}\n\n{brief_text}"
```

- [ ] **Step 4: Run renderer tests and confirm pass**

Run:

```bash
rtk pytest tests/test_status.py::test_render_carryover_candidates_is_human_readable tests/test_status.py::test_render_prepared_carryover_mentions_next_user_goal -q
```

Expected: tests pass.

- [ ] **Step 5: Commit renderers**

```bash
git add wlcodex/status.py tests/test_status.py
git commit -m "feat: render workbench carryover cards"
```

---

## Task 5: Assemble Carryover Sources From Workbench State

**Files:**
- Modify: `wlcodex/carryover.py`
- Modify: `wlcodex/db.py`
- Test: `tests/test_carryover.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Add source assembly tests**

Add to `tests/test_carryover.py`:

```python
def test_source_fingerprint_prefers_latest_run_ids() -> None:
    from wlcodex.carryover import build_source_fingerprint

    fingerprint = build_source_fingerprint(
        conversation_id=36,
        latest_agent_run_ids=[80, 81],
        latest_orchestration_run_ids=[58],
    )

    assert fingerprint == "conversation=36;agent_runs=80,81;orchestration_runs=58"
```

Add to `tests/test_db.py`:

```python
def test_list_recent_carryover_evidence_from_conversation(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    conversation = ledger.create_conversation(
        chat_id=10,
        user_id=1,
        title="Source",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    run = ledger.create_agent_run(
        conversation.id,
        "codex",
        "auto_analysis",
        prompt_packet_summary="分析输入",
    )
    ledger.update_agent_run_status(run.id, "done", completion_summary="关键结论")
    orch = ledger.create_orchestration_run(conversation.id, "目标")
    ledger.update_orchestration_run(
        orch.id,
        status="needs_user",
        current_step="draft_ready",
        last_codex_analysis="最终方案摘要",
    )

    evidence = ledger.list_carryover_evidence(conversation.id)

    assert evidence.agent_runs[0].id == run.id
    assert evidence.orchestration_runs[0].id == orch.id
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
rtk pytest tests/test_carryover.py::test_source_fingerprint_prefers_latest_run_ids tests/test_db.py::test_list_recent_carryover_evidence_from_conversation -q
```

Expected: missing helper and ledger evidence method.

- [ ] **Step 3: Add fingerprint helper**

In `wlcodex/carryover.py`, add:

```python
def build_source_fingerprint(
    *,
    conversation_id: int,
    latest_agent_run_ids: list[int],
    latest_orchestration_run_ids: list[int],
) -> str:
    agent_ids = ",".join(str(item) for item in latest_agent_run_ids)
    orch_ids = ",".join(str(item) for item in latest_orchestration_run_ids)
    return (
        f"conversation={conversation_id};"
        f"agent_runs={agent_ids};"
        f"orchestration_runs={orch_ids}"
    )
```

- [ ] **Step 4: Add evidence dataclass and ledger method**

In `wlcodex/models.py`, add:

```python
@dataclass(frozen=True)
class CarryoverEvidence:
    agent_runs: list[AgentRun] = field(default_factory=list)
    orchestration_runs: list[OrchestrationRun] = field(default_factory=list)
```

In `wlcodex/db.py`, add:

```python
def list_carryover_evidence(
    self, conversation_id: int, *, limit: int = 5
) -> CarryoverEvidence:
    return CarryoverEvidence(
        agent_runs=self.list_recent_agent_runs(conversation_id, limit=limit),
        orchestration_runs=self.list_orchestration_runs(conversation_id, limit=limit),
    )
```

- [ ] **Step 5: Add source builder in controller or helper**

Prefer a private controller helper that uses ledger data and pure carryover
functions:

```python
def _build_carryover_source(self, conversation: ConversationSession) -> CarryoverSource:
    evidence = self._ledger.list_carryover_evidence(conversation.id)
    agent_runs = evidence.agent_runs
    orch_runs = evidence.orchestration_runs
    latest_codex = next((r.completion_summary for r in agent_runs if r.agent == "codex" and r.completion_summary), "")
    latest_claude = next((r.completion_summary for r in agent_runs if r.agent == "claude" and r.completion_summary), "")
    latest_verification = next((r.last_verification_result for r in orch_runs if r.last_verification_result), "")
    refs = [
        *(f"agent_run={run.id}:{run.agent}/{run.role}/{run.status}" for run in agent_runs[:3]),
        *(f"orchestration_run={run.id}:{run.status}/{run.current_step}" for run in orch_runs[:3]),
    ]
    return CarryoverSource(
        source_conversation_id=conversation.id,
        title=conversation.title,
        workspace_alias=conversation.workspace_alias,
        conversation_summary=conversation.conversation_summary,
        latest_codex_summary=latest_codex,
        latest_claude_summary=latest_claude,
        latest_verification_result=latest_verification,
        evidence_refs=refs,
    )
```

This helper is used in Task 6.

- [ ] **Step 6: Run tests and confirm pass**

Run:

```bash
rtk pytest tests/test_carryover.py tests/test_db.py::test_list_carryover_evidence_from_conversation -q
```

Expected: tests pass.

- [ ] **Step 7: Commit source assembly**

```bash
git add wlcodex/carryover.py wlcodex/models.py wlcodex/db.py tests/test_carryover.py tests/test_db.py
git commit -m "feat: assemble workbench carryover evidence"
```

---

## Task 6: Controller `/carry` List, Search, Show, Refresh, Prepare

**Files:**
- Modify: `wlcodex/controller.py`
- Test: `tests/test_controller_flow.py`

- [ ] **Step 1: Add failing controller tests for listing**

Add to `tests/test_controller_flow.py`:

```python
@pytest.mark.asyncio
async def test_carry_lists_workbench_candidates(ctrl: CommandController) -> None:
    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="云上部署核验",
        mode="chief_engineer",
        workspace_alias="lightfeev2",
    )
    ctrl._ledger.update_conversation_summary(source.id, "ALTUSDT 状态收敛未闭环。")
    ctrl._ledger.archive_conversation(source.id)

    response = await ctrl.handle("/carry", {"chat_id": 100, "user_id": 7})

    assert "可接棒历史工作台" in response.text
    assert "云上部署核验" in response.text
    labels = [button["text"] for row in (response.buttons or []) for button in row]
    assert "接棒开新工作台" in labels
    assert "查看接棒摘要" in labels
    assert "刷新摘要" in labels


@pytest.mark.asyncio
async def test_carry_search_filters_workbenches(ctrl: CommandController) -> None:
    hit = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="reduce-only 线上问题",
        mode="chief_engineer",
        workspace_alias="lightfeev2",
    )
    ctrl._ledger.update_conversation_summary(hit.id, "Binance reduce-only 仍失败。")
    ctrl._ledger.archive_conversation(hit.id)
    miss = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Telegram 摘要",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.archive_conversation(miss.id)

    response = await ctrl.handle("/carry reduce-only", {"chat_id": 100, "user_id": 7})

    assert "reduce-only 线上问题" in response.text
    assert "Telegram 摘要" not in response.text
```

- [ ] **Step 2: Add failing controller tests for show/prepare**

Add:

```python
@pytest.mark.asyncio
async def test_carry_by_id_prepares_next_goal_without_execution(ctrl: CommandController) -> None:
    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="云上部署核验",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.update_conversation_summary(source.id, "状态收敛未闭环。")
    ctrl._ledger.archive_conversation(source.id)

    response = await ctrl.handle(f"/carry {source.id}", {"chat_id": 100, "user_id": 7})

    assert f"准备从工作台 #{source.id} 接棒" in response.text
    assert "请发送新任务目标" in response.text
    prepared = ctrl._ledger.get_latest_prepared_carryover(100)
    assert prepared is not None
    assert prepared.source_conversation_id == source.id


@pytest.mark.asyncio
async def test_carry_show_callback_displays_full_brief(ctrl: CommandController) -> None:
    from wlcodex.conversation_callback import CARRY_SHOW, ConversationCallback

    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Source",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.update_conversation_summary(source.id, "未闭环背景。")

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation_id=source.id, action=CARRY_SHOW)
    )

    assert "接棒摘要" in response.text
    assert "<carryover_context>" in response.text
    assert "未闭环背景" in response.text
```

- [ ] **Step 3: Run controller tests and confirm failure**

Run:

```bash
rtk pytest tests/test_controller_flow.py::test_carry_lists_workbench_candidates tests/test_controller_flow.py::test_carry_search_filters_workbenches tests/test_controller_flow.py::test_carry_by_id_prepares_next_goal_without_execution tests/test_controller_flow.py::test_carry_show_callback_displays_full_brief -q
```

Expected: `/carry` not handled or callbacks unknown.

- [ ] **Step 4: Add `/carry` routing in `CommandController.handle`**

Import `CarryWorkbenchCommand`, carryover helpers, and new status renderers.

Add route:

```python
if isinstance(command, CarryWorkbenchCommand):
    return await self.handle_carry_workbench(command, telegram_context)
```

- [ ] **Step 5: Implement `handle_carry_workbench`**

Behavior:

- no query: list recent Workbenches for chat with generated previews;
- numeric query: prepare that source Workbench;
- text query: search candidate Workbenches by title, workspace, summary, and generated preview;
- reject source from another chat;
- reject unknown source with a clear message.

Implementation shape:

```python
async def handle_carry_workbench(
    self, command: CarryWorkbenchCommand, ctx: dict[str, Any] | None = None
) -> ControllerResponse:
    if self._ledger is None:
        return ControllerResponse("系统未完全初始化。请检查配置。")
    chat_id = ctx.get("chat_id", 0) if ctx else 0
    query = command.query.strip()
    if query.isdigit():
        return await self._prepare_workbench_carryover(int(query), chat_id)
    conversations = self._ledger.list_conversations_by_chat(
        chat_id, limit=20, include_archived=True
    )
    if query:
        lowered = query.lower()
        conversations = [
            item for item in conversations
            if lowered in item.title.lower()
            or lowered in item.workspace_alias.lower()
            or lowered in item.conversation_summary.lower()
        ]
    items = []
    for convo in conversations[:8]:
        source = self._build_carryover_source(convo)
        items.append((convo, build_carryover_preview(source)))
    buttons = self._build_carryover_candidate_buttons([item[0] for item in items])
    return ControllerResponse(render_carryover_candidates(items), buttons=buttons)
```

- [ ] **Step 6: Implement prepare/show/refresh helpers**

Add helpers:

```python
async def _prepare_workbench_carryover(
    self, source_conversation_id: int, chat_id: int
) -> ControllerResponse:
    source_convo = self._ledger.get_conversation(source_conversation_id)
    if source_convo.chat_id != chat_id:
        return ControllerResponse("不能接棒其他聊天里的工作台。")
    source = self._build_carryover_source(source_convo)
    brief = build_continuity_brief(source)
    preview = build_carryover_preview(source)
    fingerprint = build_source_fingerprint(
        conversation_id=source_convo.id,
        latest_agent_run_ids=[run.id for run in self._ledger.list_recent_agent_runs(source_convo.id, limit=5)],
        latest_orchestration_run_ids=[run.id for run in self._ledger.list_orchestration_runs(source_convo.id, limit=5)],
    )
    self._ledger.create_workbench_carryover(
        chat_id=chat_id,
        source_conversation_id=source_convo.id,
        workspace_alias=source_convo.workspace_alias,
        brief_text=brief,
        preview_text=preview,
        source_fingerprint=fingerprint,
        status="prepared",
    )
    return ControllerResponse(
        render_prepared_carryover(
            source_conversation_id=source_convo.id,
            source_title=source_convo.title,
            workspace_alias=source_convo.workspace_alias,
            preview=preview,
        ),
        buttons=[[
            {"text": "查看接棒摘要", "callback_data": encode_conversation_callback(source_convo.id, CARRY_SHOW)},
            {"text": "取消接棒", "callback_data": encode_conversation_callback(source_convo.id, CARRY_CANCEL)},
        ]],
    )
```

For `CARRY_SHOW`, build or load the current brief and return `render_carryover_brief_view`.

For `CARRY_REFRESH`, rebuild the deterministic brief from current source state and save a new `ready` row. It must not start Codex/Claude.

For `CARRY_CANCEL`, cancel latest prepared carryover for chat and return `已取消接棒。`

- [ ] **Step 7: Add callback routing**

In `handle_conversation_callback`, before original generic callback actions:

```python
if callback.action == CARRY_START:
    convo = self._ledger.get_conversation(callback.conversation_id)
    return await self._prepare_workbench_carryover(convo.id, convo.chat_id)
if callback.action == CARRY_SHOW:
    return await self._handle_carry_show(callback.conversation_id)
if callback.action == CARRY_REFRESH:
    return await self._handle_carry_refresh(callback.conversation_id)
if callback.action == CARRY_CANCEL:
    return await self._handle_carry_cancel(callback.conversation_id)
```

- [ ] **Step 8: Run controller tests and confirm pass**

Run:

```bash
rtk pytest tests/test_controller_flow.py::test_carry_lists_workbench_candidates tests/test_controller_flow.py::test_carry_search_filters_workbenches tests/test_controller_flow.py::test_carry_by_id_prepares_next_goal_without_execution tests/test_controller_flow.py::test_carry_show_callback_displays_full_brief -q
```

Expected: tests pass.

- [ ] **Step 9: Commit carry list/show/prepare**

```bash
git add wlcodex/controller.py tests/test_controller_flow.py
git commit -m "feat: prepare workbench carryovers from history"
```

---

## Task 7: Consume Prepared Carryover Into A New Workbench

**Files:**
- Modify: `wlcodex/controller.py`
- Test: `tests/test_controller_flow.py`

- [ ] **Step 1: Add failing consume tests**

Add:

```python
@pytest.mark.asyncio
async def test_prepared_carryover_next_text_creates_new_workbench(ctrl: CommandController) -> None:
    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="云上部署核验",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.update_conversation_summary(source.id, "状态收敛未闭环。")
    await ctrl.handle(f"/carry {source.id}", {"chat_id": 100, "user_id": 7})

    response = await ctrl.handle_conversation_text(
        "继续查状态为什么没有收敛",
        {"chat_id": 100, "user_id": 7},
    )

    active = ctrl._ledger.get_active_conversation(100)
    carryover = ctrl._ledger.get_latest_prepared_carryover(100)
    assert "已从工作台" in response.text
    assert active.id != source.id
    assert active.workspace_alias == "wlcodex"
    assert "<carryover_context>" in active.conversation_summary
    assert "继续查状态为什么没有收敛" in active.conversation_summary
    assert carryover is None


@pytest.mark.asyncio
async def test_new_command_does_not_consume_prepared_carryover(ctrl: CommandController) -> None:
    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Source",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    await ctrl.handle(f"/carry {source.id}", {"chat_id": 100, "user_id": 7})

    await ctrl.handle("/new Clean", {"chat_id": 100, "user_id": 7})

    active = ctrl._ledger.get_active_conversation(100)
    assert active.title == "Clean"
    assert "<carryover_context>" not in active.conversation_summary
    assert ctrl._ledger.get_latest_prepared_carryover(100) is not None


@pytest.mark.asyncio
async def test_carryover_does_not_copy_old_runtime_state(ctrl: CommandController) -> None:
    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Source",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.set_conversation_active_task(source.id, 123)
    ctrl._ledger.set_conversation_active_claude_run(source.id, 456)
    await ctrl.handle(f"/carry {source.id}", {"chat_id": 100, "user_id": 7})

    await ctrl.handle_conversation_text(
        "新目标",
        {"chat_id": 100, "user_id": 7},
    )

    active = ctrl._ledger.get_active_conversation(100)
    assert active.active_codex_task_id is None
    assert active.active_claude_run_id is None
    assert active.codex_thread_id == ""
    assert active.claude_session_id == ""
```

- [ ] **Step 2: Run consume tests and confirm failure**

Run:

```bash
rtk pytest tests/test_controller_flow.py::test_prepared_carryover_next_text_creates_new_workbench tests/test_controller_flow.py::test_new_command_does_not_consume_prepared_carryover tests/test_controller_flow.py::test_carryover_does_not_copy_old_runtime_state -q
```

Expected: prepared carryover is ignored by normal text.

- [ ] **Step 3: Add pending carryover check before generic text routing**

In `handle_conversation_text`, after command parsing is ruled out and before
normal active-conversation append/analysis routing, add:

```python
pending = self._ledger.get_latest_prepared_carryover(chat_id)
if pending is not None:
    return await self._consume_prepared_carryover(
        pending,
        text,
        telegram_context,
    )
```

Commands must continue through `handle(...)` and not consume carryover.

- [ ] **Step 4: Implement `_consume_prepared_carryover`**

```python
async def _consume_prepared_carryover(
    self,
    carryover: WorkbenchCarryover,
    text: str,
    ctx: dict[str, Any] | None,
) -> ControllerResponse:
    chat_id = ctx.get("chat_id", 0) if ctx else carryover.chat_id
    user_id = ctx.get("user_id", 0) if ctx else 0
    source = self._ledger.get_conversation(carryover.source_conversation_id)
    if source.chat_id != chat_id:
        self._ledger.update_workbench_carryover_status(carryover.id, "cancelled")
        return ControllerResponse("接棒来源不属于当前聊天，已取消。")
    try:
        self._service.get_workspace(carryover.workspace_alias)
    except Exception:
        return ControllerResponse(
            f"来源工作区 {carryover.workspace_alias} 当前未配置。"
            "请先在配置中加入该工作区，或取消接棒后使用 /switch。"
        )
    old = self._ledger.get_active_conversation(chat_id)
    if old is not None:
        self._ledger.archive_conversation(old.id)
    title = default_title(text)
    target = self._ledger.create_conversation(
        chat_id=chat_id,
        user_id=user_id,
        title=title,
        mode=self._default_mode,
        workspace_alias=carryover.workspace_alias,
    )
    summary = trim_to_budget(
        f"{carryover.brief_text}\n\n当前用户新任务：{text[:500]}",
        ContextBudget().conversation_summary_tokens,
    )
    self._ledger.update_conversation_summary(target.id, summary)
    self._ledger.mark_workbench_carryover_used(carryover.id, target.id)
    return ControllerResponse(
        f"已从工作台 #{source.id} 接棒，创建新工作台：「{target.title}」\n"
        f"工作区：{target.workspace_alias}\n\n"
        "接棒摘要已带入。直接发消息会让 Codex 基于当前目标分析；也可以使用 /auto。",
        buttons=[[
            {"text": "查看状态", "callback_data": encode_conversation_callback(target.id, STATUS)},
            {"text": "进入 /auto", "callback_data": encode_conversation_callback(target.id, AUTO_CONTINUE_CONTEXT)},
        ]],
    )
```

If `AUTO_CONTINUE_CONTEXT` is not appropriate as a button without an active
`/auto` run, omit `进入 /auto` in implementation and keep only `查看状态`.

- [ ] **Step 5: Run consume tests and confirm pass**

Run:

```bash
rtk pytest tests/test_controller_flow.py::test_prepared_carryover_next_text_creates_new_workbench tests/test_controller_flow.py::test_new_command_does_not_consume_prepared_carryover tests/test_controller_flow.py::test_carryover_does_not_copy_old_runtime_state -q
```

Expected: tests pass.

- [ ] **Step 6: Commit consume flow**

```bash
git add wlcodex/controller.py tests/test_controller_flow.py
git commit -m "feat: create new workbench from prepared carryover"
```

---

## Task 8: Telegram Command Registration And Callback Buttons

**Files:**
- Modify: `wlcodex/telegram_app.py`
- Modify: `wlcodex/controller.py`
- Test: `tests/test_telegram_handlers.py`
- Test: `tests/test_controller_flow.py`

- [ ] **Step 1: Add or update Telegram registration test**

If `tests/test_telegram_handlers.py` already checks command registration, add:

```python
def test_telegram_registers_carry_command() -> None:
    app, _handlers = build_application(...)
    command_names = {
        command
        for handler in app.handlers[0]
        for command in getattr(handler, "commands", [])
    }
    assert "carry" in command_names
```

If the current test harness does not expose handlers easily, cover registration
through a controller-level `/carry` test instead and skip this file.

- [ ] **Step 2: Register `/carry`**

In `build_application`, add near Workbench history commands:

```python
application.add_handler(CommandHandler("carry", handlers.carry))
```

In `TelegramHandlers`, add:

```python
async def carry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await self._reply_controller(update, context, "/carry")
```

If existing command handlers pass original text through `update.effective_message.text`,
follow that local pattern instead of hardcoding `"/carry"`.

- [ ] **Step 3: Ensure candidate buttons use callback constants**

Candidate button rows should look like:

```python
[
    {
        "text": "接棒开新工作台",
        "callback_data": encode_conversation_callback(convo.id, CARRY_START),
    },
    {
        "text": "查看接棒摘要",
        "callback_data": encode_conversation_callback(convo.id, CARRY_SHOW),
    },
    {
        "text": "刷新摘要",
        "callback_data": encode_conversation_callback(convo.id, CARRY_REFRESH),
    },
]
```

- [ ] **Step 4: Run Telegram/controller tests**

Run:

```bash
rtk pytest tests/test_controller_flow.py::test_carry_lists_workbench_candidates tests/test_telegram_handlers.py -q
```

Expected: tests pass. If `tests/test_telegram_handlers.py` is not relevant,
run the exact existing Telegram test file that covers command registration.

- [ ] **Step 5: Commit Telegram integration**

```bash
git add wlcodex/telegram_app.py wlcodex/controller.py tests/test_telegram_handlers.py tests/test_controller_flow.py
git commit -m "feat: expose workbench carryover in Telegram"
```

---

## Task 9: Safety, Search, And Regression Coverage

**Files:**
- Modify: `wlcodex/controller.py`
- Modify: `wlcodex/carryover.py`
- Test: `tests/test_controller_flow.py`
- Test: `tests/test_carryover.py`

- [ ] **Step 1: Add cross-chat rejection test**

```python
@pytest.mark.asyncio
async def test_carry_rejects_source_from_another_chat(ctrl: CommandController) -> None:
    source = ctrl._ledger.create_conversation(
        chat_id=999,
        user_id=7,
        title="Other Chat",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )

    response = await ctrl.handle(f"/carry {source.id}", {"chat_id": 100, "user_id": 7})

    assert "不能接棒其他聊天" in response.text
```

- [ ] **Step 2: Add `/sessions` regression test**

If not already covered, add:

```python
@pytest.mark.asyncio
async def test_sessions_remains_agent_session_scoped_after_carry(ctrl: CommandController) -> None:
    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Source Workbench",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.create_agent_run(
        conversation_id=source.id,
        agent="codex",
        role="analysis",
        prompt_packet_summary="source run",
    )
    await ctrl.handle(f"/carry {source.id}", {"chat_id": 100, "user_id": 7})
    await ctrl.handle_conversation_text("新目标", {"chat_id": 100, "user_id": 7})

    response = await ctrl.handle("/sessions", {"chat_id": 100, "user_id": 7})

    assert "source run" not in response.text
```

- [ ] **Step 3: Add brief injection precedence test**

```python
def test_brief_contains_current_user_precedence_policy() -> None:
    from wlcodex.carryover import CarryoverSource, build_continuity_brief

    brief = build_continuity_brief(CarryoverSource(
        source_conversation_id=1,
        title="Source",
        workspace_alias="wlcodex",
        conversation_summary="old instruction: auto-run everything",
    ))

    assert "当前用户最新输入优先" in brief
    assert "不要自动继续旧任务" in brief
```

- [ ] **Step 4: Run safety/regression tests**

Run:

```bash
rtk pytest tests/test_carryover.py tests/test_controller_flow.py::test_carry_rejects_source_from_another_chat tests/test_controller_flow.py::test_sessions_remains_agent_session_scoped_after_carry -q
```

Expected: tests pass.

- [ ] **Step 5: Commit safety regressions**

```bash
git add wlcodex/carryover.py wlcodex/controller.py tests/test_carryover.py tests/test_controller_flow.py
git commit -m "test: cover workbench carryover safety"
```

---

## Task 10: Full Verification

**Files:**
- No new implementation files.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
rtk pytest tests/test_carryover.py tests/test_db.py tests/test_router.py tests/test_status.py tests/test_controller_flow.py tests/test_telegram_handlers.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run compile check**

Run:

```bash
python3 -m compileall wlcodex
```

Expected: no syntax errors.

- [ ] **Step 3: Run diff whitespace check**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 4: Run GitNexus change detection**

Run:

```text
mcp__gitnexus__.detect_changes({
  "repo": "wlcodex",
  "scope": "all"
})
```

Expected: changed symbols are limited to carryover, router, controller,
status, db/model persistence, Telegram command registration, and tests.

- [ ] **Step 5: Manual Telegram smoke**

On a local deployment:

1. Send `/new Source Carryover Smoke`.
2. Send ordinary text: `这里是接棒来源，未闭环问题是状态收敛。`
3. Send `/new Clean Workbench`.
4. Send `/carry`.
5. Confirm the source Workbench appears.
6. Tap `查看接棒摘要`; confirm it is concise and delimited.
7. Tap `接棒开新工作台`.
8. Send `继续查状态收敛未闭环的原因`.
9. Send `/status`; confirm it shows a new Workbench and the source workspace.
10. Send `/sessions`; confirm it does not list source Workbench agent sessions as current sessions.

Expected: no Codex/Claude execution starts unless explicitly requested.

- [ ] **Step 6: Final commit**

```bash
git add wlcodex tests
git commit -m "feat: add explicit workbench carryover"
```

## Self-Review Checklist

- Spec coverage:
  - `/new` clean boundary: Task 7.
  - `/carry` list/search/id: Tasks 3 and 6.
  - Workbench-level id, not task id: Tasks 3 and 6.
  - Continuity Brief shape/redaction/no long content: Task 1.
  - Evidence traceability: Task 5.
  - User-visible show/refresh/prepare buttons: Tasks 4, 6, 8.
  - No execution on carryover: Tasks 6 and 7.
  - No old runtime state copied: Task 7.
  - `/sessions` regression: Task 9.
- Placeholder scan:
  - No implementation step uses "TBD", "TODO", or "handle appropriately".
  - Every task has exact files, test commands, and expected results.
- Type consistency:
  - `WorkbenchCarryover` table fields match model fields.
  - Callback names use `CARRY_START`, `CARRY_SHOW`, `CARRY_REFRESH`, `CARRY_CANCEL`.
  - `/carry` command maps to `CarryWorkbenchCommand(query: str = "")`.
  - Source Workbench id is always `conversation_sessions.id`.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-23-wlcodex-workbench-carryover-implementation-plan.md`.

Two execution options:

1. **Subagent-Driven (recommended)** - dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** - execute tasks in this session using executing-plans, with checkpoints.

Do not start implementation until the user explicitly approves.
