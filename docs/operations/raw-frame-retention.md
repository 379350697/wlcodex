# Provider raw frame 归档、迁移与回滚手册

> **适用范围：** 仅 `provider_raw_frames` 的原始 provider payload。不要把
> `runtime_events`、Native timeline、task artifact、任务摘要或语义事件纳入
> 清理范围。

## 保留合同

`[runtime_retention]` 的默认值如下：

```toml
[runtime_retention]
hot_retention_days = 7
archive_retention_days = 90
interval_seconds = 21600
batch_size = 250
scheduled_apply_enabled = false
# archive_dir 默认是 SQLite 所在目录的 provider-raw-frame-archives
```

- 近 7 天 frame 留在 SQLite 热库，支持正常运行中的快速回放。
- 过期 frame 先按 UTC 日期写入**脱敏、版本化** `.jsonl.gz`，再登记 manifest、
  frame-to-archive 索引和 sequence cursor，最后在同一可恢复步骤中删除热行。
  任何中断都不能形成“先删后归档”。
- manifest 记录 archive ID、格式版本、路径、SHA-256、frame 数量、ID/时间范围
  和到期时间；索引让按 frame ID 的读取先查热库、再回查归档，语义保持一致。
- `verify` 与按 frame ID 回查均流式读取 gzip JSONL；首次大历史迁移的归档校验
  不会把整份 archive 或它的 index 一次性装入内存。
- sequence cursor 不依赖热表 `MAX(sequence)`，因此归档后序列仍连续。
- 活跃 agent run 或活跃 Native turn 的 frame 必须跳过，留在热库等待下一轮。
- archive 超过 90 天后可由 retention worker 清理；语义摘要和其他运行证据不清理。

`scheduled_apply_enabled` 默认关闭。初次大历史迁移只能在维护窗口运行并验收后，
才允许开启每 6 小时的后台保留 worker；不能在 GET、启动恢复或普通请求中偷偷
执行。除此之外，SQLite 会持久化一个“首迁已验证”标记：即使配置被误设为
`true`，scheduler 在维护窗口 `apply` 成功并完成 archive `verify` 前也会拒绝
apply；维护窗口打开时它同样会暂停，避免与人工归档/compact 竞争。

## CLI

所有命令显式指定配置，输出结构化 JSON，非零退出表示校验失败：

```bash
# 只计算候选、活跃跳过数；不写库、不写归档。
wlcodex-retain-raw-frames --config config/wlcodex.toml dry-run

# 先原子关闭新提交并显示尚未排空的 Relay、Native、审批和历史 Telegram 流程。
wlcodex-retain-raw-frames --config config/wlcodex.toml \
  maintenance-begin --note "2026-07-10 release"

# 只读探测维护窗口前遗留的 Native Codex 候选会话；绝不 start/continue/
# steer/interrupt。unknown 或 active 都会让 ready=false。
wlcodex-retain-raw-frames --config config/wlcodex.toml maintenance-probe-native

# 只读查看维护闸门和剩余活跃工作；ready=true 后才可继续。
wlcodex-retain-raw-frames --config config/wlcodex.toml maintenance-status

# 仅在 maintenance-begin 已冻结提交且 ready=true 时：写归档、登记
# manifest/index、删除已安全归档的热行，并清理到期 archive。
wlcodex-retain-raw-frames --config config/wlcodex.toml apply

# 校验每个 archive 的读取、数量和 index 对应关系。
wlcodex-retain-raw-frames --config config/wlcodex.toml verify

# 仅在已通过 verify 的维护窗口执行 SQLite VACUUM。
wlcodex-retain-raw-frames --config config/wlcodex.toml compact

# 未排空或决定取消时，明确重新开放提交。
wlcodex-retain-raw-frames --config config/wlcodex.toml maintenance-cancel
```

不要把 `compact` 当作归档成功的证明；它只回收 SQLite 空闲页。归档、哈希、数量
和 frame 回查的证明来自 `verify`。

