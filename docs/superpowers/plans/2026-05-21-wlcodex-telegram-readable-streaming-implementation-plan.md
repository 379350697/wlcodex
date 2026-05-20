# WLCodex Telegram Readable Streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace token-fragment Telegram output with a complete two-channel renderer: one editable status bubble plus semantic body/final delivery.

**Architecture:** Add a Telegram output rendering layer keyed by active run. It uses an outbox-aware preview send path to obtain real Telegram message ids, a pure semantic chunker for readable text boundaries, and separate product/terminal delivery policies.

**Tech Stack:** Python 3.12, pytest, python-telegram-bot, existing `RuntimeEventStore`, existing `TelegramOutbox`, existing `InteractionRenderer`.

---

## Scope Lock

Implement exactly this:

1. running status / preview bubble;
2. semantic body chunking;
3. final organized answer;
4. product cockpit vs terminal onsite policy split;
5. compatibility with busy append / interrupt / queue / new-session.

Do not modify:

- agent routing;
- `/codex`, `/claude`, `/auto` semantics;
- workspace lease rules;
- approval flows;
- model prompts;
- orchestration strategy.

## File Map

- Create: `wlcodex/telegram_output.py`
  - `SemanticChunker`
  - `ChunkPolicy`
  - `OutputSurface`
  - `OutputRunKey`
  - `TelegramOutputSession`
  - `TelegramOutputManager`
- Modify: `wlcodex/telegram_outbox.py`
  - add waitable delivery result support for preview sends.
- Modify: `wlcodex/telegram_app.py`
  - add `send_telegram_preview`;
  - pass surface resolver and preview sender into interaction renderer.
- Modify: `wlcodex/interaction/renderer.py`
  - delegate text/runtime/final events to `TelegramOutputManager`.
- Modify: `wlcodex/interaction/runtime_renderer.py`
  - prevent duplicate status-bubble ownership; share status templates or delegate to output manager.
- Modify: `wlcodex/config.py`
  - add `TelegramOutputConfig`.
- Modify: `config/wlcodex.toml` and `config/wlcodex.example.toml`
  - add `[telegram_output]` defaults.
- Test: `tests/test_telegram_output_chunker.py`
- Test: `tests/test_telegram_output_manager.py`
- Test: `tests/test_telegram_outbox.py`
- Test: `tests/test_interaction_renderer.py`
- Test: `tests/test_surface_commands.py`
- Test: `tests/test_workbench_telegram_routing.py`
- Test: `tests/test_live_telegram_smoke.py`

## Pre-Implementation Safety

- [ ] **Step 1: Run GitNexus impact for symbols that will change**

Run:

```bash
rtk npx gitnexus analyze
```

Then run impact checks:

```text
gitnexus_impact(repo="wlcodex", target="StreamingRenderer", file_path="wlcodex/streaming.py", kind="Class", direction="upstream")
gitnexus_impact(repo="wlcodex", target="InteractionRenderer", file_path="wlcodex/interaction/renderer.py", kind="Class", direction="upstream")
gitnexus_impact(repo="wlcodex", target="TelegramOutbox", file_path="wlcodex/telegram_outbox.py", kind="Class", direction="upstream")
gitnexus_impact(repo="wlcodex", target="RuntimeProgressManager", file_path="wlcodex/interaction/runtime_renderer.py", kind="Class", direction="upstream")
```

Expected:

- risk is reviewed before edits;
- HIGH or CRITICAL risk is reported before implementation continues.

## Task 1: Add Telegram Output Config

**Files:**
- Modify: `wlcodex/config.py`
- Modify: `config/wlcodex.toml`
- Modify: `config/wlcodex.example.toml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing config test**

Add to `tests/test_config.py`:

```python
def test_telegram_output_config_defaults(tmp_path):
    from wlcodex.config import load_config

    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token = "dummy"
allowed_user_ids = [123]

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[[workspaces]]
alias = "wlcodex"
path = "."
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.telegram_output.preview_enabled is True
    assert config.telegram_output.product_body_mode == "final"
    assert config.telegram_output.terminal_body_mode == "semantic_blocks"
    assert config.telegram_output.semantic_min_chars == 900
    assert config.telegram_output.semantic_max_chars == 3200
    assert config.telegram_output.final_chunk_chars == 3900
```

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
rtk pytest tests/test_config.py::test_telegram_output_config_defaults -q
```

Expected:

```text
FAILED ... AttributeError: 'AppConfig' object has no attribute 'telegram_output'
```

- [ ] **Step 3: Add config dataclass and loader**

In `wlcodex/config.py`, add:

```python
@dataclass(frozen=True)
class TelegramOutputConfig:
    preview_enabled: bool = True
    preview_edit_min_interval_seconds: float = 2.0
    preview_send_timeout_seconds: float = 5.0
    product_body_mode: str = "final"
    terminal_body_mode: str = "semantic_blocks"
    semantic_min_chars: int = 900
    semantic_max_chars: int = 3200
    final_chunk_chars: int = 3900
    terminal_block_idle_seconds: float = 2.0
```

Add a field to `AppConfig`:

```python
telegram_output: TelegramOutputConfig = TelegramOutputConfig()
```

In `load_config`, read:

```python
telegram_output_raw = data.get("telegram_output", {})
```

Add helper:

