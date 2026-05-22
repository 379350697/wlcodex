# WLCodex Claude Binary Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make WLCodex Claude runs survive Claude Code / VS Code extension updates by resolving a stable Claude binary and adapting CLI flags to probed capabilities.

**Architecture:** Add a focused `wlcodex/claude_binary.py` helper for binary discovery and help parsing, then wire it into config, main composition, and `ClaudeBackend`. Keep the existing subprocess backend and stream parser; only change how the executable and optional CLI flags are selected.

**Tech Stack:** Python 3.12, asyncio subprocesses, pathlib/shutil, pytest, existing WLCodex config and Claude backend, GitNexus MCP/CLI for impact and change detection.

---

## Required Reading

- Spec: `docs/superpowers/specs/2026-05-21-wlcodex-claude-binary-resilience-design.md`
- Claude backend: `wlcodex/claude_backend.py`
- Config loading: `wlcodex/config.py`
- Main composition: `wlcodex/main.py`
- Example config: `config/wlcodex.example.toml`
- Local config: `config/wlcodex.toml`
- Claude backend tests: `tests/test_claude_backend.py`
- Config tests: `tests/test_config.py`
- Main composition tests: `tests/test_main_composition.py`

## Non-Negotiable Engineering Rules

- Run GitNexus impact analysis before editing any existing function, class, or method.
- Stop and report before editing if impact is HIGH or CRITICAL.
- Write failing tests before implementation.
- Keep the fix local to Claude binary discovery and CLI invocation compatibility.
- Do not auto-install, auto-update, downgrade, or delete Claude binaries.
- Do not change Codex backend behavior.
- Do not expose Telegram secrets to Claude subprocesses.
- Run `gitnexus_detect_changes(scope="all")` before committing.

## Impact Baseline

Already observed on 2026-05-21:

```text
ClaudeBackend: LOW, direct upstream 0
ClaudeBackend._prompt_args: LOW, direct upstream 0
ClaudeBackend.send_terminal_input: LOW, direct upstream 0
config.ClaudeConfig: LOW, direct upstream 1 (wlcodex/main.py import)
```

Re-run the relevant impact command immediately before each edit task because
the index or code may have changed:

```text
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "ClaudeBackend",
  "file_path": "wlcodex/claude_backend.py",
  "kind": "Class",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "_prompt_args",
  "file_path": "wlcodex/claude_backend.py",
  "kind": "Function",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "send_terminal_input",
  "file_path": "wlcodex/claude_backend.py",
  "kind": "Function",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "ClaudeConfig",
  "file_path": "wlcodex/config.py",
  "kind": "Class",
  "direction": "upstream"
})
```

Expected: LOW risk. If HIGH or CRITICAL, stop and report.

## File Structure

| File | Responsibility |
| --- | --- |
| `wlcodex/claude_binary.py` | Resolve Claude binary and parse/probe CLI capabilities. Pure helper plus one async probe. |
| `tests/test_claude_binary.py` | Unit tests for resolver ordering, stale VS Code path repair, help parsing, and async probe behavior. |
| `wlcodex/config.py` | Change Claude binary default from `"claude"` to `"auto"`. |
| `wlcodex/main.py` | Resolve configured Claude binary before creating `ClaudeBackend`; log source and warning. |
| `wlcodex/claude_backend.py` | Cache CLI capabilities and build args from supported flags. |
| `config/wlcodex.example.toml` | Document `binary = "auto"` and env override. |
| `config/wlcodex.toml` | Change local binary to `"auto"` so the actual deployment stops pinning the VS Code extension path. |
| `tests/test_config.py` | Update default assertions and config load assertions. |
| `tests/test_main_composition.py` | Verify main resolves `"auto"` before constructing the backend. |
| `tests/test_claude_backend.py` | Verify optional flags are skipped when unsupported and resume fails clearly. |

## Parallelization Model

Task 1 should land first because later tasks import the new helper. Tasks 2 and
3 can proceed after Task 1. Task 4 depends on Task 3. Task 5 is final
integration and verification.

