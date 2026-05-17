# WLCodex Interaction Layer Natural Streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a swappable Telegram interaction layer and make the default `natural` profile render plain-text Codex/Claude conversations with typing plus same-run streaming, while preserving legacy cockpit/task behavior.

**Architecture:** Add a focused `wlcodex/interaction/` package that consumes presentation events and wraps the existing Telegram send/edit/typing functions. Keep TaskService, Ledger, controller routing, approvals, Codex app-server events, and Claude execution as the runtime. Wire the new layer in small steps because `send_telegram` is high risk and `EventBridge.process_event` is medium risk.

**Tech Stack:** Python 3.12, python-telegram-bot, existing `StreamingRenderer`, existing `BackendEvent`, SQLite-backed Ledger, pytest, GitNexus impact checks before symbol edits.

---

## File Structure

Create:

- `wlcodex/interaction/__init__.py` - public interaction package exports.
- `wlcodex/interaction/events.py` - `InteractionEvent` dataclass and event type literals.
- `wlcodex/interaction/transport.py` - thin wrapper around existing Telegram send/edit/typing callbacks.
- `wlcodex/interaction/buttons.py` - deterministic button builders for natural profile.
- `wlcodex/interaction/errors.py` - user-facing error classification.
- `wlcodex/interaction/profiles.py` - `InteractionProfile`, `NaturalChatProfile`, `LegacyProfile`, and profile factory.
- `wlcodex/interaction/renderer.py` - event-driven renderer session manager using `StreamingRenderer`.
- `tests/test_interaction_events.py`
- `tests/test_interaction_transport.py`
- `tests/test_interaction_profiles.py`
- `tests/test_interaction_renderer.py`

Modify:

- `wlcodex/config.py` - add interaction config.
- `config/wlcodex.example.toml` - document interaction profile settings.
- `wlcodex/telegram_app.py` - construct interaction transport/renderer, remove natural-profile textual ACK path, keep legacy fallback.
- `wlcodex/event_bridge.py` - optionally forward `agent_message_delta` and terminal events to interaction renderer without changing status-card throttling behavior.
- `wlcodex/controller.py` - humanize greeting and natural-profile startup response text.
- `wlcodex/menu.py` - reduce natural-profile menu to common commands when config chooses natural.
- `wlcodex/status.py` - shorten natural help text while preserving legacy diagnostics.
- `README.md` - describe natural streaming and token behavior.

Do not modify:

- `wlcodex/jsonrpc.py`
- `wlcodex/task_service.py`
- `wlcodex/codex_backend.py`
- `wlcodex/claude_backend.py`

## Pre-Implementation Safety Gate

- [ ] **Step 1: Confirm current dirty state**

Run:

```bash
git status --short --untracked-files=all
```

Expected now:

```text
No unexpected source-code changes.
 M README.md
?? docs/superpowers/plans/2026-05-17-wlcodex-interaction-layer-natural-streaming-implementation-plan.md
?? docs/superpowers/specs/2026-05-17-wlcodex-interaction-layer-natural-streaming-design.md
```

If the spec/plan have already been committed, only unrelated pre-existing
changes may remain. If additional source files are dirty, inspect them before
editing and preserve user changes.

- [ ] **Step 2: Run GitNexus impact checks before symbol edits**

Run before changing these symbols:

```python
impact(target="send_telegram", file_path="wlcodex/telegram_app.py", direction="upstream", repo="wlcodex", includeTests=True)
impact(target="process_event", file_path="wlcodex/event_bridge.py", direction="upstream", repo="wlcodex", includeTests=True)
impact(target="handle_conversation_text", file_path="wlcodex/controller.py", direction="upstream", repo="wlcodex", includeTests=True)
impact(target="conversation_text", file_path="wlcodex/telegram_app.py", direction="upstream", repo="wlcodex", includeTests=True)
```

Expected risk note:

```text
send_telegram: HIGH
process_event: MEDIUM
handle_conversation_text: LOW
conversation_text: LOW
```

Proceed by wrapping high-risk functions rather than rewriting them.

## Task 1: Add Interaction Events

**Files:**

- Create: `wlcodex/interaction/__init__.py`
- Create: `wlcodex/interaction/events.py`
- Test: `tests/test_interaction_events.py`

- [ ] **Step 1: Write failing tests**

Add `tests/test_interaction_events.py`:

```python
from wlcodex.interaction.events import InteractionEvent


def test_interaction_event_defaults_are_empty_and_safe() -> None:
    event = InteractionEvent(event_type="text_delta", chat_id=123, text="hello")

    assert event.event_type == "text_delta"
    assert event.chat_id == 123
    assert event.conversation_id is None
    assert event.task_id is None
    assert event.thread_id == ""
    assert event.text == "hello"
    assert event.buttons == []
    assert event.metadata == {}


def test_interaction_event_buttons_and_metadata_are_not_shared() -> None:
    first = InteractionEvent(event_type="run_completed", chat_id=1)
    second = InteractionEvent(event_type="run_completed", chat_id=1)

    first.buttons.append([{"text": "状态", "callback_data": "x"}])
    first.metadata["task"] = 7

    assert second.buttons == []
    assert second.metadata == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_interaction_events.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'wlcodex.interaction'
```

- [ ] **Step 3: Implement event dataclass**

