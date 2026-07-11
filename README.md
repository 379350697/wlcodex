# WLCodex

WLCodex 是把本机开发会话和长任务编排安全地带到手机/浏览器的产品。它不再把
Telegram Workbench 当作主产品：**Native 是直接会话的真相，Relay 是长任务与
编排的真相，Telegram 只保留历史兼容入口。**

## 当前产品与导航

从 PWA 或网页开始的固定路径是：

```text
/native  →  /native/workflows  →  /native/workflows/relay  →  task detail
```

| Surface | 用户任务 | 真相来源 |
|---|---|---|
| Native (`/native`) | 查看、恢复和继续直接 provider 会话 | 真实 Native session；同步失败时明确展示缓存、时间和恢复指引 |
| Workflows (`/native/workflows`) | 选择已实现的工作流 | Native 工作流目录 |
| Relay (`/native/workflows/relay`) | 创建长任务、跟踪协作、处理阻塞、查看验收证据 | Relay task 与其已持久化 artifact/run |
| Telegram | 已有历史 conversation 的恢复、回调和会话兼容 | 仅 `legacy_compatible` 记录；新工作跳转 Native/Relay |

Relay 首页只承诺“任务”和“设置”。Skills、Profile、Dev Flow 和工作树等未完成
能力不应作为产品入口；旧 URL 只做兼容说明或跳转。

当前用户可见语义、状态机、动作和无障碍合同见
[当前产品语义合同](docs/product-semantics.md)。它优先于所有历史设计和 review。

## Relay 执行方式

Relay 只提供三种有业务语义的模式：

- **标准执行**：创建前显示执行合同，系统自动选择角色与子代理。
- **先计划**：架构/执行计划得到用户确认后才进入实现。
- **目标验收**：必须填写目标和验收条件；完成需要一个具体 implementation run
  和实际执行过的独立测试或审计证据。

历史 `simple`、`auto` 数据读取时映射为“标准执行”。不再暴露无实际含义的
“使用子代理”手工开关。

每张任务卡、列表、详情和 SSE 初始快照使用同一个只读 `presentation`：
`state`、`freshness`、`current_actor`、`blocking_reason`、`next_action` 与
`allowed_actions`。详情 GET、页面刷新和 SSE 首帧不会 reconcile 生命周期、创建
artifact 或 dispatch；这些变更由带幂等 claim 的后台 worker 完成。

## Telegram 历史兼容

Telegram 不再获得新任务功能。已有持久化 `legacy_compatible` conversation 仍可
按原有恢复、回调和会话路径处理；危险动作仍遵守实际后端状态。新的 Telegram
对话、`/new`、plain text、`/auto`、`/codex` 和 `/claude` 不创建旧主状态，而是
提供 Native/Relay 入口。`/native` 与 `/relay` 是跳转命令；每个 Telegram 按钮
只能序列化为 URL 或 callback，不能同时携带两种动作。

## 数据保留与维护

只治理 `provider_raw_frames`：SQLite 热库默认保留 7 天，脱敏、版本化 gzip JSONL
归档保留 90 天，摘要、`runtime_events`、Native timeline 和 task artifact 保留。
归档可按 frame ID 回查，并且 archive 写入、校验、登记后才会删除热库行。

运行配置：

```toml
[runtime_retention]
hot_retention_days = 7
archive_retention_days = 90
interval_seconds = 21600
scheduled_apply_enabled = false
```

首次大历史迁移必须在维护窗口中完成；不要在 GET、启动恢复或普通后台请求中做。
即使误把 `scheduled_apply_enabled` 设为 `true`，scheduler 在维护窗口 `apply` 成功且
archive `verify` 通过、持久化首迁完成标记前也只会拒绝运行，不会开始清理历史库。
新部署仍应从 `false` 开始；本仓库当前正式运行配置已完成首迁校验，因此显式设为
`true`，每 6 小时执行一次只读 Native turn 探测后再处理到期 raw frame。探测失败或
状态不明时，该次会 fail-closed，保留对应 Codex frame，不会按缓存状态猜测删除。
完整的 dry-run、apply、verify、compact、`VACUUM INTO`、原子交换和回滚流程见
[Raw frame 维护手册](docs/operations/raw-frame-retention.md)。

## 本地运行

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
cp config/wlcodex.example.toml config/wlcodex.toml

# 填好 Telegram allowlist、工作区和本地运行参数后启动。
.venv/bin/wlcodex --config config/wlcodex.toml
```

Native live stream 配置保持 loopback；公网访问仍沿用既有 tunnel 和认证配置。
本次版本**不改变**公网 token/cookie/认证传递方式，也**不改变** Native 完全访问
权限策略。

## 测试与 CI

默认 pytest 门禁是 `not slow and not live`，**不排除 integration**：

```bash
# 默认质量门禁（包含 integration，排除 slow/live）
.venv/bin/python -m pytest -q

# 明确显示同一选择器，适合 CI 或人工复现
.venv/bin/python -m pytest -q -m "not slow and not live"

# 全量本地测试（包括标记测试；live 仍需要其环境变量/凭据）
.venv/bin/python -m pytest -q -m ""

# 静态检查
.venv/bin/python -m ruff check .
```

GitHub Actions 执行前两项质量门禁。浏览器、真实 Native/Relay、SSE 断线和历史
Telegram 兼容流还需要在发布前维护窗口中做真实烟测，不能只依赖单元测试。

## 发布维护窗口

一次发布不等于一次冒险。维护窗口按以下门槛推进：

1. 执行 `wlcodex-retain-raw-frames maintenance-begin` 暂停新提交，再执行
   `maintenance-probe-native` 只读确认 Native Codex 候选会话的真实 turn 状态；用
   `maintenance-status` 确认活跃任务、真实 Native turn、待审批项和历史兼容流程均已
   排空。探测未知或未排空就执行 `maintenance-cancel` 取消。
2. 备份 SQLite 与 raw-frame archive，预检磁盘，执行 retention dry-run/apply/
   verify，任何 manifest 或完整性失败都不切换。
3. `apply` 会在同一维护窗口中重新校验 archive；只有成功 `verify` 后才写入允许
   后续 scheduler 运行的首迁完成标记。用 `VACUUM INTO` 生成新库并完成 `integrity_check` 后，在同一文件系统中原子
   交换；运行迁移与应用发布。
4. 启动 `com.wlcodex.formal`，以 GET 验证本地和公网 health，再冒烟 Native、
   Relay task 与一条历史 Telegram 兼容流。
5. 任何 archive、数据库完整性或烟测失败，都用发布前 SQLite/archive 快照和
   上一版服务回滚。

操作细节与证据清单在
[Raw frame 维护手册](docs/operations/raw-frame-retention.md)。

## 文档状态

`docs/superpowers/` 下的按日期设计、计划、review 与报告均为历史审计材料，已
**superseded**，不再表达现行产品事实。请从
[历史文档说明](docs/superpowers/README.md) 进入，或直接阅读当前语义合同。
