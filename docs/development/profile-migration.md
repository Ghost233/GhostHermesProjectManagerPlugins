# 选择性迁移到新 Profile

`Manager.migrate_profile(identity, action, details)` 由认证 Unix bridge 的 `ManagementClient.migrate_profile`、Dashboard `/migration` 和已核验本人群消息共用。原生 `hermes_pm_migration` 只读 participant 自己可见的计划和事实；工具参数不接受本人身份、宿主路径或迁移动作。迁移计划与实际原生落盘、外部身份验收、旧入口停止和切换分别记录。

本实现支持明确命名的 multiplexer satellite，包括项目负责人、总管和树外 Wiki/个人助手。原/目标保留各自已审定长期项目或独立范围。原 native Profile 名、identity、bot、native credential 和 Codex binding 不沿用；目标模型/provider/toolsets 按新职责重建。默认、launch、standalone、外部 bank 和缺少原执行控制证据的作用域保持受阻。原有精选项目记忆的 project/role/identity、own-task 验收限制不变；迁移不会改写旧精选 ledger 的身份。

## 本人审定的不可变计划

先分别登记原来源身份和独立目标身份、当前项目与非敏感连接引用。`preview` 只读取固定原 native Profile 的 `memories/MEMORY.md` 和 `SOUL.md`；返回真实原文件 locator、version、SHA-256 和有界候选内容。它不读取原 `.env`、通道、provider、bank、PID、仓库或全部聊天。

`plan` 必须带原预览版本和明确原/新 Profile IDs；同 ID 不同内容拒绝，目录变化拒绝。审定正文、digest、OwnerOrigin、原/目标身份/职责/仓库关系、SourceGrant revision 和原档案 provider binding 持久保存。后续准备用原 plan ID/digest，不能悄悄把旧决定更新到新选择或新权限。

实际材料选择的每项包含 `kind=knowledge|persona`、原 locator/version/digest 和确切选定文本。目标写入前再次验证原文件及文本范围；来源变化时停止，保留检查点和已经存在的准备目录。明确偏好只接受本人原始 statement 与指定 target Profile/project scope；不从原记忆或一次任务选择推导永久偏好。selected ledger 保留来源、内容 digest、目标身份及 native 回执；原人设/记忆/偏好不整包迁移。

以下是**合成示例**，版本和来源 SHA 必须使用本人实际看过的公开快照与 `preview` 结果：

```json
{
  "plan_id": "migration-reviewed-30",
  "expected_version": 8,
  "expected_profile_ids": ["old-lead", "new-lead"],
  "source_profile_id": "old-lead",
  "target_profile_id": "new-lead",
  "selection": [{
    "kind": "knowledge",
    "locator": "memories/MEMORY.md",
    "source_version": "<preview 的真实 SHA-256>",
    "source_digest": "<同一原文件的真实 SHA-256>",
    "text": "<本人确切选定的原文>"
  }],
  "preferences": [{
    "statement": "使用简短进度汇报。",
    "scope": {"kind": "project", "id": "mono"}
  }],
  "execution": {"model": "<新职责的模型>", "provider": "<新 provider>", "toolsets": ["memory"]},
  "archive_source_ids": ["explicit-original-history"],
  "external_memory": {"kind": "builtin"},
  "human_steps": [
    "开通独立的新机器人 app/open identity，并按新 native credential reference 签发凭据。",
    "按审定版本在新 native Profile 安装插件；登记新 bot 的准确 app/open identity、tenant、群及本人权限。",
    "按新职责核对模型路由、工具与执行接口；在停放目标中启动获准的全新 native 验证会话。"
  ]
}
```

旧档案来源必须先登记给新 identity，并由 Owner 明确设置长期保护。迁移只复用原 provider/source/grant/scope/authorization 映射；不会把 Wiki 原库连接复制为新库。最终切换要求非空的原档案清单、已有 `verified_native_cleanup_copy` 保护，以及覆盖全部已授权原会话的完整 SQLite 检查点。不能用窄授权备份兄弟历史；外部资料服务没有真实备份/保留能力时，计划受阻并显示具体来源。保护等级仍区分已验证 native cleanup copy 与 ordinary tools/外部服务未验证，不将迁移升级为全局永久保护承诺。

## 准备与真实新会话

原生设置示例：

```yaml
native_profile_migration:
  host_home: /canonical/approved/native-host
  memory_write_approval: true
```

宿主路径由受信任安装配置提供，管理请求不能指定路径。准备先建立完整 Manager SQLite checkpoint、哈希与控制意图，随后为审定原档案建立独立一致性检查点。原生操作在短生命周期、独立 HOME 和目标 HERMES_HOME 中运行；继承环境中的 provider/机器人凭据不进入子进程。

固定 SDK 的 fresh create 会 seed launch model/providers 并通知 multiplexer。因此真实 `create_profile(no_skills=True)` 先在没有运行 gateway 的隐藏准备根中完成，写原生 `gateway.parked` 标记和审定的新 model/provider/toolsets/builtin-memory 配置后，才原子发布到宿主 profiles 根。目标在首次可见时已经停放；原执行配置、通道、旧凭据、外部 bank 和 PID 均未复制。