```python
def _telegram_output_config(data: dict[str, object]) -> TelegramOutputConfig:
    product_mode = str(data.get("product_body_mode", "final"))
    terminal_mode = str(data.get("terminal_body_mode", "semantic_blocks"))
    allowed = {"final", "semantic_blocks"}
    if product_mode not in allowed:
        raise ValueError("telegram_output.product_body_mode must be final or semantic_blocks")
    if terminal_mode not in allowed:
        raise ValueError("telegram_output.terminal_body_mode must be final or semantic_blocks")
    return TelegramOutputConfig(
        preview_enabled=bool(data.get("preview_enabled", True)),
        preview_edit_min_interval_seconds=float(
            data.get("preview_edit_min_interval_seconds", 2.0)
        ),
        preview_send_timeout_seconds=float(
            data.get("preview_send_timeout_seconds", 5.0)
        ),
        product_body_mode=product_mode,
        terminal_body_mode=terminal_mode,
        semantic_min_chars=int(data.get("semantic_min_chars", 900)),
        semantic_max_chars=int(data.get("semantic_max_chars", 3200)),
        final_chunk_chars=int(data.get("final_chunk_chars", 3900)),
        terminal_block_idle_seconds=float(
            data.get("terminal_block_idle_seconds", 2.0)
        ),
    )
```

Wire it into `AppConfig(...)`:

```python
telegram_output=_telegram_output_config(telegram_output_raw),
```

- [ ] **Step 4: Add TOML defaults**

Add to both `config/wlcodex.toml` and `config/wlcodex.example.toml`:

```toml
[telegram_output]
preview_enabled = true
preview_edit_min_interval_seconds = 2.0
preview_send_timeout_seconds = 5.0
product_body_mode = "final"
terminal_body_mode = "semantic_blocks"
semantic_min_chars = 900
semantic_max_chars = 3200
final_chunk_chars = 3900
terminal_block_idle_seconds = 2.0
```

- [ ] **Step 5: Run config tests**

Run:

```bash
rtk pytest tests/test_config.py -q
```

Expected:

```text
passed
```

- [ ] **Step 6: Commit**

```bash
rtk git add wlcodex/config.py config/wlcodex.toml config/wlcodex.example.toml tests/test_config.py
rtk git commit -m "feat: configure telegram readable output"
```

## Task 2: Make Outbox Preview Sends Waitable

**Files:**
- Modify: `wlcodex/telegram_outbox.py`
- Test: `tests/test_telegram_outbox.py`

- [ ] **Step 1: Write failing waitable send test**

Add to `tests/test_telegram_outbox.py`:

```python
def test_outbox_send_wait_returns_real_message_id(tmp_path: Path) -> None:
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.telegram_outbox import TelegramOutbox

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    outbox = TelegramOutbox(store=store)

    async def fake_send(chat_id, text, buttons=None):
        return 1234

    async def scenario():
        waiter = asyncio.create_task(
            outbox.enqueue_send_wait(
                chat_id=1,
                text="preview",
                send_fn=fake_send,
                timeout_seconds=2.0,
            )
        )
        await outbox.process_all()
        return await waiter

    assert _run(scenario()) == 1234
```

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
rtk pytest tests/test_telegram_outbox.py::test_outbox_send_wait_returns_real_message_id -q
```

Expected:

```text
FAILED ... AttributeError: 'TelegramOutbox' object has no attribute 'enqueue_send_wait'
```

- [ ] **Step 3: Implement waitable delivery**

In `TelegramOutbox.__init__`, add:

```python
self._waiters: dict[str, asyncio.Future[int]] = {}
```

Add method:

```python
async def enqueue_send_wait(
    self,
    chat_id: int,
    text: str,
    buttons: list[list[dict[str, str]]] | None = None,
    *,
    send_fn: Any = None,
    edit_fn: Any = None,
    correlation_id: str = "",
    timeout_seconds: float = 5.0,
) -> int:
    delivery_id = self.enqueue_send(
        chat_id,
        text,
        buttons,
        send_fn=send_fn,
        edit_fn=edit_fn,
        correlation_id=correlation_id,
    )
    loop = asyncio.get_running_loop()
    future: asyncio.Future[int] = loop.create_future()
    self._waiters[delivery_id] = future
    try:
        return await asyncio.wait_for(future, timeout=timeout_seconds)
    finally:
        self._waiters.pop(delivery_id, None)
```

In `_deliver`, after successful send event:

```python
waiter = self._waiters.get(req.delivery_id)
if waiter is not None and not waiter.done():
    waiter.set_result(req.result_message_id)
```

On permanent failure:

```python
waiter = self._waiters.get(req.delivery_id)
if waiter is not None and not waiter.done():
    waiter.set_result(-1)
```

- [ ] **Step 4: Add timeout test**

Add:

```python
def test_outbox_send_wait_times_out_without_processor(tmp_path: Path) -> None:
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.telegram_outbox import TelegramOutbox

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    outbox = TelegramOutbox(store=store)

    async def fake_send(chat_id, text, buttons=None):
        return 1234

    async def scenario():
        try:
            await outbox.enqueue_send_wait(
                chat_id=1,
                text="preview",
                send_fn=fake_send,
                timeout_seconds=0.01,
            )
        except asyncio.TimeoutError:
            return "timeout"
        return "no-timeout"

    assert _run(scenario()) == "timeout"
```

- [ ] **Step 5: Run outbox tests**

Run:

```bash
rtk pytest tests/test_telegram_outbox.py -q
```

Expected:

```text
passed
```

- [ ] **Step 6: Commit**

```bash
rtk git add wlcodex/telegram_outbox.py tests/test_telegram_outbox.py
rtk git commit -m "feat: resolve telegram preview message ids"
```

## Task 3: Add Semantic Chunker

**Files:**
- Create: `wlcodex/telegram_output.py`
- Test: `tests/test_telegram_output_chunker.py`

- [ ] **Step 1: Write failing paragraph/list/link/code tests**

Create `tests/test_telegram_output_chunker.py`:

```python
from wlcodex.telegram_output import ChunkPolicy, SemanticChunker


