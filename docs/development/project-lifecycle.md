# 项目封存与逐个恢复

统一入口是 `Manager.lifecycle(identity, action, details)`，认证 bridge、Dashboard `/lifecycle` 和已核验本人群消息复用该入口。责任角色不能代作生命周期授权；原生 `hermes_pm_lifecycle` 工具只读取当前 participant 可见事实，不接收 Owner、路径或执行操作。Profile.role 仍为插件目录职责，不冒充 Hermes 原生字段。

`archive`/`restore` 使用稳定 `operation_id`、`profile_id`、原预览的 `expected_version` 和明确 `expected_profile_ids`。提交时版本或当前子树不同即拒绝，不用新版本扩大旧决定；群消息在 prepare 时冻结同一范围，重复 ID 保留原 approved_scope。封存总负责人时冻结该 Profile 和全部显式子负责人、长期项目/仓库/native identity 绑定；Wiki、Ghost 和总管在树外。先把 `archiving` 和 `archive_intent` 连同 OwnerOrigin 事务保存，再请求停止原执行及入口。重复 ID 只返回原操作，内容不同拒绝；`check` 才核对当前事实，未知请求结果只读对账而不重放。

目录修正保留已有生命周期意图；封存期间不能新挂子负责人绕过范围。原 native identity、责任、父关系或仓库边界改变时处理受阻，不能停止另一身份。新受理、派发、追加、继续和新手动接管均检查项目/Profile 意图。未启动旧请求停止且取消其自动队列意图；恢复后它仍需新的明确工作请求。

## 独立停止条件

操作记录 `lifecycle_operations` 的 `checks` 分别展示原任务执行、只观察的手动执行、每个 Profile 的 `profile_service`、`bot`、`scheduled_entry` 与 `manual_execution` 覆盖。原任务使用 #20 的每条原执行器/current grant 和 #17 的 durable stop：interrupt 受理不释放占用，完整原 turn、相关 thread/item/background 和宿主进程覆盖核实停止后才释放。原执行或未知启动无法核实时保持处理中/受阻，不创建新执行器，也不改变 PR、Issue、改动或资料。

已观察的手动活动始终只读；封存不创建控制授权、不发送其 interrupt。本人在原界面明确处理后，通过 `check` 的 `handled_manual_session_ids` 登记对应记录，并再次取得当前终止证据。消失、断连、历史 idle 或本人一句已处理均不能替代终止证明。

native 组件动作前必须取得当前、同 operation/Profile/component 的 scoped control 证明；动作后独立取得当前状态证明。证明要求完整身份、`scope=profile`、有时区且不超过 300 秒的核实时间及非敏感 evidence。Host 范围、未知能力、过期或错误绑定都阻止该动作；没有实际终止证明不得报告封存完成。

## 实际 Hermes 接缝

`NativeMultiplexLifecycleHost` 实际调用固定 SDK 的 `gateway.control_socket.identify_gateway`、`request_unserve_profile` 和 `request_serve_profile_hot`，只操作明确绑定的命名 satellite。`native_profile_lifecycle` 原生配置包含一个 `host_home` 和明确的 `profile_homes`；没有请求体路径、shell、`--all`、service-manager stop 或 Profile 创建。default、宿主 launch Profile、standalone、未知宿主身份及不匹配的 canonical home 保持受阻。

封存先保存插件 intent，再写该 named Profile 的 `gateway.parked`，然后发送 scoped unserve；恢复只移除指定 named Profile 的 marker 并发送 scoped serve。marker 只防止下次扫描重新接入。真实 control socket 的 `unserved`/`served` 与当前 live identify 的完整 served set 分别核对；它们不能代替运行工作已停止。

实际 SDK `_unserve_profile` 会取消目标 adapter 重连、disconnect、移除 route/cache 和关闭 scoped MCP/状态句柄，但不保证中断原 chat turn 或在途 cron。cron pause 仅阻止未来触发，interrupted ledger 仅是记账。因此每个组件仍需独立当前宿主报告，包括原 Profile runtime、bot routing、scheduled admission、相关进程覆盖；缺少实际 scoped cancel/终止证据时等待本人处理或已获准的自然终结。不会调用全局 `get_running_job_details` 推断单项目已停。

