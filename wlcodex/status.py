from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from wlcodex.models import TaskStatus
from wlcodex.models import Task
from wlcodex.models import ConversationSession, AgentRun, OrchestrationRun

if TYPE_CHECKING:
    from wlcodex.health_snapshot import HealthSnapshot


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

KIND_LABELS = {
    "command": "命令",
    "file_change": "文件变更",
    "permissions": "权限",
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
    if task.active_turn_id:
        lines.append(f"当前 turn：{task.active_turn_id}")
    if task.codex_thread_id:
        lines.append(f"线程：{task.codex_thread_id}")
    return "\n".join(lines)


def render_task_list(
    tasks: Sequence[Task],
    *,
    waiting_meta: dict[int, tuple[int, str, int]] | None = None,
) -> str:
    """Render a compact task list.

    waiting_meta maps task_id → (blocker_id, blocker_status, queue_position)
    for waiting_slot tasks.  Callers should pre-compute this from the service
    so the renderer stays a pure function.
    """
    if not tasks:
        return "暂无任务。用 /task <workspace> <prompt> 创建新任务。"
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


def render_approval_card(
    task_id: int, approval_id: int, kind: str, summary: str
) -> str:
    lines = [
        f"审批 #{approval_id}（任务 #{task_id}）",
        f"类型：{KIND_LABELS.get(kind, kind)}",
        f"摘要：{_trim(summary, 200)}",
        "",
        "请使用下面按钮批准或拒绝。",
    ]
    return "\n".join(lines)


def render_health_card(
    health: object, *, snapshot: HealthSnapshot | None = None
) -> str:
    if hasattr(health, "is_healthy"):
        if bool(health.is_healthy):  # type: ignore[union-attr]
            prefix = "后端健康"
        elif hasattr(health, "summary"):
            prefix = f"后端异常：{health.summary()}"  # type: ignore[union-attr]
        else:
            prefix = "后端异常"
    elif hasattr(health, "summary"):
        s = health.summary()  # type: ignore[union-attr]
        prefix = f"后端状态：{s}"
    else:
        prefix = f"后端状态：{health}"

    if snapshot is None:
        return prefix

    lines = [prefix, ""]
    lines.append(f"活跃任务：{snapshot.active_task_count}")
    if snapshot.running_count:
        lines.append(f"  运行中：{snapshot.running_count}")
    if snapshot.waiting_approval_count:
        lines.append(f"  等待审批：{snapshot.waiting_approval_count}")
    if snapshot.queued_count:
        lines.append(f"  排队中：{snapshot.queued_count}")
    if snapshot.paused_count:
        lines.append(f"  已暂停：{snapshot.paused_count}")
    if snapshot.waiting_count:
        lines.append(f"  等待中：{snapshot.waiting_count}")
    if snapshot.isolated_running_count:
        lines.append(f"  隔离 worktree：{snapshot.isolated_running_count}")
    return "\n".join(lines)


def render_help() -> str:
    return """WLCodex — 总工程师驾驶舱

双面模式：
  /product — 切到手机端产品模式
  /terminal — 切到远程终端模式
  /terminal claude — 接入 Claude 终端
  /terminal codex — 接入 Codex 终端
  /terminal agent claude — 接入 Claude 终端（显式）
  /terminal agent codex — 接入 Codex 终端（显式）
  /terminal tail — 恢复终端推送 / 查看最新输出
  /terminal pause — 暂停终端推送但保留会话
  /terminal detach — 停止终端推送但保留会话
  /terminal product — 切回产品模式
  /mode — 查看当前模式

对话模式 — 直接发消息开始：
  • 默认交给总工程师（Codex 分析 + 验收）
  • /codex <提示> — 直接和 Codex 对话
  • /claude <提示> — 直接叫 Claude Code 实施
  • /auto <提示> — 完整 Codex 分析 → Claude 实施 → Codex 验收

常用命令：
  /new — 开始新对话
  /stop — 停止当前运行
  /status — 查看当前对话和任务
  /sessions — 查看会话列表
  /switch <工作区> — 切换工作区
  /model — 切换或查看当前模型
  /claude_mode — 切换 Claude 权限模式
  /diff — 查看变更
  /files — 相关文件
  /verify — Codex 验收
  /health — 系统健康
  /help — 此帮助

高级命令（诊断用）：
  /task /continue /steer /tail /events
  /pause /abort /archive /fork

安全规则：
  • 只允许私聊
  • 只允许白名单用户
  • 每个工作区同一时间只允许一个写任务
  • 状态和日志永远不会回灌到 Codex 上下文"""


def render_inspection_result(title: str, body: str) -> str:
    return f"{title}\n\n{body}"


def _trim(value: str, max_chars: int) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"


def _status_label(status: TaskStatus) -> str:
    return STATUS_LABELS.get(status, status.value)


def _status_label_str(status: str) -> str:
    mapping = {
        "running": "运行中",
        "completed": "已完成",
        "failed": "已失败",
        "cancelled": "已取消",
        "idle": "空闲",
    }
    return mapping.get(status, status)


def _phase_cn(phase: str) -> str:
    mapping = {
        "running_analysis": "Codex 分析",
        "running_implementation": "Claude 实施",
        "running_verification": "Codex 验收",
        "retrying_implementation": "重新实施",
    }
    return mapping.get(phase, phase)


def _event_label(event_type: str) -> str:
    mapping: dict[str, str] = {
        "run.requested": "运行请求",
        "run.started": "运行开始",
        "run.phase.changed": "阶段变更",
        "run.completed": "运行完成",
        "run.failed": "运行失败",
        "run.cancelled": "运行取消",
        "agent.run.queued": "Agent 排队",
        "agent.run.started": "Agent 启动",
        "agent.run.activity": "Agent 活动",
        "agent.run.heartbeat": "Agent 心跳",
        "agent.run.waiting_for_approval": "等待审批",
        "agent.run.completed": "Agent 完成",
        "agent.run.failed": "Agent 失败",
        "agent.run.timed_out": "Agent 超时",
        "agent.run.orphaned": "Agent 孤儿",
        "tool.call.started": "工具调用",
        "tool.call.completed": "工具调用完成",
        "tool.call.failed": "工具调用失败",
        "command.started": "命令开始",
        "command.completed": "命令完成",
        "command.failed": "命令失败",
        "file.changed": "文件变更",
        "diff.updated": "Diff 更新",
        "approval.requested": "审批请求",
        "approval.resolved": "审批完成",
        "approval.expired": "审批过期",
        "verification.started": "验收开始",
        "verification.decision.recorded": "验收决策",
        "verification.completed": "验收完成",
        "verification.retry.requested": "重新验收",
        "watchdog.idle_timeout": "空闲超时",
        "watchdog.hard_timeout": "硬超时",
        "system.started": "系统启动",
        "system.recovery.started": "恢复开始",
        "system.recovery.completed": "恢复完成",
        "projection.rebuilt": "投影重建",
        "model.usage.updated": "Token 更新",
        "model.message.completed": "模型消息完成",
        "model.text.delta": "模型文本",
        "model.api.retry": "API 重试",
        "user.message.received": "用户消息",
        "telegram.message.sent": "Telegram 发送",
        "telegram.message.failed": "Telegram 失败",
    }
    return mapping.get(event_type, event_type)


def _duration_str(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}秒"
    if seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}分{s}秒"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}时{m}分"


