# Workbench History and Workspace Switching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add historical Workbench visibility, restore archived Workbenches safely, and make project workspace switching clear for repositories under `/media/wl/新加卷/codex`.

**Architecture:** Keep `conversation_sessions` as the source of Workbench truth, add small `Ledger` methods for list/restore, keep `/sessions` scoped to Agent sessions, and add separate `/workbenches` plus `/workspaces` commands. Workspace discovery is optional config-driven startup behavior that expands `AppConfig.workspaces` before `TaskService` is constructed.

**Tech Stack:** Python, sqlite3, python-telegram-bot command handlers, TOML config via `tomllib`, pytest.

---

## Current Baseline

- `/new` archives the active Workbench, then creates a new one.
- `Ledger.get_active_conversation` and `Ledger.list_conversations_by_chat` currently filter `archived_at IS NULL`.
- Telegram `/sessions` lists Agent sessions inside the active Workbench.
- `/switch <alias>` only works when `<alias>` is configured in `[[workspaces]]`.
- `config/wlcodex.toml` currently registers only `wlcodex`.

## File Map

- Modify `wlcodex/router.py`: add command dataclasses and parse `/workbenches`, `/history`, `/workspaces`.
- Modify `wlcodex/db.py`: add archived-aware Workbench listing and restore helpers.
- Modify `wlcodex/status.py`: add Workbench history and workspace list renderers.
- Modify `wlcodex/conversation_callback.py`: add callback constants for Workbench restore/status/session actions.
- Modify `wlcodex/controller.py`: handle new commands and restore callbacks.
- Modify `wlcodex/telegram_app.py`: register command handlers and route Workbench callback buttons.
- Modify `wlcodex/config.py`: add optional workspace discovery config.
- Modify `config/wlcodex.example.toml`: document workspace discovery and manual workspace entries.
- Add or modify tests in `tests/test_router.py`, `tests/test_db.py`, `tests/test_status.py`, `tests/test_controller_flow.py`, `tests/test_telegram_handlers.py`, and `tests/test_config.py`.

---

### Task 1: Parser Commands

**Files:**
- Modify: `wlcodex/router.py`
- Test: `tests/test_router.py`

- [ ] **Step 1: Add failing parser tests**

Add these tests to `tests/test_router.py`:

```python
def test_parse_workbenches_command() -> None:
    from wlcodex.router import WorkbenchHistoryCommand, parse_command

    assert isinstance(parse_command("/workbenches"), WorkbenchHistoryCommand)
    assert isinstance(parse_command("/history"), WorkbenchHistoryCommand)


def test_parse_workspaces_command() -> None:
    from wlcodex.router import WorkspaceListCommand, parse_command

    assert isinstance(parse_command("/workspaces"), WorkspaceListCommand)
```

- [ ] **Step 2: Run parser tests and confirm failure**

Run:

```bash
rtk pytest tests/test_router.py::test_parse_workbenches_command tests/test_router.py::test_parse_workspaces_command -q
```

Expected: imports or assertions fail because the command classes do not exist yet.

- [ ] **Step 3: Add command dataclasses and parse branches**

In `wlcodex/router.py`, add dataclasses near the other command types:

```python
@dataclass(frozen=True)
class WorkbenchHistoryCommand:
    pass


@dataclass(frozen=True)
class WorkspaceListCommand:
    pass
```

Add both types to the `ParsedCommand` union.

Add parse branches before `/new`:

```python
    if stripped == "/workbenches" or stripped == "/history":
        return WorkbenchHistoryCommand()
    if stripped == "/workspaces":
        return WorkspaceListCommand()
```

- [ ] **Step 4: Run parser tests and confirm pass**

Run:

```bash
rtk pytest tests/test_router.py::test_parse_workbenches_command tests/test_router.py::test_parse_workspaces_command -q
```

Expected: both tests pass.

- [ ] **Step 5: Commit parser changes**

