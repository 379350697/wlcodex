# Workbench History and Workspace Switching Design

## Goal

Make long-running WLCodex operation practical by adding a user-visible historical Workbench list and a clear project switching flow for local repositories under `/media/wl/新加卷/codex`.

This document is a design/spec only. It intentionally does not change code.

## Verified Current Behavior

The current code has two related concepts:

- Workbench / conversation: a row in `conversation_sessions`.
- Agent session / onsite session: a Codex or Claude run inside one Workbench.

Current behavior from code:

- `/new` archives the current active Workbench before creating a new one in `CommandController.handle_new_conversation`.
- `Ledger.get_active_conversation(chat_id)` and `Ledger.list_conversations_by_chat(chat_id)` only return `archived_at IS NULL` rows.
- Telegram `/sessions` uses `AgentSessionLibrary.list_for_workbench(active.id)`, so it lists Agent sessions for the current active Workbench, not all historical Workbenches.
- `/switch <workspace>` already exists, but it only accepts aliases present in configured `[[workspaces]]`.
- The current `config/wlcodex.toml` registers only:

```toml
[[workspaces]]
alias = "wlcodex"
path = "/media/wl/新加卷/codex/wlcodex"
allow_write = true
```

So today, switching to another project requires adding it to config and restarting WLCodex before using `/switch <alias>`.

## Operator Guidance Today

To switch to another local project before this feature is implemented:

1. Add one workspace entry per project to `config/wlcodex.toml`.

```toml
[[workspaces]]
alias = "lightfee"
path = "/media/wl/新加卷/codex/LightFee"
allow_write = true

[[workspaces]]
alias = "finance"
path = "/media/wl/新加卷/codex/Finance"
allow_write = true
```

2. Restart the WLCodex bot so the config is loaded.
3. In Telegram, create or use an active Workbench.
4. Send `/switch lightfee` or `/switch finance`.
5. Send your task text. New execution leases will use the selected Workbench workspace.

Important current limitation: `/new` creates a Workbench in `conversation.default_workspace`, which is currently `wlcodex`. If you want a fresh Workbench for another project today, send `/new`, then `/switch <alias>`, then send the task.

## UX Terms

- "工作台" means Workbench/conversation.
- "历史工作台" means archived or inactive Workbench records.
- "现场" means Agent session inside a Workbench, usually Codex or Claude.
- `/sessions` should keep meaning "current Workbench's historical Agent sessions".
- New historical Workbench commands should use "工作台" wording to avoid mixing them with Agent sessions.

## Requirements

### Historical Workbench List

1. Add a Telegram-visible command that lists active and archived Workbenches for the current chat.
2. Recommended commands:
   - `/workbenches`
   - `/history`
3. The list must include:
   - Workbench id
   - title
   - workspace alias
   - mode label
   - active or archived marker
   - last updated timestamp
4. The active Workbench must be visually marked.
5. Archived Workbenches must remain read-only until explicitly restored.
6. A restore action must not start Codex or Claude automatically.
7. Restoring an archived Workbench must archive the current active Workbench first, preserving the invariant that one chat has one active Workbench.
8. Restoring should switch the user's product surface back to that Workbench and allow `/status`, `/diff`, `/files`, `/terminal`, and ordinary follow-up text to operate on it.
9. If a restored Workbench points at a workspace alias no longer configured, the user must get an actionable message explaining that the workspace must be reconfigured before execution can continue.

### Workspace Switching

1. Add a user-visible list of switchable project workspaces.
2. Recommended command:
   - `/workspaces`
3. `/workspaces` must show:
   - alias
   - path
   - write permission
   - whether it is the active Workbench workspace
4. Keep `/switch <alias>` as the switching command.
5. If `/switch <alias>` fails because the alias is unknown, the error should mention `/workspaces`.
6. Workspaces must continue to be explicit and safe. The bot must not recursively scan arbitrary nested directories.
7. For the user's local layout, support optional immediate-child discovery under `/media/wl/新加卷/codex`.
8. Auto-discovered workspaces must be bounded to immediate children and should default to directories that look like repositories, preferably `.git` directories.
9. Explicit `[[workspaces]]` entries override discovered aliases.

## Proposed User Flow

### Historical Workbench

The user sends:

```text
/workbenches
```

The bot replies:

```text
工作台历史

* #42 [总工程师] 修复 Telegram 审批 · wlcodex · 当前 · 2026-05-21 14:10
  #37 [Claude 直聊] LightFee 部署检查 · lightfee · 已归档 · 2026-05-20 23:41
  #31 [Codex 直聊] Finance 账单模型 · finance · 已归档 · 2026-05-19 18:22
```

Each row can expose buttons:

- `恢复工作台`
- `查看状态`
- `查看现场`

Restoring `#37` archives `#42`, clears `archived_at` on `#37`, updates `updated_at`, emits a runtime event, and replies:

```text
已恢复工作台 #37：「LightFee 部署检查」
工作区：lightfee

直接发消息会继续这个工作台。
```

### Workspace Switching

The user sends:

```text
/workspaces
```

The bot replies:

```text
可用工作区

* wlcodex  /media/wl/新加卷/codex/wlcodex  可写  当前
  lightfee /media/wl/新加卷/codex/LightFee 可写
  finance  /media/wl/新加卷/codex/Finance  可写
```

The user sends:

```text
/switch lightfee
```

The bot replies:

```text
对话「新工作台」工作区已切换至 lightfee。
```

## Architecture

### Data Model

No new database table is required for V1.

Use existing `conversation_sessions` fields:

- `chat_id`
- `title`
- `mode`
- `workspace_alias`
- `archived_at`
- `updated_at`
- `active_codex_task_id`
- `active_claude_run_id`

Add focused `Ledger` methods:

- `list_conversations_by_chat(chat_id, limit=20, include_archived=False)`
- `restore_conversation(conversation_id)`
- `archive_other_conversations(chat_id, except_conversation_id)`

These methods keep archive/restore logic in the database layer instead of scattering SQL in the controller.

### Runtime Events

Use existing event concepts where possible:

- `conversation.activated` for restore.
- `conversation.closed` for archiving the previously active Workbench.
- `conversation.mode.switched` is not needed for Workbench restore unless the surface changes.

If `EventType.CONVERSATION_ACTIVATED` exists, use it. If it does not, add it with the string value `conversation.activated`.

### Commands

Add parser dataclasses:

- `WorkbenchHistoryCommand`
- `WorkspaceListCommand`

Parse:

- `/workbenches`
- `/history`
- `/workspaces`

Keep `/sessions` for current-Workbench Agent session history.

### Telegram Callbacks

Use the existing `conv:<conversation_id>:<action>` callback style.

Recommended callback actions:

- `restore_workbench`
- `workbench_status`
- `workbench_sessions`

If callback data length becomes tight, use short equivalents:

- `restore`
- `status`
- `sessions`

### Workspace Discovery

Add optional config:

```toml
[workspace_discovery]
enabled = true
root = "/media/wl/新加卷/codex"
include_git_only = true
allow_write = true
exclude = ["wlcodex/runtime", "wlcodex/.venv"]
```

Discovery rules:

1. Scan only immediate children of `root`.
2. Include a child when it is a directory and either:
   - `include_git_only = false`, or
   - child contains `.git`.
3. Alias is a safe lowercase slug derived from the directory name.
4. Explicit `[[workspaces]]` entries win on alias collision.
5. Raise `ConfigError` if two discovered directories produce the same alias.
6. Do not create directories.
7. Do not follow symlinks by default.

This makes your local layout convenient while keeping the trust boundary visible.

## Error Handling

- `/workbenches` with no rows: "还没有历史工作台。发送 /new 开始新的工作台。"
- Restore missing id: "工作台不存在或已被删除。"
- Restore workspace no longer configured: restore read-only metadata, but block execution and tell the user to add the workspace alias back.
- `/workspaces` with no active Workbench: list workspaces anyway and say `/new` creates one.
- `/switch unknown`: "工作区 'unknown' 不存在。发送 /workspaces 查看可用工作区。"
- Auto-discovery root missing: load explicit workspaces and include a warning in logs; do not fail startup when explicit workspaces exist.
- Auto-discovery root missing with zero explicit workspaces: fail config with `ConfigError`.

## Testing Requirements

Unit tests:

- Parser recognizes `/workbenches`, `/history`, `/workspaces`.
- `Ledger.list_conversations_by_chat(..., include_archived=True)` returns active and archived Workbenches.
- `Ledger.restore_conversation` clears `archived_at`.
- Restore archives other active Workbenches for the same chat.
- Workbench history renderer marks active vs archived.
- Workspace renderer marks active workspace.
- Config discovery includes immediate `.git` children and ignores nested directories.
- Explicit workspace entries override discovered entries.
- `/switch unknown` mentions `/workspaces`.

Integration tests:

- `/new`, `/new`, `/workbenches`, restore first Workbench, then `/status` points at restored Workbench.
- `/workspaces`, `/switch lightfee`, ordinary text creates execution in `lightfee`.
- `/sessions` still lists current Workbench Agent sessions and does not become global Workbench history.

## Non-Goals

- Do not merge Workbenches.
- Do not delete archived Workbenches.
- Do not auto-run old Workbenches after restore.
- Do not recursively scan every nested directory under `/media/wl/新加卷/codex`.
- Do not change approval timeout behavior.
- Do not change terminal/onsite session resume semantics.

## Rollout

1. Ship history listing and restore for configured workspaces.
2. Ship `/workspaces` for configured workspace visibility.
3. Add optional root discovery behind config.
4. Add operator guide text to `config/wlcodex.example.toml`.
5. After implementation, update live config separately if the operator wants discovery enabled.

## Spec Self-Review

- Placeholder scan: no unresolved placeholders are present.
- Scope check: the feature is focused on Workbench visibility and workspace switching only.
- Ambiguity check: `/sessions` remains Agent-session scoped; `/workbenches` owns Workbench history.
- Safety check: workspace discovery is bounded to immediate children and explicit config remains authoritative.