知识/明确偏好经实际 `memory_tool` 和目标 on-disk store 写入；SOUL 经 SDK 的实际 Profile SOUL 接口写入及重读。native 回执中的 pending 不是已写入，拒绝不是成功。后续 `check` 只核对原 pending ID 和实际磁盘，不重发或绕过审批。原 native `/memory` 批准后才显示 written；拒绝后显示 rejected/blocked。审定新 config/SOUL/选定 memory/user 文件形成独立不可变材料检查点，排除 credentials、provider bank、channel、state.db、cron、PID、原 Profile 与仓库。

新 Profile 需要另行安装获准的插件 artifact；本操作不会克隆原 Profile 的插件设置或秘密。正常插件声明并注册 SDK `pre_api_request` observer。它在原目标 native 上下文核对原 SessionDB 的 `profile_name` 与持久 prompt，只保存实际请求的会话定位、model/provider、工具名及 prompt/tools digest，不保存 raw request、prompt、消息或 credentials。该 observer 不创建会话、回复、授权或迁移决定。

本人在停放目标中另行明确运行全新的 native 验证会话后，`check` 带该 session ID。插件核对创建时间晚于计划、目标 ownership、真实持久 prompt 中的选定知识/人设/偏好、第一次 user/成功 assistant、实际已记录 model/provider usage，以及正常 SDK hook 的实际工具列表。缺失插件/capture、工具被禁用、错误会话、失败输出、旧 prompt 或模型路由不符都保持 blocked。不会先补造 session metadata 再宣布闭环。

## 明确切换和受阻回退

`activate` 需要原 plan ID/digest、本人切换预览的 `expected_version`、明确 `expected_profile_ids`、原完成封存 `archive_operation_id` 和实际新 `session_id`。目录变化或不同 scope 拒绝。先持久化 switching intent，再重新核对原 SourceGrant/身份绑定、原 Profile 服务/bot/定时入口/manual-related execution 的当前独立停止证据，并在旧入口停止后建立最终原档案 checkpoint。

总管、独立 Wiki/个人助手及其他明确单入口迁移使用 `expected_old_profile_ids: [source_profile_id]`；`archive_operation_id` 为本人审定的稳定单入口停用 ID。它是 migration 自己的 single-entry retirement，不调用项目子树封存、不更改 projects/上下级、不扩大为其他 host。先验证新 native 材料、实际新会话和新 bot 身份，再保存原 Profile 的 `lifecycle=archiving` 与 durable gate；目录修正及 [重启后对账任务与人工请求](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/22) 重启都会保留抑制状态。随后只停止原 Profile 的受控 request/current executor/current grant，并逐项核对服务、bot、timer 和完整在途覆盖；unserved 受理不宣称 chat/cron 已终止。

原生 scope capability 的独立证据可列出 `manual_execution_ids`；已有只观察会话同样保留。本人在原界面明确处理，`check` 传 `handled_manual_execution_ids`，并取得实际 terminal proof 才可完成；一句已处理不够，不创建 takeover/interrupt。外部动作前保存每项 intent；丢回执/restart 只沿原 ID/原宿主/原执行对账，不重放 unknown request。单入口停止完成后仍不自动 serve 新入口；本人重新查看当前版本、沿同一单入口范围明确 `activate`，原 stop approved_scope 的 version 始终保留。

新的 Feishu identity 验收调用实际 `NativeFeishuTransport.verify_identity()`，核对独立新 app/open ID 与新的 native credentials；准确旧/新 namespace、群和本人范围来自 Owner 已登记配置。目标 `connection_refs.bot` 使用 `identity:<app_id>:<open_id>`。只登记引用或填写报告不能替代实际 bot-info 成功；目标未装配真实受控 transport 时阻塞。机器人开通、租户权限、凭据签发/撤销、群邀请和外部 bank 授权仍由明确人工步骤完成。

只有上述资料、授权和控制证据都满足，才向同一已核验 multiplexer 发原生 `serve-profile`，重读准确宿主/target serving 状态后解除目标 Manager admission gate。旧 Profile/入口保持封存，原 children 不自动恢复，旧 Codex/手动任务不自动续跑。成功后的同一原确认、prepare/check 返回带核实时间的同一 durable completed fact，不更新确认 version 或再做宿主动作。它表示该次切换的历史事实，当前运行状态仍由正常监督入口核对。

失败/未知结果保留同 ID、原 scope/digest、检查点和 target gate。准备或切换受阻可 `rollback`：验证原 Manager checkpoint 的 hash/integrity 与原计划，真实恢复到 Manager 自有 artifact；核对审定 target config/native memory 的检查点 bytes，并保留原生 parked intent。数据变化、未知 alias、未核实的运行执行或已实际切换后的停止覆盖不足时，保留材料并 blocked，不覆盖手工数据。

