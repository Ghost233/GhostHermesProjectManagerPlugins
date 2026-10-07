# 只读迁移档案、原生保留与恢复

来源工单：[28](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/28)，父规格 `docs/specs/hermes-plugin-v1.md` 的旧档案与永久保护约束。首个可审查增量从公共 token API 查询合成 SQLite 原始/压缩历史，查询前后数据库字节一致；后续测试使用固定 pristine SDK 和隔离合成 HOME。实际用户旧资料、群、原 Codex 服务、记忆服务均未迁移或查询，执行阶段 gate 保持原状态。

## 来源与身份

`register_archive_source(registration)` 只接受可信 Owner bridge。登记包含稳定 `id`、`kind`、可信宿主配置的 `provider_ref`、现有 `grant_source_id`、`new_profile_id`、明确 `scope_ids`、本人 `authorization_ref`，冻结新 Profile identity 与实际 provider/database/manifest 绑定，引用换到另一数据库不继承旧授权。既有 SourceGrant 同时核对原 Requester 查询主体和公开群的完整 app/tenant/recipient/chat 及共享范围。Wiki、群成员或上级权限不代替本人授权。新 Profile 只读自身明确迁移来源；增加原始来源需要 Owner 再登记并授予明确材料范围。

`query_archive(source_id, query_id, question, scope_ids, complete=False)` 走统一 token bridge；`query_id` 绑定原调用者、问题、来源、范围、完整意图。同 ID 不再次读取新版材料。资料返回原文、locator、原时间、逻辑一致版本哈希和覆盖说明。资料中的指令始终是数据，不触发任务、审批、steer、resume 或旧入口恢复。秘密拒绝公开。撤销范围后 snapshot 隐藏结果，未知群发送不换 UUID 重放。

普通 `hermes_pm_archive` 原生工具借用独立 participant token，只能查询本人明确授予该 Profile 的迁移范围，不能注入 caller/owner、路径、verifier、保护、备份或恢复参数。Owner token alias 被 participant snapshot 检查拒绝。群命令在原有可信 Owner @当前已登记机器人入口上使用：

- `查档案 SOURCE_ID SCOPE1,SCOPE2：问题`
- `完整档案 SOURCE_ID SCOPE1,SCOPE2：问题`

真实 SDK 回复 Builder @原提问者并 reply 原消息，稳定逐段 outbox 和 Dashboard `/archives` API / snapshot 展示相同结果。Dashboard 展示来源、覆盖、受阻、原生保护、每日副本与恢复后查询证据；目前查询/保护/备份/恢复通过认证 API，未新增 UI 提交表单。

## 三类覆盖

- **Hermes 本地**：显式普通文件 SQLite，`mode=ro`、`query_only=ON`、单一读事务。沿压缩祖先/唯一 continuation，排除 native `_is_explicit_fork_child_row(include_reset=True)` 所定义 branch/delegate/reset/tool。逐段 keyset 分页包含 inactive、compacted、summary、工具/模型原字段；保留原生 BLOB display identity 的 hex 编码。不存在的段、分叉歧义、循环、无法读的原字段均不能宣称完整。检索补读相邻上下文；完整请求返回实际原始行及必要 summary，而非只返回摘要。另可登记明确 ID 与批准绝对路径的 UTF-8 封存文件，读前后核对普通文件绑定与 stat；不扫描未登记目录。
- **飞书远端**：`FeishuArchiveProvider` 接受可信宿主的实际 client、确切 app/tenant/bot 绑定及 chat scope。每次要求当前来源身份证据；真实 `ListMessageRequest` GET 顺序分页，核对 chat/message identity、`has_more`、token 唯一性。无权、已删除内容、重复游标或分页无法结束明确 incomplete。真实端点尚未验收；没有原服务证据时保持 capability_unverified。公开 API 不可注入证明。
- **Codex 历史**：复用 #19 `ReadOnlyCodexAdapter` 原执行器身份/connection generation。只允许 read RPC，完整读取 turn 与 item 的全部 cursor 分页、核对读前后线程版本。不给同 HOME 的新 app-server 当原执行器，不遍历未经单独授权的 fork/subagent 祖先。`contextCompaction` 的压缩前上下文若仅通过 API 无法证明，返回明确缺口；pagination 尾部出现不代表压缩前原文已恢复。

宿主默认 `archive_providers` 仅装配明确本地数据库/session manifest（可选 `files`）。远端与原 Codex provider 是可调用的真实协议 adapter seam，需可信宿主装配原来源证明，不能用群或 Dashboard JSON 静态 True 开启。测试协议替身不等于真实旧服务验收。

## 永久保护的证据边界

