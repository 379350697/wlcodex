# Warp 与 WLCodex Agent 平台对比分析

日期：2026-05-21

范围：本报告对比的是 Warp 当前公开的 Agentic Development Environment / Oz Agent Platform 能力，以及本地 `wlcodex` 项目的现有实现和已写入的远程工作台设计。重点不是判断谁更“完整”，而是判断哪些 Warp 思路最适合借到 WLCodex 的本地业务里。

## 资料来源与判断边界

Warp 侧主要参考官方资料：

- Warp 开源公告：https://www.warp.dev/blog/warp-is-now-open-source
- Warp GitHub 仓库：https://github.com/warpdotdev/warp
- Warp 仓库工程说明 `WARP.md`：https://github.com/warpdotdev/warp/blob/master/WARP.md
- Warp / Oz 入门文档：https://docs.warp.dev/
- Agents 概览：https://docs.warp.dev/agents
- Agent Mode 文档：https://docs.warp.dev/agents/warp-ai/agent-mode
- Agent modality 文档：https://docs.warp.dev/agent-platform/local-agents/interacting-with-agents/agent-modality
- Oz Platform 文档：https://docs.warp.dev/agent-platform/cloud-agents/platform
- Cloud agents environments：https://docs.warp.dev/agent-platform/cloud-agents/environments
- Cloud agent 管理：https://docs.warp.dev/agent-platform/cloud-agents/managing-cloud-agents
- Cloud agent run 查看：https://docs.warp.dev/agent-platform/cloud-agents/viewing-cloud-agent-runs
- Universal Agent Support 博客：https://www.warp.dev/blog/universal-agent-support-level-up-coding-agent-warp
- Multi-harness Oz 博客：https://www.warp.dev/blog/multi-harness-cloud-agent-orchestration
- Skills 文档：https://docs.warp.dev/agent-platform/capabilities/skills
- Session sharing 文档：https://docs.warp.dev/features/session-sharing
- Agent session sharing 文档：https://docs.warp.dev/knowledge-and-collaboration/session-sharing/agent-session-sharing
- Warp Drive Web 文档：https://docs.warp.dev/knowledge-and-collaboration/warp-drive/web

本地 WLCodex 侧主要参考：

- `wlcodex/telegram_app.py`
- `wlcodex/orchestrator.py`
- `wlcodex/orchestration_runner.py`
- `wlcodex/task_service.py`
- `wlcodex/runtime_events.py`
- `wlcodex/runtime_state.py`
- `wlcodex/runtime_event_store.py`
- `wlcodex/agent_backend.py`
- `wlcodex/surfaces/terminal/manager.py`
- `wlcodex/workbench/sessions.py`
- `wlcodex/workbench/rendering.py`
- `wlcodex/interaction/renderer.py`
- `docs/superpowers/specs/2026-05-20-wlcodex-remote-workbench-cockpit-and-onsite-design.md`

边界说明：

- Warp 的客户端仓库已开源，但 Oz 服务端、云端编排、企业控制面等内部实现不能从公开仓库完整复原；本报告对 Warp 内部服务架构的描述是基于公开文档和产品行为的归纳。
- WLCodex 当前分析基于本地代码与 GitNexus 索引。GitNexus 资源提示索引相对 Git 有 5 个提交差异，但本地执行 `npx gitnexus analyze` 返回 `Already up to date`，因此本报告继续结合 GitNexus 结果和直接文件阅读判断。
- 本报告只给方案，不改业务代码。

## 一句话结论

Warp 最值得借的不是“把一切搬到云上”，而是它把 Agent 工作做成了一个可观察、可排队、可远程接管、可代码审查、可多 Agent 管理的产品面。WLCodex 已经有更适合本地工程自动化的内核：事件溯源、Codex 分析、Claude 实施、Codex 验收、Telegram 审批、workspace lock、worktree 隔离。下一步最优方向是把 WLCodex 的强内核外面包一层 Warp 式的 Web / Mobile Cockpit，而不是重写编排内核。

建议路线：

1. 先做本地 Web Agent Dashboard：手机浏览器可登录，展示运行中/排队/历史任务、阶段、日志、diff、验证结论。
2. 再补 Prompt Queue 与浏览器控制：忙时追加意图，完成后自动推进；允许 stop、continue、takeover、verify、merge/discard。
3. 然后做 Code Review 面板：把现在 Telegram 文本式 diff/验证升级成可扫视的文件级审查界面。
4. 再做 Agent Profiles / Skills：把硬编码模式沉淀成可复用配置与项目技能。
5. 最后再考虑 Warp/Oz 式多 harness、多 subagent、Docker/远端 runner。这个阶段价值大，但风险和工程量也大。