# --- Conversation renderers ---

MODE_LABELS = {
    "chief_engineer": "总工程师",
    "codex_direct": "Codex 直聊",
    "claude_direct": "Claude 直聊",
}


def render_conversation_status(
    session: ConversationSession,
    latest_run: AgentRun | None = None,
    orch_run: OrchestrationRun | None = None,
) -> str:
    lines = [
        f"当前对话：{_trim(session.title, 80)}",
        f"模式：{MODE_LABELS.get(session.mode, session.mode)}",
        f"工作区：{session.workspace_alias}",
    ]

    if orch_run:
        lines.append(f"步骤：{orch_run.current_step or '分析中'}")
        if orch_run.verify_round > 0:
            lines.append(f"轮次：第 {orch_run.verify_round} 轮")

    if latest_run:
        if latest_run.agent:
            lines.append(f"最近运行：{latest_run.agent} / {latest_run.status}")
        if latest_run.token_input or latest_run.token_output:
            lines.append(f"Token：{latest_run.token_input} 输入 / {latest_run.token_output} 输出")

    if session.conversation_summary:
        lines.append(f"摘要：{_trim(session.conversation_summary, 120)}")

    # Advanced details: task IDs if present
    advanced: list[str] = []
    if session.active_codex_task_id:
        advanced.append(f"内部任务：#{session.active_codex_task_id}")
    if session.active_claude_run_id:
        advanced.append(f"内部 Claude 运行：#{session.active_claude_run_id}")
    if advanced:
        lines.append("")
        lines.append("高级详情：")
        lines.extend(advanced)

    return "\n".join(lines)


