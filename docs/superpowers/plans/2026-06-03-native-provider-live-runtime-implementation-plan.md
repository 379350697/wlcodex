# Native Provider Live Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Claude SDK DeepSeek and Antigravity SDK native providers behave like Codex native remote control by starting turns asynchronously and streaming provider output into `runtime_events`.

**Architecture:** Keep `codex`, `claude`, and `antigravity` as top-level providers. Add a small ccswitch credential resolver for DeepSeek, and a shared native runtime event emitter used by non-Codex SDK providers. Provider methods return control results immediately and run prompts in background tasks that append lifecycle, user, assistant, completion, and failure events.

**Tech Stack:** Python 3.12 asyncio, SQLite, existing `RuntimeEventStore`, existing live stream projection, pytest, ruff.

---

### Task 1: ccswitch DeepSeek Credential Resolver

**Files:**
- Create: `wlcodex/native_agents/ccswitch_deepseek.py`
- Test: `tests/test_native_agent_ccswitch_deepseek.py`
- Modify: `wlcodex/native_agents/claude_sdk_deepseek_provider.py`

- [ ] **Step 1: Write failing resolver tests**

Add tests that create a temporary SQLite database with a `providers` table matching ccswitch fields. Verify that the resolver:
- prefers `DEEPSEEK_API_KEY` from env;
- falls back to a current provider with `settings_config.env.ANTHROPIC_AUTH_TOKEN`;
- ignores providers without DeepSeek base URL or auth value;
- never returns the secret in metadata.

Run: `.venv/bin/python -m pytest tests/test_native_agent_ccswitch_deepseek.py -q`
Expected: fails because `wlcodex.native_agents.ccswitch_deepseek` does not exist.

- [ ] **Step 2: Implement resolver**

