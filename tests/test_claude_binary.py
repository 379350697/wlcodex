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
  --output-format <format>   Output format (text, json, stream-json)
  --include-partial-messages
  --include-hook-events
  --input-format <format>    Input format (text, stream-json)
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