## 首次历史迁移维护窗口

首次大库迁移（例如数十 GB）不是常规请求的一部分。维护窗口必须满足以下顺序；
任一前置条件不成立即取消窗口，不切换数据库：

1. 运行 `maintenance-begin` 关闭新的提交入口。随后执行
   `maintenance-probe-native`，只读确认维护前遗留的 Native Codex 候选会话是否
   真的仍有 active/waiting turn；`notLoaded`、缓存历史和 `idle` 不会误阻塞。
   `maintenance-status` 必须显示 `ready=true`，即所有活跃 Relay task、真实 Native
   turn、待审批项和历史 `orchestration_runs` 均已排空。探测失败或状态未知同样阻塞；
   无法排空时运行 `maintenance-cancel`，而不是强行归档活跃 frame。
2. 记录当前版本，停止 `com.wlcodex.formal`，并在同一文件系统创建发布前
   SQLite 快照和 archive 目录快照。确认目标卷有足够空间容纳旧库、归档和新库。
3. 先运行 `dry-run`，人工确认候选数量、活跃跳过数和 archive 目标目录；再运行
   `apply`。该命令会在写入后立即执行同一份 archive `verify`，且仅验证通过时
   写入“首迁已验证”标记；随后仍应独立运行 `verify` 并保存其 JSON 作为发布
   证据。任何 manifest、SHA、数量或 index 错误都视为失败，不继续。
4. 对原数据库执行 `PRAGMA integrity_check`。在原库仍保留的前提下，以
   `VACUUM INTO` 生成新文件，再对新文件执行 `PRAGMA integrity_check`。示例：

   ```bash
   sqlite3 runtime/wlcodex.sqlite3 'PRAGMA integrity_check;'
   sqlite3 runtime/wlcodex.sqlite3 \
     "VACUUM INTO 'runtime/wlcodex.sqlite3.compacted';"
   sqlite3 runtime/wlcodex.sqlite3.compacted 'PRAGMA integrity_check;'
   ```

   仅当两次完整性检查都返回 `ok` 时，才在相同文件系统内原子交换数据库文件。
   `compact` CLI 适合较小的已验证维护；大型首迁移应使用这个可回滚的
   `VACUUM INTO` 步骤。
5. 运行数据库迁移和应用发布，启动
   `launchctl kickstart -k gui/501/com.wlcodex.formal`。用 **GET** 验证本地和
   公网 `/health`，再分别冒烟 Native、Relay task 和一个
   `legacy_compatible` Telegram 历史流程。

归档文件是运行数据的一部分：与 SQLite 快照一起保留，不能只备份数据库而遗漏
`archive_dir`。

首次迁移和验证完成后，才可将
`scheduled_apply_enabled = true` 写入 `[runtime_retention]` 并重启服务。这样每
6 小时的常规 worker 才会开始执行；它只处理后续过期 frame，不替代首次维护窗口。

## 回滚与故障处理

- `dry-run` 和 `verify` 失败：不运行 `apply` 或数据库交换；保留证据并退出窗口。
- `apply` 中断：重新运行 `verify`。热行在 archive 完整登记前不会被删除；对
  未登记的临时文件先调查，不把它当作可读 archive。
- manifest、hash、数量、frame 回查或烟测失败：停止新版本，用发布前 SQLite
  快照、匹配的 archive 目录和上一版服务回滚；不要尝试手工删除索引来“修好”
  数据。
- 原子交换后问题：先停止服务，再恢复同一份 SQLite 快照和 archive 快照，启动
  上一版服务，重复 `integrity_check` 和 `verify`，最后才重新开放提交入口。

维护记录至少保存：版本、开始/结束时间、排空证据、磁盘预检、dry-run/apply/
verify JSON、两次 integrity check、切换/回滚决定及 Native/Relay/Telegram
烟测结果。