def test_chunker_prefers_paragraph_boundary():
    chunker = SemanticChunker(ChunkPolicy(min_chars=20, max_chars=60))
    chunker.append("第一段很长很长。\n\n第二段也很长很长。")

    chunks = chunker.ready_chunks(force=False)

    assert chunks == ["第一段很长很长。"]
    assert chunker.buffer == "第二段也很长很长。"


def test_chunker_does_not_split_markdown_link_when_avoidable():
    chunker = SemanticChunker(ChunkPolicy(min_chars=20, max_chars=80))
    text = "来源：[上海黄金交易所](https://www.sge.com.cn/sjzx/yshqbg)\n\n下一段。"
    chunker.append(text)

    chunks = chunker.ready_chunks(force=False)

    assert chunks == ["来源：[上海黄金交易所](https://www.sge.com.cn/sjzx/yshqbg)"]
    assert ".cn/)" not in chunks


def test_chunker_keeps_list_item_readable():
    chunker = SemanticChunker(ChunkPolicy(min_chars=20, max_chars=80))
    chunker.append("- 国内金价：986 元/克\n- 周大福首饰金：1396 元/克\n- 回收价：971 元/克")

    chunks = chunker.ready_chunks(force=False)

    assert chunks[0] == "- 国内金价：986 元/克"
    assert chunker.buffer.startswith("- 周大福首饰金")


def test_chunker_does_not_split_inside_code_fence_when_avoidable():
    chunker = SemanticChunker(ChunkPolicy(min_chars=20, max_chars=120))
    chunker.append("说明：\n\n```bash\npytest tests/test_streaming.py -q\n```\n\n结束。")

    chunks = chunker.ready_chunks(force=False)

    assert chunks == ["说明：\n\n```bash\npytest tests/test_streaming.py -q\n```"]
    assert chunker.buffer == "结束。"


def test_chunker_flushes_final_chunks_with_part_numbers():
    chunker = SemanticChunker(ChunkPolicy(min_chars=10, max_chars=30, final_max_chars=40))
    chunker.append("第一句很长很长。第二句很长很长。第三句很长很长。")

    chunks = chunker.final_chunks(number_parts=True)

    assert len(chunks) >= 2
    assert chunks[0].startswith("1/")
    assert chunks[-1].startswith(f"{len(chunks)}/")
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
rtk pytest tests/test_telegram_output_chunker.py -q
```

Expected:

```text
FAILED ... ModuleNotFoundError: No module named 'wlcodex.telegram_output'
```

- [ ] **Step 3: Implement chunker**

Create `wlcodex/telegram_output.py` with at least:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OutputSurface(str, Enum):
    PRODUCT = "product"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class ChunkPolicy:
    min_chars: int = 900
    max_chars: int = 3200
    final_max_chars: int = 3900


class SemanticChunker:
    def __init__(self, policy: ChunkPolicy | None = None) -> None:
        self.policy = policy or ChunkPolicy()
        self.buffer = ""

    def append(self, text: str) -> None:
        if text:
            self.buffer += text

    def ready_chunks(self, *, force: bool = False) -> list[str]:
        chunks: list[str] = []
        while self.buffer:
            limit = self.policy.max_chars
            if not force and len(self.buffer) < self.policy.min_chars:
                break
            if len(self.buffer) <= limit:
                if force:
                    chunks.append(self.buffer)
                    self.buffer = ""
                break
            split_at = _find_split(self.buffer, limit)
            chunk = self.buffer[:split_at].rstrip()
            if not chunk:
                chunk = self.buffer[:limit]
                split_at = limit
            chunks.append(chunk)
            self.buffer = self.buffer[split_at:].lstrip()
        return chunks

    def final_chunks(self, *, number_parts: bool = False) -> list[str]:
        old_max = self.policy.max_chars
        self.policy = ChunkPolicy(
            min_chars=1,
            max_chars=self.policy.final_max_chars,
            final_max_chars=self.policy.final_max_chars,
        )
        chunks = self.ready_chunks(force=True)
        self.policy = ChunkPolicy(
            min_chars=self.policy.min_chars,
            max_chars=old_max,
            final_max_chars=self.policy.final_max_chars,
        )
        if number_parts and len(chunks) > 1:
            total = len(chunks)
            return [f"{idx}/{total}\n{chunk}" for idx, chunk in enumerate(chunks, 1)]
        return chunks
```

Implement helpers in the same file:

```python
def _find_split(text: str, limit: int) -> int:
    safe_limit = _safe_limit(text, limit)
    candidates = [
        text.rfind("\n\n", 0, safe_limit),
        _rfind_list_boundary(text, safe_limit),
        _rfind_sentence_boundary(text, safe_limit),
        text.rfind(" ", 0, safe_limit),
    ]
    for pos in candidates:
        if pos > 0 and _is_safe_split(text, pos):
            return pos
    return safe_limit
```

The helper implementation must scan Markdown link spans and fenced-code spans and reject split positions inside those spans.

- [ ] **Step 4: Run chunker tests**

Run:

```bash
rtk pytest tests/test_telegram_output_chunker.py -q
```

Expected:

```text
passed
```

- [ ] **Step 5: Commit**

