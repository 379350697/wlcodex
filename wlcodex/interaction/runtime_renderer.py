"""Deterministic Telegram rendering from projected runtime state.

Never echoes raw Codex/Claude model chatter.  Uses Chinese templates
for every phase.  Supports verbosity 0/1/2.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from wlcodex.interaction.transport import TelegramTransport

# ---------------------------------------------------------------------------
# Projected state contract (consumed by renderer; produced by Lane D)
# ---------------------------------------------------------------------------

KNOWN_PHASES = {
    "queued": "排队中",
    "running_analysis": "正在分析需求",
    "running_implementation": "正在实施代码",
    "running_verification": "正在验收结果",
    "retrying_implementation": "正在重新实施",
    "completed": "运行完成",
    "failed": "运行失败",
    "cancelled": "运行已取消",
}

_NEXT_STEP = {
    "queued": "稍等片刻，即将开始处理",
    "running_analysis": "分析完成后将进入代码实施",
    "running_implementation": "代码写完后将自动验收",
    "running_verification": "验收通过后返回结果",
    "retrying_implementation": "修复后将再次验收",
}

_PHASE_ACTIONS = {
    "queued": "排队",
    "running_analysis": "分析需求",
    "running_implementation": "实现代码",
    "running_verification": "验收结果",
    "retrying_implementation": "重新实现",
}

_PHASE_ESTIMATES = {
    "queued": "约1分钟内",
    "running_analysis": "约2-6分钟",
    "running_implementation": "约5-15分钟",
    "running_verification": "约3-8分钟",
    "retrying_implementation": "约2-8分钟",
}


@dataclass
class RuntimeRunState:
    """Projected runtime state snapshot for the renderer.

    This is the contract between the projector (Lane D) and the renderer
    (Lane E).  The renderer never queries provider internals.
    """

    phase: str = ""
    active_agent: str = ""          # "codex", "claude", ""
    agent_status: str = ""          # "running", "waiting_for_approval", etc.
    last_activity_at: str | None = None  # ISO timestamp
    last_event_type: str = ""
    tool_names: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    current_detail: str = ""
    current_command: str = ""
    elapsed_seconds: int | None = None
    estimated_remaining: str = ""
    retry_count: int = 0
    total_tokens: int = 0
    error_summary: str = ""
    verification_decision: str = ""  # "pass", "fail", "retry"
    has_diff: bool = False
    is_terminal: bool = False


# ---------------------------------------------------------------------------
# Pure renderer — text generation only
# ---------------------------------------------------------------------------

class RuntimeRenderer:
    """Generates deterministic human-readable Chinese progress text from
    projected runtime state.  Does no I/O and knows nothing about Telegram."""

    def __init__(self, verbosity: int = 1) -> None:
        if verbosity not in (0, 1, 2):
            raise ValueError(f"verbosity must be 0, 1, or 2, got {verbosity}")
        self.verbosity = verbosity

    # -- public API ----------------------------------------------------------

    def progress_text(self, state: RuntimeRunState) -> str:
        """Deterministic progress line(s) for *state*."""
        if self.verbosity == 0:
            return ""

        lines: list[str] = []

        # Phase label
        phase_line = self._phase_line(state)
        if phase_line:
            lines.append(phase_line)

        if self.verbosity >= 1 and not state.is_terminal:
            elapsed_line = _elapsed_line(getattr(state, "elapsed_seconds", None))
            if elapsed_line:
                lines.append(elapsed_line)
            estimate = _estimate_remaining(state)
            if estimate:
                lines.append(f"预计还需：{estimate}")

        # Next-step hint (verbosity 2 only)
        if self.verbosity >= 1:
            next_step = _NEXT_STEP.get(state.phase, "")
            if self.verbosity >= 2 and next_step and not state.is_terminal:
                lines.append(next_step)

        # Detail lines (verbosity 2 only)
        if self.verbosity >= 2:
            detail = self._detail_lines(state)
            if detail:
                lines.extend(detail)

        return "\n".join(lines) if lines else ""

    def heartbeat_text(self, state: RuntimeRunState) -> str:
        """Heartbeat for long-running runs (verbosity 1+ only)."""
        if self.verbosity < 1:
            return ""
        ago = _time_ago(state.last_activity_at)
        return f"还在执行，最近活动：{ago}"

    def final_text(self, state: RuntimeRunState) -> str:
        """Final summary when a run reaches a terminal state."""
        if state.phase == "completed":
            base = "运行完成"
        elif state.phase == "failed":
            reason = state.error_summary or "未知错误"
            # Keep error summary brief, strip internal details
            brief = reason[:200].split("\n")[0].strip()
            base = f"运行失败\n{brief}" if brief else "运行失败，请重试"
        elif state.phase == "cancelled":
            base = "运行已取消"
        else:
            base = "执行结束"

        if self.verbosity >= 1 and state.total_tokens > 0:
            base += f"\n消耗 {state.total_tokens} tokens"
        if self.verbosity >= 2 and state.retry_count > 0:
            base += f"\nAPI 重试 {state.retry_count} 次"
        return base

    def approval_text(self, kind: str, summary: str) -> str:
        """Render an approval card (semantics unchanged from existing)."""
        kind_cn = {"command": "命令", "file_change": "文件修改", "tool": "工具"}.get(
            kind, kind
        )
        return f"需要审批 — {kind_cn}\n{summary}"

    # -- internal ------------------------------------------------------------

    def _phase_line(self, state: RuntimeRunState) -> str:
        """Single-line phase description."""
        if state.is_terminal or state.phase in ("completed", "failed", "cancelled"):
            return ""

        label = self._phase_label(state.phase)
        if not label:
            return ""

        # Add approval signal
        if state.agent_status == "waiting_for_approval":
            short = self._phase_label(state.phase).replace("正在", "")
            return f"等待审批 — {short}"

        agent = _agent_display(state.active_agent)
        command = _compact_line(getattr(state, "current_command", ""))
        if command:
            prefix = f"{agent}正在执行" if agent else "正在执行"
            return f"{prefix}：{command}"

        if state.phase not in _PHASE_ACTIONS and state.phase not in KNOWN_PHASES:
            return label

        action = _PHASE_ACTIONS.get(state.phase, label.replace("正在", ""))
        detail = _compact_line(getattr(state, "current_detail", ""))
        prefix = f"{agent}正在{action}" if agent else label
        if detail:
            return f"{prefix}：{detail}"
        return prefix

    def _detail_lines(self, state: RuntimeRunState) -> list[str]:
        """Verbosity-2 detail lines — tools, files, commands, retries, tokens."""
        lines: list[str] = []
        for fname in state.changed_files[-3:]:
            lines.append(f"修改文件: {fname}")
        for cmd in state.commands[-3:]:
            lines.append(f"运行命令: {cmd}")
        for tool in state.tool_names[-3:]:
            lines.append(f"使用工具: {tool}")
        if state.retry_count > 0:
            lines.append(f"API 重试: {state.retry_count} 次")
        if state.total_tokens > 0:
            lines.append(f"已消耗 {state.total_tokens} tokens")
        return lines

    def _phase_label(self, phase: str) -> str:
        return KNOWN_PHASES.get(phase, phase)


# ---------------------------------------------------------------------------
# Progress manager — throttled Telegram message edits
# ---------------------------------------------------------------------------

@dataclass
class _ProgressSlot:
    """Per-conversation progress-message state."""
    message_id: int | None = None
    chat_id: int | None = None
    last_state_hash: str = ""
    last_edit_time: float = 0.0
    last_heartbeat_time: float = 0.0
    is_finished: bool = False


class RuntimeProgressManager:
    """Manages a single editable progress message per conversation.

    Throttles edits to avoid *message is not modified* churn.
    Uses state-content hashing to skip no-op updates.
    """

    def __init__(
        self,
        transport: TelegramTransport,
        *,
        verbosity: int = 1,
        min_edit_interval: float = 2.0,
        heartbeat_interval: float = 15.0,
        clock: object = None,
    ) -> None:
        self._transport = transport
        self._renderer = RuntimeRenderer(verbosity)
        self._min_edit_interval = min_edit_interval
        self._heartbeat_interval = heartbeat_interval
        self._clock = clock or time
        self._slots: dict[int, _ProgressSlot] = {}  # keyed by conversation_id or chat_id

    @property
    def verbosity(self) -> int:
        return self._renderer.verbosity

    # -- public API ----------------------------------------------------------

    async def update_progress(
        self, state: RuntimeRunState, *, chat_id: int, conversation_id: int = 0
    ) -> None:
        """Send or edit the progress message if *state* has changed enough."""
        key = conversation_id or chat_id
        slot = self._slots.get(key)
        if slot is None:
            slot = _ProgressSlot(chat_id=chat_id)
            self._slots[key] = slot

        if slot.is_finished:
            return

        # Verbosity 0: no progress messages, only finals
        if self._renderer.verbosity == 0:
            return

        now = self._clock.time()
        text = self._renderer.progress_text(state)
        state_hash = _content_hash(text)

        # Skip if content unchanged and not yet due for heartbeat
        if state_hash == slot.last_state_hash:
            if self._renderer.verbosity >= 1 and now - slot.last_heartbeat_time >= self._heartbeat_interval:
                heartbeat = self._renderer.heartbeat_text(state)
                if heartbeat:
                    text = f"{text}\n{heartbeat}" if text else heartbeat
            else:
                return

        # Throttle edit frequency
        if slot.message_id is not None and now - slot.last_edit_time < self._min_edit_interval:
            return

        if not text:
            return

        if slot.message_id is not None:
            try:
                await self._transport.edit(slot.chat_id, slot.message_id, text)
                slot.last_edit_time = now
                slot.last_state_hash = state_hash
                slot.last_heartbeat_time = now
                return
            except Exception:
                # Edit failed (e.g. message deleted) — fall through to send
                slot.message_id = None

        msg_id = await self._transport.send(slot.chat_id, text)
        slot.message_id = _editable_message_id(msg_id)
        slot.last_edit_time = now
        slot.last_state_hash = state_hash
        slot.last_heartbeat_time = now

    async def finish(
        self,
        state: RuntimeRunState,
        *,
        chat_id: int,
        conversation_id: int = 0,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> None:
        """Post the final message and mark the slot finished."""
        key = conversation_id or chat_id
        slot = self._slots.get(key)
        if slot is None:
            slot = _ProgressSlot(chat_id=chat_id)
            self._slots[key] = slot

        if slot.is_finished:
            return

        final_text = self._renderer.final_text(state)

        if slot.message_id is not None:
            try:
                await self._transport.edit(
                    slot.chat_id, slot.message_id, final_text, buttons=buttons
                )
                slot.is_finished = True
                return
            except Exception:
                slot.message_id = None

        msg_id = await self._transport.send(slot.chat_id, final_text, buttons=buttons)
        slot.message_id = _editable_message_id(msg_id)
        slot.is_finished = True

    async def show_approval(
        self,
        kind: str,
        summary: str,
        *,
        chat_id: int,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> int:
        """Send an approval card (always a new message, never edits progress)."""
        text = self._renderer.approval_text(kind, summary)
        return await self._transport.send(chat_id, text, buttons=buttons)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _editable_message_id(message_id: int | None) -> int | None:
    if isinstance(message_id, int) and message_id > 0:
        return message_id
    return None


def _time_ago(iso_timestamp: str | None) -> str:
    if not iso_timestamp:
        return "未知"
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        delta = datetime.now(timezone.utc) - dt
        seconds = int(delta.total_seconds())
        if seconds < 5:
            return "刚刚"
        if seconds < 60:
            return f"{seconds} 秒前"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} 分钟前"
        hours = minutes // 60
        return f"{hours} 小时前"
    except (ValueError, TypeError):
        return "未知"


def _agent_display(agent: str) -> str:
    normalized = str(agent or "").strip().lower()
    if normalized == "codex":
        return "Codex"
    if normalized == "claude":
        return "Claude"
    return str(agent or "").strip()


def _compact_line(text: str, *, limit: int = 80) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)].rstrip() + "..."


def _elapsed_line(seconds: int | float | None) -> str:
    if seconds is None:
        return ""
    try:
        value = max(0, int(seconds))
    except (TypeError, ValueError):
        return ""
    return f"已运行：{_format_duration(value)}"


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _estimate_remaining(state: RuntimeRunState) -> str:
    explicit = str(getattr(state, "estimated_remaining", "") or "").strip()
    if explicit:
        return explicit
    command = str(getattr(state, "current_command", "") or "").strip().lower()
    if command:
        if "pytest tests/ -q" in command or "pytest tests/" in command:
            return "约3-5分钟"
        if "pytest" in command:
            return "约1-3分钟"
        if "gitnexus analyze" in command:
            return "约10-30秒"
        return "约1-5分钟"
    return _PHASE_ESTIMATES.get(state.phase, "")