```bash
git add wlcodex/router.py tests/test_router.py
git commit -m "feat: parse workbench and workspace commands"
```

---

### Task 2: Ledger Workbench History and Restore

**Files:**
- Modify: `wlcodex/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Add failing database tests**

Add these tests to `tests/test_db.py`:

```python
def test_list_conversations_by_chat_can_include_archived(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    first = ledger.create_conversation(
        chat_id=10,
        user_id=1,
        title="First",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    second = ledger.create_conversation(
        chat_id=10,
        user_id=1,
        title="Second",
        mode="chief_engineer",
        workspace_alias="lightfee",
    )
    ledger.archive_conversation(first.id)

    active_only = ledger.list_conversations_by_chat(10)
    with_archived = ledger.list_conversations_by_chat(10, include_archived=True)

    assert [c.id for c in active_only] == [second.id]
    assert {c.id for c in with_archived} == {first.id, second.id}


def test_restore_conversation_archives_other_active_workbench(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    old = ledger.create_conversation(
        chat_id=10,
        user_id=1,
        title="Old",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ledger.archive_conversation(old.id)
    current = ledger.create_conversation(
        chat_id=10,
        user_id=1,
        title="Current",
        mode="chief_engineer",
        workspace_alias="lightfee",
    )

    restored = ledger.restore_conversation(old.id)

    assert restored.id == old.id
    assert restored.archived_at is None
    assert ledger.get_conversation(current.id).archived_at is not None
    assert ledger.get_active_conversation(10).id == old.id
```

- [ ] **Step 2: Run database tests and confirm failure**

Run:

```bash
rtk pytest tests/test_db.py::test_list_conversations_by_chat_can_include_archived tests/test_db.py::test_restore_conversation_archives_other_active_workbench -q
```

Expected: failure because `include_archived` and `restore_conversation` are not implemented.

- [ ] **Step 3: Implement archived-aware listing**

Update `Ledger.list_conversations_by_chat` in `wlcodex/db.py`:

```python
    def list_conversations_by_chat(
        self, chat_id: int, limit: int = 20, include_archived: bool = False
    ) -> list[ConversationSession]:
        if include_archived:
            rows = self._conn.execute(
                """
                SELECT * FROM conversation_sessions
                WHERE chat_id = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM conversation_sessions
                WHERE chat_id = ? AND archived_at IS NULL
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        return [_conversation(row) for row in rows]
```

- [ ] **Step 4: Implement restore helper**

Add this method near the conversation helpers in `wlcodex/db.py`:

```python
    def restore_conversation(self, conversation_id: int) -> ConversationSession:
        conversation = self.get_conversation(conversation_id)
        now = _now()
        self._conn.execute(
            """
            UPDATE conversation_sessions
            SET archived_at = ?, updated_at = ?
            WHERE chat_id = ? AND id != ? AND archived_at IS NULL
            """,
            (now, now, conversation.chat_id, conversation_id),
        )
        self._conn.execute(
            """
            UPDATE conversation_sessions
            SET archived_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (now, conversation_id),
        )
        self._conn.commit()
        return self.get_conversation(conversation_id)
```

- [ ] **Step 5: Run database tests and confirm pass**

Run:

```bash
rtk pytest tests/test_db.py::test_list_conversations_by_chat_can_include_archived tests/test_db.py::test_restore_conversation_archives_other_active_workbench -q
```

Expected: both tests pass.

- [ ] **Step 6: Commit database changes**

```bash
git add wlcodex/db.py tests/test_db.py
git commit -m "feat: restore archived workbenches"
```

---

### Task 3: Render Workbench History and Workspaces

**Files:**
- Modify: `wlcodex/status.py`
- Test: `tests/test_status.py`

- [ ] **Step 1: Add failing renderer tests**

Add tests to `tests/test_status.py`:

```python
from datetime import datetime, timezone
from types import SimpleNamespace


def test_render_workbench_history_marks_active_and_archived() -> None:
    from wlcodex.status import render_workbench_history

    now = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    sessions = [
        SimpleNamespace(
            id=2,
            title="Current",
            mode="chief_engineer",
            workspace_alias="wlcodex",
            archived_at=None,
            updated_at=now,
        ),
        SimpleNamespace(
            id=1,
            title="Old",
            mode="codex_direct",
            workspace_alias="lightfee",
            archived_at=now,
            updated_at=now,
        ),
    ]

    text = render_workbench_history(sessions)

    assert "工作台历史" in text
    assert "#2" in text and "当前" in text
    assert "#1" in text and "已归档" in text
    assert "lightfee" in text


def test_render_workspace_list_marks_active_workspace() -> None:
    from wlcodex.config import WorkspaceConfig
    from wlcodex.status import render_workspace_list
    from pathlib import Path

    workspaces = [
        WorkspaceConfig("wlcodex", Path("/repo/wlcodex"), True),
        WorkspaceConfig("lightfee", Path("/repo/LightFee"), True),
    ]

    text = render_workspace_list(workspaces, active_alias="lightfee")

    assert "可用工作区" in text
    assert "wlcodex" in text
    assert "lightfee" in text
    assert "当前" in text
```

- [ ] **Step 2: Run renderer tests and confirm failure**

Run:

```bash
rtk pytest tests/test_status.py::test_render_workbench_history_marks_active_and_archived tests/test_status.py::test_render_workspace_list_marks_active_workspace -q
```

Expected: imports fail because renderers are missing.

- [ ] **Step 3: Implement renderers**

Add these functions in `wlcodex/status.py` near the conversation renderers:

```python
def render_workbench_history(sessions: Sequence[ConversationSession]) -> str:
    if not sessions:
        return "还没有历史工作台。发送 /new 开始新的工作台。"

    lines = ["工作台历史", ""]
    for session in sessions:
        mode_label = MODE_LABELS.get(session.mode, session.mode)
        state = "当前" if session.archived_at is None else "已归档"
        updated = _format_dt(getattr(session, "updated_at", None))
        marker = "*" if session.archived_at is None else " "
        lines.append(
            f"{marker} #{session.id} [{mode_label}] "
            f"{_trim(session.title, 60)} · {session.workspace_alias} · "
            f"{state} · {updated}"
        )
    return "\n".join(lines)


def render_workspace_list(
    workspaces: Sequence[object], *, active_alias: str = ""
) -> str:
    if not workspaces:
        return "当前没有可用工作区。请检查配置。"

    lines = ["可用工作区", ""]
    for workspace in workspaces:
        alias = str(getattr(workspace, "alias", ""))
        path = str(getattr(workspace, "path", ""))
        writable = "可写" if bool(getattr(workspace, "allow_write", False)) else "只读"
        current = " · 当前" if alias == active_alias else ""
        marker = "*" if alias == active_alias else " "
        lines.append(f"{marker} {alias}  {path}  {writable}{current}")
    return "\n".join(lines)


def _format_dt(value: object) -> str:
    if value is None:
        return "未知时间"
    text = str(value)
    return text.replace("T", " ")[:16]
```

- [ ] **Step 4: Run renderer tests and confirm pass**

Run:

```bash
rtk pytest tests/test_status.py::test_render_workbench_history_marks_active_and_archived tests/test_status.py::test_render_workspace_list_marks_active_workspace -q
```

Expected: both tests pass.

- [ ] **Step 5: Commit renderer changes**

```bash
git add wlcodex/status.py tests/test_status.py
git commit -m "feat: render workbench and workspace lists"
```

---

### Task 4: Controller Command Handling

**Files:**
- Modify: `wlcodex/controller.py`
- Test: `tests/test_controller_flow.py`

- [ ] **Step 1: Add failing controller tests**

Add tests to `tests/test_controller_flow.py`:

```python
async def test_workbenches_command_lists_archived_conversations(ctrl: CommandController) -> None:
    ledger = ctrl._ledger
    first = ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="First",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ledger.archive_conversation(first.id)
    ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Second",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )

    response = await ctrl.handle("/workbenches", {"chat_id": 100, "user_id": 7})

    assert "工作台历史" in response.text
    assert "First" in response.text
    assert "Second" in response.text


async def test_workspaces_command_lists_configured_workspaces(ctrl: CommandController) -> None:
    response = await ctrl.handle("/workspaces", {"chat_id": 100, "user_id": 7})

    assert "可用工作区" in response.text
    assert "wlcodex" in response.text


async def test_switch_unknown_workspace_mentions_workspaces(ctrl: CommandController) -> None:
    conversation = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Demo",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )

    response = await ctrl.handle("/switch missing", {"chat_id": 100, "user_id": 7})

    assert conversation.id
    assert "/workspaces" in response.text
```

- [ ] **Step 2: Run controller tests and confirm failure**

Run:

```bash
rtk pytest tests/test_controller_flow.py::test_workbenches_command_lists_archived_conversations tests/test_controller_flow.py::test_workspaces_command_lists_configured_workspaces tests/test_controller_flow.py::test_switch_unknown_workspace_mentions_workspaces -q
```

Expected: command handling or error text fails.

- [ ] **Step 3: Import new command/renderers**

In `wlcodex/controller.py`, import:

```python
    WorkbenchHistoryCommand,
    WorkspaceListCommand,
```

from `wlcodex.router`, and import:

```python
    render_workbench_history,
    render_workspace_list,
```

from `wlcodex.status`.

- [ ] **Step 4: Handle `/workbenches` and `/workspaces`**

Add command branches before `NewConversationCommand`:

```python
            elif isinstance(command, WorkbenchHistoryCommand):
                if self._ledger is not None and telegram_context:
                    chat_id = telegram_context.get("chat_id", 0)
                    sessions = self._ledger.list_conversations_by_chat(
                        chat_id, include_archived=True
                    )
                    return ControllerResponse(render_workbench_history(sessions))
                return ControllerResponse(
                    "还没有历史工作台。发送 /new 开始新的工作台。"
                )

            elif isinstance(command, WorkspaceListCommand):
                active_alias = ""
                if self._ledger is not None and telegram_context:
                    chat_id = telegram_context.get("chat_id", 0)
                    active = self._ledger.get_active_conversation(chat_id)
                    if active is not None:
                        active_alias = active.workspace_alias
                workspaces = list(getattr(self._service, "_workspaces", {}).values())
                return ControllerResponse(
                    render_workspace_list(workspaces, active_alias=active_alias)
                )
```

- [ ] **Step 5: Improve unknown workspace error**

In `handle_switch_workspace`, change the unknown workspace response to:

```python
            return ControllerResponse(
                f"工作区 '{command.workspace_alias}' 不存在。发送 /workspaces 查看可用工作区。"
            )
```

- [ ] **Step 6: Run controller tests and confirm pass**

Run:

```bash
rtk pytest tests/test_controller_flow.py::test_workbenches_command_lists_archived_conversations tests/test_controller_flow.py::test_workspaces_command_lists_configured_workspaces tests/test_controller_flow.py::test_switch_unknown_workspace_mentions_workspaces -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit controller command handling**

```bash
git add wlcodex/controller.py tests/test_controller_flow.py
git commit -m "feat: list workbenches and workspaces"
```

---

### Task 5: Workbench Restore Callback

**Files:**
- Modify: `wlcodex/conversation_callback.py`
- Modify: `wlcodex/controller.py`
- Modify: `wlcodex/telegram_app.py`
- Test: `tests/test_controller_flow.py`
- Test: `tests/test_telegram_handlers.py`

- [ ] **Step 1: Add failing controller restore test**

Add to `tests/test_controller_flow.py`:

```python
async def test_restore_workbench_callback_restores_archived_conversation(ctrl: CommandController) -> None:
    from wlcodex.conversation_callback import ConversationCallback

    ledger = ctrl._ledger
    old = ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Old",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ledger.archive_conversation(old.id)
    current = ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Current",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation_id=old.id, action="restore_workbench")
    )

    assert "已恢复工作台" in response.text
    assert ledger.get_conversation(old.id).archived_at is None
    assert ledger.get_conversation(current.id).archived_at is not None
```

- [ ] **Step 2: Add callback constants**

In `wlcodex/conversation_callback.py`, add:

```python
RESTORE_WORKBENCH = "restore_workbench"
WORKBENCH_STATUS = "workbench_status"
WORKBENCH_SESSIONS = "workbench_sessions"
```

- [ ] **Step 3: Implement controller callback handling**

In `CommandController.handle_conversation_callback`, add a branch before generic continuation handling:

```python
        if callback.action == RESTORE_WORKBENCH:
            return await self._handle_restore_workbench(callback.conversation_id)
```

Add helper:

```python
    async def _handle_restore_workbench(self, conversation_id: int) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")
        try:
            restored = self._ledger.restore_conversation(conversation_id)
        except KeyError:
            return ControllerResponse("工作台不存在或已被删除。")
        try:
            self._service.get_workspace(restored.workspace_alias)
            workspace_note = f"工作区：{restored.workspace_alias}"
        except Exception:
            workspace_note = (
                f"工作区：{restored.workspace_alias}（当前配置不存在，"
                "请先添加该 workspace 后再执行任务）"
            )
        return ControllerResponse(
            f"已恢复工作台 #{restored.id}：「{restored.title}」\n"
            f"{workspace_note}\n\n"
            "直接发消息会继续这个工作台。"
        )
```

- [ ] **Step 4: Add Workbench history buttons in Telegram**

In `WlCodexHandlers.codex_sessions`, do not change `/sessions`.

Add a new handler method:

```python
    async def workbenches(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        chat_id = update.effective_chat.id
        buttons: list[list[dict[str, str]]] = []
        if self._ledger is not None:
            sessions = self._ledger.list_conversations_by_chat(
                chat_id, include_archived=True
            )
            for session in sessions:
                buttons.append([{
                    "text": f"恢复 #{session.id}",
                    "callback_data": f"conv:{session.id}:restore_workbench",
                }])
        await self.send_telegram(chat_id, response.text, buttons=buttons or None)
```

Register:

```python
    application.add_handler(CommandHandler("workbenches", handlers.workbenches))
    application.add_handler(CommandHandler("history", handlers.workbenches))
```

- [ ] **Step 5: Run restore tests**

Run:

```bash
rtk pytest tests/test_controller_flow.py::test_restore_workbench_callback_restores_archived_conversation -q
```

Expected: pass.

- [ ] **Step 6: Commit restore callback**

```bash
git add wlcodex/conversation_callback.py wlcodex/controller.py wlcodex/telegram_app.py tests/test_controller_flow.py tests/test_telegram_handlers.py
git commit -m "feat: restore historical workbenches"
```

---

### Task 6: Workspace Discovery Config

**Files:**
- Modify: `wlcodex/config.py`
- Modify: `config/wlcodex.example.toml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Add failing config tests**

Add to `tests/test_config.py`:

```python
def test_workspace_discovery_adds_git_children(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    lightfee = root / "LightFee"
    lightfee.mkdir()
    (lightfee / ".git").mkdir()

    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        f"""
[telegram]
bot_token_env = "TOKEN"
allowed_user_ids = [123]

[codex]
app_server_host = "127.0.0.1"
app_server_port = 17431

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[workspace_discovery]
enabled = true
root = "{root}"
include_git_only = true
allow_write = true

[[workspaces]]
alias = "wlcodex"
path = "{tmp_path / 'wlcodex'}"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.workspace_by_alias("lightfee").path == lightfee
    assert config.workspace_by_alias("lightfee").allow_write is True


def test_explicit_workspace_overrides_discovered_alias(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    repo = root / "LightFee"
    repo.mkdir()
    (repo / ".git").mkdir()
    explicit = tmp_path / "explicit-lightfee"

    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        f"""
[telegram]
bot_token_env = "TOKEN"
allowed_user_ids = [123]

[codex]
app_server_host = "127.0.0.1"
app_server_port = 17431

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[workspace_discovery]
enabled = true
root = "{root}"
include_git_only = true

[[workspaces]]
alias = "lightfee"
path = "{explicit}"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.workspace_by_alias("lightfee").path == explicit
```

- [ ] **Step 2: Run config tests and confirm failure**

Run:

```bash
rtk pytest tests/test_config.py::test_workspace_discovery_adds_git_children tests/test_config.py::test_explicit_workspace_overrides_discovered_alias -q
```

Expected: failure because discovery config is not implemented.

- [ ] **Step 3: Add config dataclass and loader**

In `wlcodex/config.py`, add:

```python
@dataclass(frozen=True)
class WorkspaceDiscoveryConfig:
    enabled: bool = False
    root: Path | None = None
    include_git_only: bool = True
    allow_write: bool = True
    exclude: tuple[str, ...] = ()
```

Add it to `AppConfig`:

```python
    workspace_discovery: WorkspaceDiscoveryConfig = WorkspaceDiscoveryConfig()
```

- [ ] **Step 4: Implement discovery helpers**

Add helpers in `wlcodex/config.py`:

```python
def _workspace_discovery(raw: dict) -> WorkspaceDiscoveryConfig:
    root_value = raw.get("root")
    return WorkspaceDiscoveryConfig(
        enabled=bool(raw.get("enabled", False)),
        root=Path(str(root_value)) if root_value else None,
        include_git_only=bool(raw.get("include_git_only", True)),
        allow_write=bool(raw.get("allow_write", True)),
        exclude=tuple(str(item) for item in raw.get("exclude", [])),
    )


def _workspace_alias_from_dir(name: str) -> str:
    chars: list[str] = []
    previous_dash = False
    for char in name.lower():
        if char.isalnum():
            chars.append(char)
            previous_dash = False
        elif not previous_dash:
            chars.append("-")
            previous_dash = True
    alias = "".join(chars).strip("-")
    if not alias:
        raise ConfigError(f"cannot derive workspace alias from directory: {name}")
    return alias


def _discover_workspaces(discovery: WorkspaceDiscoveryConfig) -> tuple[WorkspaceConfig, ...]:
    if not discovery.enabled:
        return ()
    if discovery.root is None:
        raise ConfigError("workspace_discovery.root is required when discovery is enabled")
    if not discovery.root.exists():
        return ()
    excluded = set(discovery.exclude)
    discovered: list[WorkspaceConfig] = []
    seen: set[str] = set()
    for child in sorted(discovery.root.iterdir(), key=lambda path: path.name.lower()):
        if child.name in excluded:
            continue
        if not child.is_dir() or child.is_symlink():
            continue
        if discovery.include_git_only and not (child / ".git").exists():
            continue
        alias = _workspace_alias_from_dir(child.name)
        if alias in seen:
            raise ConfigError(f"duplicate discovered workspace alias: {alias}")
        seen.add(alias)
        discovered.append(
            WorkspaceConfig(alias=alias, path=child, allow_write=discovery.allow_write)
        )
    return tuple(discovered)
```

- [ ] **Step 5: Merge explicit and discovered workspaces**

In `load_config`, replace the initial workspace construction with:

```python
    discovery = _workspace_discovery(data.get("workspace_discovery", {}))
    explicit_workspaces = tuple(_workspace(item) for item in data.get("workspaces", []))
    discovered_workspaces = _discover_workspaces(discovery)

    explicit_aliases = {workspace.alias for workspace in explicit_workspaces}
    workspaces = explicit_workspaces + tuple(
        workspace
        for workspace in discovered_workspaces
        if workspace.alias not in explicit_aliases
    )
```

Keep the existing duplicate alias check after this merge.

Pass `workspace_discovery=discovery` into `AppConfig`.

- [ ] **Step 6: Document example config**

In `config/wlcodex.example.toml`, add:

```toml
[workspace_discovery]
# Optional: discover immediate child git repos under your local project root.
# Explicit [[workspaces]] entries override discovered aliases.
enabled = false
root = "/media/wl/新加卷/codex"
include_git_only = true
allow_write = true
exclude = []
```

Also add example workspaces:

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

- [ ] **Step 7: Run config tests and confirm pass**

Run:

```bash
rtk pytest tests/test_config.py::test_workspace_discovery_adds_git_children tests/test_config.py::test_explicit_workspace_overrides_discovered_alias -q
```

Expected: both tests pass.

- [ ] **Step 8: Commit discovery config**

```bash
git add wlcodex/config.py config/wlcodex.example.toml tests/test_config.py
git commit -m "feat: discover local project workspaces"
```

---

### Task 7: Telegram Command Wiring

**Files:**
- Modify: `wlcodex/telegram_app.py`
- Test: `tests/test_telegram_handlers.py`

- [ ] **Step 1: Add failing Telegram routing tests**

Add this test to `tests/test_telegram_handlers.py`:

```python
def test_workbench_and_workspace_commands_registered(tmp_path: Path) -> None:
    from wlcodex.approval import ApprovalService
    from wlcodex.codex_backend import FakeCodexBackend
    from wlcodex.config import WorkspaceConfig, load_config
    from wlcodex.controller import CommandController
    from wlcodex.db import Ledger
    from wlcodex.inspection import TaskInspector
    from wlcodex.task_service import TaskService

    config_path = Path(__file__).parent / "fixtures" / "test_handler_config.toml"
    config = load_config(config_path)
    ledger = Ledger.open(tmp_path / "test.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        (WorkspaceConfig("demo", Path("/tmp/demo"), True),),
    )
    controller = CommandController(
        service,
        FakeCodexBackend(),
        TaskInspector(ledger, tmp_path / "logs"),
        ledger=ledger,
    )
    application, _handlers = build_application(
        config,
        "dummy-token",
        controller,
        ledger,
        ApprovalService(),
    )

    command_names = {
        command
        for handler_group in application.handlers.values()
        for handler in handler_group
        for command in getattr(handler, "commands", set())
    }

    assert "workbenches" in command_names
    assert "history" in command_names
    assert "workspaces" in command_names
```

- [ ] **Step 2: Run Telegram registration test and confirm failure**

Run:

```bash
rtk pytest tests/test_telegram_handlers.py::test_workbench_and_workspace_commands_registered -q
```

Expected: command names are missing.

- [ ] **Step 3: Add handler methods**

In `WlCodexHandlers`, add:

```python
    async def workbenches(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await self._reply_with_buttons(update, response.text, response.buttons)

    async def workspaces(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await self._reply_with_buttons(update, response.text, response.buttons)
```

If the `workbenches` method already exists from Task 5, keep its button rendering and add only the `workspaces` method from this step.

- [ ] **Step 4: Register command handlers**

In `build_application`, add:

```python
    application.add_handler(CommandHandler("workbenches", handlers.workbenches))
    application.add_handler(CommandHandler("history", handlers.workbenches))
    application.add_handler(CommandHandler("workspaces", handlers.workspaces))
```

- [ ] **Step 5: Run Telegram registration test**

Run:

```bash
rtk pytest tests/test_telegram_handlers.py::test_workbench_and_workspace_commands_registered -q
```

Expected: pass.

- [ ] **Step 6: Commit Telegram wiring**

```bash
git add wlcodex/telegram_app.py tests/test_telegram_handlers.py
git commit -m "feat: wire workbench history commands"
```

---

### Task 8: Integration and Regression Tests

**Files:**
- Modify: `tests/test_workbench_telegram_routing.py`
- Modify: `tests/test_controller_flow.py`

- [ ] **Step 1: Add Workbench restore integration test**

Add:

```python
async def test_new_new_history_restore_status_flow(ctrl: CommandController) -> None:
    await ctrl.handle("/new First", {"chat_id": 100, "user_id": 7})
    first = ctrl._ledger.get_active_conversation(100)
    await ctrl.handle("/new Second", {"chat_id": 100, "user_id": 7})

    history = await ctrl.handle("/workbenches", {"chat_id": 100, "user_id": 7})
    assert "First" in history.text
    assert "Second" in history.text

    from wlcodex.conversation_callback import ConversationCallback
    await ctrl.handle_conversation_callback(
        ConversationCallback(conversation_id=first.id, action="restore_workbench")
    )

    status = await ctrl.handle("/status", {"chat_id": 100, "user_id": 7})
    assert "First" in status.text
```

- [ ] **Step 2: Add `/sessions` regression test**

Add:

```python
async def test_sessions_remains_current_workbench_agent_library(ctrl: CommandController) -> None:
    await ctrl.handle("/new Current", {"chat_id": 100, "user_id": 7})

    response = await ctrl.handle("/sessions", {"chat_id": 100, "user_id": 7})

    assert "工作台列表" not in response.text
```

- [ ] **Step 3: Run integration tests**

Run:

```bash
rtk pytest tests/test_controller_flow.py::test_new_new_history_restore_status_flow tests/test_controller_flow.py::test_sessions_remains_current_workbench_agent_library -q
```

Expected: pass.

- [ ] **Step 4: Run focused suite**

Run:

```bash
rtk pytest tests/test_router.py tests/test_db.py tests/test_status.py tests/test_config.py tests/test_controller_flow.py tests/test_telegram_handlers.py -q
```

Expected: pass.

- [ ] **Step 5: Commit integration tests**

```bash
git add tests/test_controller_flow.py tests/test_workbench_telegram_routing.py
git commit -m "test: cover workbench history flow"
```

---

## Manual Acceptance

After implementation, run locally:

```bash
rtk pytest tests/test_router.py tests/test_db.py tests/test_status.py tests/test_config.py tests/test_controller_flow.py tests/test_telegram_handlers.py -q
```

Then verify through Telegram:

1. Send `/new First`.
2. Send `/new Second`.
3. Send `/workbenches`.
4. Confirm both `First` and `Second` appear.
5. Restore `First`.
6. Send `/status`.
7. Confirm status shows `First`.
8. Send `/workspaces`.
9. Confirm configured or discovered project aliases appear.
10. Send `/switch lightfee`.
11. Confirm the response says the active Workbench workspace is `lightfee`.

## Operator Config After Implementation

Manual workspace registration:

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

Optional discovery:

```toml
[workspace_discovery]
enabled = true
root = "/media/wl/新加卷/codex"
include_git_only = true
allow_write = true
exclude = []
```

With discovery enabled, immediate child git repositories under `/media/wl/新加卷/codex` become workspace aliases such as `lightfee`, `finance`, and `wlcodex`, unless explicit config overrides them.

## Self-Review

- Spec coverage: historical Workbench list, restore, current `/sessions` behavior, `/workspaces`, `/switch`, and root discovery are all mapped to tasks.
- Placeholder scan: no unresolved placeholders are present.
- Type consistency: `conversation_id` and `source_run_id` stay integers; workspace aliases stay strings; paths use `Path`.
- Safety: workspace discovery scans only immediate children and explicit config wins.
- Test coverage: parser, DB, renderer, config, controller, Telegram registration, and flow tests are covered.