```bash
rtk git add wlcodex/telegram_output.py tests/test_telegram_output_chunker.py
rtk git commit -m "feat: chunk telegram output semantically"
```

## Task 4: Add Output Session Manager

**Files:**
- Modify: `wlcodex/telegram_output.py`
- Test: `tests/test_telegram_output_manager.py`

- [ ] **Step 1: Write failing product final-only test**

Create `tests/test_telegram_output_manager.py`:

```python
from types import SimpleNamespace

import pytest

from wlcodex.telegram_output import OutputRunKey, OutputSurface, TelegramOutputManager


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_preview(self, chat_id, text):
        self.sent.append(("preview", chat_id, text, None))
        return 100

    async def edit_preview(self, chat_id, message_id, text, buttons=None):
        self.edited.append((chat_id, message_id, text, buttons))

    async def send_body(self, chat_id, text, buttons=None):
        self.sent.append(("body", chat_id, text, buttons))
        return len(self.sent)


@pytest.mark.asyncio
async def test_product_buffers_body_until_completion():
    transport = FakeTransport()
    manager = TelegramOutputManager(
        transport=transport,
        semantic_min_chars=20,
        semantic_max_chars=80,
        final_chunk_chars=200,
    )
    key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-10")

    await manager.start(key, surface=OutputSurface.PRODUCT, text="Codex 正在处理")
    await manager.append_text(key, "第一段。")
    await manager.append_text(key, "第二段。")

    assert [item for item in transport.sent if item[0] == "body"] == []

    await manager.complete(key, buttons=[[{"text": "继续", "callback_data": "continue:7"}]])

    bodies = [item for item in transport.sent if item[0] == "body"]
    assert len(bodies) == 1
    assert bodies[0][2] == "第一段。第二段。"
    assert bodies[0][3] is not None
    assert key not in manager.sessions
```

- [ ] **Step 2: Write failing terminal semantic-block test**

Add:

```python
@pytest.mark.asyncio
async def test_terminal_emits_semantic_blocks_while_running():
    transport = FakeTransport()
    manager = TelegramOutputManager(
        transport=transport,
        semantic_min_chars=10,
        semantic_max_chars=30,
        final_chunk_chars=200,
    )
    key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-10")

    await manager.start(key, surface=OutputSurface.TERMINAL, text="Codex 正在处理")
    await manager.append_text(key, "第一段很长。\n\n第二段继续。")

    bodies = [item for item in transport.sent if item[0] == "body"]
    assert bodies == [("body", 1, "第一段很长。", None)]
```

- [ ] **Step 3: Write failing separate-run-key test**

Add:

```python
@pytest.mark.asyncio
async def test_output_sessions_do_not_mix_runs():
    transport = FakeTransport()
    manager = TelegramOutputManager(transport=transport)
    old_key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-old")
    new_key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-new")

    await manager.start(old_key, surface=OutputSurface.PRODUCT, text="旧任务")
    await manager.append_text(old_key, "旧输出")
    await manager.start(new_key, surface=OutputSurface.PRODUCT, text="新任务")
    await manager.append_text(new_key, "新输出")
    await manager.complete(new_key)
    await manager.complete(old_key)

    body_texts = [item[2] for item in transport.sent if item[0] == "body"]
    assert "新输出" in body_texts
    assert "旧输出" in body_texts
    assert "旧输出新输出" not in body_texts
```

- [ ] **Step 4: Run manager tests and verify RED**

Run:

```bash
rtk pytest tests/test_telegram_output_manager.py -q
```

Expected:

```text
FAILED ... ImportError or AttributeError for TelegramOutputManager
```

- [ ] **Step 5: Implement manager classes**

Add to `wlcodex/telegram_output.py`:

```python
@dataclass(frozen=True)
class OutputRunKey:
    chat_id: int
    conversation_id: int
    run_id: str


@dataclass
class TelegramOutputSession:
    key: OutputRunKey
    surface: OutputSurface
    chunker: SemanticChunker
    preview_message_id: int | None = None
    is_closed: bool = False


class TelegramOutputManager:
    def __init__(
        self,
        *,
        transport,
        semantic_min_chars: int = 900,
        semantic_max_chars: int = 3200,
        final_chunk_chars: int = 3900,
    ) -> None:
        self._transport = transport
        self._policy = ChunkPolicy(
            min_chars=semantic_min_chars,
            max_chars=semantic_max_chars,
            final_max_chars=final_chunk_chars,
        )
        self.sessions: dict[OutputRunKey, TelegramOutputSession] = {}

    async def start(self, key: OutputRunKey, *, surface: OutputSurface, text: str) -> None:
        session = TelegramOutputSession(
            key=key,
            surface=surface,
            chunker=SemanticChunker(self._policy),
        )
        session.preview_message_id = await self._transport.send_preview(key.chat_id, text)
        self.sessions[key] = session

    async def update_status(self, key: OutputRunKey, text: str) -> None:
        session = self.sessions.get(key)
        if session is None or session.preview_message_id is None or session.is_closed:
            return
        await self._transport.edit_preview(
            key.chat_id,
            session.preview_message_id,
            text,
        )

    async def append_text(self, key: OutputRunKey, text: str) -> None:
        session = self.sessions[key]
        if session.is_closed:
            return
        session.chunker.append(text)
        if session.surface == OutputSurface.TERMINAL:
            for chunk in session.chunker.ready_chunks(force=False):
                await self._transport.send_body(key.chat_id, chunk)

    async def complete(self, key: OutputRunKey, buttons=None) -> None:
        session = self.sessions.get(key)
        if session is None:
            return
        chunks = session.chunker.final_chunks(number_parts=True)
        if chunks:
            for idx, chunk in enumerate(chunks):
                chunk_buttons = buttons if idx == len(chunks) - 1 else None
                await self._transport.send_body(key.chat_id, chunk, chunk_buttons)
        elif buttons:
            await self._transport.send_body(key.chat_id, "运行完成", buttons)
        if session.preview_message_id is not None:
            await self._transport.edit_preview(
                key.chat_id,
                session.preview_message_id,
                "运行完成",
            )
        session.is_closed = True
        self.sessions.pop(key, None)

    async def interrupt(self, key: OutputRunKey) -> None:
        session = self.sessions.get(key)
        if session is None:
            return
        if session.preview_message_id is not None:
            await self._transport.edit_preview(
                key.chat_id,
                session.preview_message_id,
                "已打断",
            )
        session.is_closed = True
        self.sessions.pop(key, None)
```

