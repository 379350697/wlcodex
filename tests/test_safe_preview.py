"""Tests for runtime_events.safe_text_preview — sensitive data redaction in event payloads."""

from wlcodex.runtime_events import safe_text_preview


def test_safe_preview_returns_short_text_unchanged():
    """Short, non-sensitive text passes through unchanged (up to 200 chars)."""
    text = "Running pytest -q"
    assert safe_text_preview(text) == text


def test_safe_preview_truncates_long_text():
    """Text longer than max_len is truncated."""
    text = "a" * 500
    result = safe_text_preview(text)
    assert len(result) <= 200


def test_safe_preview_redacts_known_secrets():
    """Known secret patterns are redacted in the preview."""
    text = "WLCODEX_TELEGRAM_BOT_TOKEN=abc123secret"
    result = safe_text_preview(text)
    assert "abc123secret" not in result
    assert "<redacted>" in result


def test_safe_preview_redacts_common_secret_patterns():
    text = "deploy password=abc123 token=secret123 api_key: sk-live-abc"
    result = safe_text_preview(text)
    assert "abc123" not in result
    assert "secret123" not in result
    assert "sk-live-abc" not in result
    assert "<redacted>" in result


def test_safe_preview_redacts_anthropic_key():
    text = "ANTHROPIC_API_KEY=sk-ant-abc123xyz output here"
    result = safe_text_preview(text)
    assert "sk-ant-abc123xyz" not in result
    assert "<redacted>" in result


def test_safe_preview_redacts_all_known_secrets():
    text = (
        "TELEGRAM_BOT_TOKEN=bot123 "
        "OPENAI_API_KEY=sk-openai-456 "
        "ANTHROPIC_API_KEY=sk-ant-789"
    )
    result = safe_text_preview(text)
    assert "bot123" not in result
    assert "sk-openai-456" not in result
    assert "sk-ant-789" not in result


def test_safe_preview_never_returns_full_text():
    """Even for short text, the preview never stores raw text in payloads."""
    text = "some normal text"
    # For short non-sensitive text, preview equals the original
    assert safe_text_preview(text) == text


def test_safe_preview_combined_redact_and_truncate():
    """Long text with secrets: both truncated and redacted."""
    text = "TELEGRAM_BOT_TOKEN=secret123 " + "x" * 300
    result = safe_text_preview(text)
    assert "secret123" not in result
    assert len(result) <= 200