Create `wlcodex/interaction/__init__.py`:

```python
"""Telegram interaction layer.

This package renders runtime events into user-facing Telegram behavior.
It must not build model prompts or feed UI text back into Codex/Claude.
"""
```

Create `wlcodex/interaction/events.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

InteractionEventType = Literal[
    "run_started",
    "text_delta",
    "tool_activity",
    "approval_requested",
    "run_completed",
    "run_failed",
    "status_refresh",
]


@dataclass
class InteractionEvent:
    event_type: InteractionEventType
    chat_id: int
    conversation_id: int | None = None
    task_id: int | None = None
    thread_id: str = ""
    text: str = ""
    summary: str = ""
    buttons: list[list[dict[str, str]]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_interaction_events.py -q
```

Expected:

```text
2 passed
```

- [ ] **Step 5: Commit**

Run:

```bash
git add wlcodex/interaction/__init__.py wlcodex/interaction/events.py tests/test_interaction_events.py
git commit -m "feat: add interaction event model"
```

## Task 2: Add Telegram Transport Wrapper

**Files:**

- Create: `wlcodex/interaction/transport.py`
- Test: `tests/test_interaction_transport.py`

- [ ] **Step 1: Write failing tests**

Add `tests/test_interaction_transport.py`:

```python
import pytest

from wlcodex.interaction.transport import TelegramTransport


@pytest.mark.asyncio
async def test_transport_delegates_send_edit_and_typing() -> None:
    calls: list[tuple[str, object]] = []

    async def send(chat_id, text, buttons=None):
        calls.append(("send", (chat_id, text, buttons)))
        return 99

    async def edit(chat_id, message_id, text, buttons=None):
        calls.append(("edit", (chat_id, message_id, text, buttons)))

    async def typing(chat_id):
        calls.append(("typing", chat_id))
        return "typing-task"

    transport = TelegramTransport(send, edit, typing)

    message_id = await transport.send(1, "hello", [[{"text": "状态", "callback_data": "s"}]])
    await transport.edit(1, message_id, "updated")
    typing_task = await transport.typing(1)

    assert message_id == 99
    assert typing_task == "typing-task"
    assert calls[0][0] == "send"
    assert calls[1][0] == "edit"
    assert calls[2] == ("typing", 1)


@pytest.mark.asyncio
async def test_transport_callback_answer_is_optional() -> None:
    async def send(chat_id, text, buttons=None):
        return 1

    async def edit(chat_id, message_id, text, buttons=None):
        return None

    async def typing(chat_id):
        return None

    transport = TelegramTransport(send, edit, typing)

    await transport.answer_callback("ignored")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_interaction_transport.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'wlcodex.interaction.transport'
```

- [ ] **Step 3: Implement transport**

Create `wlcodex/interaction/transport.py`:

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

Buttons = list[list[dict[str, str]]] | None
SendFn = Callable[[int, str, Buttons], Awaitable[int]]
EditFn = Callable[[int, int, str, Buttons], Awaitable[None]]
TypingFn = Callable[[int], Awaitable[object]]
AnswerCallbackFn = Callable[[str], Awaitable[None]]


class TelegramTransport:
    def __init__(
        self,
        send_fn: SendFn,
        edit_fn: EditFn,
        typing_fn: TypingFn,
        answer_callback_fn: AnswerCallbackFn | None = None,
    ) -> None:
        self._send = send_fn
        self._edit = edit_fn
        self._typing = typing_fn
        self._answer_callback = answer_callback_fn

    async def send(self, chat_id: int, text: str, buttons: Buttons = None) -> int:
        return await self._send(chat_id, text, buttons)

    async def edit(
        self, chat_id: int, message_id: int, text: str, buttons: Buttons = None
    ) -> None:
        await self._edit(chat_id, message_id, text, buttons)

    async def typing(self, chat_id: int) -> object:
        return await self._typing(chat_id)

    async def answer_callback(self, text: str) -> None:
        if self._answer_callback is not None:
            await self._answer_callback(text)
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_interaction_transport.py -q
```

Expected:

```text
2 passed
```

- [ ] **Step 5: Commit**

Run:

```bash
git add wlcodex/interaction/transport.py tests/test_interaction_transport.py
git commit -m "feat: wrap telegram transport callbacks"
```

## Task 3: Add Natural Buttons and Error Text

**Files:**

- Create: `wlcodex/interaction/buttons.py`
- Create: `wlcodex/interaction/errors.py`
- Test: `tests/test_interaction_profiles.py`

- [ ] **Step 1: Write failing tests**

Add the first section of `tests/test_interaction_profiles.py`:

```python
from wlcodex.interaction.buttons import natural_completion_buttons
from wlcodex.interaction.errors import classify_user_error


def test_natural_completion_buttons_are_small_and_deterministic() -> None:
    buttons = natural_completion_buttons(
        conversation_id=7,
        has_diff=True,
        include_new=True,
    )

    labels = [button["text"] for row in buttons for button in row]

    assert labels == ["继续", "查看 diff", "状态", "新对话"]
    assert all("callback_data" in button for row in buttons for button in row)


