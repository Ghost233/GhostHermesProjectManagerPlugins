# 公开分层协作

[按责任角色跨群派发并逐级回传](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/24) 使用同一权威 Manager 的目录、请求、队列和原执行证据。Owner 在入口群明确指定项目目标，总管在项目群真实 @ 总负责人；只有已独立受理该目标的总负责人，才能把明确 GitHub 子 Issue 派给 Owner 已登记的自己的 child。角色与 development/non_development 能力分别登记。合并两层时由 project_lead 直接承接项目工作，不自动创建 submodule 助手。Wiki 与个人助手保持 independent、无 project/parent 绑定。

## 公共入口

- `Manager.collaborate(identity, action, details)`；本机 token bridge 的 `ManagementClient.collaborate(action, details)`。identity 只来自入口 token，details 不能自称 actor/Owner 或声明权限。
- Dashboard `POST /collaboration` 接受 `{action, details}`，拒绝额外 actor 字段，复用宿主认证后的 bridge。Owner 表单核对既有群路由和原本人消息锚后提交明确目标。组织视图展示 parent、长期 Profile/project 绑定，以及 work/result/summary 的父目标、原锚、独立接收锚、任务确认锚和逐段凭据。
- 原 Hermes registry 的 `hermes_pm_collaborate` 使用独立 participant credential。允许 read_routes、delegate_issue、report_progress、report_result、report_summary、publish_owner_summary。model-facing 工具不能 register_channels、project_goal、owner_project_goal 或 ingest；Owner credential alias 在 participant bridge 被拒绝。
- 原 native owned Feishu 入口负责 ingest。接收 credential 与 Owner/model-facing token 分开；source is_bot、原 SDK sender/native IDs、header app/tenant、实际 recipient/mention、group、routed runtime、原 Gateway source authorization 和 bot budget 都先核对。

## 已登记职责与群身份

`register_channels` 只由 Owner 登记已有 Profile 与群，不创建 Profile、机器人或群。每个 channel 保存 id、profile_id、group_kind、project_id、app_id、recipient_open_id、recipient_tenant_key、transport_tenant_key、chat_id、owner_open_id、owner_tenant_key、repository、verification_ref 和 bot_sources。bot_sources 保存来源角色在该 app namespace 中实际观察到的 open_id、tenant_key、native_ids；不同 app 的 open_id 不互借。

总管可同时登记 entry/project 群；project_lead 留本项目群，subproject_lead 留其上级的大项目群。真实 CreateMessage builder 在目标群建立该群独立锚并构造 at；跨群不复用另一群的 native reply。入口回 Owner 则使用原 Owner 消息的真实 ReplyMessage builder 与 Owner mention。

每条新交接冻结两侧完整 channel/profile 身份快照。Profile 上级、原 app/group/recipient 或来源映射变化后，旧交接发送、受理或汇总返回 binding_conflict。已有旧版未带冻结 channel 身份的交接须重新对账，不能补猜为可信。跨上级移交只由总管执行，保持项目、native Profile、identity 与连接绑定；directory_audit 保留操作入口 actor、版本及 before/after 责任绑定，并按目录权限过滤查询。资料与项目记忆仍按原 Profile 所属模块保管。

## 工作与结果账本

