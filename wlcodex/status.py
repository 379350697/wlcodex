from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from wlcodex.models import ConversationSession, AgentRun, OrchestrationRun
from wlcodex.conversation import relative_time

if TYPE_CHECKING:
    from wlcodex.health_snapshot import HealthSnapshot

KIND_LABELS = {
    "command": "命令",
    "file_change": "文件变更",
    "permissions": "权限",
}


def render_approval_card(
    task_id: int, approval_id: int, kind: str, summary: str
) -> str:
    lines = [
        f"审批 #{approval_id}（当前 Workbench）",
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
    lines.append(f"活跃执行：{snapshot.active_task_count}")
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
    return """WLCodex — 远程工作台驾驶舱

普通消息：Codex 分析/核验
/auto：Codex 主导闭环（分析 → 确认 → Claude 执行 → 确认 → 验收）

驾驶舱与现场：
  /product — 回驾驶舱
  /terminal — 接管现场
  /terminal claude — 接入 Claude 现场
  /terminal codex — 接入 Codex 现场
  /terminal agent claude — 接入 Claude 现场（显式）
  /terminal agent codex — 接入 Codex 现场（显式）
  /terminal tail — 恢复现场推送 / 查看最新输出
  /terminal pause — 暂停现场推送但保留会话
  /terminal detach — 停止现场推送但保留会话
  /terminal product — 回驾驶舱
  /mode — 查看当前模式

对话模式 — 直接发消息开始：
  • 普通消息 — Codex 分析/核验
  • /codex <提示> — 直接和 Codex 对话
  • /claude <提示> — 直接叫 Claude Code 实施
  • /auto <提示> — Codex 主导闭环：分析 → 确认 → Claude 执行 → 确认 → Codex 验收

常用命令：
  /new — 开始新工作台
  /stop — 停止当前运行
  /status — 查看当前工作台
  /sessions — 查看历史现场
  /history — 查看历史工作台
  /workspaces — 查看可用工作区
  /switch <工作区> — 切换工作区
  /model — 切换或查看当前模型
  /claude_mode — 切换 Claude 权限模式
  /diff — 查看变更
  /files — 相关文件
  /verify — Codex 验收
  /health — 系统健康
  /help — 此帮助

安全规则：
  • 只允许私聊
  • 只允许白名单用户
  • 每个工作区同一时间只允许一个写执行
  • 状态和日志永远不会回灌到 Codex 上下文"""


def render_inspection_result(title: str, body: str) -> str:
    return f"{title}\n\n{body}"


def _trim(value: str, max_chars: int) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"


def _status_label_str(status: str) -> str:
    mapping = {
        "running": "运行中",
        "completed": "已完成",
        "done": "已完成",
        "failed": "已失败",
        "cancelled": "已取消",
        "queued": "排队中",
        "idle": "空闲",
    }
    return mapping.get(status, status)


def _phase_cn(phase: str) -> str:
    from wlcodex.auto_workflow import auto_stage_label
    # Check for auto workflow stages first
    auto_label = auto_stage_label(phase)
    if auto_label != phase:
        return auto_label
    mapping = {
        "running_analysis": "Codex 分析",
        "running_implementation": "开发工程师实施",
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

SURFACE_MODE_LABELS = {
    "product": "驾驶舱",
    "terminal": "现场",
}

TEAM_ROLE_LABELS = {
    "director": "总工程师",
    "investigator": "诊断工程师",
    "architect": "架构工程师",
    "implementer": "开发工程师",
    "tester": "测试工程师",
    "auditor": "审计工程师",
}

TEAM_ROLE_ORDER = (
    "director",
    "investigator",
    "architect",
    "implementer",
    "tester",
    "auditor",
)

TEAM_ROUTE_LABELS = {
    "Adaptive Engineering Team": "开发团队",
    "staged_auto": "开发团队",
}

TEAM_ARTIFACT_LABELS = {
    "architecture_plan": "方案",
    "implementation_report": "实现记录",
    "test_report": "测试记录",
    "audit_report": "审计报告",
    "verification_request": "验收请求",
}


def _latest_team_roles(
    roles: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    latest_by_role: dict[str, tuple[str, str, str]] = {}
    unknown_role_order: list[str] = []
    known_roles = set(TEAM_ROLE_ORDER)

    for role, model_profile, status in roles:
        if role not in known_roles and role not in latest_by_role:
            unknown_role_order.append(role)
        latest_by_role[role] = (role, model_profile, status)

    ordered_roles = [
        latest_by_role[role]
        for role in TEAM_ROLE_ORDER
        if role in latest_by_role
    ]
    ordered_roles.extend(
        latest_by_role[role]
        for role in unknown_role_order
        if role in latest_by_role
    )
    return ordered_roles


def _team_route_label(route: str) -> str:
    base = str(route or "").split("/", 1)[0].strip()
    return TEAM_ROUTE_LABELS.get(base, base or "开发团队")


def _humanize_team_text(text: str) -> str:
    replacements = {
        "needs_implementation: true": "需要改代码",
        "needs_implementation=true": "需要改代码",
        "implementation = true": "需要改代码",
        "implementation=true": "需要改代码",
        "needs_implementation: false": "无需改代码",
        "needs_implementation=false": "无需改代码",
        "implementation = false": "无需改代码",
        "implementation=false": "无需改代码",
    }
    result = str(text)
    for raw, label in replacements.items():
        result = result.replace(raw, label)
    return result


def _team_artifact_display_text(kind: str, summary: str) -> str:
    text = _humanize_team_text(str(summary).strip())
    if kind == "implementation_report":
        if (
            "implementation_report" in text
            or "生成实施报告" in text
            or "所有验证通过" in text
        ):
            return "已更新说明，并完成验证。"
    if kind == "test_report":
        if text == "Implementation test evidence collected.":
            return "已收集测试结果。"
    return _trim(text, 120)


def render_team_artifact_summary(artifact: str) -> str:
    kind, sep, summary = str(artifact).partition(":")
    if not sep:
        return _humanize_team_text(_trim(artifact, 120))
    label = TEAM_ARTIFACT_LABELS.get(kind.strip(), kind.strip())
    return f"{label}：{_team_artifact_display_text(kind.strip(), summary)}"


def render_team_status_summary(
    goal: str,
    route: str,
    roles: list[tuple[str, str, str]],
    latest_artifacts: list[str],
) -> str:
    lines = [
        "团队状态：",
        f"目标：{_trim(goal, 100)}",
        f"路线：{_team_route_label(route)}",
    ]
    if roles:
        lines.append("角色：")
        for role, _model_profile, status in _latest_team_roles(roles):
            role_label = TEAM_ROLE_LABELS.get(role, role)
            status_label = _status_label_str(status)
            lines.append(f"- {role_label}：{status_label}")
    if latest_artifacts:
        lines.append("最新产物：")
        for artifact in latest_artifacts[:4]:
            lines.append(f"- {render_team_artifact_summary(artifact)}")
    return "\n".join(lines)


def render_conversation_status(
    session: ConversationSession,
    latest_run: AgentRun | None = None,
    orch_run: OrchestrationRun | None = None,
    surface_mode: str | None = None,
    terminal_agent: str | None = None,
    active_phase: str | None = None,
    active_command: str | None = None,
) -> str:
    lines = [
        f"当前对话：{_trim(session.title, 80)}",
        f"模式：{MODE_LABELS.get(session.mode, session.mode)}",
        f"当前视图：{SURFACE_MODE_LABELS.get(surface_mode or 'product', '驾驶舱')}",
    ]

    # Terminal agent line — only shown when in terminal mode
    if surface_mode == "terminal" and terminal_agent:
        agent_label = {"codex": "Codex", "claude": "Claude"}.get(terminal_agent, terminal_agent)
        lines.append(f"现场 Agent：{agent_label}")

    lines.append(f"工作区：{session.workspace_alias}")

    if orch_run:
        phase_label = _phase_cn(orch_run.current_step or "")
        if phase_label:
            lines.append(f"阶段：{phase_label}")
        if orch_run.verify_round > 0:
            lines.append(f"轮次：第 {orch_run.verify_round} 轮")

    # Active phase and command summary from runtime state
    if active_phase:
        lines.append(f"当前阶段：{active_phase}")
    if active_command:
        lines.append(f"当前命令：{_trim(active_command, 60)}")

    if latest_run:
        if latest_run.agent:
            lines.append(f"最近运行：{latest_run.agent} / {latest_run.status}")
        if latest_run.token_input or latest_run.token_output:
            lines.append(f"Token：{latest_run.token_input} 输入 / {latest_run.token_output} 输出")

    if session.conversation_summary:
        lines.append(f"摘要：{_trim(session.conversation_summary, 120)}")

    return "\n".join(lines)


def render_conversation_help(profile: str = "natural") -> str:
    if profile == "natural":
        return "\n".join(
            [
                "WLCodex 已连接",
                "",
                "普通消息：Codex 分析/核验",
                "/auto：Codex 主导闭环（分析→确认→执行→确认→验收）",
                "当前视图：驾驶舱",
                "工作区：当前项目",
                "Codex：可用",
                "Claude：可用",
                "现场接管：可用",
                "",
                "直接发消息开始。",
                "",
                "[新工作台] [接管现场] [设置]",
            ]
        )
    return """WLCodex — 远程工作台驾驶舱

普通消息：Codex 分析/核验；/auto：Codex -> Claude -> Codex

驾驶舱与现场：
  • /product — 回驾驶舱
  • /terminal — 接管现场
  • /terminal claude — 接入 Claude 现场
  • /terminal codex — 接入 Codex 现场
  • /terminal agent claude — 接入 Claude 现场（显式）
  • /terminal agent codex — 接入 Codex 现场（显式）
  • /terminal tail — 恢复现场推送
  • /terminal pause — 暂停现场推送
  • /terminal detach — 停止现场推送但保留会话
  • /terminal product — 回驾驶舱
  • /mode — 查看当前模式

对话模式：
  • 直接发消息 — Codex 分析/核验
  • /codex <prompt> — 直接和 Codex 对话
  • /claude <prompt> — 直接叫 Claude Code 实施
  • /auto <prompt> — Codex 主导闭环（分析→确认→执行→确认→验收）
  • /verify — Codex 验收最新结果

常用命令：
  • /new — 开始新工作台
  • /stop — 停止当前运行
  • /status — 查看当前对话状态
  • /sessions — 查看历史现场
  • /history — 查看历史工作台
  • /workspaces — 查看可用工作区
  • /switch <workspace> — 切换工作区
  • /model — 切换或查看当前模型
  • /claude_mode — 切换 Claude 权限模式
  • /diff — 查看变更
  • /files — 相关文件
  • /health — 系统健康
  • /help — 此帮助

安全规则：
  • 只允许私聊
  • 只允许白名单用户
  • 每个工作区同一时间只允许一个写执行
  • 状态和日志永远不会回灌到 Codex 上下文"""


def render_workbench_history(sessions: Sequence[ConversationSession]) -> str:
    if not sessions:
        return "还没有历史工作台。发送 /new 开始新的工作台。"

    lines = ["工作台历史", ""]
    for session in sessions:
        marker = "*" if session.archived_at is None else " "
        suffix = "（当前）" if session.archived_at is None else ""
        age = relative_time(session.created_at)
        lines.append(
            f"{marker} {_trim(session.title, 42)}  {age}{suffix}"
        )
    return "\n".join(lines)


def render_workspace_list(
    workspaces: Sequence[object], *, active_alias: str = ""
) -> str:
    if not workspaces:
        return "当前没有可用工作区。请检查配置。"

    if active_alias:
        return f"选择工作区\n当前工作区：{active_alias}"
    return "选择工作区"


def _format_dt(value: object) -> str:
    if value is None:
        return "未知时间"
    text = str(value)
    return text.replace("T", " ")[:16]


def render_session_list(sessions: Sequence[ConversationSession]) -> str:
    if not sessions:
        return "当前还没有工作台。发送消息或用 /new 开始新的工作台。"

    lines = ["工作台列表："]
    for s in sessions:
        mode_label = MODE_LABELS.get(s.mode, s.mode)
        line = f"  #{s.id} [{mode_label}] {_trim(s.title, 60)} · {s.workspace_alias}"
        if s.archived_at:
            line += " · 已归档"
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


# --- Carryover renderers ---


def render_carryover_candidates(
    items: Sequence[tuple[ConversationSession, str]]
) -> str:
    if not items:
        return "没有找到可接棒的历史工作台。可以换个关键词，或用 /new 开始干净工作台。"
    lines = ["可接棒历史工作台", ""]
    for session, preview in items:
        lines.append(
            f"#{session.id} {_trim(session.title, 36)} · {session.workspace_alias} · {_format_dt(session.updated_at)}"
        )
        lines.append(f"摘要：{_trim(preview, 120)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_prepared_carryover(
    *,
    source_conversation_id: int,
    source_title: str,
    workspace_alias: str,
    preview: str,
) -> str:
    return "\n".join([
        f"准备从工作台 #{source_conversation_id} 接棒",
        f"来源：{_trim(source_title, 60)}",
        f"工作区：{workspace_alias}",
        "",
        "接棒摘要：",
        _trim(preview, 220),
        "",
        "请发送新任务目标。",
        "",
        "说明：新工作台只继承接棒摘要，不继承旧会话全文、旧执行状态、旧权限或旧终端现场，也不会启动 Claude。",
    ])


def render_carryover_brief_view(
    *, source_conversation_id: int, brief_text: str
) -> str:
    return f"接棒摘要 · 来源工作台 #{source_conversation_id}\n\n{brief_text}"


def render_carryover_target_created(
    *,
    source_conversation_id: int,
    target_title: str,
    workspace_alias: str,
) -> str:
    return (
        f"已从工作台 #{source_conversation_id} 接棒，创建新工作台：「{target_title}」\n"
        f"工作区：{workspace_alias}\n\n"
        "接棒摘要已带入。直接发消息会让 Codex 基于当前目标分析；也可以使用 /auto。"
    )


def render_carryover_cancelled() -> str:
    return "已取消接棒。可以重新 /carry 或使用 /new 开始干净工作台。"


def render_agent_result_summary(result: AgentRun) -> str:
    parts = [f"运行 #{result.id}"]
    parts.append(f"类型：{result.agent} / {result.role}")
    parts.append(f"状态：{result.status}")
    if result.prompt_packet_summary:
        parts.append(f"提示：{_trim(result.prompt_packet_summary, 120)}")
    if result.token_input or result.token_output:
        parts.append(f"Token：{result.token_input} 输入 / {result.token_output} 输出")
    return "\n".join(parts)