def test_natural_completion_buttons_hide_diff_when_none() -> None:
    buttons = natural_completion_buttons(
        conversation_id=7,
        has_diff=False,
        include_new=True,
    )

    labels = [button["text"] for row in buttons for button in row]

    assert "查看 diff" not in labels
    assert labels == ["继续", "状态", "新对话"]


def test_classify_user_error_hides_internal_exception_details() -> None:
    text = classify_user_error("Codex 启动失败：ConnectionError('token expired')")

    assert "ConnectionError" not in text
    assert "认证" in text or "登录" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_interaction_profiles.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'wlcodex.interaction.buttons'
```

- [ ] **Step 3: Implement button builder**

Create `wlcodex/interaction/buttons.py`:

```python
from __future__ import annotations

from wlcodex.conversation_callback import (
    CONTINUE,
    DIFF,
    NEW_CONVO,
    encode_conversation_callback,
)


def natural_completion_buttons(
    *,
    conversation_id: int,
    has_diff: bool = False,
    include_new: bool = True,
) -> list[list[dict[str, str]]]:
    row = [
        {
            "text": "继续",
            "callback_data": encode_conversation_callback(conversation_id, CONTINUE),
        },
    ]
    if has_diff:
        row.append(
            {
                "text": "查看 diff",
                "callback_data": encode_conversation_callback(conversation_id, DIFF),
            }
        )
    row.append(
        {
            "text": "状态",
            "callback_data": encode_conversation_callback(conversation_id, CONTINUE),
        }
    )
    if include_new:
        row.append(
            {
                "text": "新对话",
                "callback_data": encode_conversation_callback(conversation_id, NEW_CONVO),
            }
        )
    return [row]
```

- [ ] **Step 4: Implement error classifier**

Create `wlcodex/interaction/errors.py`:

```python
from __future__ import annotations


def classify_user_error(error: object) -> str:
    raw = str(error)
    lowered = raw.lower()

    if "401" in lowered or "unauthorized" in lowered or "token" in lowered:
        return "认证看起来失效了，需要先检查登录状态。"
    if "429" in lowered or "rate limit" in lowered or "too many requests" in lowered:
        return "现在被限流了，等一会儿再继续会更稳。"
    if "context" in lowered and ("long" in lowered or "length" in lowered):
        return "上下文太长了。我建议开新对话，或者先让我压缩范围。"
    if "codex" in lowered and ("启动失败" in raw or "failed" in lowered):
        return "Codex 没启动起来。我保留了这次请求，可以稍后重试。"
    if "network" in lowered or "timed out" in lowered or "timeout" in lowered:
        return "网络这下没接稳，我会尽量保留当前状态。"
    return "这次运行失败了。详细错误已写入日志，可以用状态或诊断命令查看。"
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
pytest tests/test_interaction_profiles.py -q
```

Expected:

```text
3 passed
```

- [ ] **Step 6: Commit**

Run:

```bash
git add wlcodex/interaction/buttons.py wlcodex/interaction/errors.py tests/test_interaction_profiles.py
git commit -m "feat: add natural interaction copy helpers"
```

## Task 4: Add Interaction Profiles

**Files:**

- Modify: `wlcodex/interaction/profiles.py`
- Modify: `tests/test_interaction_profiles.py`

- [ ] **Step 1: Extend failing profile tests**

Append to `tests/test_interaction_profiles.py`:

```python
from wlcodex.interaction.events import InteractionEvent
from wlcodex.interaction.profiles import (
    LegacyProfile,
    NaturalChatProfile,
    profile_from_name,
)


def test_profile_factory_accepts_known_profiles() -> None:
    assert isinstance(profile_from_name("natural"), NaturalChatProfile)
    assert isinstance(profile_from_name("legacy"), LegacyProfile)
    assert isinstance(profile_from_name("cockpit"), LegacyProfile)


def test_natural_profile_hides_started_message() -> None:
    profile = NaturalChatProfile()
    event = InteractionEvent(event_type="run_started", chat_id=1)

    assert profile.started_text(event) == ""


def test_legacy_profile_keeps_started_message() -> None:
    profile = LegacyProfile()
    event = InteractionEvent(event_type="run_started", chat_id=1, summary="正在处理")

    assert "正在处理" in profile.started_text(event)


def test_natural_profile_short_greeting() -> None:
    profile = NaturalChatProfile()

    assert profile.greeting_text() == "你好！直接说需要我看什么就行。"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_interaction_profiles.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'wlcodex.interaction.profiles'
```

- [ ] **Step 3: Implement profiles**

Create `wlcodex/interaction/profiles.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from wlcodex.interaction.buttons import natural_completion_buttons
from wlcodex.interaction.events import InteractionEvent
from wlcodex.interaction.errors import classify_user_error


@dataclass
class InteractionProfile:
    name: str

    def started_text(self, event: InteractionEvent) -> str:
        return event.summary

    def greeting_text(self) -> str:
        return "你好，我在。"

    def error_text(self, error: object) -> str:
        return classify_user_error(error)

    def completion_buttons(
        self, *, conversation_id: int | None, has_diff: bool = False
    ) -> list[list[dict[str, str]]]:
        return []


