# 原任务控制切片（#17）

本切片在 #16 的原 stdio 服务、连接代次、任务会话及仓库占用上实现追加、任务停止和明确继续。JSONL 协议 peer、人工宿主收据及 pristine Hermes SDK smoke 都是离线验证。当前没有获准模型服务的追加、中断、后台核实或继续验收；#16 的完整生产写入边界也仍未通过。真实能力保持未启用，不能以收据形状或 RPC 返回成功替代真实结果。

## 一个控制入口

`Manager.control_task(identity, request_id, action, instruction_id, text=None, expected_turn_id=None)` 是管理实例公共接缝。`ManagementClient.control_task` 使用同样的参数，但身份只从令牌 bridge 取得。Dashboard `/task` 和原生 `hermes_pm_task` 以 `action=append|stop|continue` 调用它。调用体不接受 actor、角色、控制权、verifier 或 enabled 声明。

`instruction_id` 是调用者保存的稳定操作 ID。同一个任务内，ID 与动作、文字、预期回合及真实调用者永久绑定；相同输入沿原记录返回，变更内容拒绝。RPC 超时或未知应答后保留该 ID；未核实的旧输入既不能用原 ID 重发，也不能换 ID 重发。只有新的明确停止及完整停止核实可以形成之后新安排的边界。`clientUserMessageId` 仅供协议关联，插件自己持久去重，不假定服务已提供持久去重。

本人或当前仍持有冻结任务责任的负责人可以控制。可见目录、上级身份或当前 Profile 名称不等于控制授权。已交付工作释放仓库后，原任务控制授权过期，追加不能绕过新的受理安排。目录、负责人或 Codex 引用纠正后，负责人不能延伸原控制关系；本人可按原 session 的实际仓库、服务及既有控制范围处理旧停止，不能把原任务转到新仓库。新的继续还须通过当前受理责任与原仓库边界核对。连接代次、实际服务或仓库物理布局不符时，控制待对账，历史路径不能替代原执行器。

飞书输入必须先唯一关联到原任务消息。明确命令为：

- `追加：要求`：追加到原会话。
- `停止` 或 `结束当前任务`：登记停止意图。
- `继续：要求`、`明确继续` 或 `继续原工作`：登记停止之后的新执行安排。
- `核对执行`：共享 refresh 入口核对原执行；停止处理中时自动核对停止条件。

多个候选先澄清，其他文字仅保存关联材料，不猜测停止或继续。消息 ID 派生的稳定关联 ID用于控制去重；反馈沿原群任务锚和真实 @ 的既有 outbox 回传。Dashboard 展示相同停止状态、RPC 受理、仓库占用、遗留相关执行与旧停止/继续证据；未知控制结果保留提交 ID。

## 追加与竞态

活动线程必须有唯一 `inProgress` 回合，其 ID 与登记会话及 `expected_turn_id` 一致。追加只发 `turn/steer`，携带真实 `expectedTurnId`；服务端前置条件拒绝活动回合竞争。RPC 返回的 `turnId` 也必须匹配。受理反馈只说明服务接受输入，执行结果仍由原 thread/read 与后续证据核对。

idle 输入以登记的上一回合为 `expected_turn_id`。先确认原 cwd、直接输入能力、idle 状态、上一回合完整终态以及所有返回回合均属于插件登记的回合链；未登记插入回合先对账。登记 durable intent 后，在 adapter 写锁内再读一次原会话及 cwd，然后 `turn/start`。新回合必须与已读回合不同，实际 ID 落盘，原 thread 不变。

`turn/start` 没有 expected-idle 原子前置条件，而且协议允许它转为 steer。adapter 锁只能序列化自身写入；因此 idle 输入另须当前可信宿主的排他输入路径能力收据，证明其他客户端不能在核对与发送之间插入输入。缺少这份实际能力证据时关闭 idle 输入与继续，不以重复 read 冒充跨客户端竞态保障。

## 停止证据与占用