## Warp 的产品和技术架构复原

### 1. Warp 已经不是单纯 Terminal

公开定位里，Warp 是 “agentic development environment, born out of the terminal”。它有三个层次：

- Warp Terminal：本地桌面端，承载 shell、terminal block、rich input、file tree、diff/code review、Agent Mode。
- Warp Agent：嵌在本地工作区里的对话型 coding agent，能够理解自然语言、执行命令、编辑代码、根据输出继续迭代。
- Oz Agent Platform：更像云端 agent 控制面，负责 cloud agents、环境、任务、队列、触发器、审计、团队可见性、多 harness 编排。

这点对 WLCodex 很关键：Warp 的价值不是“Agent 会写代码”这一点，而是把 Agent 变成了可管理的工作负载。它把一次自然语言请求抽象成可跟踪的 run/task，把终端、对话、代码审查、远程查看和多端接入放到同一个产品面里。

### 2. Warp 的公开代码架构

从 `warpdotdev/warp` 仓库与 `WARP.md` 看，Warp 客户端是 Rust Cargo workspace：

- 主应用在 `app/`。
- UI 基础设施在 `crates/warpui/` 与 `crates/warpui_core/`。
- 核心工具和平台抽象在 `crates/warp_core/`。
- 编辑器能力在 `crates/editor/`。
- IPC 在 `crates/ipc/`。
- GraphQL schema/client 在 `crates/graphql/` 和相关 schema crate。
- 数据层使用 SQLite/Diesel。
- 桌面客户端支持 macOS、Windows、Linux，也有 WASM target 相关描述。

它的 UI 模式不是 Web 前端常见的 React，而是自研 WarpUI，公开工程说明里提到 Entity-Component-Handle 风格：

- 全局 `App` 持有 views/models。
- View 通过 handle 引用其他 view。
- `AppContext` 在 render/event 时提供临时访问。
- Elements 描述布局，风格接近 Flutter。
- Actions 负责事件处理。

对 WLCodex 的启发不是要照搬 Rust/WarpUI，而是它的“产品对象拆分”值得借：

- Session
- Conversation
- Agent run/task
- Agent profile
- Context attachment
- Diff/code review
- Worktree/branch/PR metadata
- Notification / attention-needed state
- Remote/shared session

这些对象在 WLCodex 里大多已有对应雏形，只是还没有变成统一的 Web 产品模型。

### 3. Warp 的交互架构

Warp 有两个相互分离但可切换的主模式：

- Terminal mode：干净终端，主要运行命令。
- Agent conversation view：专门承载多轮 Agent 对话，带模型选择、上下文附件、语音/图片/历史等 richer controls。

它还支持自然语言/命令 auto-detection，把用户输入路由到 shell 或 agent。这一点和 WLCodex 当前 `product` / `terminal` surface 切换非常相似：

- Warp：Terminal mode 与 Agent conversation view 分离。
- WLCodex：`conversation.mode.switched` 在 product/terminal 间切换；Telegram 输入在 terminal mode 下被 `_handle_terminal_text` 接管，防止误落入产品 orchestrator。

差异是：

- Warp 的模式切换是高密度 UI 原生体验。
- WLCodex 目前是 Telegram 命令和按钮驱动，适合手机消息入口，但不适合浏览 diff、队列、文件树、长日志。

### 4. Warp 的 Agent 管理架构

公开文档里，Warp/Oz 对 Agent 管理至少包含：

- 多个本地或云端 agent run 的统一列表。
- running / queued / completed / failed 等状态。
- 从桌面或 Oz web app 管理 cloud agents，且 web/mobile 可访问。
- 查看完整 session、命令、日志、上下文、输出。
- 对远程 agent run 继续追问、接管或 fork 到本地环境。
- cloud agents 支持触发器、计划任务、集成和 API/SDK。
- Oz 的 API 返回 run id、state 等任务状态。

Warp 的 Agent Dashboard 本质是一个控制面：

```text
Trigger / Prompt / API
        |
        v
Task / Run created
        |
        v
Queued / Pending / Claimed / Running / Succeeded / Failed
        |
        v
Transcript / Diff / Artifacts / Session link / Audit
        |
        v
Human review / follow-up / handoff / PR
```

WLCodex 现在已有类似后端基元：

- `RuntimeEventStore.append/list` 存事件。
- `runtime_state.replay_events` 从事件恢复 run 状态。
- `TaskService.reserve_waiting_task/promote_waiting_task` 有等待槽和排队雏形。
- `TaskService.setup_worktree/start_worktree_task/merge_worktree/discard_worktree` 有 worktree 隔离和合并雏形。
- `OrchestrationRunner` 有 run lifecycle、phase、delta、completed/failed 事件。
- `ChiefEngineerOrchestrator` 有固定的分析、实施、验证循环。