class NaturalChatProfile(InteractionProfile):
    def __init__(self) -> None:
        super().__init__(name="natural")

    def started_text(self, event: InteractionEvent) -> str:
        return ""

    def greeting_text(self) -> str:
        return "你好！直接说需要我看什么就行。"

    def completion_buttons(
        self, *, conversation_id: int | None, has_diff: bool = False
    ) -> list[list[dict[str, str]]]:
        if conversation_id is None:
            return []
        return natural_completion_buttons(
            conversation_id=conversation_id,
            has_diff=has_diff,
            include_new=True,
        )


class LegacyProfile(InteractionProfile):
    def __init__(self) -> None:
        super().__init__(name="legacy")

    def started_text(self, event: InteractionEvent) -> str:
        return event.summary or "正在处理你的消息，请稍候..."


def profile_from_name(name: str) -> InteractionProfile:
    normalized = name.strip().lower()
    if normalized == "natural":
        return NaturalChatProfile()
    if normalized in {"legacy", "cockpit"}:
        return LegacyProfile()
    raise ValueError(f"Unknown interaction profile: {name}")
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_interaction_profiles.py -q
```

Expected:

```text
7 passed
```

- [ ] **Step 5: Commit**

Run:

```bash
git add wlcodex/interaction/profiles.py tests/test_interaction_profiles.py
git commit -m "feat: add interaction profiles"
```

## Task 5: Add Event-Driven Interaction Renderer

**Files:**

- Create: `wlcodex/interaction/renderer.py`
- Test: `tests/test_interaction_renderer.py`

- [ ] **Step 1: Write failing renderer tests**

Add `tests/test_interaction_renderer.py`:

```python
import pytest

from wlcodex.interaction.events import InteractionEvent
from wlcodex.interaction.profiles import NaturalChatProfile
from wlcodex.interaction.renderer import InteractionRenderer
from wlcodex.interaction.transport import TelegramTransport


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.edited: list[tuple[int, int, str, object]] = []
        self.typing_count = 0

    async def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text, buttons))
        return len(self.sent)

    async def edit(self, chat_id, message_id, text, buttons=None):
        self.edited.append((chat_id, message_id, text, buttons))

    async def typing(self, chat_id):
        self.typing_count += 1
        return None


@pytest.mark.asyncio
async def test_natural_renderer_uses_typing_not_ack_on_started() -> None:
    fake = FakeTransport()
    renderer = InteractionRenderer(
        transport=TelegramTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
    )

    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1))

    assert fake.typing_count == 1
    assert fake.sent == []


@pytest.mark.asyncio
async def test_natural_renderer_streams_delta_into_single_message() -> None:
    fake = FakeTransport()
    renderer = InteractionRenderer(
        transport=TelegramTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
    )

    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, task_id=10, text="hel"))
    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, task_id=10, text="lo"))

    assert fake.sent == [(1, "hel", None)]
    assert fake.edited[-1][2] == "hello"


@pytest.mark.asyncio
async def test_natural_renderer_flushes_buttons_on_completion() -> None:
    fake = FakeTransport()
    renderer = InteractionRenderer(
        transport=TelegramTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
    )

    await renderer.handle(
        InteractionEvent(
            event_type="text_delta",
            chat_id=1,
            conversation_id=7,
            task_id=10,
            text="done",
        )
    )
    await renderer.handle(
        InteractionEvent(
            event_type="run_completed",
            chat_id=1,
            conversation_id=7,
            task_id=10,
            metadata={"has_diff": True},
        )
    )

    assert fake.edited[-1][3] is not None
    labels = [button["text"] for row in fake.edited[-1][3] for button in row]
    assert "查看 diff" in labels
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_interaction_renderer.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'wlcodex.interaction.renderer'
```

- [ ] **Step 3: Implement renderer**

Create `wlcodex/interaction/renderer.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from wlcodex.interaction.events import InteractionEvent
from wlcodex.interaction.profiles import InteractionProfile
from wlcodex.interaction.transport import TelegramTransport
from wlcodex.streaming import StreamingRenderer


@dataclass
class _StreamSession:
    renderer: StreamingRenderer
    conversation_id: int | None = None


