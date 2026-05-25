"""Product/Cockpit surface renderer — turns runtime state into compact status cards.

The cockpit is a controlled dashboard. It shows:
  - Current phase, agent, command summary
  - Elapsed time and estimated remaining time
  - Waiting for approval with clear buttons
  - Brief completion/failure summaries
  - Explicit guidance to use terminal for detailed output

It NEVER shows:
  - Raw JSON, internal IDs
  - Full diffs or long stdout/stderr
  - Untruncated tool output
"""

from __future__ import annotations

from wlcodex.surfaces.product.events import ProductDisplayEvent
from wlcodex.surfaces.product.speaker import product_speaker_line

# ---------------------------------------------------------------------------
# Phase labels (Chinese, matching RuntimeRenderer and status.py)
# ---------------------------------------------------------------------------

_PHASE_LABELS = {
    "queued": "排队中",
    "running_analysis": "诊断工程师正在分析",
    "running_implementation": "开发工程师正在实施",
    "running_verification": "审计工程师正在验收",
    "retrying_implementation": "正在重新实施",
    "completed": "运行完成",
    "failed": "运行失败",
    "cancelled": "运行已取消",
    "waiting_for_approval": "等待审批",
}

_AGENT_LABELS = {
    "codex": "GPT 开发工程师",
    "claude": "DeepSeek 开发工程师",
}

_KIND_LABELS = {"command": "命令", "file_change": "文件修改", "tool": "工具"}

_TERMINAL_HINT = "\n详细输出可使用 /terminal tail 查看。"


def render_cockpit_status(state) -> str:
    """Render a compact cockpit status card from RuntimeRunState.

    Shows: phase, agent, current command/time summary, elapsed, ETA.
    Never shows raw JSON, internal IDs, or full output.
    """
    phase = getattr(state, "phase", "")
    agent = getattr(state, "active_agent", "")
    agent_status = getattr(state, "agent_status", "")
    is_terminal = getattr(state, "is_terminal", False)

    # Terminal states get their own rendering
    if is_terminal or phase in ("completed", "failed", "cancelled"):
        return _render_terminal_status(state)

    # Waiting for approval
    if agent_status == "waiting_for_approval":
        return _render_approval_status(state)

    lines: list[str] = []

    # Phase label
    phase_label = _PHASE_LABELS.get(phase, phase)
    agent_label = _AGENT_LABELS.get(agent, agent) if agent else ""

    # Command or detail summary
    command = _compact_line(getattr(state, "current_command", ""))
    detail = _compact_line(getattr(state, "current_detail", ""))

    if command:
        prefix = f"{agent_label}正在执行" if agent_label else "正在执行"
        lines.append(f"{prefix}：{command}")
    elif detail:
        prefix = f"{agent_label}{phase_label}" if agent_label else phase_label
        lines.append(f"{prefix}：{detail}")
    elif phase_label:
        prefix = f"{agent_label}{phase_label}" if agent_label else phase_label
        lines.append(prefix)

    # Elapsed time
    elapsed = getattr(state, "elapsed_seconds", None)
    if elapsed is not None:
        lines.append(f"已运行：{_format_duration(int(elapsed))}")

    # ETA
    estimate = _estimate_remaining(state)
    if estimate:
        lines.append(f"预计还需：{estimate}")

    return "\n".join(lines) if lines else "正在处理"


def render_cockpit_completion(state) -> str:
    """Render a brief completion card. No raw output, no full diff."""
    phase = getattr(state, "phase", "")
    total_tokens = getattr(state, "total_tokens", 0)

    if phase == "completed":
        base = "运行完成"
    elif phase == "cancelled":
        return "运行已取消。"
    else:
        base = "执行结束"

    if total_tokens > 0:
        base += f"\n消耗 {total_tokens} tokens"

    has_diff = getattr(state, "has_diff", False)
    if has_diff:
        base += "\n代码改动已记录，可点 查看 diff。"

    return base