WLCodex 缺的是 Warp 那层可视化管理面和统一的 Agent/task 列表 API。

### 5. Warp 的云端/环境架构

Warp/Oz 对 cloud agents 的公开模型是：

- Trigger：Slack、Linear、GitHub Actions、schedule、API、manual run。
- Task：Oz 创建并追踪 agent 任务。
- Environment：Docker image、repo clone、setup commands、env/secrets。
- Host：Warp-hosted 或 self-hosted。
- Agent execution：在准备好的环境中执行。
- Outputs：PR、消息、报告、transcript 等。
- Teardown：容器销毁，保证每次从干净环境开始。

这个模型对 WLCodex 有启发，但不应该第一阶段照搬。WLCodex 的业务需求是“本地可用、手机可控、工程结果可靠”，不是立即变成企业 SaaS agent platform。更合理的借鉴是：

- 先把本地 workspace/worktree 当成 `local host`。
- 用 `TaskService` 的 worktree 作为轻量 environment。
- 将来再加 Docker runner 作为可选 isolation level。
- 更远期才考虑 cloud/self-hosted runner。

### 6. Warp 的 Universal Agent Support

Warp 不只跑自己的 Agent，还把 Claude Code、Codex、Gemini CLI、OpenCode 等 CLI coding agent 包进同一套 workbench：

- vertical tabs 管多 agent session。
- notification center 汇总各种 agent 的 attention-needed。
- native code review 可以把 inline comment 送回正在跑的第三方 agent。
- rich input 支持多行 prompt、上下文附件、图片、文件、saved prompts/skills。
- remote control 可以把 CLI agent session 发布到云端，用手机或另一台电脑监控和 steering。

这一点和 WLCodex 非常贴近：WLCodex 本来就是一个多 backend wrapper：

- `AgentBackend` 协议定义 `send/send_streaming/interrupt/health`。
- Codex backend 走本地 app-server/WebSocket。
- Claude backend 走 CLI stream-json。
- Orchestrator 把 Codex 和 Claude 组织成 Chief Engineer 工作流。

区别是 Warp 把“任意 agent harness”产品化了；WLCodex 目前把 Codex/Claude 深度绑定进一个强约束流程。两者不是冲突关系：

- WLCodex 的默认流程应继续保持强约束，保证结果质量。
- 同时可以新增 `AgentBackendRegistry`，把 harness 列表、能力、权限、resume 能力、worktree 模式、输出解析能力显式化。

### 7. Warp 的 Skills / Profiles / Permissions

Warp 公开文档里，Skills 是可复用 instruction set：

- 项目级或用户级。
- 每个 skill 一个目录和 `SKILL.md`。
- 可以带 scripts、templates、config。
- Agent 会先看到 skill 名称/描述，需要时再加载完整内容。
- 可通过 slash command 直接调用。

Profiles/Permissions 则控制：

- 模型选择。
- 权限模式。
- 命令 allow/ask/deny。
- 不同风险环境的 autonomy。

WLCodex 当前已经有很多“隐式 profile”：

- `/codex`
- `/claude`
- `/auto`
- Chief Engineer 默认流程。
- verify loop。
- Codex 分析/验证 no-write 约束。
- Telegram approval。
- Context budget。

但这些还没有形成用户可配置对象。借鉴 Warp 后，WLCodex 可以把“模式”从命令变为 profile：

```text
profile:
  name: safe-chief-engineer
  model_policy:
    analyst: codex
    implementer: claude
    verifier: codex
  permission_policy:
    shell: ask
    write_files: implementer_only
    network: deny_by_default
  workspace_policy:
    default: main_workspace_lock
    parallel: worktree_required
  verification_policy:
    max_rounds: 3
    require_tests: when_code_changed
```

这比继续增加 Telegram slash command 更可扩展。

## WLCodex 当前架构状态

### 1. 当前核心链路

WLCodex 当前更像一个“Telegram 驱动的本地 Chief Engineer”：

```text
Telegram / user message
        |
        v
WlCodexHandlers auth guard + command routing
        |
        v
Controller / OrchestrationRunner
        |
        v
ChiefEngineerOrchestrator
        |
        +-- Codex analysis
        +-- Claude implementation
        +-- Codex verification
        +-- retry / need_user / pass
        |
        v
Runtime events + Telegram output + task ledger
```

关键特点：

