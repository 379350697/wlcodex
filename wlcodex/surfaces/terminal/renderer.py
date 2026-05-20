"""Terminal frame renderer — turns frames and decisions into user-facing text.

Onsite language: the renderer produces user-facing "现场" headers
and start-card copy, while preserving raw terminal-style output for frames.
"""

_AGENT_LABEL = {"claude": "Claude", "codex": "Codex"}


def render_terminal_frame(frame) -> str:
    """Render a single terminal frame as a human-readable string.

    Format: [agent:phase] text

    The prefix mirrors the design spec's terminal rendering examples:
        [claude:implementation] $ Write src/foo.py
        [tool] Bash(pytest -q)
        [diff] src/foo.py +12 -3
    """
    return f"[{frame.agent}:{frame.phase}] {frame.text}"


def render_onsite_header(agent: str, phase: str) -> str:
    """Render a concise Onsite status header in user-facing language."""
    return f"现场 · {agent} · {phase} · running"


def render_start_card(available_agents: tuple[str, ...] | None = None) -> str:
    """Render a no-session start card with next-action suggestions.

    If *available_agents* is given, only those agents are listed in the
    buttons.  Otherwise the default set (claude, codex) is shown.

    This must never leave the user at a dead end — always offer next steps.
    """
    agents = available_agents if available_agents else ("claude", "codex")
    button_text = " ".join(
        f"[启动 {_AGENT_LABEL.get(a, a)} 现场]" for a in agents
    )
    return (
        "当前没有可接管的现场。\n\n"
        "你可以：\n"
        f"{button_text} [回驾驶舱]"
    )