停止先持久保存 `controls` 中的操作和 `stop_records` 中的停止标记，再尝试原 `turn/interrupt`。服务未知、断线或回合改变也保留停止意图。`{}` 只将 `rpc_status` 标为 accepted；状态继续为 stopping，原仓库占用不释放。RPC 不重放，不 kill 全局进程、清理终端或操作 daemon。

`refresh_task` 在停止处理中核对：

1. 同一服务/连接代次、原仓库与原 thread，匹配停止回合的完整 `completed|failed|interrupted` 终态。
2. 原 thread 返回的全部回合、完整 loaded/list 的同仓库线程、collab receiver 子线程的完整回合与相关 item。idle 但缺少子线程回合证据仍不算停止。
3. 对每个相关 thread 全页读取实验 `thread/backgroundTerminals/list`。缺方法、缺 nextCursor、循环 cursor、畸形项、活跃后台或未结束工具都保留停止处理中。
4. 当前可信宿主的 process coverage：已实际验证不存在未登记进程执行路径，或绑定原 thread/turn 的全部关联进程退出收据。空原生后台列表不能证明 nohup、shell 后代或其他未登记进程已退出。覆盖未知时保留占用，不进行全局进程扫描或终止。

只有完整证据满足时标记 stopped、结束当前外层安排并释放原任务占用。保留原会话、用户文件、改动、停止时间及必要身份/回合/后台/进程覆盖证据。停止不产生交付，不回滚，不关闭 Issue、合并 PR或自动续跑。后续 `refresh_task` 对已停止外层安排只返回停止记录。

## 明确继续与可信收据

明确继续必须有已确认停止记录、当前有效责任及原服务代次，并且没有其他未释放的同逻辑仓库任务；当前实际执行覆盖证据也重新核验。先登记新的 `execution_arrangements`，记录原 request/thread、上一 stop/turn、真实授权者、当前基线、连接代次及占用，再发核实 idle 的原会话新回合。应答未知仍保留新安排和占用。旧 `stop_records` 保持原终态；新的活动安排不能改写旧停止决定。

可信宿主的 `codex-validation.json` 继续要求 #16 的四类当前 hashed 收据，可另外包含 `task_control`。这份收据与固定二进制、配置、平台、仓库 fingerprint、服务与 generation 绑定，包含：

- 实际方法 `thread/read`、`turn/steer`、`turn/start`、`turn/interrupt`、`thread/backgroundTerminals/list`、`thread/loaded/list`。
- active_append、idle_input、interrupt、stop_verification、explicit_continue、wrong_turn、duplicate_instruction、disconnect、background_pagination、related_children、exclusive_input 的实际 PASS 结果，以及真实原 thread/turn 和不同的新 turn。
- `unregistered_process_paths=disabled_and_verified` 的实际路径能力证明；或 `process_coverage` 的 `kind=task_processes_stopped`、原 thread/turn 及 `all_registered_processes_exited=true`。后者在停止时必须匹配当前原回合，旧回合退出证据不能核实新回合。

解析器只验证可信宿主已有证据的完整性和当前绑定；它不运行验收探针，也不把静态 PASS 文字变成实际能力。群、HTTP、原生工具和任务进程都不能上传 validator 或写管理实例证据目录。当前没有这些获准真实控制收据，生产控制保持关闭。

## 验证与遗留项

`tests/test_task_control.py` 通过令牌 Unix bridge、HTTP、原任务飞书入口与真实 JSONL 子进程 peer 核对活动/idle语义、误回合与协议竞争拒绝、持久重复/未知指令、停止缺覆盖、完整分页与子执行、保留文件/会话、明确继续、当前仓库竞争、目录纠正以及 hashed 收据门槛。peer 从 authoritative SQLite 检查停止/输入意图先落盘。`task_control_smoke_runner.py` 在 staged pristine SDK 的真实 registry 工具入口执行追加、停止、核对、继续，拒绝读取真实 .hermes/.codex 或网络连接。

获准真实服务验收仍需实际模型、预算、可写测试仓库、完整写入与进程边界、实际原 thread/turn、跨客户端竞争、后台与子进程停止、断线恢复及真实群反馈。离线通过不填补这些 acceptance gaps。