1. `project_goal` 接受 Owner 明确 sender/target/source_anchor/issue_url；native Owner 项目命令由已核验 steward 入口映射至 owner_project_goal。OwnerOrigin 保留该原请求，机器人不能借它作新的 Owner 决定或执行审批。
2. steward 的 role-work 逐段发送与 API receipt 持久化；发送 delivered 后仍 awaiting_receiver。只有目标 bot 在独立原 native 入口看到全部原全文、正确 namespace 与来源，才进入原 accept_request / repository queue。Task 的 ActorProvenance 是实际接收角色。
3. `delegate_issue` 只接受 parent_handoff_id、target_profile_id、issue_url。目标必须是本人明确登记的该 lead 的 child，Issue 仓库必须匹配该接收群绑定。每个明确 Issue 仍走原 preparation/start/control/delivery 路径；总负责人自身 mono 可沿同一路径开发。
4. Owner 直接联系 child 时，原 task source 与确认/结果答复仍引用 Owner 原锚。另建立 child→parent 的 progress 记录；没有可核对的上级路由时 parent_sync_status=unverified。同步不建立另一个 task，也不代作上级批准。
5. `report_result` 对委派工作使用 `{handoff_id}`，对直接 Owner child 工作使用 `{request_id}`。必须原 task_delivery=delivered，且原负责人绑定未变。固定 Issue、源码/测试/原命令/后台进程/工作区证据仍由原 delivery 接缝验证。不能以自然语言 done 或 bot 送达回执代替。
6. child result 在 parent 原 native 入口独立收到后登记 received_results 与 awaiting_integration。`report_summary` 由该项目 lead 汇总原 task 状态和独立已收到的子交付证据，公开送回原 steward；`report_progress` 只传递当前原 Issue 状态。steward 独立收到后，在入口原 Owner 目标锚返回汇总。

所有 summary/result 都保留 whole_project_complete=false。[验证 mono 交付组合并按 Issue 返工](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/27) 的稳定组合与全局验证完成条件另行核对；子交付不能宣称项目整体完成。新的汇总内容建立新的记录，同样内容重复发布保留已有记录。普通 ack/thanks/进度文本不创建开发 task；只有严格原 role-work marker 进入派发。

## 投递、重复与恢复

- 每段有持久 UUID、独立 send receipt、原 native received anchor。原消息重复及重复分段不新增 task；第一段第一次独立观察锚保持不变，正文尾部空白属于全文核对的一部分。
- missing/unknown/failed/partial 发送与独立 reception 分列。所有原分段未独立收到前不受理；收到真实原消息是独立证据，API delivered 本身不是接单证明。
- 发送结果 unknown 或重启发现没有 in-flight 的 sending，保留 unknown；不自动重放。原任务 queue/executor 占用及来源冻结仍遵循既有仓库交接契约。
- manager Gateway 生命周期监督只排出持久待发送的公开材料，通过原已挂接 native transports 再次核对实际 app/open_id。source auth、recipient 与 runtime gate 不被后台转发代替。

## 离线验收与真实服务边界

`tests/test_collaboration.py` 从公共 Manager/token bridge/HTTP/original Feishu events 检查三层、合并两层、树外助手、直接请求、子原 stdio 交付、逐级汇总、scope/audit、route freeze、分段、重复、伪造 namespace 与 participant/source credential 隔离。测试不写数据库或伪造已交付状态。

`tests/collaboration_smoke_runner.py` 在 pristine Hermes SDK 的人工家目录中，用实际 GatewayRunner/OwnedFeishuAdapter/Lark CreateMessage、ReplyMessage builder 与原工具 registry 验证 steward/lead/child 三个已登记 bot namespace、entry/project 两个群。合成原 stdio peer 提供真实协议中的 commandExecution 测试证据，最终公开结果回入口原 Owner 锚。外网 HTTP、model launch 和真实配置/凭据访问被测试 audit guard 拒绝。这里验证合成边界上的实际 SDK 调用；不会据此宣称用户真实 Profile、群或服务已开通。

必跑命令：

```sh
HERMES_TEST_SDK_ROOT=/private/tmp/hermes-implementation/sdk-isolated HERMES_REQUIRE_SDK_SMOKE=1 \
  /private/tmp/hermes-plugin-dev-venv/bin/python -m pytest --basetemp=/private/tmp/hc24-final -q
```

SDK fixture 缺失在 mandatory 模式直接失败；普通 SDK skip 不构成验收。Dashboard 另需真实 React runtime 对新增关系与 Owner 表单操作验证；原来其他控件的点击记录不覆盖 [按责任角色跨群派发并逐级回传](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/24) 新控件。发布前仍需独立 merger、双轴审查和 Retro，以及授权测试 Profile/群/服务的具体验收。真实 bots、开通、生产配置、资料迁移、未知执行权限或未验证能力保持 unverified/off。