- Telegram 私聊 + allowlist 是主要入口。
- `WlCodexHandlers._guard` 做用户授权。
- `build_application` 注册 `/task`、`/status`、`/trace`、`/terminal`、`/product`、`/new`、`/codex`、`/claude`、`/auto` 等命令。
- `ChiefEngineerOrchestrator.run` 固定执行 Codex 分析、Claude 实施、Codex 验证，最多多轮修复。
- `ChiefEngineerOrchestrator._call_codex` 会 snapshot workspace，防止 Codex 分析/验证阶段改实现文件。
- verification packet 和 delivery drift 检测能防止“口头说完成但实际没交付”。

这部分是 WLCodex 相比 Warp 的优势：它更像本地工程治理工作流，而不是泛用 agent 对话。

### 2. Runtime event 与状态恢复

`runtime_events.py` 已经定义了很完整的事件类型：

- Telegram 输入/输出。
- run lifecycle。
- agent run queued/started/activity/heartbeat/waiting/completed/failed/timed_out。
- model call/tool call/file patch/approval/diff/verification/recovery/security。
- conversation mode。
- terminal session attach/detach/input/output。
- product display frame。
- workspace busy/queue。

`runtime_state.py` 用纯函数 reducer 从事件恢复：

- run 状态。
- agent 状态。
- surface 状态。
- workbench 状态。
- 验证通过才能合法完成 run 的保护逻辑。

这是非常适合 Web Dashboard 的基础。Warp/Oz 的 dashboard 需要一个任务系统数据库；WLCodex 已经有 event log，可以先直接做 projection，不必先重建复杂数据库。

### 3. Workbench / Terminal 设计雏形

本地设计文档已经提出：

- 一个 remote workbench。
- 两个手机视图：Cockpit 和 Onsite。
- Cockpit：简洁进度、决策、diff、审批。
- Onsite：原始 terminal live control。
- 视图切换不重启工作。
- session id 对用户隐藏。
- event log 是 source of truth。

代码中也已有对应模块：

- `surfaces/terminal/manager.py` 管 terminal session 引用、attach、input、tail、pause/resume。
- `workbench/sessions.py` 把 agent runs 投影成用户可读 session cards。
- `workbench/rendering.py` 渲染 Cockpit/Onsite 文本视图。
- `interaction/renderer.py` 按 surface 分发输出。

但当前实现仍偏 Telegram 文本交互：

- `/terminal tail` 仍提示现场会话实现后可用。
- session library 有 `LIVE/RESUMABLE/SUMMARY_ONLY` 概念，但 `LIVE` 还未充分落地。
- browser UI 基本不存在；本地 app-server 是 Codex 后端集成，不是用户 Web 产品。

### 4. TaskService 与队列/并行雏形

`TaskService` 已具备非常关键的后端能力：

- `ensure_workspace_available` 防止主 workspace 被并发写坏。
- `reserve_waiting_task` / `promote_waiting_task` 支持等待任务。
- `force_parallel_start` 支持用户强制并行。
- `setup_worktree` / `start_worktree_task` 创建隔离 worktree。
- `merge_worktree` / `discard_worktree` 支持回主分支或丢弃。
- merge 前拒绝 dirty main workspace，并处理 conflict。

这说明 WLCodex 不缺“并行执行的底座”，缺的是：

- 用户侧队列体验。
- worktree 任务可视化。
- 多 agent session 列表。
- 分支/diff/merge 控制面。

Warp 的 vertical tabs、Agent Dashboard、worktree metadata、remote control 刚好能补这一层。

## 逐项对比