- [ ] **Step 6: Run manager tests**

Run:

```bash
rtk pytest tests/test_telegram_output_manager.py -q
```

Expected:

```text
passed
```

- [ ] **Step 7: Commit**

```bash
rtk git add wlcodex/telegram_output.py tests/test_telegram_output_manager.py
rtk git commit -m "feat: manage telegram output sessions"
```

## Task 5: Add Preview-Aware Telegram Transport

**Files:**
- Modify: `wlcodex/telegram_app.py`
- Modify: `wlcodex/interaction/transport.py`
- Test: `tests/test_surface_commands.py`

- [ ] **Step 1: Write failing preview send test**

Add to `tests/test_surface_commands.py`:

```python
@pytest.mark.asyncio
async def test_send_telegram_preview_waits_for_outbox_message_id(tmp_path):
    from types import SimpleNamespace
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.telegram_app import WlCodexHandlers
    from wlcodex.telegram_outbox import TelegramOutbox

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    outbox = TelegramOutbox(store=store)

    class Bot:
        async def send_message(self, **kwargs):
            return SimpleNamespace(message_id=4321)

    handlers = WlCodexHandlers(
        config=SimpleNamespace(
            telegram=SimpleNamespace(allowed_user_ids=frozenset({123})),
            interaction=SimpleNamespace(profile="natural"),
            telegram_output=SimpleNamespace(preview_send_timeout_seconds=2.0),
        ),
        controller=SimpleNamespace(),
        ledger=ledger,
        approval_service=SimpleNamespace(),
        bot=Bot(),
        runtime_event_store=store,
        outbox=outbox,
    )

    waiter = asyncio.create_task(
        handlers.send_telegram_preview(1, "Codex 正在处理")
    )
    await outbox.process_all()

    assert await waiter == 4321
```

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
rtk pytest tests/test_surface_commands.py::test_send_telegram_preview_waits_for_outbox_message_id -q
```

Expected:

```text
FAILED ... AttributeError: 'WlCodexHandlers' object has no attribute 'send_telegram_preview'
```

- [ ] **Step 3: Implement preview send/edit methods**

In `wlcodex/telegram_app.py`, add:

```python
async def send_telegram_preview(self, chat_id: int, text: str) -> int:
    if self._outbox is None:
        return await self._raw_send_message(chat_id, text)
    timeout = float(
        getattr(
            getattr(self._config, "telegram_output", None),
            "preview_send_timeout_seconds",
            5.0,
        )
    )
    return await self._outbox.enqueue_send_wait(
        chat_id,
        text,
        send_fn=self._raw_send_message,
        edit_fn=self._raw_edit_message,
        correlation_id="preview-send",
        timeout_seconds=timeout,
    )

async def edit_telegram_preview(
    self,
    chat_id: int,
    message_id: int,
    text: str,
    buttons: list[list[dict[str, str]]] | None = None,
) -> None:
    await self.edit_telegram(chat_id, message_id, text, buttons)
```

In `wlcodex/interaction/transport.py`, add optional callables or a small wrapper class for:

```python
async def send_preview(self, chat_id: int, text: str) -> int
async def edit_preview(self, chat_id: int, message_id: int, text: str, buttons=None) -> None
async def send_body(self, chat_id: int, text: str, buttons=None) -> int
```

Use existing `send` for body and existing `edit` for preview edit.

- [ ] **Step 4: Run preview transport tests**

Run:

```bash
rtk pytest tests/test_surface_commands.py::test_send_telegram_preview_waits_for_outbox_message_id -q
```

Expected:

```text
passed
```

- [ ] **Step 5: Commit**

```bash
rtk git add wlcodex/telegram_app.py wlcodex/interaction/transport.py tests/test_surface_commands.py
rtk git commit -m "feat: support editable telegram previews"
```

## Task 6: Integrate Output Manager Into InteractionRenderer

**Files:**
- Modify: `wlcodex/interaction/renderer.py`
- Modify: `wlcodex/telegram_app.py`
- Modify: `wlcodex/main.py`
- Test: `tests/test_interaction_renderer.py`
- Test: `tests/test_runtime_interaction_renderer.py`

- [ ] **Step 1: Write failing product final-only renderer test**

Add to `tests/test_interaction_renderer.py`:

```python
@pytest.mark.asyncio
async def test_product_renderer_buffers_deltas_and_sends_final_once():
    fake = FakeTransport()

    class PreviewTransport(TelegramTransport):
        async def send_preview(self, chat_id, text):
            return await self.send(chat_id, text)
        async def edit_preview(self, chat_id, message_id, text, buttons=None):
            await self.edit(chat_id, message_id, text, buttons)
        async def send_body(self, chat_id, text, buttons=None):
            return await self.send(chat_id, text, buttons)

    renderer = InteractionRenderer(
        transport=PreviewTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
        surface_resolver=lambda chat_id: "product",
        telegram_output_config=SimpleNamespace(
            semantic_min_chars=20,
            semantic_max_chars=80,
            final_chunk_chars=200,
            product_body_mode="final",
            terminal_body_mode="semantic_blocks",
        ),
    )

    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1, conversation_id=7, task_id=10))
    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, conversation_id=7, task_id=10, text="第一段。"))
    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, conversation_id=7, task_id=10, text="第二段。"))

    body_messages_before_completion = [m for m in fake.sent if "第一段" in m[1]]
    assert body_messages_before_completion == []

    await renderer.handle(InteractionEvent(event_type="run_completed", chat_id=1, conversation_id=7, task_id=10))

    body_messages = [m for m in fake.sent if "第一段。第二段。" in m[1]]
    assert len(body_messages) == 1
