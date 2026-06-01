from __future__ import annotations

from pathlib import Path

import pytest

from wlcodex.claude_binary import (
    ClaudeCliCapabilities,
    parse_claude_help,
    probe_claude_capabilities,
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


def test_resolve_claude_binary_checks_local_bin_in_auto_mode(
    tmp_path: Path,
) -> None:
    local_binary = tmp_path / ".local" / "bin" / "claude"
    _make_executable(local_binary)

    result = resolve_claude_binary("auto", env={"PATH": ""}, home=tmp_path)

    assert result.binary == str(local_binary)
    assert result.source == "local-bin"
    assert "~/.local/bin/claude" in result.attempted


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


@pytest.mark.asyncio
async def test_probe_claude_capabilities_strips_telegram_secrets(
    tmp_path: Path,
) -> None:
    fake_claude = tmp_path / "fake-claude"
    marker = tmp_path / "env-marker.txt"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "from pathlib import Path\n"
        "leaked = [\n"
        "    key for key in os.environ\n"
        "    if key in {'WLCODEX_TELEGRAM_BOT_TOKEN', 'TELEGRAM_API_HASH', 'WLC_CHAT_ID'}\n"
        "    or 'TELEGRAM_BOT_TOKEN' in key\n"
        "    or 'TELEGRAM_API_TOKEN' in key\n"
        "]\n"
        "Path(os.environ['WLCODEX_PROBE_MARKER_FILE']).write_text(\n"
        "    '\\n'.join(sorted(leaked)) or 'clean', encoding='utf-8'\n"
        ")\n"
        "print('Usage: claude')\n"
        "print('  -p, --print')\n"
        "print('  --model <model>')\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    caps = await probe_claude_capabilities(
        str(fake_claude),
        env={
            "WLCODEX_TELEGRAM_BOT_TOKEN": "secret-token",
            "TELEGRAM_API_HASH": "secret-hash",
            "WLC_CHAT_ID": "123",
            "CUSTOM_TELEGRAM_BOT_TOKEN": "custom-secret",
            "WLCODEX_PROBE_MARKER_FILE": str(marker),
            "PATH": "/usr/bin:/bin",
        },
    )

    assert marker.read_text(encoding="utf-8") == "clean"
    assert caps.print_prompt is True
    assert caps.model is True


@pytest.mark.asyncio
async def test_probe_claude_capabilities_marks_missing_binary(
    tmp_path: Path,
) -> None:
    caps = await probe_claude_capabilities(str(tmp_path / "missing-claude"))

    assert caps.print_prompt is False
    assert caps.probe_error == "binary_not_found"