| 维度 | Warp / Oz | WLCodex 当前 | 可借鉴结论 |
| --- | --- | --- | --- |
| 主入口 | Desktop terminal + Agent conversation + Web/Oz app + mobile browser | Telegram 私聊为主 | 保留 Telegram 做通知和轻操作，新增本地 Web Cockpit 做主控制面 |
| 交互模式 | Terminal mode 与 Agent conversation 分离，可 auto-detect | product/terminal surface 已有事件和路由 | 方向一致，但 WLCodex 需要更清晰的 UI 化模式切换 |
| Agent 管理 | Agent Dashboard 管理 local/cloud/running/queued/history | `/status`、runtime events、session cards 雏形 | 直接做 Agent Dashboard MVP，读取 runtime event projection |
| 队列 | Cloud task queue、API run state、管理视图 | waiting slot、workspace busy、run queued 事件 | 完成 prompt queue：忙时可追加、排队、取消、提升优先级 |
| 多 Agent | 多 harness、多 cloud agents、subagents、parallel orchestration | Codex/Claude 固定链路，worktree 并行底座 | 默认保持强约束流程；并行先限于 worktree 子任务 |
| 代码审查 | Native code review、inline comment 回 agent | diff/verification 以文本输出为主 | 做浏览器 Code Review 面板，绑定 retry/merge/discard |
| 远程/手机 | Web/mobile 查看和 steering，session sharing | Telegram 手机可用，但浏览能力弱 | 最该借：手机浏览器 Cockpit，Telegram 作为推送 |
| 会话分享 | session links、web viewer、协作/clone/fork | 私有 Telegram，基本无分享 | 先做“自用远程链接”，协作权限后置 |
| Skills | 项目/用户级 `SKILL.md`，自动发现，slash 调用 | Codex skills 存在于运行环境，但 WLCodex 未产品化 | 做 WLCodex skill registry，注入 ContextPacket |
| Profiles/Permissions | 模型、权限、工具、工作目录、autonomy 可配置 | slash command + 硬编码策略 | 把 `/auto` 等升级为 profile 对象 |
| Observability | run list、session transcript、audit、notifications | event log 很强，UI 弱 | WLCodex 后端更适合审计，前端需要补 |
| 环境 | Docker image、repos、setup commands、secrets、host | 本地 workspace/worktree | 先用 worktree 做 local environment，Docker runner 后置 |
| 通用 agent | Claude Code/Codex/Gemini/OpenCode 统一增强 | `AgentBackend` 协议已抽象 | 做 backend registry，而不是把每个 agent 写死到 handler |
| 安全 | command permissions、org access、secrets、self-hosting | Telegram allowlist、审批、no-write verify、delivery drift | 本地安全很好；Web 必须默认本地绑定和 token |

## 最值得借鉴的部分

### P0：Web / Mobile Agent Dashboard

这是最高优先级。

用户提到 Warp 最合适的原因是“对话型交互，也是 web 产品，手机浏览器能用”。这正是 WLCodex 当前最大短板：Telegram 可以手机用，但它不是 dashboard。长日志、diff、队列、多个 agent、worktree、验证证据，在聊天窗口里都会变得拥挤。

建议做一个本地 Web Dashboard：

```text
左侧：Agent / Task 列表
中间：当前 run 时间线、phase、agent output、verification
右侧：diff、files、approvals、queue、actions
底部：follow-up prompt / terminal input
```

第一版只读也有价值：

- 当前是否忙。
- 正在跑哪一个 run。
- 当前阶段是 analysis / implementation / verification / retry。
- 哪个 agent 在活动。
- 最近输出摘要。
- diff 和验证结论。
- 等待队列。
- 失败原因和下一步按钮。

实现上不需要先建复杂新状态库：

- API 从 `RuntimeEventStore` 读取事件。
- 用 `runtime_state.replay_events` / `replay_workbench_events` 生成 projection。
- SSE/WebSocket 推事件。
- 浏览器端做 projection cache。

这基本就是 WLCodex 版的 “Warp Agent Dashboard”，但更适合本地单人使用。

### P0：Prompt Queue / Busy Queue 产品化

Warp/Oz 的核心体验之一是任务可以 queued、running、completed，并且 cloud agent run 是可追踪对象。WLCodex 已经有 waiting task 机制，但用户体验还没闭环。

建议把忙时输入变成明确选择：

- `append to current run`：给当前 agent 追加上下文或纠偏。
- `queue next task`：当前 run 结束后自动执行。
- `start in worktree`：并行但隔离。
- `interrupt/takeover`：打断当前 agent。
- `discard`：取消排队消息。

对应事件：

- `run.queued`
- `workspace.busy.choice.presented`
- `workspace.busy.choice.selected`
- `agent.run.queued`
- `run.promoted`
- `run.cancelled`

这个功能非常贴合手机使用：人在外面看到 agent 正在跑，可以先把下一条想法排进去，不必打断当前工作。

### P0：Code Review 面板

Warp 强调 native code review，可以把 inline comment 直接送回 agent。WLCodex 现在有更强的验证机制，但展示层弱。

建议 Web 里做 Code Review：

- 文件列表。
- 每个文件 diff。
- 修改来源：Claude implementation / retry round / manual。
- Codex verification summary。
- 测试命令和结果。
- delivery drift 检测结果。
- 按钮：`Approve`、`Ask fix`、`Run verify again`、`Merge worktree`、`Discard worktree`。

关键不是做复杂 IDE，而是把“验收”从 Telegram 文本提升成可扫视界面。WLCodex 的可靠性优势会因此更明显。

### P1：Agent Profiles

Warp Profiles 把模型和权限绑定到使用场景。WLCodex 应该也做 profile，但要服务本地工程安全。

建议默认 profiles：

