# WLCodex Claude Binary Resilience Design

## Decision

WLCodex should stop depending on a versioned Claude Code binary path from the
VS Code extension and introduce a small Claude CLI compatibility layer:

```text
config / environment
  -> Claude binary resolver
       WLCODEX_CLAUDE_BINARY
       configured binary or "auto"
       PATH lookup
       latest VS Code Claude extension fallback
  -> Claude CLI capability probe
       parse `claude --help`
       pass only supported optional flags
  -> existing ClaudeBackend stream-json adapter
```

The first implementation should keep the current subprocess backend. It should
make the binary location stable, degrade optional flags by capability, and
surface actionable diagnostics when Claude cannot be started.

## Source Evidence

### Existing WLCodex Evidence

- `config/wlcodex.toml` currently pins Claude to
  `/home/wl/.vscode/extensions/anthropic.claude-code-2.1.145-linux-x64/resources/native-binary/claude`.
  This path includes the extension version and breaks when the extension path
  changes.
- `wlcodex/claude_backend.py` runs Claude directly through
  `asyncio.create_subprocess_exec` and currently builds arguments in
  `ClaudeBackend._prompt_args()`.
- `ClaudeBackend.send_streaming()` already parses non-JSON lines as text through
  `parse_line()`, so losing `stream-json` can degrade to a coarser text stream
  instead of hard failing.
- `ClaudeBackend._probe_hook_events()` already demonstrates the right pattern:
  probe a capability from `claude --help`, cache the result, and emit a missing
  capability event when useful.
- GitNexus impact analysis on 2026-05-21 reported LOW risk for:
  `ClaudeBackend`, `ClaudeBackend._prompt_args`,
  `ClaudeBackend.send_terminal_input`, and `config.ClaudeConfig`.

### Official Reference Evidence

- Claude Code CLI documents `-p/--print`, `--output-format stream-json`,
  `--include-partial-messages`, `--input-format stream-json`, `--resume`,
  `--permission-mode`, `--model`, `--effort`, and `--include-hook-events`.
  Source: https://code.claude.com/docs/en/cli-usage
- Claude Code settings document update controls such as `autoUpdatesChannel`
  and the `DISABLE_AUTOUPDATER` environment variable. These reduce update
  churn, but they do not remove WLCodex's need for a stable binary resolver.
  Source: https://code.claude.com/docs/en/settings
- Claude Code Remote Control and Channels provide official remote interfaces,
  including Telegram Channels. Those are useful future directions, but they do
  not preserve WLCodex's current Codex -> Claude -> Codex orchestration by
  themselves.
  Sources:
  https://code.claude.com/docs/en/remote-control and
  https://code.claude.com/docs/en/channels
- Claude Agent SDK is the official SDK path for programmatic agent loops. It is
  a candidate future backend, not the minimum fix for the current broken binary
  path.
  Source: https://platform.claude.com/docs/zh-CN/agent-sdk/overview

### Telegram Claude Bridge Evidence

- CCGram uses Claude hooks and Telegram to handle approvals, prompts, and
  session notifications. The reusable idea is event/capability isolation rather
  than relying on one raw CLI shape forever.
  Source: https://github.com/jsayubi/ccgram
- `claude-telegram` invokes Claude CLI with `stream-json` and parses the stream
  for Telegram updates. This validates the current WLCodex approach, but also
  shows why a compatibility layer is needed around CLI invocation.
  Source: https://avaleriani.github.io/claude-telegram/
- HeyAgent abstracts provider configuration for Claude Code and Codex over
  Telegram. The reusable idea is a provider adapter boundary and diagnostics.
  Source: https://github.com/gergomiklos/heyagent

## Goals

1. Make Claude usable after VS Code extension updates without manually editing
   `config/wlcodex.toml`.
2. Preserve the current Claude subprocess backend, stream parser, runtime event
   source, terminal resume path, and Codex -> Claude -> Codex workflow.
3. Support an explicit operator override through `WLCODEX_CLAUDE_BINARY`.
4. Support `binary = "auto"` in config and examples.
5. Repair stale VS Code extension binary paths by scanning installed
   `anthropic.claude-code-*` extension directories and selecting the newest
   executable Claude binary.
6. Probe CLI capabilities once per backend instance and pass optional flags only
   when supported.
7. Degrade streaming to raw text when `--output-format stream-json` is not
   supported instead of failing before Claude can answer.
8. Fail clearly when required behavior is unavailable, especially terminal
   `--resume`.
9. Keep all diagnostics local and avoid exposing Telegram secrets to Claude.

## Non-Goals

- Do not replace WLCodex with Claude's official Telegram Channels.
- Do not replace the backend with Claude Agent SDK in this repair.
- Do not install, update, or downgrade Claude automatically from WLCodex.
- Do not change Anthropic authentication, subscription, or provider settings.
- Do not change Codex backend behavior.
- Do not change Telegram command semantics except clearer diagnostics when
  Claude is unavailable.
- Do not remove support for explicit absolute Claude binary paths.

## User-Facing Behavior

### Normal Run

With `binary = "auto"`, WLCodex resolves Claude in this order:

1. `WLCODEX_CLAUDE_BINARY`, when set.
2. Configured binary, when it is not empty and not `"auto"`.
3. `claude` on `PATH`.
4. The newest executable
   `~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude`.

If the configured value is a missing VS Code extension binary path, WLCodex
also attempts the VS Code extension scan and uses the newest installed binary.
The log should say that the configured path was stale and name the replacement
path.

### Failure Message

When no Claude binary can be resolved, Claude-only and orchestration flows
should fail with a concise message:

```text
Claude binary not found.
Tried: WLCODEX_CLAUDE_BINARY, configured binary, PATH claude, VS Code extension scan.
Set WLCODEX_CLAUDE_BINARY or install Claude Code CLI.
```

The existing Telegram display can render this through the current backend error
path. No new Telegram command is required.

### Capability Degradation

The backend should parse `claude --help` and cache support for these flags:

| Capability | Used by | Behavior when missing |
| --- | --- | --- |
| `-p` / `--print` | all non-interactive calls | fail, because WLCodex cannot run this backend |
| `--output-format` with `stream-json` | streaming output | skip stream-json flags and parse raw stdout as text |
| `--include-partial-messages` | lower-latency streaming | skip |
| `--include-hook-events` | runtime tool/hook visibility | skip and emit existing capability-missing event |
| `--permission-mode` | permission policy | skip and warn in logs/runtime payload |
| `--model` | configured model | skip and warn in logs/runtime payload |
| `--effort` | configured effort | skip and warn in logs/runtime payload |
| `--resume` | Onsite/terminal continuation | fail clearly for terminal input |

## Architecture

### `wlcodex/claude_binary.py`

Create a focused helper module. It owns binary discovery and CLI help parsing.

Public objects:

```python
@dataclass(frozen=True)
class ClaudeBinaryResolution:
    binary: str
    source: str
    warning: str = ""
    attempted: tuple[str, ...] = ()

@dataclass(frozen=True)
class ClaudeCliCapabilities:
    print_prompt: bool
    output_format: bool
    stream_json_output: bool
    include_partial_messages: bool
    include_hook_events: bool
    input_stream_json: bool
    permission_mode: bool
    model: bool
    effort: bool
    resume: bool

def resolve_claude_binary(config_binary: str) -> ClaudeBinaryResolution
def parse_claude_help(help_text: str) -> ClaudeCliCapabilities
async def probe_claude_capabilities(binary: str, timeout_seconds: float = 5.0) -> ClaudeCliCapabilities
```

The resolver should avoid side effects. It should not create symlinks, mutate
config, or run update commands.

### Config

`ClaudeConfig.binary` should default to `"auto"` in both `wlcodex/config.py`
and `wlcodex/claude_backend.py`. `config/wlcodex.example.toml` and the local
`config/wlcodex.toml` should use:

```toml
binary = "auto"
```

The local config can still be overridden with:

```bash
WLCODEX_CLAUDE_BINARY=/absolute/path/to/claude
```

### Main Composition

`wlcodex/main.py` should resolve the binary before constructing
`ClaudeBackend`. The backend should receive the resolved executable path, not
the literal `"auto"` value.

The startup log should include:

```text
Claude backend enabled (binary: <resolved>, source: <source>, model: <model>, effort: <effort>, permission: <label>)
```

If resolution includes a warning, log it at warning level.

### Backend Capability Cache

`ClaudeBackend` should hold one cached capability object:

```python
self._cli_capabilities: ClaudeCliCapabilities | None = None
```

Before launching Claude in `send()`, `send_streaming()`, and
`send_terminal_input()`, it should probe capabilities if not cached. Argument
construction should inspect that cached object and skip unsupported optional
flags.

`_probe_hook_events()` can be kept for compatibility with existing tests, but
should delegate to the unified capability probe and set
`_hook_events_supported` from `capabilities.include_hook_events`.

### Streaming Fallback

When `stream_json=True` and `stream_json_output` is unavailable, `_prompt_args`
should omit:

```text
--output-format stream-json --verbose --include-partial-messages --include-hook-events
```

`send_streaming()` should still read stdout line by line and call `parse_line`.
Invalid JSON lines already become visible text events, which is the desired
degraded behavior.

### Terminal Resume Guard

`send_terminal_input()` depends on `--resume`. If the capability probe reports
that `--resume` is unavailable, it should raise:

```text
Claude CLI does not support --resume; update Claude Code or run a new Claude task first.
```

This is better than launching an unsupported command and returning an opaque CLI
usage error.

## Impact And Risk

GitNexus impact analysis on 2026-05-21:

| Target | Risk | Direct upstream impact |
| --- | --- | --- |
| `ClaudeBackend` | LOW | 0 |
| `ClaudeBackend._prompt_args` | LOW | 0 |
| `ClaudeBackend.send_terminal_input` | LOW | 0 |
| `config.ClaudeConfig` | LOW | `wlcodex/main.py` import |

Main risk is behavioral drift in Claude invocation arguments. Mitigation:

- Pure unit tests for resolver and help parsing.
- Fake-Claude subprocess tests for argument construction.
- Regression tests proving raw-text fallback still emits text.
- Regression tests proving missing `--resume` fails clearly before spawning.
- Final GitNexus `detect_changes(scope="all")` before commit.

## Acceptance Criteria

1. Updating VS Code Claude extension no longer requires editing
   `config/wlcodex.toml` when `binary = "auto"`.
2. A stale configured VS Code extension binary path is repaired to the newest
   installed extension binary when one exists.
3. `WLCODEX_CLAUDE_BINARY` overrides config.
4. Missing binary returns an actionable error listing attempted strategies.
5. Unsupported optional CLI flags are not passed.
6. Missing `--output-format stream-json` degrades to raw text streaming.
7. Missing `--resume` fails terminal input with a clear message.
8. Existing `tests/test_claude_backend.py` and `tests/test_config.py` pass after
   expected assertion updates.
9. No Telegram delivery secrets are added to Claude's subprocess environment.

## Future Work

- Add an optional Claude Agent SDK backend after the CLI backend is stable.
- Add an operator command or health card showing the resolved Claude binary,
  source, version, and capability matrix.
- Evaluate official Claude Channels as an alternate product surface, not as a
  replacement for the WLCodex orchestration path.