```

- [ ] **Step 2: Write failing terminal semantic block renderer test**

Add:

```python
@pytest.mark.asyncio
async def test_terminal_renderer_sends_semantic_blocks_while_running():
    fake = FakeTransport()

    class PreviewTransport(TelegramTransport):
        async def send_preview(self, chat_id, text):
            return await self.send(chat_id, text)
        async def edit_preview(self, chat_id, message_id, text, buttons=None):
            await self.edit(chat_id, message_id, text, buttons)
        async def send_body(self, chat_id, text, buttons=None):
            return await self.send(chat_id, text, buttons)

    renderer = InteractionRenderer(
        transport=PreviewTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
        surface_resolver=lambda chat_id: "terminal",
        telegram_output_config=SimpleNamespace(
            semantic_min_chars=10,
            semantic_max_chars=30,
            final_chunk_chars=200,
            product_body_mode="final",
            terminal_body_mode="semantic_blocks",
        ),
    )

    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1, conversation_id=7, task_id=10))
    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, conversation_id=7, task_id=10, text="第一段很长。\n\n第二段继续。"))

    assert any(message[1] == "第一段很长。" for message in fake.sent)
```

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
rtk pytest tests/test_interaction_renderer.py::test_product_renderer_buffers_deltas_and_sends_final_once tests/test_interaction_renderer.py::test_terminal_renderer_sends_semantic_blocks_while_running -q
```

Expected:

```text
FAILED ... TypeError: InteractionRenderer.__init__() got an unexpected keyword argument ...
```

- [ ] **Step 4: Modify InteractionRenderer constructor**

In `wlcodex/interaction/renderer.py`, add constructor args:

```python
surface_resolver=None,
telegram_output_config=None,
```

Create `TelegramOutputManager` when config is present:

```python
self._surface_resolver = surface_resolver or (lambda _chat_id: "product")
self._output_manager = TelegramOutputManager(
    transport=transport,
    semantic_min_chars=getattr(telegram_output_config, "semantic_min_chars", 900),
    semantic_max_chars=getattr(telegram_output_config, "semantic_max_chars", 3200),
    final_chunk_chars=getattr(telegram_output_config, "final_chunk_chars", 3900),
) if telegram_output_config is not None else None
```

Add helper:

```python
def _output_key(self, event: InteractionEvent) -> OutputRunKey:
    run_id = str(event.task_id or event.agent_run_id or "chat")
    return OutputRunKey(
        chat_id=event.chat_id,
        conversation_id=event.conversation_id or 0,
        run_id=run_id,
    )
```

- [ ] **Step 5: Route events to output manager**

In `_handle_started`:

```python
if self._output_manager is not None:
    key = self._output_key(event)
    surface = OutputSurface.TERMINAL if self._surface_resolver(event.chat_id) == "terminal" else OutputSurface.PRODUCT
    await self._output_manager.start(
        key,
        surface=surface,
        text=self._profile.started_text(event) or "正在处理",
    )
    return
```

In `_handle_text_delta`:

```python
if self._output_manager is not None:
    await self._output_manager.append_text(self._output_key(event), event.text)
    return
```

In `_handle_completed`, build existing completion buttons and call:

```python
if self._output_manager is not None:
    await self._output_manager.complete(self._output_key(event), buttons=buttons)
    self._cancel_typing(key)
    return
```

In `_handle_failed`:

```python
if self._output_manager is not None:
    await self._output_manager.interrupt(self._output_key(event))
    self._cancel_typing(key)
    return
```

- [ ] **Step 6: Wire Telegram app**

In `WlCodexHandlers.create_interaction_renderer`, pass:

```python
surface_resolver=self._get_active_surface_mode,
telegram_output_config=getattr(self._config, "telegram_output", None),
```

Use a transport that exposes `send_preview`, `edit_preview`, and `send_body` backed by:

- `send_telegram_preview`;
- `edit_telegram_preview`;
- `send_telegram`.

- [ ] **Step 7: Run interaction tests**

Run:

```bash
rtk pytest tests/test_interaction_renderer.py tests/test_runtime_interaction_renderer.py -q
```

Expected:

```text
passed
```

- [ ] **Step 8: Commit**

```bash
rtk git add wlcodex/interaction/renderer.py wlcodex/telegram_app.py wlcodex/main.py tests/test_interaction_renderer.py tests/test_runtime_interaction_renderer.py
rtk git commit -m "feat: render telegram output by surface policy"
```

## Task 7: Preserve Busy Choice Semantics

