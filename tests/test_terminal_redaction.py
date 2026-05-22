"""Tests for wlcodex.surfaces.terminal.redaction — secret scrubbing."""

import pytest

from wlcodex.surfaces.terminal.redaction import redact_terminal_text, redact_and_cap_frame


# ── Known secret names ─────────────────────────────────────────────────────

def test_redacts_known_secret_names():
    text = "TELEGRAM_BOT_TOKEN=123\nOPENAI_API_KEY=sk-test"

    redacted = redact_terminal_text(text)

    assert "123" not in redacted
    assert "sk-test" not in redacted
    assert "TELEGRAM_BOT_TOKEN=<redacted>" in redacted
    assert "OPENAI_API_KEY=<redacted>" in redacted


def test_redacts_anthropic_api_key():
    text = "export ANTHROPIC_API_KEY=sk-ant-abc123xyz"
    redacted = redact_terminal_text(text)
    assert "sk-ant-abc123xyz" not in redacted
    assert "ANTHROPIC_API_KEY=<redacted>" in redacted


def test_redacts_claude_code_oauth_token():
    text = "CLAUDE_CODE_OAUTH_TOKEN=ya29.secretvalue"
    redacted = redact_terminal_text(text)
    assert "ya29.secretvalue" not in redacted
    assert "CLAUDE_CODE_OAUTH_TOKEN=<redacted>" in redacted


def test_redacts_wlcodex_telegram_bot_token():
    text = "WLCODEX_TELEGRAM_BOT_TOKEN=999:abcdefgh"
    redacted = redact_terminal_text(text)
    assert "999:abcdefgh" not in redacted
    assert "WLCODEX_TELEGRAM_BOT_TOKEN=<redacted>" in redacted


def test_redacts_all_secrets_in_multiline_text():
    text = (
        "env:\n"
        "  TELEGRAM_BOT_TOKEN=bot123\n"
        "  OPENAI_API_KEY=sk-openai-456\n"
        "  ANTHROPIC_API_KEY=sk-ant-anthropic-789\n"
        "  CLAUDE_CODE_OAUTH_TOKEN=oauth-secret\n"
        "  WLCODEX_TELEGRAM_BOT_TOKEN=wl-secret\n"
    )
    redacted = redact_terminal_text(text)

    assert "bot123" not in redacted
    assert "sk-openai-456" not in redacted
    assert "sk-ant-anthropic-789" not in redacted
    assert "oauth-secret" not in redacted
    assert "wl-secret" not in redacted

    assert "TELEGRAM_BOT_TOKEN=<redacted>" in redacted
    assert "OPENAI_API_KEY=<redacted>" in redacted
    assert "ANTHROPIC_API_KEY=<redacted>" in redacted
    assert "CLAUDE_CODE_OAUTH_TOKEN=<redacted>" in redacted
    assert "WLCODEX_TELEGRAM_BOT_TOKEN=<redacted>" in redacted


def test_non_secret_text_passes_through_unchanged():
    text = "Running pytest -q\ntests passed\nno secrets here"
    redacted = redact_terminal_text(text)
    assert redacted == text


def test_redaction_preserves_text_structure():
    text = "line1\nTELEGRAM_BOT_TOKEN=abc\nline3\nOPENAI_API_KEY=xyz\nline5"
    redacted = redact_terminal_text(text)
    lines = redacted.split("\n")
    assert lines[0] == "line1"
    assert "TELEGRAM_BOT_TOKEN=<redacted>" in lines[1]
    assert lines[2] == "line3"
    assert "OPENAI_API_KEY=<redacted>" in lines[3]
    assert lines[4] == "line5"


def test_redact_terminal_text_handles_empty_string():
    assert redact_terminal_text("") == ""


def test_redact_terminal_text_handles_edge_case_no_equals():
    text = "TELEGRAM_BOT_TOKEN is set\nno value here"
    redacted = redact_terminal_text(text)
    assert "TELEGRAM_BOT_TOKEN" in redacted
    assert redacted == text  # no "=", no redaction


def test_redact_terminal_text_case_sensitive():
    text = "telegram_bot_token=abc"
    redacted = redact_terminal_text(text)
    assert "abc" in redacted  # not redacted — case sensitive


def test_redact_only_on_key_equals_value_pattern():
    line = "some prefix TELEGRAM_BOT_TOKEN=secret123 and more"
    redacted = redact_terminal_text(line)
    assert "secret123" not in redacted
    assert "TELEGRAM_BOT_TOKEN=<redacted>" in redacted


# ── redact_and_cap_frame ────────────────────────────────────────────────────


def test_redact_and_cap_frame_no_change_for_short_text():
    assert redact_and_cap_frame("hello world") == "hello world"


def test_redact_and_cap_frame_redacts_secrets():
    text = "TELEGRAM_BOT_TOKEN=secret123 output"
    result = redact_and_cap_frame(text)
    assert "secret123" not in result
    assert "<redacted>" in result


def test_redact_and_cap_frame_truncates_long_text():
    text = "a" * 5000
    result = redact_and_cap_frame(text, max_chars=3900)
    assert len(result) <= 3900
    assert "截断" in result
    assert "/terminal tail" in result


def test_redact_and_cap_frame_no_truncation_for_short_text():
    text = "short text"
    result = redact_and_cap_frame(text, max_chars=3900)
    assert result == "short text"


def test_redact_and_cap_frame_disabled_redaction():
    text = "ANTHROPIC_API_KEY=sk-test-key output"
    result = redact_and_cap_frame(text, redaction_enabled=False)
    assert "sk-test-key" in result


def test_redact_and_cap_frame_combined_redact_and_cap():
    text = "TELEGRAM_BOT_TOKEN=abc123 " + "x" * 5000
    result = redact_and_cap_frame(text, max_chars=3900)
    assert "abc123" not in result
    assert len(result) <= 3900
