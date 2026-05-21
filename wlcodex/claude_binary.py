"""Claude CLI binary discovery and capability probing."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


_CLAUDE_ENV_DENY_LIST: tuple[str, ...] = (
    "WLCODEX_TELEGRAM_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_API_TOKEN",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "WLC_CHAT_ID",
    "WLCODEX_CHAT_ID",
)

_CLAUDE_ENV_DENY_SUBSTRINGS: tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_API_TOKEN",
)


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
    probe_error: str = ""

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
    env: Mapping[str, str] | None = None,
) -> ClaudeCliCapabilities:
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "--help",
            env=sanitized_claude_env(env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return ClaudeCliCapabilities(probe_error="binary_not_found")
    except Exception:
        return ClaudeCliCapabilities.minimal()

    text = ""
    if stdout:
        text += stdout.decode("utf-8", errors="replace")
    if stderr:
        text += "\n" + stderr.decode("utf-8", errors="replace")
    caps = parse_claude_help(text)
    return caps if caps.print_prompt else ClaudeCliCapabilities.minimal()


def sanitized_claude_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a Claude subprocess environment without Telegram delivery secrets."""
    source = env if env is not None else os.environ
    result = dict(source)
    for key in list(result.keys()):
        if key in _CLAUDE_ENV_DENY_LIST:
            del result[key]
            continue
        for sub in _CLAUDE_ENV_DENY_SUBSTRINGS:
            if sub in key:
                del result[key]
                break
    return result


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
