"""Legacy diagnostics renderers for internal task rows.

These cards are intentionally kept outside the default Workbench status
module.  They are used only by explicit legacy diagnostics.
"""

from __future__ import annotations

from collections.abc import Sequence

from wlcodex.models import Task, TaskStatus


STATUS_LABELS = {
    TaskStatus.QUEUED: "排队中",
    TaskStatus.RUNNING: "运行中",
    TaskStatus.WAITING_APPROVAL: "等待审批",
    TaskStatus.PAUSED: "已暂停",
    TaskStatus.WAITING_SLOT: "等待中",
    TaskStatus.DONE: "已完成",
    TaskStatus.FAILED: "已失败",
    TaskStatus.ABORTED: "已中止",
    TaskStatus.ARCHIVED: "已归档",
}


def render_task_card(
    task: Task,
    *,
    blocker_id: int | None = None,
    blocker_status: str = "",
    queue_position: int = 0,
) -> str:
    lines = [
        f"任务 #{task.id} — {_status_label(task.status)}",
        f"工作区：{task.workspace_alias}",
        f"标题：{_trim(task.title, 120)}",
    ]
    if task.status == TaskStatus.WAITING_SLOT:
        if blocker_id is not None:
            lines.append(f"阻塞者：#{blocker_id}（{blocker_status}）")
        if queue_position > 0:
            lines.append(f"队列位置：第 {queue_position} 位")
    if task.is_force_parallel:
        lines.append("⚠️ 同目录强制并行")
    if task.worktree_path:
        lines.append(f"隔离 worktree：{task.worktree_path}")
    if task.worktree_branch:
        lines.append(f"Worktree 分支：{task.worktree_branch}")
    if task.last_phase:
        lines.append(f"阶段：{_trim(task.last_phase, 120)}")
    if task.last_summary:
        lines.append(f"摘要：{_trim(task.last_summary, 240)}")
    if task.last_error:
        lines.append(f"错误：{_trim(task.last_error, 240)}")
    if task.changed_file_count:
        lines.append(f"变更文件：{task.changed_file_count}")
    if task.pending_approval_count:
        lines.append(f"待审批：{task.pending_approval_count}")
    if task.token_input or task.token_output:
        lines.append(f"Token：{task.token_input} 输入 / {task.token_output} 输出")
    return "\n".join(lines)


def render_task_list(
    tasks: Sequence[Task],
    *,
    waiting_meta: dict[int, tuple[int, str, int]] | None = None,
) -> str:
    """Render a compact legacy-diagnostic task list."""
    if not tasks:
        return "暂无 legacy diagnostic task 记录。"
    lines = ["任务列表："]
    for task in tasks:
        status_mark = {
            "running": "▶",
            "queued": "○",
            "waiting_approval": "⏸",
            "paused": "⏸",
            "waiting_slot": "⏳",
            "done": "✓",
            "failed": "✗",
            "aborted": "✗",
            "archived": "📦",
        }.get(task.status.value, " ")
        line = (
            f"{status_mark} #{task.id} {task.workspace_alias} "
            f"{_status_label(task.status)}  {_trim(task.title, 80)}"
        )
        if task.status == TaskStatus.WAITING_SLOT and waiting_meta:
            meta = waiting_meta.get(task.id)
            if meta is not None:
                blocker_id, blocker_status, position = meta
                line += (
                    f"  ← 阻塞者 #{blocker_id}（{blocker_status}）"
                    f" 第 {position} 位"
                )
        if task.is_force_parallel:
            line += "  ⚠️并行"
        if task.worktree_path:
            line += f"  WT:{task.worktree_branch or task.worktree_path[-20:]}"
        lines.append(line)
    return "\n".join(lines)


def _trim(value: str, max_chars: int) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"


def _status_label(status: TaskStatus) -> str:
    return STATUS_LABELS.get(status, status.value)