class InteractionRenderer:
    def __init__(
        self,
        *,
        transport: TelegramTransport,
        profile: InteractionProfile,
        min_interval_seconds: float = 1.0,
    ) -> None:
        self._transport = transport
        self._profile = profile
        self._min_interval = min_interval_seconds
        self._sessions: dict[tuple[int, int], _StreamSession] = {}

    async def handle(self, event: InteractionEvent) -> None:
        if event.event_type == "run_started":
            await self._handle_started(event)
            return
        if event.event_type == "text_delta":
            await self._handle_text_delta(event)
            return
        if event.event_type == "run_completed":
            await self._handle_completed(event)
            return
        if event.event_type == "run_failed":
            await self._handle_failed(event)

    async def _handle_started(self, event: InteractionEvent) -> None:
        await self._transport.typing(event.chat_id)
        text = self._profile.started_text(event)
        if text:
            await self._transport.send(event.chat_id, text)

    async def _handle_text_delta(self, event: InteractionEvent) -> None:
        if not event.text:
            return
        key = self._key(event)
        session = self._sessions.get(key)
        if session is None:
            renderer = StreamingRenderer(
                self._transport.send,
                self._transport.edit,
                min_interval_seconds=self._min_interval,
            )
            await renderer.start(event.chat_id)
            session = _StreamSession(
                renderer=renderer,
                conversation_id=event.conversation_id,
            )
            self._sessions[key] = session
        if event.conversation_id is not None:
            session.conversation_id = event.conversation_id
        await session.renderer.append(event.text)

    async def _handle_completed(self, event: InteractionEvent) -> None:
        key = self._key(event)
        session = self._sessions.get(key)
        if session is None:
            return
        conversation_id = event.conversation_id or session.conversation_id
        buttons = self._profile.completion_buttons(
            conversation_id=conversation_id,
            has_diff=bool(event.metadata.get("has_diff", False)),
        )
        await session.renderer.finish(buttons=buttons)
        self._sessions.pop(key, None)

    async def _handle_failed(self, event: InteractionEvent) -> None:
        key = self._key(event)
        session = self._sessions.get(key)
        text = self._profile.error_text(event.text or event.summary)
        if session is None:
            await self._transport.send(event.chat_id, text)
            return
        await session.renderer.append("\n\n" + text)
        await session.renderer.finish()
        self._sessions.pop(key, None)

    def _key(self, event: InteractionEvent) -> tuple[int, int]:
        task_key = event.task_id if event.task_id is not None else 0
        return (event.chat_id, task_key)
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_interaction_renderer.py -q
```

Expected:

```text
3 passed
```

- [ ] **Step 5: Commit**

Run:

```bash
git add wlcodex/interaction/renderer.py tests/test_interaction_renderer.py
git commit -m "feat: add interaction renderer"
```

## Task 6: Add Interaction Config

**Files:**

- Modify: `wlcodex/config.py`
- Modify: `config/wlcodex.example.toml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing config tests**

Add to `tests/test_config.py`:

```python
def test_load_config_includes_default_interaction_section(tmp_path):
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]
private_chat_only = true

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[[workspaces]]
alias = "demo"
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.interaction.profile == "natural"
    assert config.interaction.streaming_enabled is True
    assert config.interaction.show_footer is False
    assert config.interaction.edit_min_interval_seconds == 1.0


def test_load_config_accepts_cockpit_interaction_profile(tmp_path):
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]
private_chat_only = true

[interaction]
profile = "cockpit"
streaming_enabled = false
show_footer = true
edit_min_interval_seconds = 2.5

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[[workspaces]]
alias = "demo"
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.interaction.profile == "cockpit"
    assert config.interaction.streaming_enabled is False
    assert config.interaction.show_footer is True
    assert config.interaction.edit_min_interval_seconds == 2.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_config.py -q
```

Expected:

```text
AttributeError: 'Config' object has no attribute 'interaction'
```

- [ ] **Step 3: Add config dataclass and parser**

In `wlcodex/config.py`, add:

```python
@dataclass(frozen=True)
class InteractionConfig:
    profile: str = "natural"
    streaming_enabled: bool = True
    show_footer: bool = False
    edit_min_interval_seconds: float = 1.0
```

Add `interaction: InteractionConfig` to the root config dataclass.

In `load_config`, parse:

```python
interaction_raw = data.get("interaction", {})
interaction = InteractionConfig(
    profile=str(interaction_raw.get("profile", "natural")),
    streaming_enabled=bool(interaction_raw.get("streaming_enabled", True)),
    show_footer=bool(interaction_raw.get("show_footer", False)),
    edit_min_interval_seconds=float(
        interaction_raw.get("edit_min_interval_seconds", 1.0)
    ),
)
if interaction.profile not in {"natural", "legacy", "cockpit"}:
    raise ConfigError(
        "interaction.profile must be one of: natural, legacy, cockpit"
    )
```

Pass `interaction=interaction` when constructing the root config.

- [ ] **Step 4: Document example config**

Add to `config/wlcodex.example.toml`:

```toml
[interaction]
# natural = quiet chat + same-run streaming
# legacy = existing task/status-card style
# cockpit = reserved for richer remote-control UI; currently falls back to legacy
profile = "natural"
streaming_enabled = true
show_footer = false
edit_min_interval_seconds = 1.0
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
pytest tests/test_config.py -q
```

Expected:

```text
all tests passed
```

- [ ] **Step 6: Commit**

Run:

```bash
git add wlcodex/config.py config/wlcodex.example.toml tests/test_config.py
git commit -m "feat: add interaction config"
```

## Task 7: Wire Renderer Into Telegram Handlers Without Rewriting send_telegram

**Files:**

- Modify: `wlcodex/telegram_app.py`
- Test: `tests/test_telegram_conversation_handlers.py`
- Test: `tests/test_telegram_handlers.py`

- [ ] **Step 1: Add failing handler construction test**

Append to `tests/test_telegram_conversation_handlers.py`:

```python
def test_handlers_expose_interaction_renderer_factory() -> None:
    from wlcodex.telegram_app import WlCodexHandlers

    assert hasattr(WlCodexHandlers, "create_interaction_renderer")
    assert callable(WlCodexHandlers.create_interaction_renderer)
```

- [ ] **Step 2: Add failing natural no-ACK test**

Add to `tests/test_telegram_handlers.py` using the existing fake style:

```python
@pytest.mark.asyncio
async def test_conversation_text_natural_profile_uses_typing_without_ack() -> None:
    from wlcodex.telegram_app import WlCodexHandlers
    from wlcodex.controller import ControllerResponse

    class Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.actions: list[tuple[int, str]] = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs["text"])
            return SimpleNamespace(message_id=len(self.sent))

        async def send_chat_action(self, **kwargs):
            self.actions.append((kwargs["chat_id"], str(kwargs["action"])))

    class Controller:
        async def handle_conversation_text(self, text, ctx):
            return ControllerResponse("自然回复")

    message = SimpleNamespace(text="帮我看下 bug")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_message=message,
    )
    config = SimpleNamespace(
        telegram=SimpleNamespace(allowed_user_ids=frozenset({123}), private_chat_only=True),
        interaction=SimpleNamespace(profile="natural", streaming_enabled=True, edit_min_interval_seconds=0.0),
    )
    bot = Bot()
    handlers = WlCodexHandlers(
        config=config,
        controller=Controller(),
        ledger=SimpleNamespace(),
        approval_service=object(),
        bot=bot,
    )

    await handlers.conversation_text(update, SimpleNamespace())

    assert "正在处理你的消息，请稍候..." not in bot.sent
    assert bot.sent == ["自然回复"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
pytest tests/test_telegram_conversation_handlers.py::test_handlers_expose_interaction_renderer_factory tests/test_telegram_handlers.py::test_conversation_text_natural_profile_uses_typing_without_ack -q
```

Expected:

```text
FAILED ... create_interaction_renderer
FAILED ... "正在处理你的消息，请稍候..." not in bot.sent
```

- [ ] **Step 4: Add renderer factory**

In `wlcodex/telegram_app.py`, import:

```python
from wlcodex.interaction.profiles import profile_from_name
from wlcodex.interaction.renderer import InteractionRenderer
from wlcodex.interaction.transport import TelegramTransport
```

Add method to `WlCodexHandlers`:

```python
    def create_interaction_renderer(self) -> InteractionRenderer:
        interaction = getattr(self._config, "interaction", None)
        profile_name = getattr(interaction, "profile", "legacy")
        min_interval = float(
            getattr(interaction, "edit_min_interval_seconds", 1.0)
        )
        transport = TelegramTransport(
            self.send_telegram,
            self.edit_telegram,
            self._start_typing,
        )
        return InteractionRenderer(
            transport=transport,
            profile=profile_from_name(profile_name),
            min_interval_seconds=min_interval,
        )
```

- [ ] **Step 5: Change only natural conversation ACK path**

In `conversation_text`, replace the unconditional ACK with profile branching:

```python
        interaction = getattr(self._config, "interaction", None)
        profile_name = getattr(interaction, "profile", "legacy")

        if profile_name == "natural":
            typing_task = await self._start_typing(chat_id)
            try:
                response = await self._controller.handle_conversation_text(
                    text, _ctx(update)
                )
            finally:
                typing_task.cancel()
            await self.send_telegram(chat_id, response.text, response.buttons)
            return

        ack_msg_id = await self.send_telegram(
            chat_id, "正在处理你的消息，请稍候..."
        )
```

This step deliberately keeps legacy behavior for non-natural profiles.

- [ ] **Step 6: Run targeted Telegram tests**

Run:

```bash
pytest tests/test_telegram_conversation_handlers.py tests/test_telegram_handlers.py -q
```

Expected:

```text
all tests passed
```

- [ ] **Step 7: Commit**

Run:

```bash
git add wlcodex/telegram_app.py tests/test_telegram_conversation_handlers.py tests/test_telegram_handlers.py
git commit -m "feat: route natural chat through interaction layer"
```

## Task 8: Humanize Controller Greeting and Startup Copy

**Files:**

- Modify: `wlcodex/controller.py`
- Test: `tests/test_conversation_router.py`
- Test: `tests/test_telegram_conversation_handlers.py`

- [ ] **Step 1: Add failing greeting test**

Add to a controller-focused test file that already constructs a
`CommandController` with a fake ledger, or create a small fake in
`tests/test_conversation_router.py`:

```python
@pytest.mark.asyncio
async def test_lightweight_greeting_is_short_and_hides_metadata(controller):
    response = await controller.handle_conversation_text(
        "你好",
        {"chat_id": 123, "user_id": 456},
    )

    assert response.text == "你好！直接说需要我看什么就行。"
    assert "工作区" not in response.text
    assert "当前对话" not in response.text
    assert "模式" not in response.text
```

If the repository does not have a reusable `controller` fixture, define one with
`FakeCodexBackend`, `Ledger.open(tmp_path / "db.sqlite3")`, `TaskService`, and
`TaskInspector`, matching existing controller tests.

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_conversation_router.py::test_lightweight_greeting_is_short_and_hides_metadata -q
```

Expected:

```text
FAILED ... assert response.text == "你好！直接说需要我看什么就行。"
```

- [ ] **Step 3: Update greeting copy**

In `CommandController.handle_conversation_text`, change the greeting return to:

```python
            return ControllerResponse("你好！直接说需要我看什么就行。")
```

- [ ] **Step 4: Update Codex-direct startup copy**

For direct Codex startup responses, replace metadata-heavy copy with:

```python
        return ControllerResponse(
            "我先看一下。完成后会把结论发在这里。",
            buttons=buttons,
        )
```

For startup failures, use the classifier from `wlcodex.interaction.errors`:

```python
from wlcodex.interaction.errors import classify_user_error
```

and:

```python
            return ControllerResponse(classify_user_error(exc))
