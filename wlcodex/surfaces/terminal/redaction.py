"""Terminal frame redaction — scrubs configured secrets from terminal output.

All terminal frames sent to Telegram MUST pass through redaction.
The design spec requires at minimum redaction of:
    TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, ANTHROPIC_API_KEY,
    CLAUDE_CODE_OAUTH_TOKEN, WLCODEX_TELEGRAM_BOT_TOKEN
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


def redact_terminal_text(text: str) -> str:
    """Replace known secret-name=value patterns with key=<redacted>.

    Returns the scrubbed text. A no-op when no secrets are present.
    """
    return _REDACTION_RE.sub(r"\1=<redacted>", text)