原生配置不生成证明。管理自有目录的 `native-lifecycle.json` 按 `operation_id:profile_id:component:phase` 引用 `native-lifecycle-evidence/` 中的 SHA-256 报告。报告绑定当前 live host protocol/PID/start time/home/launch Profile、目标身份、动作/阶段及固定 SDK profiles/control_socket/profile_reconcile 源文件哈希。capability 需要实际 scoped-control case，state 需要四项独立 runtime case；每份报告必须有当前 binding、scope、核实时间、证据和实际状态。未知/缺失报告保持 durable blocked。

## 恢复与保留

`restore` 只处理指定 archived Profile；父负责人先恢复，子负责人逐个恢复且要求父已 active。外部动作前保存 `restoring` intent，并用 SQLite backup 建立、校验及哈希管理目录一致性检查点。该 checkpoint 仅覆盖管理目录，明确不覆盖 native Profile/bot/cron/source archives 和 notification-health；当前健康/授权不会从旧副本回放。长期项目绑定、资料索引和原记录保留，旧 Codex 停止决定仍抑制自动续跑；新工作需新受理/能力核实。

档案代查复用 #28 的原 requester、迁移来源授权、SourceGrant revision 和公开 namespace，不因总管职责开放原始来源。本人公开 @ 已登记总管，授权共享 channel 对应总管回传相关结论和原 locator；不恢复旧 Profile/bot/Codex。超范围拒绝，原库不写。封存不提升 `verified_native_cleanup_copy` 或 `permanent_protection=unverified` 的既有保护证据。

全部检查满足才生成一个 durable `lifecycle_events` 事实：`id=lifecycle:<operation_id>:completed`，`kind=archive_completed|profile_restored`，另含 operation/profile/profile_ids/project_ids/verified_at。处理中、受阻、静态标志及发送成功都不产生完成事件。#23 只消费该事实并由通知 ledger 去重；重启不会制造新 Owner 授权。

群命令使用真实 @：`封存项目 <ProfileID>`、`恢复负责人 <ProfileID>`、`核对生命周期 <operationID> [已处理 <manualID,...>]`。项目入口限自己的职责范围，总管入口可管理已登记项目；处理反馈关联原消息，未知投递不重放。Dashboard 展示同一 scoped checks、核实时间、档案状态及本人需处理事项，并预览明确操作后提交。

## 验证与实际缺口

公共测试演示父子封存、interrupt 受理仍占用、独立 bot 未停不完成、观察只读/本人处理、未知/host-wide 能力拒绝、目录纠正不清 intent、原绑定漂移受阻、queued cancel、父先恢复、子逐个恢复、公开授权档案/来源及检查点。

独立原 Codex 协议服务真实启动人工任务子进程，应用 interrupt 后打断原连接；公共 archive→manager restart→原 service 对账证明占用先保留后释放、一次启动/一次中断、无旧输入/批准/续跑。pristine SDK mandatory smoke 使用实际 plugin registry、Dashboard、GatewayControlServer 和 SDK scoped lifecycle verbs；其 runtime effects 为已拥有的合成 Profile/bot/schedule 子进程，逐个核实退出/恢复，真实 native profiles_to_serve 检查 parked 范围，shared default 和 Wiki/Ghost 进程持续运行，缺报告不发 native RPC/写 marker。

这证明固定 SDK 接缝与受控作用域，未证明用户真实 gateway/model/cron/background、真实群、所有旧原库保留或生产长期运行。真实接入仍需当前宿主报告与原执行覆盖；默认未配置/未核实能力保持 blocked。没有安装、创建生产 Profile、开机器人、迁移用户资料、访问真实 HOME/config/auth/history、Docker/container 或停止用户宿主。
