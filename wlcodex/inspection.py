"""Local task inspection — never calls Codex backend."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import TYPE_CHECKING

from wlcodex.db import Ledger

if TYPE_CHECKING:
    from wlcodex.models import TaskEvent
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_diagnostics import RuntimeTrace


@dataclass
class InspectionResult:
    title: str
    body: str
    truncated: bool = False


class TaskInspector:
    def __init__(
        self,
        ledger: Ledger,
        task_log_dir: Path,
        tail_lines: int = 40,
        diff_max_chars: int = 3500,
    ) -> None:
        self._ledger = ledger
        self._log_dir = task_log_dir
        self._tail_lines = tail_lines
        self._diff_max_chars = diff_max_chars

    # --- Events ---

    def events(self, task_id: int) -> InspectionResult:
        events = self._ledger.list_events(task_id, limit=200)
        if not events:
            return InspectionResult(title=f"任务 #{task_id} 的事件", body="暂无事件记录。")

        lines = [f"任务 #{task_id} 的事件："]
        for ev in events:
            lines.append(
                f"  [{ev.created_at.isoformat()}] {ev.event_type} "
                f"{_summarize(ev)}"
            )
        return InspectionResult(
            title=f"任务 #{task_id} 的事件",
            body="\n".join(lines),
        )

    # --- Tail ---

    def tail(self, task_id: int) -> InspectionResult:
        log_file = self._log_dir / f"{task_id}.log"
        if log_file.exists():
            try:
                lines = log_file.read_text(encoding="utf-8").splitlines()
                recent = lines[-self._tail_lines :]
                return InspectionResult(
                    title=f"任务 #{task_id} 的日志尾部（最近 {len(recent)} 行）",
                    body="\n".join(recent),
                )
            except Exception as exc:
                return InspectionResult(
                    title=f"任务 #{task_id} 的日志尾部",
                    body=f"读取日志失败：{exc}",
                )

        # Fallback: read from SQLite events
        events = self._ledger.list_events(task_id, limit=200)
        deltas = []
        for ev in events:
            if ev.event_type == "command_output":
                delta = str(ev.payload.get("delta", ""))
                if delta:
                    deltas.append(delta)
            elif ev.event_type == "agent_message_delta":
                delta = str(ev.payload.get("delta", ""))
                if delta:
                    deltas.append(delta)

        if deltas:
            body = "\n".join(deltas[-self._tail_lines :])
            return InspectionResult(
                title=f"任务 #{task_id} 的日志尾部（来自事件记录）",
                body=body,
            )

        return InspectionResult(
            title=f"任务 #{task_id} 的日志尾部",
            body="没有找到本地日志或事件输出。",
        )

    # --- Files ---

    def files(self, task_id: int) -> InspectionResult:
        touched = self._ledger.list_touched_files(task_id)
        if not touched:
            return InspectionResult(
                title=f"任务 #{task_id} 涉及的文件",
                body="暂无文件记录。",
            )

        lines = [f"任务 #{task_id} 涉及的文件："]
        for tf in touched:
            lines.append(f"  [{tf.change_kind}] {tf.path}")
        return InspectionResult(
            title=f"任务 #{task_id} 涉及的文件",
            body="\n".join(lines),
        )

    # --- Runtime Trace ---

    def trace_runtime(
        self,
        runtime_store: "RuntimeEventStore",
        conversation_id: int,
        *,
        limit: int = 20,
        visibility_filter: str | None = None,
    ) -> InspectionResult:
        """Build a sanitized trace from runtime events for a conversation.

        Uses the runtime event store to produce a human-readable timeline.
        Falls back gracefully when no runtime store or events are available.
        """
        from wlcodex.runtime_diagnostics import (
            build_runtime_trace,
            format_trace_display,
        )

        trace = build_runtime_trace(
            runtime_store,
            conversation_id,
            limit=limit,
            visibility_filter=visibility_filter,
        )
        body = format_trace_display(trace)
        return InspectionResult(
            title=f"对话 #{conversation_id} 的运行时事件记录",
            body=body,
        )

    def trace_agent_run(
        self,
        runtime_store: "RuntimeEventStore",
        agent_run_id: int,
        *,
        limit: int = 50,
    ) -> InspectionResult:
        """Show runtime events for a specific agent run."""
        events = runtime_store.list_by_agent_run(agent_run_id, limit=limit)
        if not events:
            return InspectionResult(
                title=f"Agent 运行 #{agent_run_id} 的事件",
                body="暂无事件记录。",
            )

        from wlcodex.runtime_diagnostics import _sanitize_event_for_display

        lines = [f"Agent 运行 #{agent_run_id} 的事件（共 {len(events)} 条）："]
        for ev in events:
            d = _sanitize_event_for_display(ev)
            eid = d.get("id", "?")
            etype = d.get("event_type", "")
            occurred = str(d.get("occurred_at", ""))[:19]
            psum = _summarize_runtime_payload(d.get("payload", {}))
            lines.append(f"  [#{eid}] {occurred} {etype} {psum}")

        return InspectionResult(
            title=f"Agent 运行 #{agent_run_id} 的事件",
            body="\n".join(lines),
        )

    # --- Diff ---

    def diff(self, task_id: int, workspace_path: str | None = None) -> InspectionResult:
        result = self._from_last_diff_event(task_id)
        if result is not None:
            return result

        if workspace_path:
            return self._from_git_diff(task_id, workspace_path)

        return InspectionResult(
            title=f"任务 #{task_id} 的 diff",
            body="暂无 diff 信息。",
        )

    def _from_last_diff_event(self, task_id: int) -> InspectionResult | None:
        events = self._ledger.list_events(task_id)
        for ev in reversed(events):
            if ev.event_type == "diff_updated":
                diff_text = str(ev.payload.get("diff", ""))
                if diff_text:
                    truncated = len(diff_text) > self._diff_max_chars
                    body = diff_text[: self._diff_max_chars]
                    if truncated:
                        body += "\n… [已截断]"
                    return InspectionResult(
                        title=f"任务 #{task_id} 的 diff",
                        body=body,
                        truncated=truncated,
                    )
        return None

    def _from_git_diff(self, task_id: int, workspace_path: str) -> InspectionResult:
        try:
            proc = subprocess.run(
                ["git", "-C", workspace_path, "diff", "--stat"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = proc.stdout.strip()
            if not output:
                return InspectionResult(
                    title=f"任务 #{task_id} 的 diff", body="工作区没有未提交变更。"
                )

            truncated = len(output) > self._diff_max_chars
            body = output[: self._diff_max_chars]
            if truncated:
                body += "\n… [已截断]"
            return InspectionResult(
                title=f"任务 #{task_id} 的 diff（git diff --stat）",
                body=body,
                truncated=truncated,
            )
        except subprocess.TimeoutExpired:
            return InspectionResult(
                title=f"任务 #{task_id} 的 diff",
                body="git diff 超时。",
            )
        except FileNotFoundError:
            return InspectionResult(
                title=f"任务 #{task_id} 的 diff",
                body="当前主机没有可用的 git。",
            )


def _summarize(event: "TaskEvent") -> str:
    p = event.payload
    if "prompt" in p:
        return str(p["prompt"])[:100]
    if "delta" in p:
        return str(p["delta"])[:100]
    if "type" in p:
        return str(p["type"])
    if "summary" in p:
        return str(p["summary"])[:100]
    return "—"


def _summarize_runtime_payload(payload: dict) -> str:
    if "phase" in payload:
        return str(payload["phase"])[:80]
    if "agent" in payload:
        return f"agent={payload['agent']}"
    if "reason" in payload:
        return str(payload["reason"])[:80]
    if "summary" in payload:
        return str(payload["summary"])[:80]
    if "delta" in payload:
        return str(payload["delta"])[:80]
    if "orphaned_run_count" in payload:
        return f"orphaned={payload['orphaned_run_count']}"
    if "tool_name" in payload:
        return f"tool={payload['tool_name']}"
    return "—"