**Files:**
- Modify: `wlcodex/controller.py`
- Modify: `wlcodex/interaction/renderer.py`
- Test: `tests/test_controller_flow.py`
- Test: `tests/test_interaction_renderer.py`

- [ ] **Step 1: Write failing interrupt output-session test**

Add to `tests/test_interaction_renderer.py`:

```python
@pytest.mark.asyncio
async def test_interrupt_closes_old_output_session_before_new_run():
    fake = FakeTransport()

    class PreviewTransport(TelegramTransport):
        async def send_preview(self, chat_id, text):
            return await self.send(chat_id, text)
        async def edit_preview(self, chat_id, message_id, text, buttons=None):
            await self.edit(chat_id, message_id, text, buttons)
        async def send_body(self, chat_id, text, buttons=None):
            return await self.send(chat_id, text, buttons)

    renderer = InteractionRenderer(
        transport=PreviewTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
        surface_resolver=lambda chat_id: "product",
        telegram_output_config=SimpleNamespace(
            semantic_min_chars=20,
            semantic_max_chars=80,
            final_chunk_chars=200,
            product_body_mode="final",
            terminal_body_mode="semantic_blocks",
        ),
    )

    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1, conversation_id=7, task_id=10))
    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, conversation_id=7, task_id=10, text="旧输出"))
    await renderer.handle(InteractionEvent(event_type="run_failed", chat_id=1, conversation_id=7, task_id=10, text="interrupted", metadata={"runtime_state": "cancelled"}))
    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1, conversation_id=7, task_id=11))
    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, conversation_id=7, task_id=11, text="新输出"))
    await renderer.handle(InteractionEvent(event_type="run_completed", chat_id=1, conversation_id=7, task_id=11))

    assert any("已打断" in edit[2] for edit in fake.edited)
    assert any("新输出" in sent[1] for sent in fake.sent)
    assert not any("旧输出新输出" in sent[1] for sent in fake.sent)
```

- [ ] **Step 2: Ensure busy callback still sends to current**

Keep existing tests:

```bash
rtk pytest tests/test_controller_flow.py::test_busy_append_steers_current_codex_turn tests/test_controller_flow.py::test_busy_interrupt_aborts_current_and_runs_pending_codex -q
```

Expected:

```text
passed
```

- [ ] **Step 3: Implement interrupt status**

If `run_failed` metadata indicates `runtime_state` in `{"cancelled", "aborted"}` or text contains `interrupted`, call:

```python
await self._output_manager.interrupt(self._output_key(event))
```

Otherwise, call a failure method that edits the status bubble to:

```text
运行失败：<short error>
```

- [ ] **Step 4: Run busy and interrupt tests**

Run:

```bash
rtk pytest tests/test_controller_flow.py tests/test_interaction_renderer.py -q
```

Expected:

```text
passed
```

- [ ] **Step 5: Commit**

```bash
rtk git add wlcodex/controller.py wlcodex/interaction/renderer.py tests/test_controller_flow.py tests/test_interaction_renderer.py
rtk git commit -m "fix: keep readable output compatible with busy controls"
```

## Task 8: Product and Terminal Integration Tests

**Files:**
- Modify: `tests/test_surface_commands.py`
- Modify: `tests/test_workbench_telegram_routing.py`

- [ ] **Step 1: Add product cockpit no-fragment test**

Add to `tests/test_surface_commands.py`:

```python
@pytest.mark.asyncio
async def test_product_mode_does_not_send_token_fragments_during_stream(tmp_path):
    # Build handlers with natural interaction and product mode.
    # Drive InteractionRenderer events directly because this is output rendering,
    # not command parsing.
    from wlcodex.interaction.events import InteractionEvent

    sent = []
    edited = []

    async def send(chat_id, text, buttons=None):
        sent.append((chat_id, text, buttons))
        return len(sent)

    async def edit(chat_id, message_id, text, buttons=None):
        edited.append((chat_id, message_id, text, buttons))

    async def typing(chat_id):
        return None

    handlers = _make_handlers()
    handlers.send_telegram = send
    handlers.edit_telegram = edit
    handlers.send_telegram_preview = send
    handlers.edit_telegram_preview = edit
    with patch.object(handlers, "_get_active_surface_mode", return_value="product"):
        renderer = handlers.create_interaction_renderer()

    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1, conversation_id=7, task_id=10))
    for token in ["我", "查", "到", "的", "最新", "金价", "如下："]:
        await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, conversation_id=7, task_id=10, text=token))
    await renderer.handle(InteractionEvent(event_type="run_completed", chat_id=1, conversation_id=7, task_id=10))

    body_texts = [text for _, text, _ in sent if "我查到的最新金价如下：" in text]
    tiny_texts = [text for _, text, _ in sent if text in {"我", "查", "到", "的"}]
    assert len(body_texts) == 1
    assert tiny_texts == []
```

- [ ] **Step 2: Add terminal semantic block test**

Add to `tests/test_workbench_telegram_routing.py`:

```python
@pytest.mark.asyncio
async def test_terminal_mode_streams_semantic_blocks_not_token_fragments():
    from wlcodex.interaction.events import InteractionEvent

    sent = []
    edited = []

    async def send(chat_id, text, buttons=None):
        sent.append((chat_id, text, buttons))
        return len(sent)

    async def edit(chat_id, message_id, text, buttons=None):
        edited.append((chat_id, message_id, text, buttons))

    handlers = _make_handlers()
    handlers.send_telegram = send
    handlers.edit_telegram = edit
    handlers.send_telegram_preview = send
    handlers.edit_telegram_preview = edit
    with patch.object(handlers, "_get_active_surface_mode", return_value="terminal"):
        renderer = handlers.create_interaction_renderer()

    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1, conversation_id=7, task_id=10))
    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, conversation_id=7, task_id=10, text="第一段很长很长。\n\n第二段也很长很长。"))

    assert any(text == "第一段很长很长。" for _, text, _ in sent)
    assert not any(text == "第一段" for _, text, _ in sent)
```

