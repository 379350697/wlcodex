"""Terminal frame renderer — turns a TerminalFrame into a Telegram-safe string."""


def render_terminal_frame(frame) -> str:
    """Render a single terminal frame as a human-readable string.

    Format: [agent:phase] text

    The prefix mirrors the design spec's terminal rendering examples:
        [claude:implementation] $ Write src/foo.py
        [tool] Bash(pytest -q)
        [diff] src/foo.py +12 -3
    """
    return f"[{frame.agent}:{frame.phase}] {frame.text}"