`rollback.checkpoint_data=verified_restored_artifact` 证明数据 artifact；`target_configuration/native_memory=checkpoint_bytes_verified` 证明目标 bytes；`target_control=parked` 证明未来 admission intent。它没有恢复原 Profile/外部服务，也没有倒退当前 SourceGrant、健康、通知或 Manager directory。`related_executions/external_services=unverified` 独立展示；parked/unserved 不证明已有 chat/cron 已终止。`old_entry=not_started` 和 `old_tasks=not_resumed` 表示插件没有启动/恢复旧执行，不把原入口原本是否运行的事实改写为停止。仓库及未提交/未跟踪改动不参与回退。

群命令均需真实本人 @，并关联原消息反馈同一状态与 digest：

- `准备迁移 <plan_id> <digest>`
- `核对迁移 <plan_id> <digest> [会话 <session_id>] [已处理 <manual_execution_id,...>]`
- `切换迁移 <plan_id> <digest> 版本 <reviewed_version> 封存 <archive_operation_id> 会话 <session_id> [单入口 <source_profile_id>]`
- `回退迁移 <plan_id> <digest>`

Dashboard 提供原材料 preview、不可变计划、selected ledger、人工待办、native/会话回执、原档案 checkpoints、切换和回退范围。本人先预览再确认；refresh 的 version 变化会撤销旧 plan/activate preview，确认时不会自动升级到新 version。

## 本地验收与剩余实际步骤

mandatory matrix 新增实际 migration case。真实固定 SDK fresh Profile、memory/USER/SOUL、审批批准/拒绝、原 SessionDB/archive保护/SQLite备份/完整查询、原 native gateway ControlSocket/serve/unserve 和普通插件 request hook 均执行。`AIAgent` 对自有 localhost OpenAI-compatible streaming peer 发真实请求并保存真实 prompt/messages/usage；Profile/bot/ticker 作用域使用已拥有的合成进程及当前退出事实，Feishu bot-info 为明确的外部服务响应 fixture。

独立 named Wiki、Ghost 和 steward 三条 mandatory cases 逐个执行正常 SDK 的新会话、原 SourceGrant/完整历史查询、single-entry stop/manual等待/明确完成、切换及受阻回退；unrelated/default Profile 与 PID 保持运行。Wiki case 丢失实际宿主动作的 Manager receipt 后重启，原 scope/version/未知状态与原观察 PID 保留，实证无重复 RPC/自动恢复。developer case 的原独立 Codex 协议服务真实启动子进程，收到原 turn/interrupt 后打断确认连接；重启沿原 service/thread/turn 对账，未核实前保留占用及 source admission gate，terminal 后释放，只有一次 start/interrupt，无旧输入/审批/续跑。执行配置不复制旧 `codex-development.json` 的 GitHub user、token、container、provider/bank 或 PID；新 local executor 另行按新计划重建。当前业务 GitHub 必须实查 Ghost233，当前测试计划禁止 container/Docker；静态回执不会把此类账户/运行权限显示为已验证。

```sh
PYTHONDONTWRITEBYTECODE=1 \
HERMES_TEST_SDK_ROOT=/approved/pristine/hermes-sdk \
HERMES_REQUIRE_SDK_SMOKE=1 \
python -m pytest -q -p no:cacheprovider --basetemp=/tmp/hpm-migration-check
```

可用 `HERMES_TEST_SESSION_PYTHON` 指向独立 test-only runtime；未提供时使用当前 Python。`.[test]` 的四个新增精确依赖取自固定 SDK lock：openai 2.24.0、anthropic 0.87.0、Pillow 12.3.0、tenacity 9.1.4。CI 安装 `.[test]` 并运行所有 mandatory SDK cases，不跳过缺失依赖；PR 验证保留，push 仅 main，远程 CI 用于同一已本地完整验收版本的最终确认。

这些证据覆盖公开协议与 native primitives。真实用户 Profile/原库/secret storage、实际新机器人和群权限、生产新会话、全部旧 chat/cron/background 的当前终止覆盖、外部 bank reader/writer 身份隔离、外部服务原位恢复和长期 scheduler 仍须按具体获准计划逐项验收。当前代码验收没有正式安装、开真实 bot、迁移用户原资料、业务网络调用或自动启动旧入口。

## 新机器人通道收据

`bot-info` 成功只证明新 app 的有效 bot 身份。切换前 `verify_new_bot` 对本计划 digest 重新读取每个新通道的群权限和实际平台消息，要求新 bot 的 `通道验收 DIGEST` 已投递到准确 tenant/chat，以及准确 Owner 在该消息下独立回复 `已受理验收 DIGEST`。`feishu_intake.channel_acceptance[DIGEST][CHAT_ID]` 只登记两条原消息定位符，不能填写状态或能力证明。权限拒绝、消息删除、另一 app/tenant/bot/group/Owner、缺独立受理或另一计划的文本均保持 blocked，发生任何旧入口控制动作之前先完成这些核验。适配器只读已有收据，不自动发送真实消息。
