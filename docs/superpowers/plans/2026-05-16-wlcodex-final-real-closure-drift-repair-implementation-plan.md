# WLCodex Final Real Closure Drift Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair WLCodex drift so the real Telegram cockpit drives the real local Codex app-server end to end, with fake backends limited to unit-test helpers and excluded from smoke acceptance.

**Architecture:** Keep the existing lightweight Python package, but make the real app-server backend the authoritative runtime path. Add schema contract tests around Codex app-server JSON-RPC, integrate app-server process lifecycle into `main.py`, repair task/approval state consistency, and finish with real app-server plus real Telegram acceptance evidence.

**Tech Stack:** Python 3.12, `python-telegram-bot`, `websockets`, stdlib `sqlite3`, `pytest`, `pytest-asyncio`, `ruff`, Codex CLI app-server, systemd.

---

## Source Spec

Implement this spec:

`docs/superpowers/specs/2026-05-16-wlcodex-final-real-closure-drift-repair-design.md`

## File Structure

- Modify: `wlcodex/jsonrpc.py` - add request timeout, non-blocking held server requests, safe cancellation.
- Modify: `wlcodex/app_server_process.py` - add real WebSocket readiness probe and external-process reuse support.
- Modify: `wlcodex/codex_backend.py` - fix app-server request/response schema mapping, approval response shapes, health.
- Modify: `wlcodex/main.py` - own app-server lifecycle and pass process manager/request timeout into backend.
- Modify: `wlcodex/task_service.py` - fix workspace write checks, start reservation, continue state, approval counters, diff/log events.
- Modify: `wlcodex/controller.py` - reorder `/task`, fix `/continue`, `/fork`, and backend failure paths.
- Modify: `wlcodex/approval.py` - send backend response before final local resolution, support expiry and retry.
- Modify: `wlcodex/db.py` - add schema versioning, guarded column upgrades, approval counter helpers, task thread setter.
- Modify: `wlcodex/inspection.py` - add event fallback for `/tail`, store and read app-server diff payloads.
- Modify: `wlcodex/router.py` - parse `/sessions` as the canonical session command.
- Modify: `wlcodex/status.py` - update help text to `/sessions`.
- Modify: `wlcodex/telegram_app.py` - register `/sessions`, keep optional alias only if Telegram supports it.
- Modify: `config/wlcodex.example.toml` - expose real smoke env guidance and backend timeout settings.
- Modify: `README.md` - document real-only acceptance.
- Modify: `docs/superpowers/reports/2026-05-16-wlcodex-real-app-server-smoke.md` - replace blocked/fake-era conclusion with real evidence after tests pass.
- Create: `tests/test_protocol_contracts.py`
- Create: `tests/test_real_app_server_integration.py`
- Create: `tests/test_live_telegram_smoke.py`
- Create: `tests/test_drift_repairs.py`

## Verification Rule

Every task may use unit tests while developing, but final acceptance must run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
WLCODEX_RUN_CODEX_INTEGRATION=1 .venv/bin/python -m pytest tests/test_real_app_server_integration.py -q
WLCODEX_RUN_TELEGRAM_LIVE=1 .venv/bin/python -m pytest tests/test_live_telegram_smoke.py -q
```

`tests/test_e2e_fake_backend.py` may remain, but it is not smoke evidence.

## Task 1: Lock Protocol Drift With Schema Contract Tests

**Files:**
- Create: `tests/test_protocol_contracts.py`
- Modify: `wlcodex/codex_backend.py`

- [ ] **Step 1: Write failing contract tests for request payload builders**

Create `tests/test_protocol_contracts.py`:

```python
from wlcodex.codex_backend import (
    build_thread_start_params,
    build_turn_start_params,
    build_turn_steer_params,
    parse_thread_start_response,
    parse_turn_response,
    parse_turn_notification_ids,
    build_approval_response,
)
from wlcodex.models import ApprovalKind


def test_turn_start_uses_input_not_items() -> None:
    params = build_turn_start_params("thread-1", "hello")
    assert params == {
        "threadId": "thread-1",
        "input": [{"type": "text", "text": "hello"}],
    }
    assert "items" not in params


def test_turn_steer_uses_input_and_expected_turn_id() -> None:
    params = build_turn_steer_params("thread-1", "turn-1", "stop")
    assert params == {
        "threadId": "thread-1",
        "expectedTurnId": "turn-1",
        "input": [{"type": "text", "text": "stop"}],
    }


def test_thread_start_includes_policy_and_sandbox() -> None:
    params = build_thread_start_params("/tmp/work", "on-request", "workspace-write")
    assert params["cwd"] == "/tmp/work"
    assert params["approvalPolicy"] == "on-request"
    assert params["sandbox"] == "workspace-write"


def test_parse_nested_thread_and_turn_responses() -> None:
    assert parse_thread_start_response({"thread": {"id": "thread-1"}}) == "thread-1"
    assert parse_turn_response({"turn": {"id": "turn-1"}}) == "turn-1"


def test_parse_turn_notification_ids_from_nested_turn() -> None:
    assert parse_turn_notification_ids(
        {"threadId": "thread-1", "turn": {"id": "turn-1"}}
    ) == ("thread-1", "turn-1")


def test_command_approval_response_shape() -> None:
    assert build_approval_response(
        kind=ApprovalKind.COMMAND,
        action="approve_once",
        requested_permissions={},
        allow_session=True,
    ) == {"decision": "accept"}


def test_permissions_approval_response_shape() -> None:
    assert build_approval_response(
        kind=ApprovalKind.PERMISSIONS,
        action="approve_session",
        requested_permissions={"network": {"enabled": True}},
        allow_session=True,
    ) == {"permissions": {"network": {"enabled": True}}, "scope": "session"}