| Task | Purpose | Write ownership | Depends on |
| --- | --- | --- | --- |
| 1 | Binary resolver and capability parser | `wlcodex/claude_binary.py`, `tests/test_claude_binary.py` | none |
| 2 | Config defaults and docs | `wlcodex/config.py`, configs, `tests/test_config.py` | 1 |
| 3 | Main composition resolver wiring | `wlcodex/main.py`, `tests/test_main_composition.py` | 1, 2 |
| 4 | Backend capability-aware args | `wlcodex/claude_backend.py`, `tests/test_claude_backend.py` | 1 |
| 5 | Final verification and change impact | tests, GitNexus detect changes | 1-4 |

## Task 1: Claude Binary Resolver And Capability Parser

**Files:**
- Create: `wlcodex/claude_binary.py`
- Create: `tests/test_claude_binary.py`

- [ ] **Step 1: Create failing resolver tests**

Create `tests/test_claude_binary.py` with these tests:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from wlcodex.claude_binary import (
    ClaudeCliCapabilities,
    parse_claude_help,
    resolve_claude_binary,
)


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def test_env_binary_overrides_configured_binary(tmp_path: Path) -> None:
    env_binary = tmp_path / "env-claude"
    config_binary = tmp_path / "config-claude"
    _make_executable(env_binary)
    _make_executable(config_binary)

    result = resolve_claude_binary(
        str(config_binary),
        env={"WLCODEX_CLAUDE_BINARY": str(env_binary), "PATH": ""},
        home=tmp_path,
    )

    assert result.binary == str(env_binary)
    assert result.source == "env"
    assert result.warning == ""