- `safe-chief-engineer`：Codex 分析、Claude 实施、Codex 验证，主 workspace lock，写文件需实施阶段。
- `fast-fix`：小改动，缩短上下文和验证轮次，仍需 diff review。
- `deep-review`：只分析和审查，不写文件。
- `codex-only`：直接 Codex。
- `claude-only`：直接 Claude，但最终仍可走 Codex verify。
- `terminal-onsite`：进入现场 terminal，适合人工接管。

Profile 应该控制：

- backend/harness。
- model。
- max rounds。
- context budget。
- write policy。
- shell command policy。
- workspace/worktree policy。
- verification policy。
- notification policy。

这样 Telegram 命令、Web 按钮、未来 API 都可以引用同一套 profile。

### P1：Skills / Project Instructions

Warp 的 skills 很值得借。WLCodex 当前已经运行在 Codex 技能体系环境里，但自己的编排未把 skills 产品化。

建议 WLCodex 支持：

- `.agents/skills/*/SKILL.md`
- `.codex/skills/*/SKILL.md`
- `.wlcodex/skills/*/SKILL.md`
- 用户级 `~/.wlcodex/skills/*/SKILL.md`

注入策略：

- Orchestrator 只把 skill 名称/描述放入初始 context。
- Agent 明确需要时再加载全文。
- Web/Telegram 支持 `/skill <name> <prompt>`。
- Profile 可默认启用一组 skills。

这能减少“每次都写很长 prompt”，也适合把你的本地业务规则沉淀下来。

### P1：Browser Remote Access，而不是多人协作优先

Warp 的 session sharing 很完整，但 WLCodex 首要需求不是多人协作，而是自己手机浏览器远程看和控制。

建议第一版只做：

- 本机 `127.0.0.1` 默认绑定。
- 可选 `--host 0.0.0.0` 或 Tailscale/Cloudflare Tunnel 手动暴露。
- 一次性 signed session token。
- 只允许 allowlisted owner。
- 所有控制动作仍经过 controller，不直接碰 subprocess。
- Terminal raw output 默认脱敏。

多人 view/edit、avatar、协作光标等全部后置。

### P2：Subagents / Worktree Fan-out

Warp/Oz 的自动多 agent 编排很诱人，但 WLCodex 不应第一阶段做泛化 DAG。更稳妥的是“计划驱动的 worktree fan-out”：

```text
Chief Engineer analysis
        |
        v
Plan splits into independent subtasks
        |
        +--> worktree A / Claude
        +--> worktree B / Claude
        +--> worktree C / Codex or Claude
        |
        v
Codex aggregate verification
        |
        v
human review / merge selected worktrees
```

适用场景：

- 文档/测试/实现分离。
- 前后端不重叠文件。
- 多个独立 bug。
- 大规模迁移先做样本 worktree。

边界：

- 默认不并行写主 workspace。
- 每个 subtask 必须有明确写范围。
- 合并前必须 Codex aggregate verify。
- 冲突时必须人工审。

这比“让一堆 agent 自由发挥”更符合 WLCodex 的可靠性路线。

### P2：Universal Agent Backend Registry

Warp 对第三方 CLI agent 的支持说明：未来 agent harness 会一直变化，控制面最好高一层。

WLCodex 已经有 `AgentBackend` 协议，但还可以进一步显式化：

```text
AgentBackendSpec:
  id
  display_name
  command
  transport: websocket | stdio | jsonl | pty
  supports_streaming
  supports_interrupt
  supports_resume
  supports_diff
  supports_worktree_mode
  permission_model
  output_parser
```

收益：

- 新增 Gemini/OpenCode/本地模型时，不改 Telegram handler。
- Web Dashboard 可以用同一套能力表展示按钮。
- Orchestrator 可以按能力选择实现 agent。

## 不建议直接照搬的地方

### 1. 不要先做完整云平台

Warp/Oz 的 cloud agents、integrations、schedules、self-hosting、enterprise access controls 很强，但 WLCodex 当前最重要的是本地可靠性和手机可控。先做云平台会把复杂度拉爆：

- 认证授权。
- 远端代码安全。
- secrets 管理。
- runner 生命周期。
- 网络隔离。
- 审计合规。
- 成本控制。

第一阶段应该是 local-first web cockpit。

### 2. 不要把 Telegram 丢掉

Warp 的 Web/mobile 很强，但 Telegram 对 WLCodex 仍有价值：

- 推送天然好。
- 手机输入快。
- 审批按钮简单。
- 私聊 allowlist 简洁。
- 无需给公网开 Web。

更合理的组合：

- Telegram：通知、审批、短 prompt、紧急 stop。
- Web：dashboard、diff、日志、队列、terminal onsite。

### 3. 不要弱化 Codex 验证闭环