def test_permissions_deny_returns_empty_permission_profile() -> None:
    assert build_approval_response(
        kind=ApprovalKind.PERMISSIONS,
        action="deny",
        requested_permissions={"network": {"enabled": True}},
        allow_session=True,
    ) == {"permissions": {}, "scope": "turn"}
```

- [ ] **Step 2: Run the new contract tests and confirm drift failures**

Run:

```bash
.venv/bin/python -m pytest tests/test_protocol_contracts.py -q
```

Expected before implementation:

```text
FAILED
```

The failures should mention missing helper functions or old `items`/top-level id behavior.

- [ ] **Step 3: Add protocol helper functions**

In `wlcodex/codex_backend.py`, add pure helpers near the top:

```python
def _text_input(prompt: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": prompt}]


def build_thread_start_params(
    workspace_path: str, approval_policy: str, sandbox: str
) -> dict[str, object]:
    return {
        "cwd": workspace_path,
        "approvalPolicy": approval_policy,
        "sandbox": sandbox,
    }


def build_turn_start_params(thread_id: str, prompt: str) -> dict[str, object]:
    return {"threadId": thread_id, "input": _text_input(prompt)}


def build_turn_steer_params(
    thread_id: str, expected_turn_id: str, prompt: str
) -> dict[str, object]:
    return {
        "threadId": thread_id,
        "expectedTurnId": expected_turn_id,
        "input": _text_input(prompt),
    }


def parse_thread_start_response(result: dict[str, object]) -> str:
    thread = result.get("thread")
    if isinstance(thread, dict) and thread.get("id"):
        return str(thread["id"])
    if result.get("threadId"):
        return str(result["threadId"])
    if result.get("id"):
        return str(result["id"])
    raise RuntimeError(f"thread/start response missing thread id: {result}")


def parse_turn_response(result: dict[str, object]) -> str:
    turn = result.get("turn")
    if isinstance(turn, dict) and turn.get("id"):
        return str(turn["id"])
    if result.get("turnId"):
        return str(result["turnId"])
    if result.get("id"):
        return str(result["id"])
    raise RuntimeError(f"turn response missing turn id: {result}")


def parse_turn_notification_ids(payload: dict[str, object]) -> tuple[str, str]:
    thread_id = str(payload.get("threadId", ""))
    turn = payload.get("turn")
    turn_id = ""
    if isinstance(turn, dict):
        turn_id = str(turn.get("id", ""))
    if not turn_id:
        turn_id = str(payload.get("turnId", ""))
    if not thread_id or not turn_id:
        raise RuntimeError(f"turn notification missing ids: {payload}")
    return thread_id, turn_id
```

Add:

```python
def build_approval_response(
    *,
    kind,
    action: str,
    requested_permissions: dict[str, object],
    allow_session: bool,
) -> dict[str, object]:
    kind_value = kind.value if hasattr(kind, "value") else str(kind)
    if kind_value == "permissions":
        scope = "session" if action == "approve_session" and allow_session else "turn"
        if action in ("approve_once", "approve_session"):
            return {"permissions": requested_permissions, "scope": scope}
        return {"permissions": {}, "scope": "turn"}

    decision_map = {
        "approve_once": "accept",
        "approve_session": "acceptForSession" if allow_session else "accept",
        "deny": "decline",
        "cancel": "cancel",
    }
    return {"decision": decision_map[action]}
```

- [ ] **Step 4: Run contract tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_protocol_contracts.py -q
```

Expected:

```text
passed
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_protocol_contracts.py wlcodex/codex_backend.py
git commit -m "test: lock codex app-server protocol contracts"
```

## Task 2: Fix JSON-RPC Timeout And Held Approval Requests

**Files:**
- Modify: `wlcodex/jsonrpc.py`
- Modify: `tests/test_jsonrpc.py`

- [ ] **Step 1: Add failing JSON-RPC timeout and non-blocking server-request tests**

Append to `tests/test_jsonrpc.py`:

```python
import asyncio

import pytest

from wlcodex.jsonrpc import JsonRpcClient, JsonRpcTimeout


@pytest.mark.asyncio
async def test_request_times_out() -> None:
    sent = []

    async def send_json(message: dict) -> None:
        sent.append(message)

    client = JsonRpcClient(send_json=send_json, request_timeout_seconds=0.01)

    with pytest.raises(JsonRpcTimeout):
        await client.request("thread/start", {"cwd": "/tmp/work"})

    assert sent[0]["method"] == "thread/start"


@pytest.mark.asyncio
async def test_server_request_does_not_block_next_response() -> None:
    sent = []

    async def send_json(message: dict) -> None:
        sent.append(message)

    client = JsonRpcClient(send_json=send_json, request_timeout_seconds=1)

    async def approval_handler(params: dict, request_id: str) -> None:
        assert request_id == "approval-1"

    client.on_server_request("item/commandExecution/requestApproval", approval_handler)

    request_task = asyncio.create_task(
        client.receive_message({
            "jsonrpc": "2.0",
            "id": "approval-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1"},
        })
    )
    await asyncio.sleep(0)

    rpc_task = asyncio.create_task(client.request("thread/start", {"cwd": "/tmp/work"}))
    await asyncio.sleep(0)
    await client.receive_message({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

    assert await rpc_task == {"ok": True}

    client.resolve_server_request("approval-1", {"decision": "accept"})
    await request_task
    assert sent[-1] == {
        "jsonrpc": "2.0",
        "id": "approval-1",
        "result": {"decision": "accept"},
    }
```

- [ ] **Step 2: Run tests and confirm failures**

Run:

```bash
.venv/bin/python -m pytest tests/test_jsonrpc.py -q
```

Expected:

```text
FAILED
```

- [ ] **Step 3: Implement timeout and non-blocking held requests**

In `wlcodex/jsonrpc.py`:

- Add `JsonRpcTimeout(RuntimeError)`.
- Add `request_timeout_seconds: float = 60.0` to `JsonRpcClient`.
- Wrap request future waits with `asyncio.wait_for`.
- Remove pending future on timeout.
- For server requests, create a task that waits for the held future and sends the eventual response. `receive_message()` should return after the handler registers the held request.

The server request branch should follow this shape:

```python
future: asyncio.Future[dict[str, Any]] = asyncio.Future()
self._held_requests[rid] = future

try:
    await handler(message.get("params", {}), rid)
except Exception:
    if not future.done():
        future.set_exception(JsonRpcError(code=-32000, message="Approval handler error"))

asyncio.create_task(self._send_held_response(rid, future))
return
```

Add `_send_held_response()`:

```python
async def _send_held_response(
    self, rid: str, future: asyncio.Future[dict[str, Any]]
) -> None:
    try:
        result = await future
        await self.send_json({"jsonrpc": "2.0", "id": rid, "result": result})
    except JsonRpcError as exc:
        await self.send_json({
            "jsonrpc": "2.0",
            "id": rid,
            "error": {"code": exc.code, "message": exc.rpc_message},
        })
    except asyncio.CancelledError:
        pass
    finally:
        self._held_requests.pop(rid, None)
```

- [ ] **Step 4: Run JSON-RPC tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_jsonrpc.py -q
```

Expected:

```text
passed
```

- [ ] **Step 5: Commit**

```bash
git add wlcodex/jsonrpc.py tests/test_jsonrpc.py
git commit -m "fix: make jsonrpc requests bounded and approvals nonblocking"
```

## Task 3: Integrate Real App-Server Process Lifecycle

**Files:**
- Modify: `wlcodex/app_server_process.py`
- Modify: `wlcodex/codex_backend.py`
- Modify: `wlcodex/main.py`
- Modify: `tests/test_app_server_process.py`
- Modify: `tests/test_main_composition.py`

- [ ] **Step 1: Add process readiness tests**

In `tests/test_app_server_process.py`, add:

```python
def test_process_config_endpoint_is_loopback() -> None:
    from wlcodex.app_server_process import AppServerProcessConfig

    cfg = AppServerProcessConfig(binary="codex", host="127.0.0.1", port=17431)
    assert cfg.endpoint == "ws://127.0.0.1:17431"


def test_backend_health_external_process_can_be_healthy() -> None:
    from wlcodex.app_server_process import BackendHealth

    health = BackendHealth(
        process_alive=True,
        websocket_connected=True,
        external_process=True,
    )
    assert health.is_healthy
```

- [ ] **Step 2: Extend `BackendHealth`**

In `wlcodex/app_server_process.py`, extend the dataclass:

```python
@dataclass
class BackendHealth:
    process_alive: bool
    websocket_connected: bool
    error: str | None = None
    external_process: bool = False
```

Keep `is_healthy` true when process is alive or external process is known healthy and the websocket is connected.

- [ ] **Step 3: Replace fake readiness with WebSocket readiness**

Add to `AppServerProcess`:

```python
async def wait_ready_async(self) -> bool:
    import asyncio
    import websockets

    deadline = time.monotonic() + self._config.startup_timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        if self._process is not None and self._process.poll() is not None:
            return False
        try:
            async with websockets.connect(self.endpoint):
                return True
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(0.25)
    logger.warning("app-server readiness failed: %s", last_error)
    return False
```

Keep `wait_ready()` as a thin synchronous wrapper only if existing tests need it; real startup should use the async method.

- [ ] **Step 4: Wire process manager into `main.py`**

In `main.py`, real mode should create:

```python
from wlcodex.app_server_process import AppServerProcess, AppServerProcessConfig

process = AppServerProcess(AppServerProcessConfig(
    binary=config.codex.binary,
    host=config.codex.app_server_host,
    port=config.codex.app_server_port,
    startup_timeout_seconds=config.backend.startup_timeout_seconds,
))
backend = AppServerCodexBackend(
    endpoint=process.endpoint,
    approval_policy=config.codex.approval_policy,
    sandbox=config.codex.sandbox,
    request_timeout_seconds=config.backend.request_timeout_seconds,
)
backend.set_process_manager(process)
```

Before Telegram polling, attempt backend readiness. If not ready, keep bot alive and let `/health` report the error.

- [ ] **Step 5: Make backend health reflect real websocket state**

In `AppServerCodexBackend.health()`, return:

- process alive from process manager when present
- `websocket_connected=True` only when `_websocket` is not `None` and not closed
- stored last startup error when readiness failed

- [ ] **Step 6: Run process and composition tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_app_server_process.py tests/test_main_composition.py -q
```

Expected:

```text
passed
```

- [ ] **Step 7: Commit**

```bash
git add wlcodex/app_server_process.py wlcodex/codex_backend.py wlcodex/main.py tests/test_app_server_process.py tests/test_main_composition.py
git commit -m "fix: manage real codex app-server lifecycle"
```

## Task 4: Repair Backend Protocol Calls And Event Mapping

**Files:**
- Modify: `wlcodex/codex_backend.py`
- Modify: `wlcodex/task_service.py`
- Modify: `tests/test_codex_backend_events.py`
- Modify: `tests/test_protocol_contracts.py`

- [ ] **Step 1: Add backend send-shape tests**

Add tests using an injected transport that captures outgoing JSON-RPC messages:

```python
@pytest.mark.asyncio
async def test_real_backend_turn_start_sends_input(monkeypatch) -> None:
    sent = []
    backend = AppServerCodexBackend(
        "ws://127.0.0.1:1",
        approval_policy="on-request",
        sandbox="workspace-write",
        request_timeout_seconds=1,
    )

    async def send_json(message: dict) -> None:
        sent.append(message)

    backend.set_transport(send_json, None)
    client = await backend._ensure_client()
    start = asyncio.create_task(backend.start_turn("thread-1", "hello"))
    await asyncio.sleep(0)
    await client.receive_message({"jsonrpc": "2.0", "id": 1, "result": {"turn": {"id": "turn-1"}}})
    assert await start == "turn-1"
    assert sent[0]["params"]["input"] == [{"type": "text", "text": "hello"}]
    assert "items" not in sent[0]["params"]
```

- [ ] **Step 2: Update backend methods**

Change `create_thread`, `start_turn`, `continue_turn`, and `steer_turn` to use helpers from Task 1.

Expected method bodies:

```python
async def create_thread(self, workspace_path: str) -> str:
    client = await self._ensure_client()
    result = await client.request(
        "thread/start",
        build_thread_start_params(
            workspace_path,
            self.approval_policy,
            self.sandbox,
        ),
    )
    return parse_thread_start_response(result)
```

`start_turn` and `continue_turn` must return `parse_turn_response(result)`.

- [ ] **Step 3: Update event handlers to normalize nested turn ids**

In `_on_turn_started` and `_on_turn_completed`, emit payloads that always include top-level `threadId` and `turnId`:

```python
thread_id, turn_id = parse_turn_notification_ids(params)
self._emit(BackendEvent("turn_started", {**params, "threadId": thread_id, "turnId": turn_id}))
```

Do the same for completion.

- [ ] **Step 4: Run backend and event tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_protocol_contracts.py tests/test_codex_backend_events.py -q
```

Expected:

```text
passed
```

- [ ] **Step 5: Commit**

```bash
git add wlcodex/codex_backend.py wlcodex/task_service.py tests/test_codex_backend_events.py tests/test_protocol_contracts.py
git commit -m "fix: align backend with real app-server protocol"
```

## Task 5: Repair Workspace Locks, Task Reservation, And Continue State

**Files:**
- Modify: `wlcodex/db.py`
- Modify: `wlcodex/task_service.py`
- Modify: `wlcodex/controller.py`
- Create: `tests/test_drift_repairs.py`

- [ ] **Step 1: Add drift repair tests**

Create `tests/test_drift_repairs.py`:

```python
from pathlib import Path

import pytest

from wlcodex.codex_backend import FakeCodexBackend
from wlcodex.config import WorkspaceConfig
from wlcodex.controller import CommandController
from wlcodex.db import Ledger
from wlcodex.inspection import TaskInspector
from wlcodex.models import TaskStatus
from wlcodex.task_service import TaskService, WorkspaceBusy


def make_service(tmp_path: Path, allow_write: bool = True) -> TaskService:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    return TaskService(ledger, (WorkspaceConfig("demo", tmp_path, allow_write),))


def test_start_task_rejects_read_only_workspace(tmp_path: Path) -> None:
    service = make_service(tmp_path, allow_write=False)
    with pytest.raises(PermissionError):
        service.reserve_task("demo", "write something", telegram_chat_id=123)


@pytest.mark.asyncio
async def test_controller_does_not_create_thread_when_workspace_busy(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (WorkspaceConfig("demo", tmp_path, True),))
    backend = FakeCodexBackend()
    ctrl = CommandController(service, backend, TaskInspector(ledger, tmp_path / "logs"))

    first = service.start_task("demo", "first", codex_thread_id="thread-1")
    ledger.set_task_status(first.id, TaskStatus.RUNNING)

    response = await ctrl.handle("/task demo second", {"chat_id": 123})

    assert "busy" in response.text.lower()
    assert backend.threads == {}


def test_continue_requires_workspace_available(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    original = service.start_task("demo", "original", codex_thread_id="thread-1")
    service._ledger.set_task_status(original.id, TaskStatus.DONE)
    active = service.start_task("demo", "active", codex_thread_id="thread-2")
    service._ledger.set_task_status(active.id, TaskStatus.RUNNING)

    with pytest.raises(WorkspaceBusy):
        service.continue_task(original.id, "continue")


def test_continue_moves_done_task_to_queued(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    task = service.start_task("demo", "original", codex_thread_id="thread-1")
    service._ledger.set_task_status(task.id, TaskStatus.DONE)

    updated = service.continue_task(task.id, "continue")

    assert updated.status == TaskStatus.QUEUED
```

- [ ] **Step 2: Run drift tests and confirm failures**

Run:

```bash
.venv/bin/python -m pytest tests/test_drift_repairs.py -q
```

Expected:

```text
FAILED
```

- [ ] **Step 3: Add DB methods for reservation and thread updates**

In `wlcodex/db.py`, add:

```python
def set_thread_id(self, task_id: int, codex_thread_id: str) -> None:
    self._conn.execute(
        "UPDATE tasks SET codex_thread_id = ?, updated_at = ? WHERE id = ?",
        (codex_thread_id, _now(), task_id),
    )
    self._conn.commit()
```

Add a guarded migration for every missing existing task column before row mappers read those columns.

Add a `TaskService.set_task_thread(task_id, thread_id)` wrapper:

```python
def set_task_thread(self, task_id: int, thread_id: str) -> Task:
    self._ledger.set_thread_id(task_id, thread_id)
    self._ledger.add_event(task_id, "thread_started", {"threadId": thread_id})
    return self._ledger.get_task(task_id)
```

- [ ] **Step 4: Split reservation from thread creation**

In `TaskService`, add:

```python
def ensure_workspace_writable(self, workspace_alias: str) -> WorkspaceConfig:
    workspace = self.get_workspace(workspace_alias)
    if not workspace.allow_write:
        raise PermissionError(f"workspace {workspace_alias} is read-only")
    return workspace


def reserve_task(
    self,
    workspace_alias: str,
    prompt: str,
    telegram_chat_id: int | None = None,
    parent_task_id: int | None = None,
) -> Task:
    workspace = self.ensure_workspace_writable(workspace_alias)
    self.ensure_workspace_available(workspace_alias)
    task = self._ledger.create_task(
        workspace_alias=workspace.alias,
        workspace_path=str(workspace.path),
        title=_title(prompt),
        codex_thread_id=None,
        parent_task_id=parent_task_id,
        telegram_chat_id=telegram_chat_id,
    )
    self._ledger.add_event(
        task.id,
        "task_reserved",
        {"prompt": prompt, "context_policy": "fresh_thread_by_default"},
    )
    return task
```

Keep `start_task()` for compatibility by calling `reserve_task()` then setting thread id when a thread id is passed.

- [ ] **Step 5: Reorder controller `/task`**

In `CommandController._handle_start()`:

1. Reserve the task first.
2. Create the app-server thread.
3. Persist thread id.
4. Start the turn.
5. If backend fails, call `fail_task()`.

The shape should be:

```python
task = self._service.reserve_task(command.workspace_alias, command.prompt, chat_id)
workspace = self._service.get_workspace(command.workspace_alias)
try:
    thread_id = await self._backend.create_thread(str(workspace.path))
    self._service.set_task_thread(task.id, thread_id)
    await self._backend.start_turn(thread_id, command.prompt)
except Exception as exc:
    task = self._service.fail_task(task.id, str(exc))
    return ControllerResponse(f"Task #{task.id} failed to start: {exc}\n\n{render_task_card(task)}")
```

- [ ] **Step 6: Repair `/continue` state**

In `TaskService.continue_task()`:

- Reject `queued`, `running`, `waiting_approval`, and `archived`.
- Enforce writable workspace and availability.
- Transition the task to `queued`.
- Clear active turn.
- Add `user_continue` event.

Return the updated task.

- [ ] **Step 7: Run task/controller tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_drift_repairs.py tests/test_task_service.py tests/test_controller_flow.py -q
```

Expected:

```text
passed
```

- [ ] **Step 8: Commit**

```bash
git add wlcodex/db.py wlcodex/task_service.py wlcodex/controller.py tests/test_drift_repairs.py
git commit -m "fix: close task reservation and workspace lock gaps"
```

## Task 6: Repair Approval Consistency

**Files:**
- Modify: `wlcodex/approval.py`
- Modify: `wlcodex/db.py`
- Modify: `wlcodex/task_service.py`
- Modify: `wlcodex/codex_backend.py`
- Modify: `tests/test_approval.py`

- [ ] **Step 1: Add approval consistency tests**

Append to `tests/test_approval.py`:

```python
from datetime import timedelta


class FailingBackend:
    async def resolve_approval(self, codex_request_id: str, response: dict) -> None:
        raise RuntimeError("backend down")


@pytest.mark.asyncio
async def test_backend_failure_keeps_approval_pending(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)
    approval = ledger.create_approval(
        task.id, "req-1", None, None, ApprovalKind.COMMAND, "Run ls"
    )
    svc = ApprovalService(callback_timeout_seconds=3600, allow_session_approval=True)
    cb = decode_approval_callback(encode_approval_callback(approval.id, "approve_once"))

    msg = await svc.resolve_callback(cb, FailingBackend(), ledger)

    assert "failed" in msg.lower()
    assert ledger.get_approval(approval.id).status.value == "pending"


@pytest.mark.asyncio
async def test_resolved_approval_decrements_pending_count(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)
    ledger.increment_pending_approvals(task.id, 1)
    approval = ledger.create_approval(
        task.id, "req-1", None, None, ApprovalKind.COMMAND, "Run ls"
    )
    backend = FakeCodexBackend()
    svc = ApprovalService(callback_timeout_seconds=3600, allow_session_approval=True)
    cb = decode_approval_callback(encode_approval_callback(approval.id, "approve_once"))

    await svc.resolve_callback(cb, backend, ledger)

    assert ledger.get_task(task.id).pending_approval_count == 0
```

- [ ] **Step 2: Run approval tests and confirm failures**

Run:

```bash
.venv/bin/python -m pytest tests/test_approval.py -q
```

Expected:

```text
FAILED
```

- [ ] **Step 3: Add ledger helpers**

In `db.py`, add:

```python
def decrement_pending_approvals(self, task_id: int) -> None:
    self._conn.execute(
        """
        UPDATE tasks
        SET pending_approval_count = CASE
            WHEN pending_approval_count > 0 THEN pending_approval_count - 1
            ELSE 0
        END,
        updated_at = ?
        WHERE id = ?
        """,
        (_now(), task_id),
    )
    self._conn.commit()
```

Add `set_approval_error(approval_id, error)` if approval delivery fails.

- [ ] **Step 4: Update `ApprovalService`**

Constructor:

```python
def __init__(
    self,
    callback_timeout_seconds: int = 3600,
    allow_session_approval: bool = True,
) -> None:
    self._callback_timeout = callback_timeout_seconds
    self._allow_session_approval = allow_session_approval
```

Resolution order:

1. Load row.
2. Check pending.
3. Check expiry from `created_at`.
4. Build schema response through `build_approval_response()`.
5. Send backend response.
6. Resolve local row.
7. Decrement pending count.
8. Move task back to running if it was waiting and no pending approvals remain.

- [ ] **Step 5: Change backend approval signature**

Change real and fake backends to:

```python
async def resolve_approval(self, codex_request_id: str, response: dict[str, object]) -> None:
    ...
```

The real backend calls:

```python
client.resolve_server_request(codex_request_id, response)
```

- [ ] **Step 6: Run approval and fake E2E tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_approval.py tests/test_e2e_fake_backend.py -q
```

Expected:

```text
passed
```

- [ ] **Step 7: Commit**

```bash
git add wlcodex/approval.py wlcodex/db.py wlcodex/task_service.py wlcodex/codex_backend.py tests/test_approval.py tests/test_e2e_fake_backend.py
git commit -m "fix: make approval resolution retryable and schema-correct"
```

## Task 7: Repair Local Monitoring And Session Command

**Files:**
- Modify: `wlcodex/task_service.py`
- Modify: `wlcodex/inspection.py`
- Modify: `wlcodex/router.py`
- Modify: `wlcodex/status.py`
- Modify: `wlcodex/telegram_app.py`
- Modify: `tests/test_inspection.py`
- Modify: `tests/test_router.py`
- Modify: `tests/test_telegram_handlers.py`

- [ ] **Step 1: Add monitoring tests**

In `tests/test_inspection.py`, add:

```python
def test_tail_falls_back_to_command_output_events(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", str(tmp_path), "Test", "thread-1", None)
    ledger.add_event(task.id, "command_output", {"delta": "line from event"})

    inspector = TaskInspector(ledger, tmp_path / "logs")
    result = inspector.tail(task.id)

    assert "line from event" in result.body


def test_diff_reads_stored_diff_event(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", str(tmp_path), "Test", "thread-1", None)
    ledger.add_event(task.id, "diff_updated", {"diff": "README.md | 1 +"})

    inspector = TaskInspector(ledger, tmp_path / "logs")
    result = inspector.diff(task.id, workspace_path=str(tmp_path))

    assert "README.md" in result.body
```

In `tests/test_router.py`, assert:

```python
def test_parse_sessions_command() -> None:
    from wlcodex.router import CodexSessionsCommand

    assert isinstance(parse_command("/sessions"), CodexSessionsCommand)
```

- [ ] **Step 2: Run monitoring tests and confirm failures**

Run:

```bash
.venv/bin/python -m pytest tests/test_inspection.py tests/test_router.py tests/test_telegram_handlers.py -q
```

Expected:

```text
FAILED
```

- [ ] **Step 3: Store diff and log events**

In `TaskService.apply_backend_event()`:

- For `diff_updated`, add a `diff_updated` event with `diff`, `summary`, or a compact payload from app-server.
- For `command_output_delta`, keep SQLite event and append bounded text to task log.
- For `agent_message_delta`, append bounded text to task log.

Add a private helper:

```python
def _append_task_log(self, task_id: int, text: str) -> None:
    if not text:
        return
    log_dir = self._task_log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / f"{task_id}.log").open("a", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")
```

Add `task_log_dir: Path | None = None` to `TaskService.__init__`. When it is `None`, skip file writes and keep SQLite event fallback. In `main.py`, pass `config.storage.task_log_dir` so production writes per-task logs.

- [ ] **Step 4: Add `/tail` event fallback**

In `TaskInspector.tail()`, when no log file exists, read latest events and render `command_output` and `agent_message_delta` deltas.

- [ ] **Step 5: Switch command surface to `/sessions`**

Change:

- `router.parse_command("/sessions")` returns `CodexSessionsCommand`.
- `status.render_help()` advertises `/sessions`.
- `telegram_app.build_application()` registers `CommandHandler("sessions", handlers.codex_sessions)`.

Keep `parse_command("/codex-sessions")` as an alias if desired, but do not advertise it.

- [ ] **Step 6: Run monitoring and handler tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_inspection.py tests/test_router.py tests/test_telegram_handlers.py -q
```

Expected:

```text
passed
```

- [ ] **Step 7: Commit**

```bash
git add wlcodex/task_service.py wlcodex/inspection.py wlcodex/router.py wlcodex/status.py wlcodex/telegram_app.py tests/test_inspection.py tests/test_router.py tests/test_telegram_handlers.py
git commit -m "fix: complete local monitoring and sessions command"
```

## Task 8: Harden SQLite Migrations For Existing Databases

**Files:**
- Modify: `wlcodex/db.py`
- Modify: `tests/test_db.py`

- [ ] **Step 1: Add legacy database migration test**

In `tests/test_db.py`, add:

```python
def test_migrate_upgrades_legacy_tasks_table(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_alias TEXT NOT NULL,
            workspace_path TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            codex_thread_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    ledger = Ledger.open(db_path)
    ledger.migrate()

    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)
    assert task.pending_approval_count == 0
    assert task.telegram_status_message_id is None
```

- [ ] **Step 2: Run DB tests and confirm failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_db.py -q
```

Expected:

```text
FAILED
```

- [ ] **Step 3: Add guarded migration helpers**

In `db.py`, add:

```python
def _table_columns(self, table: str) -> set[str]:
    rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def _add_column_if_missing(self, table: str, column: str, ddl: str) -> None:
    if column not in self._table_columns(table):
        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
```

After creating tables, call `_add_column_if_missing()` for every extended `tasks` and `approval_requests` column.

- [ ] **Step 4: Add schema version table**

Add:

```sql
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

Set `schema_version` to `"2"` after migration.

- [ ] **Step 5: Run DB tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_db.py -q
```

Expected:

```text
passed
```

- [ ] **Step 6: Commit**

```bash
git add wlcodex/db.py tests/test_db.py
git commit -m "fix: make sqlite migrations upgrade legacy ledgers"
```

## Task 9: Add Real App-Server Integration Test

**Files:**
- Create: `tests/test_real_app_server_integration.py`
- Modify: `tests/test_app_server_backend_integration.py`
- Modify: `docs/superpowers/reports/2026-05-16-wlcodex-real-app-server-smoke.md`

- [ ] **Step 1: Replace blocked integration test with real receive-loop test**

Create `tests/test_real_app_server_integration.py`:

```python
import os
from pathlib import Path

import pytest

from wlcodex.app_server_process import AppServerProcess, AppServerProcessConfig
from wlcodex.codex_backend import AppServerCodexBackend


pytestmark = pytest.mark.skipif(
    os.environ.get("WLCODEX_RUN_CODEX_INTEGRATION") != "1",
    reason="set WLCODEX_RUN_CODEX_INTEGRATION=1 to run real Codex app-server tests",
)


@pytest.mark.asyncio
async def test_real_app_server_thread_turn_and_events(tmp_path: Path) -> None:
    port = int(os.environ.get("WLCODEX_TEST_APP_SERVER_PORT", "17432"))
    process = AppServerProcess(AppServerProcessConfig(
        binary=os.environ.get("WLCODEX_CODEX_BINARY", "codex"),
        host="127.0.0.1",
        port=port,
        startup_timeout_seconds=20,
    ))
    process.start()
    try:
        assert await process.wait_ready_async()

        backend = AppServerCodexBackend(
            process.endpoint,
            approval_policy="on-request",
            sandbox="workspace-write",
            request_timeout_seconds=60,
        )
        backend.set_process_manager(process)

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "README.md").write_text("wlcodex integration\n", encoding="utf-8")

        thread_id = await backend.create_thread(str(workspace))
        assert thread_id

        turn_id = await backend.start_turn(
            thread_id,
            "Reply exactly with: wlcodex real integration ok",
        )
        assert turn_id

        seen = []
        async for event in backend.events():
            seen.append(event.event_type)
            if event.event_type == "turn_completed":
                break
            if len(seen) > 200:
                raise AssertionError(f"too many events without completion: {seen}")

        assert "turn_started" in seen
        assert "turn_completed" in seen
        await backend.close()
    finally:
        process.shutdown()
```

- [ ] **Step 2: Keep old integration filename as import/skip shim**

If `tests/test_app_server_backend_integration.py` remains, make it import the new test module or mark it as replaced. It must not contain the old manually injected WebSocket shortcut that bypasses `_recv_loop`.

- [ ] **Step 3: Run real app-server integration**

Run:

```bash
WLCODEX_RUN_CODEX_INTEGRATION=1 .venv/bin/python -m pytest tests/test_real_app_server_integration.py -q
```

Expected:

```text
passed
```

If Codex auth is missing, fix local Codex auth and rerun. Do not convert this to a fake test.

- [ ] **Step 4: Commit**

```bash
git add tests/test_real_app_server_integration.py tests/test_app_server_backend_integration.py
git commit -m "test: prove real codex app-server integration"
```

## Task 10: Add Live Telegram Smoke Gate

**Files:**
- Create: `tests/test_live_telegram_smoke.py`
- Modify: `README.md`
- Modify: `config/wlcodex.example.toml`

- [ ] **Step 1: Add operator-assisted live Telegram smoke test**

Create `tests/test_live_telegram_smoke.py`:

```python
import os
import sqlite3

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("WLCODEX_RUN_TELEGRAM_LIVE") != "1",
    reason="set WLCODEX_RUN_TELEGRAM_LIVE=1 to run live Telegram smoke",
)


def test_live_telegram_env_is_configured() -> None:
    required = [
        "WLCODEX_TELEGRAM_BOT_TOKEN",
        "WLCODEX_TELEGRAM_ALLOWED_USER_ID",
        "WLCODEX_TELEGRAM_CHAT_ID",
        "WLCODEX_LIVE_WORKSPACE_ALIAS",
        "WLCODEX_LIVE_SQLITE_PATH",
        "WLCODEX_LIVE_SMOKE_TASK_ID",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    assert not missing, f"missing live Telegram env vars: {missing}"


def test_live_telegram_task_has_real_ledger_evidence() -> None:
    db_path = os.environ["WLCODEX_LIVE_SQLITE_PATH"]
    task_id = int(os.environ["WLCODEX_LIVE_SMOKE_TASK_ID"])
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert task is not None, f"task #{task_id} not found"
    assert task["telegram_chat_id"] == int(os.environ["WLCODEX_TELEGRAM_CHAT_ID"])
    assert task["telegram_status_message_id"] is not None
    assert task["codex_thread_id"], "task has no real Codex thread id"
    assert task["status"] in {"done", "running", "waiting_approval", "failed", "aborted"}

    events = conn.execute(
        "SELECT event_type FROM task_events WHERE task_id = ? ORDER BY id ASC",
        (task_id,),
    ).fetchall()
    event_types = {row["event_type"] for row in events}
    assert "task_reserved" in event_types or "task_created" in event_types
    assert "turn_started" in event_types


def test_live_telegram_approval_evidence_when_required() -> None:
    if os.environ.get("WLCODEX_LIVE_APPROVAL_REQUIRED") != "1":
        pytest.skip("set WLCODEX_LIVE_APPROVAL_REQUIRED=1 after running approval smoke")

    db_path = os.environ["WLCODEX_LIVE_SQLITE_PATH"]
    task_id = int(os.environ["WLCODEX_LIVE_SMOKE_TASK_ID"])
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT status, resolution
        FROM approval_requests
        WHERE task_id = ?
        ORDER BY id ASC
        """,
        (task_id,),
    ).fetchall()
    assert rows, f"task #{task_id} has no approval rows"
    assert any(row["status"] in {"approved", "denied", "cancelled"} for row in rows)
```

This test verifies evidence produced by a real human-to-bot Telegram command. It does not try to fake a Telegram user through Bot API.

- [ ] **Step 2: Document exact operator flow**

In `README.md`, add this live smoke procedure:

```bash
export WLCODEX_TELEGRAM_BOT_TOKEN=...
export WLCODEX_TELEGRAM_ALLOWED_USER_ID=...
export WLCODEX_TELEGRAM_CHAT_ID=...
export WLCODEX_LIVE_WORKSPACE_ALIAS=...
export WLCODEX_LIVE_SQLITE_PATH=/absolute/path/to/wlcodex.sqlite3
```

Start WLCodex with the same config. From the authorized private Telegram chat, send:

```text
/health
/task <workspace> Reply exactly with: wlcodex telegram live ok
/tasks
/sessions
```

After the status card shows the created task id, export:

```bash
export WLCODEX_RUN_TELEGRAM_LIVE=1
export WLCODEX_LIVE_SMOKE_TASK_ID=<task_id>
.venv/bin/python -m pytest tests/test_live_telegram_smoke.py -q
```

- [ ] **Step 3: Add live approval smoke**

Use a harmless prompt intended to trigger a real app-server approval request under the configured policy:

```text
/task <workspace> Create file wlcodex_approval_probe.txt with text approval-ok. If permission is requested, wait for my Telegram approval.
```

The test or manual operator must:

1. Observe the Telegram approval card.
2. Click Approve once.
3. Observe task status returning to running or done.
4. Verify the file exists or the task recorded the denied/cancelled outcome.

Then set:

```bash
export WLCODEX_LIVE_APPROVAL_REQUIRED=1
export WLCODEX_LIVE_SMOKE_TASK_ID=<approval_task_id>
WLCODEX_RUN_TELEGRAM_LIVE=1 .venv/bin/python -m pytest tests/test_live_telegram_smoke.py -q
```

If app-server does not request approval for this action under the configured policy, adjust the policy or prompt until a real approval card appears. Do not replace this with fake backend evidence.

- [ ] **Step 4: Document live smoke env**

In `README.md` and `config/wlcodex.example.toml`, document:

```bash
export WLCODEX_RUN_TELEGRAM_LIVE=1
export WLCODEX_TELEGRAM_BOT_TOKEN=...
export WLCODEX_TELEGRAM_ALLOWED_USER_ID=...
export WLCODEX_TELEGRAM_CHAT_ID=...
export WLCODEX_LIVE_WORKSPACE_ALIAS=...
export WLCODEX_LIVE_SQLITE_PATH=...
export WLCODEX_LIVE_SMOKE_TASK_ID=...
```

- [ ] **Step 5: Run live smoke**

Run:

```bash
WLCODEX_RUN_TELEGRAM_LIVE=1 .venv/bin/python -m pytest tests/test_live_telegram_smoke.py -q
```

Expected:

```text
passed
```

- [ ] **Step 6: Commit**

```bash
git add tests/test_live_telegram_smoke.py README.md config/wlcodex.example.toml
git commit -m "test: add real telegram smoke gate"
```

## Task 11: Update Final Smoke Report With Real Evidence

**Files:**
- Modify: `docs/superpowers/reports/2026-05-16-wlcodex-real-app-server-smoke.md`

- [ ] **Step 1: Replace blocked conclusion**

Rewrite the report with these sections:

```markdown
# WLCodex Final Real Closure Smoke Report

## Environment

- Date:
- Codex CLI version:
- OS:
- Python:
- App-server endpoint:
- Telegram bot mode:

## Commands Run

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
WLCODEX_RUN_CODEX_INTEGRATION=1 .venv/bin/python -m pytest tests/test_real_app_server_integration.py -q
WLCODEX_RUN_TELEGRAM_LIVE=1 .venv/bin/python -m pytest tests/test_live_telegram_smoke.py -q
```

## Real App-Server Evidence

- Thread id:
- Turn id:
- Observed events:
- Completion evidence:

## Real Telegram Evidence

- `/health` response:
- `/task` response:
- Approval card message id:
- Approval action:
- `/events` response:
- `/sessions` response:

## Conclusion

The real app-server path and real Telegram path passed. Fake backend tests were not used as smoke evidence.
```

Close the fenced code block correctly in the actual file.

- [ ] **Step 2: Fill report from real command output**

Run the real acceptance commands and paste only concise evidence. Do not paste secrets or bot tokens.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/reports/2026-05-16-wlcodex-real-app-server-smoke.md
git commit -m "docs: record final real closure smoke evidence"
```

## Task 12: Full Regression And Real Acceptance

**Files:**
- All touched implementation and test files.

- [ ] **Step 1: Run full unit suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected:

```text
passed
```

- [ ] **Step 2: Run lint**

Run:

```bash
.venv/bin/python -m ruff check .
```

Expected:

```text
All checks passed!
```

- [ ] **Step 3: Run real app-server integration**

Run:

```bash
WLCODEX_RUN_CODEX_INTEGRATION=1 .venv/bin/python -m pytest tests/test_real_app_server_integration.py -q
```

Expected:

```text
passed
```

- [ ] **Step 4: Run live Telegram smoke**

Run:

```bash
WLCODEX_RUN_TELEGRAM_LIVE=1 .venv/bin/python -m pytest tests/test_live_telegram_smoke.py -q
```

Expected:

```text
passed
```

- [ ] **Step 5: Search for fake smoke claims**

Run:

```bash
rg -n "fake.*smoke|smoke.*fake|complete in code|not a production code issue|currently hangs|BLOCKED" docs README.md tests wlcodex
```

Expected:

```text
No output, or only historical text that is explicitly marked superseded by the final real closure report.
```

- [ ] **Step 6: Commit final verification updates**

```bash
git add .
git commit -m "chore: verify final real closure"
```

## Self-Review

- Spec coverage: protocol drift, app-server lifecycle, task isolation, approval consistency, monitoring, migration, command drift, real app-server smoke, and live Telegram smoke are covered.
- Placeholder scan: this plan uses concrete files, commands, and expected outputs. No acceptance step permits fake smoke.
- Type consistency: task ids remain integers; Codex thread and turn ids remain strings; approval backend responses are dicts keyed by schema-specific fields; `/sessions` is the canonical Telegram command.
- Acceptance boundary: fake backend tests remain useful for unit feedback but cannot satisfy the final smoke gate.