Create `resolve_deepseek_credentials(env, db_path, api_key_env)` that returns a dataclass with `api_key`, `base_url`, `source`, `provider_id`, and `provider_name`. Read JSON from `settings_config`, check `env.ANTHROPIC_BASE_URL`, `base_url`, or `ANTHROPIC_BASE_URL`, and use `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `apiKey`, or `api_key` as the auth value. Do not print or persist secrets.

- [ ] **Step 3: Integrate resolver with Claude SDK provider**

Update `ClaudeSdkDeepSeekProvider.status()` and prompt execution so missing `DEEPSEEK_API_KEY` can still connect through ccswitch. Runtime metadata may include source/provider id/name but must not include key material.

Run: `.venv/bin/python -m pytest tests/test_native_agent_ccswitch_deepseek.py tests/test_native_agent_claude_sdk_deepseek_provider.py -q`
Expected: pass.
### Task 2: Shared Runtime Event Emitter

**Files:**
- Create: `wlcodex/native_agents/runtime_events.py`
- Test: `tests/test_native_agent_runtime_events.py`
- Modify: `wlcodex/native_agents/claude_cli_provider.py`
- Modify: `wlcodex/native_agents/claude_sdk_deepseek_provider.py`
- Modify: `wlcodex/native_agents/antigravity_provider.py`

- [ ] **Step 1: Write failing emitter tests**

Verify that a `NativeAgentRuntimeEmitter` appends:
- `AGENT_RUN_STARTED`;
- `USER_MESSAGE_RECEIVED`;
- `MODEL_TEXT_DELTA`;
- `AGENT_RUN_COMPLETED`;
- `AGENT_RUN_FAILED`.

Every payload must contain `native_thread_id`, `native_turn_id`, `provider`, `provider_engine`, and `source_kind`.

Run: `.venv/bin/python -m pytest tests/test_native_agent_runtime_events.py -q`
Expected: fails because emitter module does not exist.

- [ ] **Step 2: Implement emitter**

Use `RuntimeEvent` directly. Use `AggregateType.AGENT_RUN`, `Visibility.OPERATOR`, provider-specific `EventSource`, and `agent_run_id` / `conversation_id` from `NativeAgentSession`. Add helper extraction for text chunks from dicts, strings, and simple SDK objects.

Run: `.venv/bin/python -m pytest tests/test_native_agent_runtime_events.py -q`
Expected: pass.

### Task 3: Async Provider Turns

**Files:**
- Modify: `wlcodex/native_agents/claude_cli_provider.py`
- Modify: `wlcodex/native_agents/claude_sdk_deepseek_provider.py`
- Modify: `wlcodex/native_agents/antigravity_provider.py`
- Test: `tests/test_native_agent_claude_cli_provider.py`
- Test: `tests/test_native_agent_claude_sdk_deepseek_provider.py`
- Test: `tests/test_native_agent_antigravity_provider.py`

- [ ] **Step 1: Write failing async-turn tests**

For each provider, use a fake runner that waits on an `asyncio.Event`. Assert `start_session()` returns before the event is released, returns `turn_running=True`, and appends lifecycle/user events before completion. After releasing the event, assert text and completion events appear in `RuntimeEventStore`.

Run: `.venv/bin/python -m pytest tests/test_native_agent_claude_cli_provider.py tests/test_native_agent_claude_sdk_deepseek_provider.py tests/test_native_agent_antigravity_provider.py -q`
Expected: fails because current providers await the full runner.

- [ ] **Step 2: Implement background tasks**

Providers create a new `native_turn_id`, mark the session running, append start/user events, spawn an asyncio task, and return immediately. The background task streams runner output through the emitter, updates session status to `done` or `failed`, and appends terminal event. Existing `create_session()` remains synchronous and does not run a prompt.

Run: `.venv/bin/python -m pytest tests/test_native_agent_claude_cli_provider.py tests/test_native_agent_claude_sdk_deepseek_provider.py tests/test_native_agent_antigravity_provider.py -q`
Expected: pass.

### Task 4: Composition and Config

**Files:**
- Modify: `wlcodex/main.py`
- Modify: `wlcodex/config.py`
- Modify: `config/wlcodex.example.toml`
- Test: `tests/test_main_composition.py`
- Test: `tests/test_native_agent_config.py`

- [ ] **Step 1: Write failing composition tests**

Verify that `_create_live_stream_components()` injects `runtime_store` into Claude and Antigravity providers, and that Claude SDK provider can be configured with the default ccswitch db path.

Run: `.venv/bin/python -m pytest tests/test_main_composition.py::test_create_live_stream_components_wires_single_claude_sdk_engine tests/test_native_agent_config.py -q`
Expected: fail until config and composition are updated.

- [ ] **Step 2: Implement composition and docs**

Add config fields for ccswitch fallback path and enabled flag under `[native_agents.claude.sdk_deepseek]`. Pass `runtime_store` into non-Codex providers. Update example config comments.

Run: `.venv/bin/python -m pytest tests/test_main_composition.py tests/test_native_agent_config.py -q`
Expected: pass.

### Task 5: Verification

**Files:**
- All files changed above.

- [ ] **Step 1: Focused verification**

Run:
`.venv/bin/python -m pytest tests/test_native_agent_ccswitch_deepseek.py tests/test_native_agent_runtime_events.py tests/test_native_agent_claude_cli_provider.py tests/test_native_agent_claude_sdk_deepseek_provider.py tests/test_native_agent_antigravity_provider.py tests/test_main_composition.py tests/test_native_agent_config.py tests/test_worker_live_stream_native_agent_routes.py tests/test_worker_live_stream_native_routes.py -q`

Expected: pass.

- [ ] **Step 2: Full verification**

Run:
`.venv/bin/python -m pytest -q`

If sandbox blocks loopback binding, rerun the same command outside the sandbox with approval.

- [ ] **Step 3: Lint**

Run:
`.venv/bin/python -m ruff check wlcodex/native_agents wlcodex/main.py wlcodex/config.py tests/test_native_agent_ccswitch_deepseek.py tests/test_native_agent_runtime_events.py tests/test_native_agent_claude_cli_provider.py tests/test_native_agent_claude_sdk_deepseek_provider.py tests/test_native_agent_antigravity_provider.py tests/test_main_composition.py tests/test_native_agent_config.py`

Expected: pass.