Warp 更强调 agent workbench 和多 harness。WLCodex 的独特优势是“Codex 分析和验证夹住 Claude 实施”，还有 no-write snapshot 和 delivery drift 检测。不要为了做成通用 agent chat 把这个闭环稀释掉。

默认模式仍应是：

```text
Codex 分析 -> Claude 实施 -> Codex 验证 -> 人类验收/重试
```

通用 agent chat 可以作为 profile，而不是替代主流程。

### 4. 不要先做复杂协作

Warp session sharing 支持多人、权限、web viewer、edit access，这对团队产品有价值。但 WLCodex 本地业务先做 owner-only remote access 就够了。多人协作会牵涉：

- 身份系统。
- 多用户权限。
- 控制权冲突。
- terminal secret 泄漏。
- 操作审计。

这些都应该等单人 Web Cockpit 稳定后再做。

## 推荐目标架构

建议 WLCodex 形成如下分层：

```text
Surfaces
  - Telegram Bot
  - Local Web Cockpit
  - Optional mobile browser remote tunnel
  - Future CLI/API

Application Controller
  - Conversation router
  - Workbench controller
  - Action authorization
  - Prompt queue controller

Agent Orchestration
  - ChiefEngineerOrchestrator
  - AgentBackendRegistry
  - Profile resolver
  - Skill/context resolver
  - Verification policy

Execution
  - Main workspace lock
  - Worktree tasks
  - Terminal sessions
  - Future Docker runner

Event Source
  - RuntimeEventStore
  - Runtime reducers/projections
  - Audit and recovery

Storage
  - Task ledger
  - Runtime event log
  - Profile/skill config
  - Optional projection cache
```

核心原则：

- Event log 仍是 source of truth。
- 所有 surface 都发 action，不直接操作 agent subprocess。
- Web 不复制 Telegram handler 逻辑，而是调用 controller。
- Diff、verification、queue、session state 都从 projection 来。
- 默认本地安全，远程暴露必须显式开启。

## 分阶段落地方案

### Phase 1：Warp-like Agent Dashboard MVP

目标：手机浏览器能看清 WLCodex 正在干什么。

范围：

- 本地 Web 服务。
- 登录 token。
- 当前 run 列表。
- active/queued/completed/failed 状态。
- runtime event 时间线。
- 当前 phase。
- agent activity。
- verification summary。
- Telegram 仍保留通知。

不做：

- 多人协作。
- 云 runner。
- inline code edit。
- 复杂文件树。

验收标准：

- 在手机浏览器打开 dashboard，能看到当前任务状态和最近输出。
- Telegram 发起任务，Web 自动更新。
- Web 刷新后能从 event log 恢复状态。
- 没有 runtime event 时显示空状态，而不是死页面。

### Phase 2：控制动作与 Prompt Queue

目标：手机浏览器不仅能看，还能轻量 steering。

范围：

- queue next prompt。
- append to current run。
- stop/cancel queued。
- continue/retry。
- switch product/terminal view。
- terminal input 走 `TerminalSessionManager`。
- queue position 和预计 next action。

验收标准：

- 主 workspace busy 时，新 prompt 不丢失。
- queued task 在当前 run 结束后能被提升。
- 用户能取消 queued task。
- 终端输入不会误进 product orchestrator。

### Phase 3：Code Review / Verification 面板

目标：让 WLCodex 的验证优势可见。

范围：

- changed files 列表。
- unified diff viewer。
- test/verification evidence。
- delivery drift warning。
- ask fix / verify again / approve。
- worktree merge/discard 按钮。

验收标准：

- 用户不需要翻 Telegram 长文本就能判断本次改动。
- worktree merge 前显示 main workspace dirty 风险。
- verification 未 pass 时不能一键标记完成。

### Phase 4：Profiles 与 Skills

目标：把隐式操作习惯沉淀成可复用配置。

范围：

- Profile config 文件。
- 默认 profile 列表。
- Web/Telegram 切换 profile。
- Skill discovery。
- Skill 注入到 ContextPacket。
- `/skill` 或 Web skill picker。

验收标准：

- 不改代码也能新增一个项目 skill。
- profile 能控制 backend、验证轮次、写入策略。
- Web 显示当前 run 使用的 profile 和 skills。

### Phase 5：Worktree Subagents / Universal Backend

目标：借鉴 Warp/Oz 的多 agent 编排，但保持 WLCodex 的安全边界。

范围：

- backend registry。
- subtask plan schema。
- worktree fan-out。
- subtask status cards。
- aggregate verification。
- selective merge/discard。

验收标准：

- 并行任务写集互不冲突时可以独立执行。
- 冲突必须进入人工审查。
- 每个 subagent 都有 transcript、diff、verification。
- 主 workspace 不被未验证任务直接污染。

