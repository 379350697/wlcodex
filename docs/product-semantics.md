# WLCodex 当前产品语义合同

> **现行合同。** 本文是 Native、Relay、SSE 与 Telegram 兼容层的共同
> 用户可见语义。`docs/superpowers/` 中所有按日期归档的设计、计划和 review
> 都是历史材料，不能覆盖本文。

## 产品边界与导航

WLCodex 不把底层数据库实体硬合为一个对象：Native session、Relay task 和
历史 Workbench 各自保留其生命周期和审计记录。对用户而言，它们通过一个
只读 `presentation` 投影说同一种状态语言。

正常入口固定为：

```text
/native  →  /native/workflows  →  /native/workflows/relay  →  task detail
```

- **Native** 是直接会话的真相来源。它显示真实 provider/session 状态；同步
  失败时必须显示缓存来源、最后更新时间和恢复指引，不能伪装为在线或替换模型。
- **Relay** 是长任务、角色协作和验收的真相来源。它创建、调度和展示 task，
  但不拥有或重写 Native session 的历史。
- **Telegram** 是历史兼容入口，不是新功能入口。已有、持久化为
  `legacy_compatible` 的 conversation 可以完成原有恢复、回调和会话流程；新
  对话、`/new`、plain text、`/auto`、`/codex`、`/claude` 只提供 Native/Relay
  跳转，不再创建旧主状态。

PWA 的起点为 `/native`。Relay 首页只暴露“任务”和“设置”；没有实现完成的
Skills、Profile、Dev Flow 和工作树入口不应作为承诺功能出现。旧 URL 可以给出
兼容说明或跳转，但不能制造一个看似可用的空页面。

## 统一 presentation 投影

所有 Relay task 详情、Native session、SSE 初始快照和 Telegram 兼容提示应使用
下面的只读字段。投影可以由多个底层记录组合而来，但读取投影本身绝不修改
任务、创建 artifact、派发角色或触发 provider 操作。

| 字段 | 合同 |
|---|---|
| `state` | `running`、`waiting_user`、`waiting_approval`、`blocked`、`completed`、`interrupted`、`failed` 或 `stale`。状态表示现在可观察到的工作事实，不是按钮文案。 |
| `freshness` | 包含来源、最后更新时间、是否陈旧和陈旧原因。缓存、断连、未知同步结果都必须可见。 |
| `current_actor` | 当前负责推进的人或系统角色；无负责人时明确为空，而不是沿用旧角色。 |
| `blocking_reason` | 不能继续时的具体原因；没有阻塞时为空。 |
| `next_action` | 一个唯一、可解释的下一步。 |
| `allowed_actions` | 此刻真正允许的动作集合；界面不得展示后端不会执行的操作。 |

`GET`、页面刷新及 SSE 首帧只读取上述投影和已持久化证据。生命周期 reconcile、
artifact 生成、dispatch 和恢复扫描由后台 worker 领取，并使用数据库幂等 claim
防止重启、并发或重放导致重复处理。

## Relay 任务合同

Relay 只支持三种用户可理解的执行模式：

| 模式 | 创建时承诺 | 进入实现的条件 |
|---|---|---|
| 标准执行 | 系统按任务自动选择角色和子代理，并在创建前展示执行合同。 | 创建即按合同调度。 |
| 先计划 | 先产出架构/执行计划。 | 用户确认计划后才进入实现。 |
| 目标验收 | 必填目标和验收条件。 | 有 implementation run，且独立测试或审计证据已经实际执行。 |

历史 `simple`/`auto` 数据兼容映射到“标准执行”。不再提供没有业务语义的“使用
子代理”手工开关。

动作的语义也必须可验证：

- 活跃任务点击“新建”时，用户必须选择“后台继续”或“真实中断后新建”；不能
  静默丢弃活动 run。
- 暂停、停止、恢复、补充信息和审批只在 `allowed_actions` 包含该动作时出现。
  每个 mutation 有 loading、幂等键、成功/失败反馈和可重试路径。
- 审批 supersede 是一个原子结果：provider cancel/deny、数据库状态、task
  projection 和 pending 计数要一起收敛，不能留下可再次批准的幽灵请求。
- 验收绑定一个具体 implementation run，并展示声明测试的
  `passed`、`failed` 或 `not_run`；“没有运行测试”不能被渲染为通过。
- 队列按 workspace 领取。消费者只有取得 lease 后才能消费；失败释放或过期后
  可以安全重试。

任务列表、卡片和详情统一显示阶段、责任角色、最新 handoff、阻塞原因、最后
新鲜时间和唯一下一动作。筛选在服务端完成，再分页；页面内隐藏当前页元素不是
筛选。Blocked Inbox 按“等待我 / 等待系统 / 需要恢复 / 已陈旧”归类，并提供
恢复、补充信息、归档和证据入口。

## 交互、连接和可访问性合同

- “发送”与“中断”是两个独立控件。仅专用 interrupt button 可以请求中断；
  点击输入框、附件或发送不会把事件冒泡成中断。
- SSE 在正常连接时停止秒级轮询；断线后可重连，页面隐藏时暂停，恢复可续接。
  自动滚动只在用户停留在底部时发生；否则显示“有新消息”提示。
- 弹窗遵循标准 dialog 行为：初始焦点、焦点圈、Escape、焦点陷阱和背景 `inert`。
  状态/日志使用 `role=status` 或 `role=log`，完成结果可被辅助技术播报。
- 移动端允许缩放，文本对比度至少 4.5:1，保留 forced-colors，所有可触控操作
  的热区至少 44px。

## 有意不在本版本改变的边界

本次收口**不改变**公网环境的 token/cookie/认证传递方式，也**不改变** Native
完全访问权限策略。其他语义、体验和可靠性改动不得借此扩大这两个边界。

## 运行数据保留

`provider_raw_frames` 是唯一受保留策略治理的大体积原始数据：SQLite 热库保留
7 天，脱敏、版本化 `.jsonl.gz` 归档保留 90 天，摘要、`runtime_events`、Native
timeline、task artifact 和语义事件永久保留。完整维护与回滚流程见
[Raw frame 维护手册](operations/raw-frame-retention.md)。首次历史归档必须由已排空
的维护窗口完成并通过 archive `verify`；持久化验证标记前，后台 scheduler 不会
执行 apply。
