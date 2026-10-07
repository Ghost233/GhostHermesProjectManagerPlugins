# 已知事实答复与所属项目记忆（#26）

公开管理入口为 `manage_memory(identity, action, details)`；Dashboard `/memory`、原生 `hermes_pm_memory` 工具和 `ManagementClient.manage_memory` 共用相同身份、来源范围与控制规则。`details` 只接受所选操作的准确字段，不能指定 actor、role、Owner origin、任意资料路径或借用权限。原生工具使用独立 participant 凭据，拒绝 Owner token alias。

## 已知事实答复

`answer` 引用 `request_id/human_request_id/query_id/material_ids`。来源必须是实际原提问者拥有查询和任务分享范围的 SourceGrant，查询与原任务、问题、实际 thread/turn/generation/service、执行安排和当前控制 grant 一致。Wiki 的公开结果已独立受理，全部结果发送段已核实，才能使用其 `fact` 材料。

负责人只处理一条可保守核对的事实问题；来源文字需支持问题的具体词项。新需求、取舍、审批、新授权、明确要求本人答复、选项问题、无法核实或不支持的措辞保持 Owner 待处理。事实答复引用原文，注明定位、版本与更新时间，内容始终是资料，不产生新授权。最终 HumanRequest 应答边界再次核对证据与准确引文，不允许以事实权限替换任意决定。

使用原服务的正式 RPC 应答或原问题的当前回合输入。当前唯一活动回合、原控制范围与有效 grant 是必要条件；idle、已结束、归还控制、过期、未知服务或不同回合都不能借事实答复开启新执行。稳定事实应答 ID 与原人工请求凭据保持幂等，未知结果不重放。手动接管使用每记录的实际原执行器，默认受管执行器不是替代通道。

## 验收后精选

`curate` 接受 `profile_id/entry_id/request_id/selection`，可用 `supersedes` 明确纠正旧摘要。`selection` 仅含明确 `facts` 查询／材料 ID、`decisions` 原人工请求 ID 和 `include_delivery` 结果索引意图。已核验冻结 Issue 交付及验收时间是必要条件；Wiki 回答、模型完成陈述和轮次结束本身没有验收效力。

只保存精选已验收事实、已确认 Owner 决定与最小测试／PR 索引。每条材料保留来源定位、来源版本／时间、查询范围及授权修订；每条摘要保留冻结 Issue 版本、固定源码提交／源摘要与验收时间。测试只保留命令、条目定位、退出状态和输出 digest，PR 只保留真实位置、版本和审查／合并状态。完整聊天、执行日志和资料库不被复制。

子负责人只写本项目。总负责人只写自己的 mono／协调摘要，可以保留可见子任务必要交付索引；总管只在自己的存储中保留必要汇总。上级可见任务不等于下级原始记忆的读写权。来源范围或责任绑定改变后，历史材料也不能越权读出或加载。

`read` 接受所属 `profile_id` 与可选 `include_superseded`。同一 entry ID 绑定不可变内容，重复写入返回原条目；不同内容拒绝。纠正保留旧文本、版本、时间和 `supersedes/replaced_by` 双向关系，活动查询排除旧条目，显式历史查询可查旧摘要。原资料库、旧档案与历史不被重写。

临时状态、未证实推断、建议、冲突、过期资料、待批请求和秘密不能成为长期事实。资料中继承的 Owner origin 不代表当前 Profile 是 Owner，也不能创建偏好或批准执行。

## 明确偏好与实际加载

`preference` 只有直接核验的 Owner 入口可调用，必须给出 `profile_id/entry_id/statement/scope`。scope 明确为所属项目、原任务或总管全局；缺失范围不会被推断。一次任务选择不能加载到后续任务，旧的已确认任务决定仍是历史事实，不能被提升为永久偏好。纠正继续保留替代关系。

`load` 给出已受理新 `request_id` 与必要 `entry_ids`，仅准备该任务自己的精选上下文。任务已有 session 时拒绝假称加载。`start_task` 在实际创建原任务前重新核对材料、来源范围和版本，在真实 `turn/start` 输入中引用这些资料；只有确认原服务 receipt 才记录 `loaded`、实际 thread/turn/generation、加载时间与整段输入 digest。未知启动结果保持待核实。

`supplement`（操作名 `supplement`）用于本人／负责人明确影响当前任务：给出 `request_id/entry_ids/expected_turn_id`，核对实际唯一活动回合、原执行器、责任与当前 grant，再沿 `control_task append` 送入。写入记忆本身不发输入，不改变运行会话已加载状态。重复或未知补入保留原 ID，不开新回合；归还后或空闲时不补入。

存储仅是插件 SQLite 中按角色项目分开的精选材料与索引。`external_memory='unverified'` 明确说明没有核验外部记忆服务、原生 Profile 记忆库或新模型会话的自行加载能力；原生 SDK 工具可用也不等于生产安装／迁移已经完成。

## 验证与仍未启用的实际能力

`test_project_memory.py` 从认证 Unix socket 与 Dashboard 驱动真实 local 资料读取、完整公开 Wiki 往返和原始 JSONL 服务请求，检验事实／Owner 边界、验收门槛、三角色隔离、源授权撤销、秘密与非事实排除、明确偏好范围、替代历史、新旧任务 context 和手动原控制。`memory_fixture_server.py` 为每次新的 thread/start 返回不同 thread／turn ID，使后续会话的实际输入可以独立核对。

`memory_smoke_runner.py` 经固定 pristine Hermes SDK 原生注册表驱动注册→公开资料→原事实 RPC→已验收精选→不同新会话的实际 turn/start 输入→卸载，人工 home 和外部网络保护均启用。mandatory SDK smoke 需要 `HERMES_REQUIRE_SDK_SMOKE=1` 与明确 pristine `HERMES_TEST_SDK_ROOT`，缺失 fixture 会失败。

这些验证使用新的合成材料、测试服务和人工 home，未读取真实 Wiki、Profile、个人记忆、用户原会话或凭据。真实原执行器／工具／文件系统边界、外部记忆服务、新 Profile 和测试群尚须按批准环境核验；没有证据时生产执行 gate 保持未启用，不降级 full access，也不安装或迁移真实资料。