查询不写 pin、不重新开启旧 Profile、bot 或 Codex。`protect_archive(source_id, protection_id)` 是单独 Owner 操作，先持久 pin intent，先只读核对明确 scope 覆盖全部 original session、原 schema version/表/列/主键与实际 SDK 的内存参考 schema 相容。范围不全或旧/未知 schema 在 writable SessionDB 初始化前拒绝，不能用保护操作隐式迁移。通过后再调用实际 `SessionDB.set_session_pinned()` 覆盖全部压缩段，创建一致性独立副本并调用实际 `maybe_auto_prune_and_vacuum(retention_days=0, min_interval_hours=0, vacuum=False)`。副本增加已结束且 archived 的 unpinned 控制 session，必须真实被删除，受保护原文/summary/compacted 行版本必须保留。receipt 状态为 `verified_native_cleanup_copy`；snapshot 另核对当前 pin 状态，外部 unpin 或来源改变不能沿用过去证明，`permanent_protection` 与普通工具读/写/删边界始终明确 unverified。receipt 明确记录 SDK 源码哈希、session 范围、原生清理结果与独立副本证明范围；缺 SDK、缺历史或能力失败保持 unverified，不能以 archive 标志替代证明。失败 pin 可能部分完成，应按已持久 operation 核对，不能换 ID 盲重试。

固定 SDK 指针：`hermes_state_sessions.py:SessionSessionsMixin.set_session_pinned`（pin 还会取消普通 hidden，可见性改变是此明确 Owner 操作的一部分）；`hermes_state_maintenance.py:SessionMaintenanceMixin.maybe_auto_prune_and_vacuum` / `_prune_filter_where`；`hermes_state_messages.py:_is_explicit_fork_child_row`。原生验收在合成原数据库实际执行 auto cleanup，pinned 压缩链及原磁盘 transcripts 保留，archived-unpinned session 和 transcript 真实删除。此实证覆盖固定 SDK / 合成授权来源，未宣称用户原库或全部外部写/清理路径已验收。没有普通工具 unpin、缩短保留或原资料删除接口；本人如需删除，必须另行明确原范围与副本影响，现有日副本轮换只删除插件自有副本。

## 数据检查点与恢复

Owner `backup_archive(source_id, backup_id, kind)` 只允许 `baseline` 或 `checkpoint`。SQLite 使用 `Connection.backup()`，校验 `integrity_check`、foreign keys 和恢复查询，不直接复制主文件或配置冒充 WAL/数据备份。文件附件随数据复制、保留时间、校验前后稳定版本与复制后的查询版本；变化期间无法建立一致版本会拒绝完整 checkpoint。整数据库 backup 仅当明确范围覆盖每个 original session 才允许，窄授权不能复制未授予的兄弟历史；需本人先扩充来源范围。checkpoint 存在 Manager 私有自有目录，公开状态只给 artifact ref / hash / coverage，不开放文件系统路径参数。

可信 Gateway 生命周期每天按 UTC 日历检查变化，启动补作当日最新实际数据；停机期间未运行的日期不伪造。无变化不新增。每天的最新变化副本按固定版本 ID 登记，同日重启后新版替代旧日副本，最近 7 个日期轮换。原资料、baseline 和长期 checkpoint 均保留。超过统一 bridge 的正常操作预算后回应 outcome_unknown，应从相同持久 query/protection/backup/restore ID 核对，不盲目换 ID 重试。版本变化失败及 blocked 状态可核对，不宣称未运行的每日任务已通过。

Owner `restore_archive(backup_id, restore_id)` 验证 retained hash、授权/范围和 SQLite integrity，将数据恢复到新的插件自有 artifact，实际打开恢复产物并完整再查询、核对原版本与材料。原资料和当前目录/Profile 状态不被覆盖；返回 `profile_state=not_restored`、`old_entry=not_started`。metadata 记录来源、SourceGrant revision、manifest、coverage，恢复不使旧配置成为新授权。飞书原库、原 Codex 原服务及记忆服务的原地恢复能力单列 unverified；本票 local artifact restore 不冒充外部服务恢复或正式迁移。

## 可重复验证

使用 `HERMES_TEST_SDK_ROOT` 的固定 pristine SDK、`HERMES_REQUIRE_SDK_SMOKE=1`、合成 tmp 目录：`tests/test_archives.py` 覆盖公开身份/范围、重复、撤权、真正消息 Builder、Dashboard、三来源缺口、每日轮换、sealed 原资料保留、同日补作、拒绝未授权 sibling backup、篡改拒绝和恢复后读取。`tests/archive_smoke_runner.py` 经原生 plugin registry 与 participant tool，实际原生压缩/compacted 行、pin、自动 prune、磁盘 transcript、SQLite checkpoint/恢复后的再查询，并审计禁止真实 HOME/外部网络。真实服务、真实群、原始用户资料、生产长期调度及正式迁移仍是单独环境验收缺口。