def render_conversation_help(profile: str = "natural") -> str:
    if profile == "natural":
        return "\n".join(
            [
                "WLCodex",
                "",
                "直接发消息就能继续当前对话。",
                "/product 手机端模式 · /terminal 远程终端",
                "/new 新对话",
                "/status 看状态",
                "/diff 看变更",
                "/model 模型 · /claude_mode Claude 权限",
                "/help 帮助",
            ]
        )
    return """WLCodex — 你的总工程师驾驶舱

双面模式：
  • /product — 切到手机端产品模式
  • /terminal — 切到远程终端模式
  • /terminal claude — 接入 Claude 终端
  • /terminal codex — 接入 Codex 终端
  • /terminal agent claude — 接入 Claude 终端（显式）
  • /terminal agent codex — 接入 Codex 终端（显式）
  • /terminal tail — 恢复终端推送
  • /terminal pause — 暂停终端推送
  • /terminal detach — 停止终端推送但保留会话
  • /terminal product — 切回产品模式
  • /mode — 查看当前模式

对话模式：
  • 直接发消息 — 默认交给 Codex 分析
  • /codex <prompt> — 直接和 Codex 对话
  • /claude <prompt> — 直接叫 Claude Code 实施
  • /auto <prompt> — 总工程师完整编排
  • /verify — Codex 验收最新结果

常用命令：
  • /new — 开始新对话
  • /stop — 停止当前运行
  • /status — 查看当前对话状态
  • /sessions — 查看所有会话
  • /switch <workspace> — 切换工作区
  • /model — 切换或查看当前模型
  • /claude_mode — 切换 Claude 权限模式
  • /diff — 查看变更
  • /files — 相关文件
  • /health — 系统健康
  • /help — 此帮助

高级命令（诊断用）：
  /task /continue /steer /tail /events
  /pause /abort /archive /fork /sessions

安全规则：
  • 只允许私聊
  • 只允许白名单用户
  • 每个工作区同一时间只允许一个写任务
  • 状态和日志永远不会回灌到 Codex 上下文"""


def render_session_list(sessions: Sequence[ConversationSession]) -> str:
    if not sessions:
        return "暂无活跃对话。发送消息或用 /new 开始新对话。"

    lines = ["对话列表："]
    for s in sessions:
        mode_label = MODE_LABELS.get(s.mode, s.mode)
        line = f"  #{s.id} [{mode_label}] {_trim(s.title, 60)} · {s.workspace_alias}"
        if s.archived_at:
            line += " · 已归档"
        elif s.active_codex_task_id:
            line += f" · 内部任务 #{s.active_codex_task_id}"
        lines.append(line)
    return "\n".join(lines)


def render_runtime_status_lines(
    *,
    conversation_id: int | None = None,
    active_agent: str = "",
    active_agent_run_id: int | None = None,
    phase: str = "",
    status: str = "",
    last_event_type: str = "",
    last_event_id: int = 0,
    idle_seconds: float = 0.0,
    hard_elapsed_seconds: float = 0.0,
    hard_timeout_seconds: int = 0,
    token_input: int = 0,
    token_output: int = 0,
    total_events: int = 0,
    agent_count: int = 0,
    last_user_event: str = "",
) -> list[str]:
    """Produce status lines enriched with runtime event-sourced data.

    Callers that have a RuntimeEventStore should compute the values via
    runtime_diagnostics.build_runtime_status() and pass them here.
    This function remains a pure formatter.
    """
    if conversation_id is None:
        return ["暂无活跃对话。"]

    if status == "idle":
        return [f"当前对话 #{conversation_id} — 空闲中"]

    lines = [f"对话 #{conversation_id} — {_status_label_str(status)}"]

    if phase:
        lines.append(f"阶段：{_phase_cn(phase)}")

    if active_agent:
        agent_line = f"活跃 Agent：{active_agent}"
        if active_agent_run_id:
            agent_line += f"（运行 #{active_agent_run_id}）"
        lines.append(agent_line)

    if last_event_type:
        lines.append(
            f"最近事件：{_event_label(last_event_type)}（#{last_event_id}）"
        )

    if idle_seconds > 0:
        lines.append(f"空闲时钟：{_duration_str(idle_seconds)}")
    if hard_elapsed_seconds > 0:
        hard_str = f"硬时钟：{_duration_str(hard_elapsed_seconds)}"
        if hard_timeout_seconds:
            hard_str += f" / 上限 {_duration_str(hard_timeout_seconds)}"
        lines.append(hard_str)

    if token_input or token_output:
        lines.append(
            f"Token：{token_input:,} 输入 / {token_output:,} 输出"
        )

    if agent_count:
        lines.append(f"Agent 运行记录：{agent_count}")

    if total_events:
        lines.append(f"事件总数：{total_events}")

    if last_user_event:
        lines.append(f"最近用户事件：{_event_label(last_user_event)}")

    return lines


def render_agent_result_summary(result: AgentRun) -> str:
    parts = [f"运行 #{result.id}"]
    parts.append(f"类型：{result.agent} / {result.role}")
    parts.append(f"状态：{result.status}")
    if result.prompt_packet_summary:
        parts.append(f"提示：{_trim(result.prompt_packet_summary, 120)}")
    if result.token_input or result.token_output:
        parts.append(f"Token：{result.token_input} 输入 / {result.token_output} 输出")
    return "\n".join(parts)