```

- [ ] **Step 5: Run controller-related tests**

Run:

```bash
pytest tests/test_conversation_router.py tests/test_conversation_state.py -q
```

Expected:

```text
all tests passed
```

- [ ] **Step 6: Commit**

Run:

```bash
git add wlcodex/controller.py tests/test_conversation_router.py
git commit -m "feat: humanize natural conversation copy"
```

## Task 9: Forward Same-Run Codex Deltas From EventBridge

**Files:**

- Modify: `wlcodex/event_bridge.py`
- Modify: `wlcodex/main.py`
- Test: `tests/test_event_bridge.py`

- [ ] **Step 1: Add failing EventBridge streaming callback test**

Append to `tests/test_event_bridge.py`:

```python
@pytest.mark.asyncio
async def test_agent_message_delta_forwards_to_interaction_renderer(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Run probe",
        codex_thread_id="thread-1",
        telegram_chat_id=123,
    )
    received = []

    class Interaction:
        async def handle(self, event):
            received.append(event)

    bridge = EventBridge(
        task_service=service,
        backend=IdleBackend(),
        ledger=ledger,
        send_telegram=_send_telegram,
        edit_telegram=_edit_telegram,
        approval_service=ApprovalSpy(),
        interaction_renderer=Interaction(),
    )

    await bridge.process_event(
        BackendEvent(
            "agent_message_delta",
            {"threadId": "thread-1", "delta": "hello"},
        )
    )

    assert received
    assert received[0].event_type == "text_delta"
    assert received[0].chat_id == 123
    assert received[0].task_id == task.id
    assert received[0].text == "hello"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_event_bridge.py::test_agent_message_delta_forwards_to_interaction_renderer -q
```

Expected:

```text
TypeError: EventBridge.__init__() got an unexpected keyword argument 'interaction_renderer'
```

- [ ] **Step 3: Add optional renderer dependency**

In `EventBridge.__init__`, add:

```python
        interaction_renderer: object | None = None,
```

and assign:

```python
        self._interaction_renderer = interaction_renderer
```

Existing callers continue passing no renderer.

- [ ] **Step 4: Forward agent message deltas before status-card skip**

In `process_event`, after task lookup and after `apply_backend_event`, add:

```python
        if event.event_type == "agent_message_delta":
            await self._forward_agent_delta(event)
```

Add method:

```python
    async def _forward_agent_delta(self, event: BackendEvent) -> None:
        if self._interaction_renderer is None:
            return
        thread_id = str(event.payload.get("threadId", ""))
        task = self._service._find_by_thread(thread_id)
        if task is None or task.telegram_chat_id is None:
            return
        delta = str(event.payload.get("delta", ""))
        if not delta:
            return
        from wlcodex.interaction.events import InteractionEvent

        await self._interaction_renderer.handle(
            InteractionEvent(
                event_type="text_delta",
                chat_id=task.telegram_chat_id,
                task_id=task.id,
                thread_id=thread_id,
                text=delta,
            )
        )
```

Keep the existing status-card skip list unchanged so approval/status noise does
not return.

- [ ] **Step 5: Forward terminal states**

Add a helper:

```python
    async def _forward_terminal_event(self, task) -> None:
        if self._interaction_renderer is None:
            return
        if task.telegram_chat_id is None:
            return
        from wlcodex.interaction.events import InteractionEvent

        event_type = "run_completed" if task.status == TaskStatus.DONE else "run_failed"
        await self._interaction_renderer.handle(
            InteractionEvent(
                event_type=event_type,
                chat_id=task.telegram_chat_id,
                task_id=task.id,
                thread_id=task.codex_thread_id or "",
                text=task.last_error or "",
                metadata={"has_diff": bool(task.changed_file_count)},
            )
        )
```

Call it in the existing terminal-state branch after `task_after` is found:

```python
                await self._forward_terminal_event(task_after)
```

- [ ] **Step 6: Wire renderer in main**

Where `EventBridge` is constructed in `wlcodex/main.py`, pass the handler
renderer when interaction config exists:

```python
interaction_renderer = handlers.create_interaction_renderer()
```

and:

```python
interaction_renderer=interaction_renderer,
```

- [ ] **Step 7: Run EventBridge tests**

Run:

```bash
pytest tests/test_event_bridge.py -q
```

Expected:

```text
all tests passed
```

- [ ] **Step 8: Commit**

Run:

```bash
git add wlcodex/event_bridge.py wlcodex/main.py tests/test_event_bridge.py
git commit -m "feat: forward codex deltas to interaction renderer"
```

## Task 10: Menu and Help Naturalization

**Files:**

- Modify: `wlcodex/menu.py`
- Modify: `wlcodex/status.py`
- Test: `tests/test_telegram_conversation_handlers.py`
- Test: `tests/test_status.py`

- [ ] **Step 1: Add failing compact menu test**

Update or add in `tests/test_telegram_conversation_handlers.py`:

```python
def test_natural_bot_commands_are_compact() -> None:
    from wlcodex.menu import build_bot_commands

    commands = build_bot_commands(profile="natural")
    names = [cmd[0] for cmd in commands]

    assert names == ["new", "stop", "status", "model", "diff", "help"]