def test_auto_uses_path_binary(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    path_binary = bin_dir / "claude"
    _make_executable(path_binary)

    result = resolve_claude_binary(
        "auto",
        env={"PATH": str(bin_dir)},
        home=tmp_path,
    )

    assert result.binary == str(path_binary)
    assert result.source == "path"


def test_stale_vscode_extension_path_repairs_to_latest_installed_binary(
    tmp_path: Path,
) -> None:
    stale = (
        tmp_path
        / ".vscode"
        / "extensions"
        / "anthropic.claude-code-2.1.145-linux-x64"
        / "resources"
        / "native-binary"
        / "claude"
    )
    older = (
        tmp_path
        / ".vscode"
        / "extensions"
        / "anthropic.claude-code-2.1.146-linux-x64"
        / "resources"
        / "native-binary"
        / "claude"
    )
    newer = (
        tmp_path
        / ".vscode"
        / "extensions"
        / "anthropic.claude-code-2.2.0-linux-x64"
        / "resources"
        / "native-binary"
        / "claude"
    )
    _make_executable(older)
    _make_executable(newer)

    result = resolve_claude_binary(
        str(stale),
        env={"PATH": ""},
        home=tmp_path,
    )

    assert result.binary == str(newer)
    assert result.source == "vscode-extension"
    assert "configured Claude binary was not found" in result.warning


def test_missing_binary_reports_attempted_strategies(tmp_path: Path) -> None:
    result = resolve_claude_binary(
        "auto",
        env={"PATH": ""},
        home=tmp_path,
    )

    assert result.binary == ""
    assert result.source == "unresolved"
    assert "PATH claude" in result.attempted
    assert "VS Code extension scan" in result.attempted


def test_parse_modern_claude_help_detects_supported_flags() -> None:
    help_text = """
Usage: claude [options] [command] [prompt]
  -p, --print
  --output-format <format>
  --include-partial-messages
  --include-hook-events
  --input-format <format>
  --permission-mode <mode>
  --model <model>
  --effort <level>
  -r, --resume [value]
"""

    caps = parse_claude_help(help_text)

    assert caps == ClaudeCliCapabilities(
        print_prompt=True,
        output_format=True,
        stream_json_output=True,
        include_partial_messages=True,
        include_hook_events=True,
        input_stream_json=True,
        permission_mode=True,
        model=True,
        effort=True,
        resume=True,
    )


def test_parse_minimal_help_disables_optional_flags() -> None:
    caps = parse_claude_help("Usage: claude\n  -p, --print\n")

    assert caps.print_prompt is True
    assert caps.output_format is False
    assert caps.stream_json_output is False
    assert caps.include_partial_messages is False
    assert caps.include_hook_events is False
    assert caps.input_stream_json is False
    assert caps.permission_mode is False
    assert caps.model is False
    assert caps.effort is False
    assert caps.resume is False
```

Run:

```bash
.venv/bin/python -m pytest tests/test_claude_binary.py -q
```

Expected: FAIL because `wlcodex.claude_binary` does not exist.

- [ ] **Step 2: Implement resolver and parser**

Create `wlcodex/claude_binary.py`:

```python
"""Claude CLI binary discovery and capability probing."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ClaudeBinaryResolution:
    binary: str
    source: str
    warning: str = ""
    attempted: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClaudeCliCapabilities:
    print_prompt: bool = False
    output_format: bool = False
    stream_json_output: bool = False
    include_partial_messages: bool = False
    include_hook_events: bool = False
    input_stream_json: bool = False
    permission_mode: bool = False
    model: bool = False
    effort: bool = False
    resume: bool = False

    @classmethod
    def minimal(cls) -> "ClaudeCliCapabilities":
        return cls(print_prompt=True)


def resolve_claude_binary(
    config_binary: str,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> ClaudeBinaryResolution:
    env_map = env if env is not None else os.environ
    home_path = home or Path.home()
    attempted: list[str] = []

    env_binary = env_map.get("WLCODEX_CLAUDE_BINARY", "").strip()
    if env_binary:
        attempted.append("WLCODEX_CLAUDE_BINARY")
        resolved = _resolve_command_or_path(env_binary, env_map)
        if resolved:
            return ClaudeBinaryResolution(resolved, "env", attempted=tuple(attempted))
        return ClaudeBinaryResolution(
            "",
            "unresolved",
            warning=f"WLCODEX_CLAUDE_BINARY does not point to an executable: {env_binary}",
            attempted=tuple(attempted),
        )

    configured = (config_binary or "").strip()
    if configured and configured.lower() != "auto":
        attempted.append("configured binary")
        resolved = _resolve_command_or_path(configured, env_map)
        if resolved:
            return ClaudeBinaryResolution(
                resolved,
                "configured",
                attempted=tuple(attempted),
            )

        repaired = _find_latest_vscode_claude(home_path)
        if _looks_like_vscode_claude_path(configured) and repaired:
            return ClaudeBinaryResolution(
                str(repaired),
                "vscode-extension",
                warning=(
                    "configured Claude binary was not found; using newest "
                    f"VS Code extension binary: {repaired}"
                ),
                attempted=tuple(attempted + ["VS Code extension scan"]),
            )

        return ClaudeBinaryResolution(
            "",
            "unresolved",
            warning=f"configured Claude binary does not point to an executable: {configured}",
            attempted=tuple(attempted),
        )

    attempted.append("PATH claude")
    path_resolved = shutil.which("claude", path=env_map.get("PATH"))
    if path_resolved:
        return ClaudeBinaryResolution(path_resolved, "path", attempted=tuple(attempted))

    attempted.append("VS Code extension scan")
    vscode_resolved = _find_latest_vscode_claude(home_path)
    if vscode_resolved:
        return ClaudeBinaryResolution(
            str(vscode_resolved),
            "vscode-extension",
            attempted=tuple(attempted),
        )

    return ClaudeBinaryResolution("", "unresolved", attempted=tuple(attempted))


def parse_claude_help(help_text: str) -> ClaudeCliCapabilities:
    return ClaudeCliCapabilities(
        print_prompt="-p, --print" in help_text or "--print" in help_text,
        output_format="--output-format" in help_text,
        stream_json_output=(
            "--output-format" in help_text and "stream-json" in help_text
        ),
        include_partial_messages="--include-partial-messages" in help_text,
        include_hook_events="--include-hook-events" in help_text,
        input_stream_json=(
            "--input-format" in help_text and "stream-json" in help_text
        ),
        permission_mode="--permission-mode" in help_text,
        model="--model" in help_text,
        effort="--effort" in help_text,
        resume="--resume" in help_text,
    )


async def probe_claude_capabilities(
    binary: str,
    *,
    timeout_seconds: float = 5.0,
) -> ClaudeCliCapabilities:
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "--help",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_seconds,
        )
    except Exception:
        return ClaudeCliCapabilities.minimal()

    text = ""
    if stdout:
        text += stdout.decode("utf-8", errors="replace")
    if stderr:
        text += "\n" + stderr.decode("utf-8", errors="replace")
    caps = parse_claude_help(text)
    return caps if caps.print_prompt else ClaudeCliCapabilities.minimal()


def _resolve_command_or_path(value: str, env: Mapping[str, str]) -> str:
    path = Path(value).expanduser()
    if path.is_absolute() or "/" in value:
        return str(path) if path.is_file() and os.access(path, os.X_OK) else ""
    return shutil.which(value, path=env.get("PATH")) or ""


def _looks_like_vscode_claude_path(value: str) -> bool:
    return (
        "anthropic.claude-code-" in value
        and "/resources/native-binary/claude" in value
    )


def _find_latest_vscode_claude(home: Path) -> Path | None:
    extension_dir = home / ".vscode" / "extensions"
    candidates = [
        path
        for path in extension_dir.glob(
            "anthropic.claude-code-*/resources/native-binary/claude"
        )
        if path.is_file() and os.access(path, os.X_OK)
    ]
    if not candidates:
        return None
    return sorted(candidates, key=_vscode_extension_version_key)[-1]


def _vscode_extension_version_key(path: Path) -> tuple[int, ...]:
    match = re.search(r"anthropic\.claude-code-([0-9]+(?:\.[0-9]+)*)", str(path))
    if not match:
        return (0,)
    return tuple(int(part) for part in match.group(1).split("."))
```

- [ ] **Step 3: Verify Task 1 passes**

Run:

```bash
.venv/bin/python -m pytest tests/test_claude_binary.py -q
```

Expected: PASS for all tests in `tests/test_claude_binary.py`.

- [ ] **Step 4: Commit Task 1**

```bash
git add wlcodex/claude_binary.py tests/test_claude_binary.py
git commit -m "feat: add claude binary resolver"
```

## Task 2: Config Defaults And Operator Docs

**Files:**
- Modify: `wlcodex/config.py`
- Modify: `wlcodex/claude_backend.py`
- Modify: `config/wlcodex.example.toml`
- Modify: `config/wlcodex.toml`
- Modify: `tests/test_config.py`
- Modify: `tests/test_claude_backend.py`

- [ ] **Step 1: Run impact analysis**

Run:

```text
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "ClaudeConfig",
  "file_path": "wlcodex/config.py",
  "kind": "Class",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "ClaudeConfig",
  "file_path": "wlcodex/claude_backend.py",
  "kind": "Class",
  "direction": "upstream"
})
```

Expected: LOW. Stop if HIGH or CRITICAL.

- [ ] **Step 2: Update failing config assertions first**

In `tests/test_config.py`, change any default Claude binary assertion to:

```python
assert config.claude.binary == "auto"
```

In `tests/test_claude_backend.py`, change `test_claude_config_defaults` to:

```python
def test_claude_config_defaults() -> None:
    config = ClaudeConfig()
    assert config.enabled is False
    assert config.binary == "auto"
    assert config.startup_timeout_seconds == 15.0
    assert config.request_timeout_seconds == 3600.0
    assert config.stream_idle_timeout_seconds == 600.0
    assert config.stream_drain_grace_seconds == 0.1
    assert config.permission_mode == "acceptEdits"
    assert config.model == "deepseek-v4-pro"
    assert config.effort == "max"
```

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py::test_load_config_defaults tests/test_claude_backend.py::test_claude_config_defaults -q
```

Expected: FAIL because implementation still defaults to `"claude"`.

- [ ] **Step 3: Change defaults to auto**

In `wlcodex/config.py`, change:

```python
class ClaudeConfig:
    enabled: bool = False
    binary: str = "claude"
```

to:

```python
class ClaudeConfig:
    enabled: bool = False
    binary: str = "auto"
```

Also change the load default:

```python
binary=str(claude_raw.get("binary", "auto")),
```

In `wlcodex/claude_backend.py`, change:

```python
class ClaudeConfig:
    enabled: bool = False
    binary: str = "claude"
```

to:

```python
class ClaudeConfig:
    enabled: bool = False
    binary: str = "auto"
```

- [ ] **Step 4: Update config files**

In `config/wlcodex.example.toml`, set:

```toml
binary = "auto"
# Optional override for operators:
#   WLCODEX_CLAUDE_BINARY=/absolute/path/to/claude
```

In `config/wlcodex.toml`, replace the versioned VS Code extension path with:

```toml
binary = "auto"
```

- [ ] **Step 5: Verify Task 2 passes**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py tests/test_claude_backend.py::test_claude_config_defaults -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add wlcodex/config.py wlcodex/claude_backend.py config/wlcodex.example.toml config/wlcodex.toml tests/test_config.py tests/test_claude_backend.py
git commit -m "fix: default claude binary to auto"
```

## Task 3: Main Composition Resolver Wiring

**Files:**
- Modify: `wlcodex/main.py`
- Modify: `tests/test_main_composition.py`

- [ ] **Step 1: Run impact analysis**

Run:

```text
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "main",
  "file_path": "wlcodex/main.py",
  "kind": "Function",
  "direction": "upstream"
})
```

If GitNexus returns multiple `main` candidates, select the `wlcodex/main.py`
candidate. Expected: LOW or MEDIUM. Stop if HIGH or CRITICAL.

- [ ] **Step 2: Write failing composition test**

In `tests/test_main_composition.py`, first extend the existing
`_write_test_config_with_path()` helper signature:

```python
def _write_test_config_with_path(
    path: Path,
    sqlite_path: Path,
    task_log_dir: Path,
    *,
    terminal_enabled: bool = False,
    claude_enabled: bool = False,
    claude_binary: str = "claude",
) -> None:
```

Then change its Claude block from:

```python
    if claude_enabled:
        claude_block = "[claude]\nenabled = true\nbinary = \"claude\"\n"
```

to:

```python
    if claude_enabled:
        claude_block = (
            "[claude]\n"
            "enabled = true\n"
            f"binary = \"{claude_binary}\"\n"
        )
```

Add this test:

```python
def test_main_resolves_auto_claude_binary_before_backend_construction(
    monkeypatch,
    tmp_path,
) -> None:
    import asyncio as _asyncio
    import os as _os
    import sys as _sys
    from asyncio import base_events as _base_events
    from unittest.mock import MagicMock

    import wlcodex.main as main_mod
    from wlcodex.claude_binary import ClaudeBinaryResolution

    captured = {}

    class FakeClaudeBackend:
        def __init__(self, config, permission_state=None):
            captured["binary"] = config.binary
            captured["config"] = config

    def _make_fake_handlers():
        fake = MagicMock()
        fake.send_telegram = MagicMock()
        fake.edit_telegram = MagicMock()
        fake.create_interaction_renderer = MagicMock(return_value=None)
        return fake

    config_path = tmp_path / "test_auto_claude.toml"
    sqlite_path = tmp_path / "wlcodex_auto.sqlite3"
    task_dir = tmp_path / "tasks_auto"
    task_dir.mkdir(exist_ok=True)
    _write_test_config_with_path(
        config_path,
        sqlite_path,
        task_dir,
        terminal_enabled=True,
        claude_enabled=True,
        claude_binary="auto",
    )

    def fake_resolve(configured_binary: str) -> ClaudeBinaryResolution:
        captured["configured_binary"] = configured_binary
        return ClaudeBinaryResolution(
            binary=str(tmp_path / "resolved-claude"),
            source="test",
        )

    def fake_build(cfg, token, ctrl, lgr, appr, runtime_event_store=None,
                   outbox=None, terminal_manager=None,
                   execution_scheduler=None):
        fake_app = MagicMock()
        fake_app.bot = MagicMock()
        fake_app.updater = MagicMock()
        fake_app.updater.running = False
        return fake_app, _make_fake_handlers()

    monkeypatch.setattr(
        main_mod,
        "resolve_claude_binary",
        fake_resolve,
        raising=False,
    )
    monkeypatch.setattr(main_mod, "ClaudeBackend", FakeClaudeBackend)
    monkeypatch.setattr(main_mod, "build_application", fake_build)
    monkeypatch.setattr(
        _base_events.BaseEventLoop,
        "run_until_complete",
        lambda self, future: None,
    )
    monkeypatch.setattr(_asyncio, "new_event_loop", lambda: MagicMock())
    monkeypatch.setattr(_asyncio, "set_event_loop", lambda loop: None)
    monkeypatch.setattr(_sys, "argv", ["main.py", "--fake-backend", "--config", str(config_path)])
    monkeypatch.setenv("WLCODEX_TELEGRAM_BOT_TOKEN", "test-main-composition-token")

    main_mod.main()

    assert captured["configured_binary"] == "auto"
    assert captured["binary"] == str(tmp_path / "resolved-claude")
```

Run the closest main composition test:

```bash
.venv/bin/python -m pytest tests/test_main_composition.py -q
```

Expected: FAIL because `wlcodex/main.py` does not resolve `"auto"` yet.

- [ ] **Step 3: Wire resolver into main**

In `wlcodex/main.py`, import:

```python
from wlcodex.claude_binary import resolve_claude_binary
```

In the `if config.claude.enabled:` block, resolve before constructing the
backend:

```python
        resolution = resolve_claude_binary(config.claude.binary)
        if resolution.warning:
            logger.warning("Claude binary resolution warning: %s", resolution.warning)
        if not resolution.binary:
            logger.error(
                "Claude binary not found. Tried: %s",
                ", ".join(resolution.attempted),
            )
        claude_backend = ClaudeBackend(ClaudeConfig(
            enabled=config.claude.enabled,
            binary=resolution.binary or config.claude.binary,
            startup_timeout_seconds=config.claude.startup_timeout_seconds,
            request_timeout_seconds=config.claude.request_timeout_seconds,
            stream_idle_timeout_seconds=config.claude.stream_idle_timeout_seconds,
            permission_mode=claude_permission_mode,
            model=config.claude.model,
            effort=config.claude.effort,
        ), permission_state=claude_permission_state)
        logger.info(
            "Claude backend enabled (binary: %s, source: %s, model: %s, effort: %s, permission: %s)",
            resolution.binary or config.claude.binary,
            resolution.source,
            config.claude.model,
            config.claude.effort,
            claude_permission_label(claude_permission_mode),
        )
```

Keep the backend enabled even when resolution fails. This preserves the current
error path: `ClaudeBackend` will produce a user-facing "binary not found"
result when invoked.

- [ ] **Step 4: Verify Task 3 passes**

Run:

```bash
.venv/bin/python -m pytest tests/test_main_composition.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add wlcodex/main.py tests/test_main_composition.py
git commit -m "fix: resolve claude binary during startup"
```

## Task 4: Capability-Aware ClaudeBackend Arguments

**Files:**
- Modify: `wlcodex/claude_backend.py`
- Modify: `tests/test_claude_backend.py`

- [ ] **Step 1: Run impact analysis**

Run:

```text
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "ClaudeBackend",
  "file_path": "wlcodex/claude_backend.py",
  "kind": "Class",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "_prompt_args",
  "file_path": "wlcodex/claude_backend.py",
  "kind": "Function",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "send_terminal_input",
  "file_path": "wlcodex/claude_backend.py",
  "kind": "Function",
  "direction": "upstream"
})
```

Expected: LOW. Stop if HIGH or CRITICAL.

- [ ] **Step 2: Add failing tests for unsupported optional flags**

Append to `tests/test_claude_backend.py`:

```python
def test_prompt_args_skip_unsupported_optional_flags() -> None:
    from wlcodex.claude_binary import ClaudeCliCapabilities
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig

    backend = ClaudeBackend(
        ClaudeConfig(
            enabled=True,
            model="deepseek-v4-pro",
            effort="max",
        )
    )
    backend._cli_capabilities = ClaudeCliCapabilities.minimal()
    backend._hook_events_supported = False

    args = backend._prompt_args("hello", stream_json=True)

    assert args == ["-p", "hello"]


def test_prompt_args_include_supported_optional_flags() -> None:
    from wlcodex.claude_binary import ClaudeCliCapabilities
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig

    backend = ClaudeBackend(
        ClaudeConfig(
            enabled=True,
            model="deepseek-v4-pro",
            effort="max",
        )
    )
    backend._cli_capabilities = ClaudeCliCapabilities(
        print_prompt=True,
        output_format=True,
        stream_json_output=True,
        include_partial_messages=True,
        include_hook_events=True,
        input_stream_json=True,
        permission_mode=True,
        model=True,
        effort=True,
        resume=True,
    )
    backend._hook_events_supported = True

    args = backend._prompt_args("hello", stream_json=True)

    assert "--permission-mode" in args
    assert "--model" in args
    assert "--effort" in args
    assert "--output-format" in args
    assert "stream-json" in args
    assert "--include-partial-messages" in args
    assert "--include-hook-events" in args
```

Run:

```bash
.venv/bin/python -m pytest tests/test_claude_backend.py::test_prompt_args_skip_unsupported_optional_flags tests/test_claude_backend.py::test_prompt_args_include_supported_optional_flags -q
```

Expected: FAIL because `_cli_capabilities` is not used.

- [ ] **Step 3: Add failing terminal resume guard test**

Append:

```python
@pytest.mark.asyncio
async def test_send_terminal_input_fails_clearly_when_resume_unsupported(
    tmp_path: Path,
) -> None:
    from wlcodex.claude_binary import ClaudeCliCapabilities
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig

    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_claude.chmod(0o755)

    backend = ClaudeBackend(ClaudeConfig(enabled=True, binary=str(fake_claude)))
    backend._cli_capabilities = ClaudeCliCapabilities.minimal()

    with pytest.raises(RuntimeError, match="does not support --resume"):
        await backend.send_terminal_input("session-1", "continue")
```

Run:

```bash
.venv/bin/python -m pytest tests/test_claude_backend.py::test_send_terminal_input_fails_clearly_when_resume_unsupported -q
```

Expected: FAIL because `send_terminal_input()` does not guard `--resume`.

- [ ] **Step 4: Import capabilities into backend**

In `wlcodex/claude_backend.py`, add:

```python
from wlcodex.claude_binary import ClaudeCliCapabilities, probe_claude_capabilities
```

In `ClaudeBackend.__init__`, add:

```python
        self._cli_capabilities: ClaudeCliCapabilities | None = None
```

Add helper methods to `ClaudeBackend`:

```python
    async def _probe_cli_capabilities(self) -> ClaudeCliCapabilities:
        if self._cli_capabilities is None:
            self._cli_capabilities = await probe_claude_capabilities(
                self._config.binary,
            )
            self._hook_events_supported = self._cli_capabilities.include_hook_events
        return self._cli_capabilities

    def _capabilities_for_args(self) -> ClaudeCliCapabilities:
        return self._cli_capabilities or ClaudeCliCapabilities(
            print_prompt=True,
            output_format=True,
            stream_json_output=True,
            include_partial_messages=True,
            include_hook_events=bool(self._hook_events_supported),
            input_stream_json=True,
            permission_mode=True,
            model=True,
            effort=True,
            resume=True,
        )
```

- [ ] **Step 5: Probe before subprocess launches**

In `send()`, before `create_subprocess_exec`, add:

```python
            await self._probe_cli_capabilities()
```

In `send_streaming()`, replace:

```python
        await self._probe_hook_events()
```

with:

```python
        await self._probe_cli_capabilities()
```

In `send_terminal_input()`, after the enabled check, add:

```python
        capabilities = await self._probe_cli_capabilities()
        if not capabilities.resume:
            raise RuntimeError(
                "Claude CLI does not support --resume; update Claude Code or run a new Claude task first."
            )
```

- [ ] **Step 6: Build `_prompt_args()` from capabilities**

Replace `_prompt_args()` with:

```python
    def _prompt_args(self, prompt: str, *, stream_json: bool = False) -> list[str]:
        capabilities = self._capabilities_for_args()
        args = ["-p", prompt]
        if capabilities.permission_mode:
            args.extend([
                "--permission-mode",
                normalize_claude_permission_mode(self.permission_mode),
            ])
        if self._config.model and capabilities.model:
            args.extend(["--model", normalize_claude_model_name(self._config.model)])
        if self._config.effort and capabilities.effort:
            args.extend(["--effort", self._config.effort])
        if stream_json and capabilities.output_format and capabilities.stream_json_output:
            args.extend([
                "--output-format",
                "stream-json",
                "--verbose",
            ])
            if capabilities.include_partial_messages:
                args.append("--include-partial-messages")
            if self._hook_events_supported and capabilities.include_hook_events:
                args.append("--include-hook-events")
        return args
```

- [ ] **Step 7: Build terminal resume args from capabilities**

In `send_terminal_input()`, replace the fixed `resume_args` construction with:

```python
        resume_args = ["--resume", session_id, "-p", text]
        if capabilities.output_format and capabilities.stream_json_output:
            resume_args.extend(["--output-format", "stream-json", "--verbose"])
            if capabilities.include_partial_messages:
                resume_args.append("--include-partial-messages")
        if capabilities.permission_mode:
            resume_args.extend([
                "--permission-mode",
                normalize_claude_permission_mode(self.permission_mode),
            ])
        if self._config.model and capabilities.model:
            resume_args.extend(["--model", normalize_claude_model_name(self._config.model)])
        if self._config.effort and capabilities.effort:
            resume_args.extend(["--effort", self._config.effort])
```

- [ ] **Step 8: Keep `_probe_hook_events()` backward compatible**

Replace `_probe_hook_events()` internals with:

```python
        if self._hook_events_supported is not None:
            return self._hook_events_supported

        capabilities = await self._probe_cli_capabilities()
        supported = capabilities.include_hook_events
        self._hook_events_supported = supported

        if not supported and self._runtime_source is not None:
            try:
                from wlcodex.claude_runtime_source import ClaudeRuntimeSource
                if isinstance(self._runtime_source, ClaudeRuntimeSource):
                    self._runtime_source.emit_capability_missing(
                        "include-hook-events"
                    )
            except Exception:
                logger.debug("Failed to emit capability missing event", exc_info=True)

        return supported
```

- [ ] **Step 9: Verify Task 4 passes**

Run:

```bash
.venv/bin/python -m pytest tests/test_claude_backend.py -q
```

Expected: PASS.

- [ ] **Step 10: Commit Task 4**

```bash
git add wlcodex/claude_backend.py tests/test_claude_backend.py
git commit -m "fix: adapt claude cli args to capabilities"
```

## Task 5: Final Verification And Change Impact

**Files:**
- No new production files beyond Tasks 1-4.
- Optional doc update: `README.md` only if the existing Claude setup section still tells users to pin a versioned extension binary.

- [ ] **Step 1: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_claude_binary.py tests/test_claude_backend.py tests/test_config.py tests/test_main_composition.py -q
```

Expected: PASS.

- [ ] **Step 2: Run broader Telegram/Claude integration-adjacent tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_surface_commands.py tests/test_terminal_surface.py tests/test_dual_surface_integration.py -q
```

Expected: PASS. If failures are unrelated pre-existing failures, capture the
exact failing test names and traceback summary before deciding whether to
expand the fix.

- [ ] **Step 3: Run GitNexus change detection**

Run:

```text
mcp__gitnexus__.detect_changes({
  "repo": "wlcodex",
  "scope": "all"
})
```

Expected affected scope: Claude binary resolution, Claude backend argument
construction, config loading, and main composition. Stop and inspect if it
reports unrelated execution flows.

- [ ] **Step 4: Manual local smoke**

Run the currently configured local binary resolution path from Python:

```bash
.venv/bin/python -c "from wlcodex.config import load_config; from wlcodex.claude_binary import resolve_claude_binary; c=load_config('config/wlcodex.toml'); r=resolve_claude_binary(c.claude.binary); print(r)"
```

Expected: `ClaudeBinaryResolution(binary='...', source='path' or
`source='vscode-extension'`, ...)` with a non-empty `binary`.

Then run:

```bash
.venv/bin/python -c "import asyncio; from wlcodex.config import load_config; from wlcodex.claude_binary import resolve_claude_binary, probe_claude_capabilities; c=load_config('config/wlcodex.toml'); r=resolve_claude_binary(c.claude.binary); print(asyncio.run(probe_claude_capabilities(r.binary)))"
```

Expected: printed `ClaudeCliCapabilities(...)` with `print_prompt=True`.

- [ ] **Step 5: Optional live Telegram smoke**

Only run this if the environment has a valid Telegram token and the operator
wants a real smoke:

```bash
WLCODEX_RUN_TELEGRAM_LIVE=1 .venv/bin/python -m pytest tests/test_live_telegram_smoke.py -q
```

Expected: PASS.

- [ ] **Step 6: Final commit**

```bash
git status --short
git add wlcodex config tests docs README.md
git commit -m "fix: make claude cli integration resilient"
```

## Rollback Plan

If the resolver causes an unexpected production issue:

1. Set `WLCODEX_CLAUDE_BINARY=/absolute/path/to/known/good/claude`.
2. Restart the WLCodex service.
3. If needed, temporarily set `config/wlcodex.toml` back to the known absolute
   path.
4. Keep the capability-aware argument code in place unless tests show it is the
   source of the failure.

## Completion Evidence

Collect these lines in the final implementation report:

- Resolved Claude binary path and source from the manual smoke.
- Capability matrix from `probe_claude_capabilities`.
- Focused pytest command and PASS result.
- Broader pytest command and PASS result, or exact unrelated failures.
- GitNexus `detect_changes` affected scope.
- Confirmation that `config/wlcodex.toml` no longer pins a VS Code extension
  versioned binary path.
