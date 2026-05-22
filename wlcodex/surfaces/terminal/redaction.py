"""Terminal frame redaction — scrubs configured secrets from terminal output.

All terminal frames sent to Telegram MUST pass through redaction.
The design spec requires at minimum redaction of:
    TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, ANTHROPIC_API_KEY,
    CLAUDE_CODE_OAUTH_TOKEN, WLCODEX_TELEGRAM_BOT_TOKEN

Additionally, oversized output is capped with a truncation hint
pointing the user to /terminal tail for details.
"""

import re

# Ordered list — longer prefixes first so WLCODEX_TELEGRAM_BOT_TOKEN
# is tried before TELEGRAM_BOT_TOKEN.
_SECRET_KEYS: tuple[str, ...] = (
    "WLCODEX_TELEGRAM_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
)

# Compiled pattern: key=value, where value is everything up to whitespace or end-of-line.
_REDACTION_RE = re.compile(
    r"(" + "|".join(re.escape(k) for k in _SECRET_KEYS) + r")=(\S+)"
)

# Default cap for terminal frames (Telegram message limit safety margin).
DEFAULT_FRAME_CAP = 3900

_TRUNCATION_HINT = (
    "\n\n输出已截断，使用 /terminal tail 查看最近片段。"
)


def redact_terminal_text(text: str) -> str:
    """Replace known secret-name=value patterns with key=<redacted>.

    Returns the scrubbed text. A no-op when no secrets are present.
    """
    return _REDACTION_RE.sub(r"\1=<redacted>", text)


def redact_and_cap_frame(
    text: str,
    *,
    max_chars: int = DEFAULT_FRAME_CAP,
    redaction_enabled: bool = True,
) -> str:
    """Apply redaction (if enabled) then cap to max_chars.

    This is the primary entry point for terminal frame processing.
    Every terminal frame sent to Telegram must go through this function.

    Args:
        text: Raw frame text.
        max_chars: Maximum characters allowed. Default 3900 (Telegram safety).
        redaction_enabled: Whether to redact secrets. Default True.

    Returns:
        Processed text, capped and optionally redacted.
    """
    if redaction_enabled:
        text = redact_terminal_text(text)

    if len(text) <= max_chars:
        return text

    # Leave room for the truncation hint
    available = max_chars - len(_TRUNCATION_HINT)
    if available < 100:
        return text[:max_chars]

    return text[:available].rstrip() + _TRUNCATION_HINT