def test_legacy_bot_commands_keep_operator_routes() -> None:
    from wlcodex.menu import build_bot_commands

    commands = build_bot_commands(profile="legacy")
    names = [cmd[0] for cmd in commands]

    assert "codex" in names
    assert "claude" in names
    assert "auto" in names
    assert "task" not in names
```

- [ ] **Step 2: Add failing compact help test**

Add to `tests/test_status.py`:

```python
def test_render_conversation_help_is_compact_for_natural_profile() -> None:
    from wlcodex.status import render_conversation_help

    text = render_conversation_help(profile="natural")

    assert "直接发消息" in text
    assert "/task" not in text
    assert len(text.splitlines()) <= 8
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
pytest tests/test_telegram_conversation_handlers.py::test_natural_bot_commands_are_compact tests/test_status.py::test_render_conversation_help_is_compact_for_natural_profile -q
```

Expected:

```text
TypeError: build_bot_commands() got an unexpected keyword argument 'profile'
TypeError: render_conversation_help() got an unexpected keyword argument 'profile'
```

- [ ] **Step 4: Update menu builder**

In `wlcodex/menu.py`, add compact natural commands:

```python
_NATURAL_COMMANDS: list[tuple[str, str]] = [
    ("new", "新对话"),
    ("stop", "停止"),
    ("status", "状态"),
    ("model", "模型"),
    ("diff", "查看 diff"),
    ("help", "帮助"),
]
```

Change the builder:

```python
def build_bot_commands(profile: str = "natural") -> list[tuple[str, str]]:
    if profile == "natural":
        return list(_NATURAL_COMMANDS)
    return list(_PRIMARY_COMMANDS)
```

Update `main.py` BotCommands registration to call:

```python
build_bot_commands(getattr(config.interaction, "profile", "natural"))
```

- [ ] **Step 5: Update help renderer**

In `wlcodex/status.py`, change `render_conversation_help` to accept a profile:

```python
def render_conversation_help(profile: str = "natural") -> str:
    if profile == "natural":
        return "\n".join(
            [
                "WLCodex",
                "",
                "直接发消息就能继续当前对话。",
                "/new 新对话",
                "/status 看状态",
                "/diff 看变更",
                "/model 切模型",
                "/help 帮助",
            ]
        )
    return render_help()
```

Keep existing `render_help()` for legacy/operator mode.

- [ ] **Step 6: Run menu/help tests**

Run:

```bash
pytest tests/test_telegram_conversation_handlers.py tests/test_status.py -q
```

Expected:

```text
all tests passed
```

- [ ] **Step 7: Commit**

Run:

```bash
git add wlcodex/menu.py wlcodex/main.py wlcodex/status.py tests/test_telegram_conversation_handlers.py tests/test_status.py
git commit -m "feat: compact natural telegram menu"
```

## Task 11: Documentation and Regression Verification

**Files:**

- Modify: `README.md`
- Test: full relevant pytest selection

- [ ] **Step 1: Update README interaction section**

In `README.md`, document:

```markdown
## Interaction profiles

WLCodex separates runtime orchestration from Telegram presentation.

```toml
[interaction]
profile = "natural"
streaming_enabled = true
show_footer = false
edit_min_interval_seconds = 1.0
```

`natural` is the default chat surface: plain text starts or continues a
conversation, Telegram shows typing while work starts, and model deltas stream
into one edited message. This does not double model tokens because it forwards
deltas from the same Codex/Claude run.

`legacy` preserves task-card style behavior for operator workflows.

`cockpit` is reserved for a future richer remote-control profile.
```

- [ ] **Step 2: Run focused tests**

Run:

```bash
pytest \
  tests/test_interaction_events.py \
  tests/test_interaction_transport.py \
  tests/test_interaction_profiles.py \
  tests/test_interaction_renderer.py \
  tests/test_telegram_conversation_handlers.py \
  tests/test_telegram_handlers.py \
  tests/test_event_bridge.py \
  tests/test_config.py \
  tests/test_status.py \
  -q
```

Expected:

```text
all tests passed
```

- [ ] **Step 3: Run full test suite**

Run:

```bash
pytest -q
```

Expected:

```text
all tests passed
```

- [ ] **Step 4: Run GitNexus detect changes before final commit**

Run:

```python
detect_changes(repo="wlcodex", scope="all")
```

Expected:

```text
Changed symbols are limited to interaction layer, Telegram presentation wiring,
EventBridge presentation forwarding, config, menu/help, and docs.
```

- [ ] **Step 5: Commit**

Run:

```bash
git add README.md
git commit -m "docs: document interaction profiles"
```

## Implementation Notes

- Streaming must forward deltas from the same existing run. It must not call
  `send_codex_prompt`, `create_thread`, `start_turn`, Claude `send`, or Claude
  `send_streaming` a second time for rendering.
- `agent_message_delta` forwarding is presentation-only. Do not append rendered
  Telegram text to prompt packets or conversation summaries.
- Keep `approval_requested` cards separate from natural streaming. Approval is
  an explicit user decision, not a chat decoration.
- If a Telegram edit fails because a message no longer exists, rely on
  `StreamingRenderer` fallback and existing `send_telegram` resilience.
- If `cockpit` is selected before a dedicated cockpit profile exists, route it
  to `LegacyProfile` so config migration is stable.