- [ ] **Step 3: Run surface tests**

Run:

```bash
rtk pytest tests/test_surface_commands.py tests/test_workbench_telegram_routing.py -q
```

Expected:

```text
passed
```

- [ ] **Step 4: Commit**

```bash
rtk git add tests/test_surface_commands.py tests/test_workbench_telegram_routing.py
rtk git commit -m "test: cover readable output across telegram surfaces"
```

## Task 9: Live Smoke Evidence

**Files:**
- Modify: `tests/test_live_telegram_smoke.py`

- [ ] **Step 1: Add DB evidence assertions**

Add a test guarded by `WLCODEX_RUN_TELEGRAM_LIVE=1`:

```python
def test_live_telegram_output_is_not_fragment_spam() -> None:
    if os.environ.get("WLCODEX_RUN_TELEGRAM_LIVE") != "1":
        pytest.skip("live Telegram smoke disabled")

    conn = sqlite3.connect(_sqlite_path())
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT event_type, payload_json
        FROM runtime_events
        WHERE event_type IN ('telegram.delivery.enqueued', 'telegram.message.sent', 'telegram.message.edited')
        ORDER BY id DESC
        LIMIT 80
        """
    ).fetchall()

    previews = []
    tiny_fragments = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        text = payload.get("text_preview", "")
        if "正在" in text or "运行" in text:
            previews.append(text)
        if text in {"我", "查", "到", "的"}:
            tiny_fragments.append(text)

    assert previews
    assert tiny_fragments == []
```

- [ ] **Step 2: Run non-live smoke suite**

Run:

```bash
rtk pytest tests/test_live_telegram_smoke.py -q
```

Expected:

```text
skipped or passed
```

- [ ] **Step 3: Run live evidence test after deployment**

Run only when the real bot token / runtime is available:

```bash
rtk env WLCODEX_RUN_TELEGRAM_LIVE=1 .venv/bin/pytest tests/test_live_telegram_smoke.py::test_live_telegram_output_is_not_fragment_spam -q
```

Expected:

```text
passed
```

- [ ] **Step 4: Commit**

```bash
rtk git add tests/test_live_telegram_smoke.py
rtk git commit -m "test: verify live telegram readable output"
```

## Task 10: Full Verification and Deployment

**Files:**
- No new files.

- [ ] **Step 1: Run targeted suite**

Run:

```bash
rtk pytest tests/test_config.py tests/test_telegram_output_chunker.py tests/test_telegram_output_manager.py tests/test_telegram_outbox.py tests/test_interaction_renderer.py tests/test_runtime_interaction_renderer.py tests/test_surface_commands.py tests/test_workbench_telegram_routing.py tests/test_controller_flow.py -q
```

Expected:

```text
passed
```

- [ ] **Step 2: Run compile check**

Run:

```bash
rtk .venv/bin/python -m compileall -q wlcodex tests
```

Expected:

```text
exit code 0
```

- [ ] **Step 3: Run GitNexus detect changes**

Run:

```text
gitnexus_detect_changes(repo="wlcodex", scope="all")
```

Expected:

- risk reviewed;
- affected processes match Telegram output rendering only;
- HIGH or CRITICAL risk is reported before deployment.

- [ ] **Step 4: Commit final integration if needed**

If there are uncommitted verification or cleanup changes:

```bash
rtk git add <changed-files>
rtk git commit -m "fix: deliver readable telegram streaming output"
```

- [ ] **Step 5: Restart service**

Run:

```bash
rtk systemctl --user restart wlcodex.service
rtk systemctl --user status wlcodex.service --no-pager
```

Expected:

```text
Active: active (running)
```

- [ ] **Step 6: Manual Telegram acceptance**

Send in product cockpit:

```text
/codex 查周大福的金价，给我来源和简单结论
```

Expected:

- one status bubble appears while running;
- final answer is readable;
- no message containing only `我`, `查`, `到`, `.cn/)`, or mid-link fragments.

Switch to terminal onsite and send:

```text
/terminal codex
请继续查一个长一点的资料并列点
```

Expected:

- status bubble updates;
- body appears as readable semantic blocks;
- no token-fragment spam.

While it runs, send:

```text
/codex 顺便查今天国际金价
```

Expected:

- busy choice card appears;
- `发给当前 Codex` appends to current;
- `打断并执行这句` closes old status bubble as interrupted and starts a new status bubble;
- no output from old and new runs is mixed.

## Self-Review

Spec coverage:

- status bubble: Tasks 2, 4, 5, 6, 10;
- semantic blocks: Tasks 3, 4, 8;
- final organized answer: Tasks 4, 6, 8, 9;
- product / terminal split: Tasks 4, 6, 8;
- busy controls compatibility: Task 7 and Task 10 manual acceptance.

Placeholder scan:

- no unresolved placeholder markers;
- no deferred implementation markers;
- every task lists files, tests, commands, and expected results.

Type consistency:

- `OutputRunKey`, `OutputSurface`, `SemanticChunker`, `TelegramOutputManager`, and `TelegramOutputSession` are defined before use.
- preview transport methods are introduced before renderer integration.
