"""Terminal frame renderer — turns frames and decisions into user-facing text.

Onsite language: the renderer produces user-facing "现场" headers
and start-card copy, while preserving raw terminal-style output for frames.

Key guarantees:
  - Every frame is redacted (secrets removed) before rendering.
  - Every frame is capped to max_frame_chars (configurable, default 3500).
  - Oversized output gets a truncation hint pointing to /terminal tail.
  - Tail output renders recent frames in reverse-chronological order.
"""

from __future__ import annotations

from wlcodex.surfaces.terminal.models import TerminalFrame
from wlcodex.surfaces.terminal.redaction import redact_terminal_text
from wlcodex.surfaces.core.models import TerminalPolicy

_AGENT_LABEL = {"claude": "Claude", "codex": "Codex"}

_TRUNCATION_HINT = "\n\n输出已截断，使用 /terminal tail 查看最近片段。"

_PAUSE_CONFIRMATION = "现场推送已暂停。任务仍在运行。\n使用 /terminal tail 恢复查看。"

_RESUME_CONFIRMATION = "现场推送已恢复。"

_DETACH_CONFIRMATION = "已离开现场，会话仍在运行。\n使用 /terminal 重新接入。"

_RETURN_COCKPIT = "已回到驾驶舱。现场会话保留。"

_NO_SESSION_HINT = "当前没有可接管的现场。"


def render_terminal_frame(
    frame: TerminalFrame,
    *,
    policy: TerminalPolicy | None = None,
) -> str:
    """Render a single terminal frame as a human-readable string.

    Applies redaction (if enabled) and cap (always) before rendering.
    Format: [agent:phase] text
    """
    if policy is None:
        policy = TerminalPolicy()

    text = frame.text

    # Step 1: redact secrets (always applied when enabled)
    if policy.redaction_enabled:
        text = redact_terminal_text(text)

    # Step 2: cap frame to max_frame_chars
    text = _cap_frame(text, policy.max_frame_chars)

    agent_label = _AGENT_LABEL.get(frame.agent, frame.agent)
    return f"[{agent_label}:{frame.phase}] {text}"


def render_onsite_header(agent: str, phase: str) -> str:
    """Render a concise Onsite status header in user-facing language."""
    agent_label = _AGENT_LABEL.get(agent, agent)
    return f"现场 · {agent_label} · {phase} · running"


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


def render_tail_output(
    frames: list[TerminalFrame],
    *,
    policy: TerminalPolicy | None = None,
    limit: int = 20,
    max_total_chars: int = 3900,
) -> str:
    """Render the most recent *limit* frames for /terminal tail.

    Returns a single string with frames separated by newlines.
    Oldest first (tail output is in chronological order).

    *max_total_chars* caps the total output to fit within a single
    Telegram message (4096 chars hard limit, 3900 default with margin).
    When the output exceeds this cap, older frames are dropped first
    so the user always sees the most recent content.
    """
    if not frames:
        return "现场暂无输出。"

    if policy is None:
        policy = TerminalPolicy()

    shown = frames[-limit:] if len(frames) > limit else frames
    rendered: list[str] = []
    total_len = 0

    # Build from newest to oldest, then reverse, so we keep recent frames.
    for frame in reversed(shown):
        line = render_terminal_frame(frame, policy=policy)
        if rendered:
            # Account for newline separator
            total_len += 1 + len(line)
        else:
            total_len = len(line)

        if total_len > max_total_chars and rendered:
            # Adding this frame would exceed the cap — stop adding older frames.
            break
        rendered.append(line)

    # Reverse back to chronological order (oldest first)
    rendered.reverse()

    if not rendered:
        # Even a single frame exceeds max_total_chars — return the newest
        # frame capped to max_total_chars.
        newest = render_terminal_frame(shown[-1], policy=policy)
        return newest[:max_total_chars]

    output = "\n".join(rendered)
    if len(output) <= max_total_chars:
        return output

    # Final safety net: hard-cap the concatenated result.
    return output[:max_total_chars]


def render_pause_confirmation() -> str:
    """Confirmation message after /terminal pause."""
    return _PAUSE_CONFIRMATION


def render_resume_confirmation() -> str:
    """Confirmation message after resuming from pause."""
    return _RESUME_CONFIRMATION


def render_detach_confirmation() -> str:
    """Confirmation message after /terminal detach."""
    return _DETACH_CONFIRMATION


def render_return_to_cockpit() -> str:
    """Confirmation message when switching back to product/cockpit mode."""
    return _RETURN_COCKPIT


def render_no_session_hint() -> str:
    """Hint shown when user sends text in terminal mode with no session."""
    return _NO_SESSION_HINT


def render_busy_selector(running_agent: str | None = None) -> str:
    """Render a message when workspace is busy with a running agent."""
    if running_agent:
        agent_label = _AGENT_LABEL.get(running_agent, running_agent)
        return f"工作区正在运行 {agent_label}，请等待完成或使用 /stop 停止。"
    return "工作区正在运行任务，请等待完成或使用 /stop 停止。"


def _cap_frame(text: str, max_chars: int) -> str:
    """Cap frame text to max_chars, appending truncation hint if cut."""
    if len(text) <= max_chars:
        return text
    # Leave room for the hint
    available = max_chars - len(_TRUNCATION_HINT)
    if available < 100:
        # Extremely small max_chars — just truncate hard
        return text[:max_chars]
    return text[:available].rstrip() + _TRUNCATION_HINT