def render_cockpit_failure(state) -> str:
    """Render a failure card. Shows real error, not silent idle.

    Always includes a hint to use terminal for details.
    """
    error = getattr(state, "error_summary", "")
    phase = getattr(state, "phase", "")

    if phase == "cancelled":
        return "运行已取消。"

    if not error:
        return f"运行失败，请重试。{_TERMINAL_HINT}"

    brief = _compact_line(error, limit=200)
    return f"运行失败: {brief}{_TERMINAL_HINT}"


def render_cockpit_approval(
    kind: str,
    summary: str,
    *,
    agent: str = "",
) -> str:
    """Render an approval card (Anthropic-plugin style).

    Shows: what is being requested, clear action buttons.
    """
    kind_label = _KIND_LABELS.get(kind, kind)
    agent_label = _AGENT_LABELS.get(agent, agent) if agent else ""

    lines: list[str] = []
    if agent_label:
        lines.append(f"{agent_label} 请求审批")
    lines.append(f"类型：{kind_label}")

    if summary:
        brief = _compact_line(summary, limit=200)
        lines.append(f"摘要：{brief}")

    lines.append("")
    lines.append("请使用下面按钮批准或拒绝。")

    return "\n".join(lines)


def render_cockpit_queued(agent: str = "") -> str:
    """Render a queued/starting card."""
    agent_label = _AGENT_LABELS.get(agent, agent) if agent else ""
    if agent_label:
        return f"{agent_label} 正在启动..."
    return "正在启动，稍等片刻..."


def render_product_display_event(event: ProductDisplayEvent) -> str:
    """Convert a ProductDisplayEvent into a cockpit display line.

    Hides diff and tool_output behind summary labels.
    """
    return product_speaker_line(event)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _render_terminal_status(state) -> str:
    """Render terminal phases (completed/failed/cancelled) as cockpit cards."""
    phase = getattr(state, "phase", "")

    if phase == "completed":
        return render_cockpit_completion(state)
    if phase == "cancelled":
        return "运行已取消。"
    if phase == "failed":
        return render_cockpit_failure(state)

    # Other terminal state
    return "执行结束"


def _render_approval_status(state) -> str:
    """Render the 'waiting for approval' status line."""
    agent = getattr(state, "active_agent", "")
    phase = getattr(state, "phase", "")
    agent_label = _AGENT_LABELS.get(agent, agent) if agent else ""
    phase_label = _PHASE_LABELS.get(phase, phase)
    short = phase_label.replace("正在", "")

    prefix = f"{agent_label}" if agent_label else ""
    return f"等待审批 — {prefix}{short}" if prefix else f"等待审批 — {short}"


def _compact_line(text: str, *, limit: int = 80) -> str:
    """Compact a line for cockpit display.

    Strips JSON-like patterns, collapses whitespace, and truncates.
    Never shows raw JSON objects or arrays in cockpit status.
    """
    import re
    compact = " ".join(str(text or "").split())
    # Strip JSON-like patterns: {"key": ...} or [...]
    compact = re.sub(r'\{["\'].*?["\']:\s*.*?\}', '...', compact)
    compact = re.sub(r'\[.*?\]', '...', compact)
    compact = " ".join(compact.split())
    if len(compact) <= limit:
        return compact
    return compact[:max(0, limit - 3)].rstrip() + "..."


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


_PHASE_ESTIMATES = {
    "running_analysis": "约2-6分钟",
    "running_implementation": "约5-15分钟",
    "running_verification": "约3-8分钟",
    "retrying_implementation": "约2-8分钟",
}


def _estimate_remaining(state) -> str:
    explicit = str(getattr(state, "estimated_remaining", "") or "").strip()
    if explicit:
        return explicit
    command = str(getattr(state, "current_command", "") or "").strip().lower()
    if command:
        if "pytest tests/ -q" in command or "pytest tests/" in command:
            return "约3-5分钟"
        if "pytest" in command:
            return "约1-3分钟"
        return "约1-5分钟"
    return _PHASE_ESTIMATES.get(getattr(state, "phase", ""), "")
