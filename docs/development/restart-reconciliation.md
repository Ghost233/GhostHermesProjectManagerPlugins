# 重启与失联对账

`Manager.reconcile_task(identity, request_id)` 是单个已受理任务的统一入口。私有 bridge、`hermes_pm_task` 的 `reconcile` 动作与 Dashboard `/task` 的同名动作调用这个入口。Gateway 在派发队列前对持久任务逐个对账；飞书进度与 Dashboard 展示同一记录的最后已确认状态、确认时间、恢复结果及需要本人的事项。监督不可用显示待核实，不能推出执行已失败或停止。

现存 SQLite 先以只读方式核对完整性、目录行、版本和基本材料，再打开可写管理入口。数据库损坏、未知版本、原责任或仓库绑定冲突时不对相关任务写入或派发。来源只有历史、原会话身份不完整、原服务缺少当前证明时保存待核对结果和仓库占用，不创建替代 thread，不把历史 PID、旧 RPC ID 或旧批准重建成当前控制。

原执行仍在运行时，只恢复监督。重连必须由受信宿主证明同一原 service ID、来源种类、endpoint、已受理范围和不可扩大的原仓库边界；同时读取完整 loaded 列表与原 thread/turn。未加载的历史 running 状态不足以恢复实时监督，活动 turn 变化是绑定冲突。新的 proxy 连接 generation 与原服务身份分开记录，旧 generation 只作历史索引。

自动继续仍使用原 thread，仅在原回合确实已 interrupted/failed、当前证明逐项指出原工作尚未完成、全部相关回合/后台和宿主进程覆盖已核实结束、原本次工作授权有效时允许。完整历史或一句“完成了”不构成结束和未完成证明。未知启动、追加或应答结果保留待核对，不盲重放。自动输入使用稳定指令 ID，先保存意图；服务受理未知时保留占用。

人工请求只以当前连接真正收到的有方向和类型的 server RPC 建立有效应答。旧连接人工请求转为 unverified/expired，关闭答复控件并提示原界面；收到相同问题的历史文本不会重建新的有效请求。当前连接真正收到的 `item/completed` 自然语言问题还需核对同一原 turn、原条目内容和直接输入能力；已结束 turn 的问题仍须经原协议的独占 idle 输入验证；单纯历史读取不建立新问题。新 live RPC 可接收本人一次新答复，重复同一答复 ID 不再发送。控制已归还或手动授权已暂停时保留只观察，重连不会续签旧授权，也不发送中断。

## 与生命周期实现的持久意图契约

对账先消费已有停止记录和目录意图，再连接原服务。`request.stop` 是已有任务控制入口先持久化的停止决定；当前 turn 的停止、仍 processing 或原目标不完整的停止决定均抑制自动继续。已确认的旧停止对应后来显式继续的其他 turn 时，保留历史而不把它当作新的停止决定。

#29 的生命周期入口在执行外部动作前持久化以下管理字段，公共 `archive → restart` 回归通过独立原协议服务的实际任务子进程证明其消费顺序；细节见 `project-lifecycle.md`：

- `profiles[profile_id].lifecycle` / `projects[project_id].lifecycle`：`archiving`、`archived`、`disabled`、`deactivating`、`maintenance` 或 `restoring` 都抑制自动继续。未知生命周期同样要求人工核对；目前可核实工作使用 `configuring` 或 `active`。
- 同一 Profile/项目的 `archive_intent`：明确停止自动续跑的意图；存在的非 `false`/`null` 值均抑制恢复执行，包括内容尚未完整的意图对象。
- 目录的 `maintenance_mode`：存在的非 `false`/`null` 值抑制自动继续。

这些字段缺省表示目录没有记录该项主动意图，不能替代原执行、授权和未完成状态的当前证明。当前证据缺失或未知仍 blocked/unverified。#29 的真实公共封存路径先保留原占用，在丢失 interrupt 回执并重启后，通过同一原服务对账核实停止才释放；父先恢复和子逐个恢复均不自动续跑旧工作。

## 原生接入与能力证据

`codex_recovery` 是受信原生配置的列表，每项严格包含 `executable`、`cwd`、`environment`、`service_ref`、`source_kind`、`endpoint` 和 `endpoint_ref`。只启动固定可执行文件的 `app-server proxy --sock <原endpoint>`，不能启动新 app-server 来读取相同历史。没有当前原服务证明时连 proxy 也不启动。

管理目录 `codex-recovery.json` 按 request ID 指向 `recovery-evidence/` 中有 SHA-256 的宿主报告。报告必须绑定新 generation、原 service ID、原 endpoint 的摘要、来源、原 thread/turn、前 generation、当前控制模式及冻结范围摘要，含带时区的 `verified_at` / `expires_at`，有效窗口不超过 300 秒。再复用完整文件系统、工具、执行覆盖及独立控制/人工应答 receipt 校验；`recovery` receipt 额外核实持久意图先行、原服务、占用、运行只观察、未完成才继续、停止/封存、未知启动/追加/答复、live RPC、手动归还、断线/持久重启和不重复执行/答复。内容声明或过期报告不能启用能力。

管理进程非正常退出后，bridge 仅在取得独占管理 lease、runtime receipt 的 inode/device/uid 一致、原 manager PID 已不存在、原 socket 拒绝连接且核对期间 inode 未变时回收自己的孤立 socket。未知文件、别人的 socket 或仍存在的进程均保留；这不构成原 Codex 执行已经结束的证明。

## 验证范围

公开行为测试覆盖原任务重连、自动继续的正负门槛、未知追加不重放、历史人工请求失效、新 live RPC 的一次答复、责任和 endpoint 冲突、SQLite 损坏、原进程覆盖缺失、手动已归还模式，以及 Dashboard/群的一致状态。独立人工服务测试实际启动任务子进程，杀掉 manager 进程并打断实际 proxy 连接；核对原子进程不变、一次启动、一次未知追加、核实停止后才释放占用且重启不续跑。

固定 pristine Hermes SDK 的强制 smoke 从真实插件注册工具和 Dashboard 入口执行同一独立人工服务恢复路径。人工协议服务和受控宿主证明用于验证实现语义，不能当作用户当前 Codex daemon/桌面来源的权限、接管或恢复能力已通过。真实 Profile、home、config、auth、history、现有任务、机器人和群均未改动；真实来源缺少正式当前 receipt 时保持未启用。