### Phase 6：可选 Docker / Remote Runner

目标：在本地 Web Cockpit 成熟后，再引入更强隔离和远程运行。

范围：

- Docker image config。
- setup commands。
- env/secrets。
- run artifact。
- teardown。
- future self-hosted worker。

验收标准：

- 同一任务在干净环境可复现。
- secrets 不进 transcript。
- 失败时能保留足够日志。

## 最小可行 API 设计草案

第一版 Web Cockpit 可以只需要这些 API：

```text
GET  /api/workbench
GET  /api/runs
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/events
GET  /api/runs/{run_id}/diff
GET  /api/queue
POST /api/actions/prompt
POST /api/actions/queue
POST /api/actions/append
POST /api/actions/stop
POST /api/actions/retry
POST /api/actions/verify
POST /api/actions/terminal-input
GET  /api/events/stream
```

数据来源：

- `/api/runs`：从 runtime events replay。
- `/api/queue`：从 TaskService / ledger / runtime queue events。
- `/api/runs/{id}/diff`：沿用现有 diff/verification 机制。
- `/api/events/stream`：SSE 推 runtime event append。

Action 原则：

- API 不直接调用 `AgentBackend`。
- API 只发 controller action。
- controller 决定是否允许、是否需要 approval、是否排队。
- 所有 action 都写 runtime event。

## 对本地文件的实现映射

| 目标能力 | 当前可复用位置 | 需要补的部分 |
| --- | --- | --- |
| Web Dashboard 状态 | `runtime_event_store.py`、`runtime_state.py` | HTTP API、SSE、前端 projection |
| 当前 run/agent 列表 | `runtime_events.py`、`workbench/sessions.py` | 更完整 session card、LIVE 状态 |
| Cockpit/Onsite | `workbench/rendering.py`、`surfaces/terminal/manager.py` | Web 视图和 action bridge |
| Telegram/Web 双 surface | `interaction/renderer.py` | 抽象 Web output manager |
| Prompt queue | `task_service.py` waiting slot | 用户选择、取消、提升、可视化 |
| Worktree 并行 | `setup_worktree/start_worktree_task/merge_worktree/discard_worktree` | Dashboard 操作和风险提示 |
| Chief Engineer 流程 | `orchestrator.py` | profile 化、Web 阶段展示 |
| Backend 抽象 | `agent_backend.py` | registry/spec/capabilities |
| Code review | verification packet、diff event | Web diff viewer、inline follow-up |
| Security | Telegram allowlist、approval | Web token、bind policy、redaction |

## 风险评估

### 大改风险

最高风险来自“把 Web surface 做成新的主流程”。如果 Web handler 直接绕过现有 controller 调 agent，会破坏当前 safety model。必须坚持：

- Web 只是 surface。
- Controller 仍是唯一动作入口。
- Event log 仍是事实来源。
- Orchestrator 的验证闭环不被绕开。

### 中改风险

Prompt queue 和 worktree 操作会触碰任务生命周期。风险包括：

- queued task 被重复启动。
- main workspace lock 与 worktree 状态不一致。
- stop/interrupt 后 agent subprocess 残留。
- Telegram 和 Web 同时操作产生竞态。

缓解：

- 所有动作写 event。
- task state transition 做幂等。
- UI 按 projection 展示，不按按钮乐观假设。
- 中断动作要有 terminal/agent heartbeat 后续确认。

### 小改风险

Profiles/Skills 初期风险较低，但容易变成“配置很多，实际没人懂”。建议先内置少量 profile，skills 只做 discovery + explicit invocation，不要一开始做复杂推荐。

## 最终建议

最适合 WLCodex 的 Warp 借鉴路径是：

```text
先借产品面：
  Agent Dashboard
  Web/Mobile Remote Cockpit
  Prompt Queue
  Code Review

再借组织模型：
  Profiles
  Skills
  Backend Registry

最后借规模化能力：
  Worktree Subagents
  Docker Runner
  Remote/Self-hosted Runner
```

WLCodex 不应该为了像 Warp 而变成 Warp。它应该保留“本地、可控、强验证”的核心优势，然后把 Warp 已经验证过的 Agent workbench 体验借过来。换句话说，WLCodex 的方向不是“云端 ADE”，而是“本地 Chief Engineer 的手机/浏览器控制塔”。

如果只选一个最该先做的功能，我建议选：

```text
Local Web Cockpit + Agent Dashboard MVP
```

因为它能同时解锁：

- 手机浏览器使用。
- 多 agent/session 可见。
- 队列可见。
- diff/验证可见。
- 后续所有控制动作的容器。

这也是 Warp 对当前 WLCodex 最直接、最可落地、收益最大的启发。
