# 维护原生运行边界（[维护升级、主动停用并从检查点回退](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/31)）

统一管理入口的 `maintenance(action, details)` 接收 `enter`、`deactivate`、`check`、`checkpoint`、`switch`、`rollback`、`reenable`。Owner 在 `enter/deactivate` 审定当前目录 revision、完整 Profile ID 列表、原版本及目标源码；`switch/rollback/reenable` 再审定当前目录 revision 与同一完整范围。已有 operation ID 的意图、原版本和目标不可改写。群与 Dashboard 共用此入口，机器人/model 的只读 token 不构成 Owner 决定。页面与群的操作方式另见维护入口文档。

`expected_release` 为 `plugin_version/source_digest/sdk_version/sdk_source_digest` 四个字段；`target_release` 为固定 `id/plugin_version/source_digest`。维护暂停新受理、新执行、继续与队列派发；已有原服务监督、观察与允许的停止仍可运行。配置了原生维护 Host 后，当前版本未知或实际 SDK registry 指向旧 bytecode，同样阻止新执行、派发与迁移。原 schema 未知或 SQLite 不可用仍沿重启对账的原 guard 拒绝打开 writable authority。

## 交接、检查点、停用与恢复

`check/checkpoint` 重新读原线程、全部相关子线程、后台终端分页与进程覆盖；完整原回合结束和监督消失是不同结果。未处理的发送/启动/控制/人工回送 outcome、在途投递、手动执行与原 Host 完整覆盖均参与门禁。手动只观察不会获得新控制授权，Owner 须实际处理、在 `check` 提交 `handled_manual_session_ids`，并再次从原来源核实 inactive。检查点在原统一 gate 持久化后采用 SQLite backup；原 Host 的配置、数据和档案须逐项覆盖、有受控 artifact、SHA256 与一致性证据。

主动 `deactivate` 先持久化 intent，对已持有控制权的原任务按原 thread/turn interrupt，再核实停止。没有启动的任务与队列保持，已有工作区改动保持；返回控制或只观察的原执行不会被擅自中断。完整条件未达时显示 blocked/processing。业务停用保留管理和监督服务，不卸载整个多 Profile 宿主。`reenable` 在 gate 仍关闭时先做原服务对账；明确停止、封存与未知 outcome 不自动重启，成功后才解除 gate。

检查点中的 Manager SQLite 是一致性/恢复证据，回退不覆盖当前 authority 数据库。真实 data-only restore 仅作用于原 Host 批准的原文件范围；当前 SourceGrant、控制 grant、明确 stop/archive intent 与通知健康继续保留。新收紧的 SourceGrant 在 rollback 后仍拒绝原已撤销访问。原生配置授权 digest 改变则拒绝覆盖旧配置；restore 后还须验证各 artifact hash、实际原版本、授权未扩大、旧入口/任务未启动。Native Host 绑定 Manager 时拒绝把 `manager.sqlite3`、其 WAL/SHM、`notification-health.json`、`manager-runtime.json` 和 socket 纳入外部 restore scope。不得 reset、clean 或替换工作仓库。

## 固定 SDK 实际版本与切换

`NativeMaintenanceHost` 使用固定 SDK 的 `gateway.control_socket.identify_gateway` 和 `reload_gateway_plugins`，并查询实际 `PluginManager.list_plugins()` 与公共 `tools.registry.get_entry/dispatch`。SDK core 的 `code_sha/code_version` 来自原 SDK 版本 API；unknown 不升级为 verified。静态 artifact SHA、loaded manifest version、native entry 的实际 code fingerprint 和本次 load instance ID 分别记录。Native entry 的运行 code object（含嵌套 constants、签名/flags/exception semantics）须与目标源码编译结果一致；仅新磁盘 SHA 或新 manifest version 不足以证明新逻辑已经生效。

根插件入口采用 SDK 管理的相对 package namespace；固定 SDK 的公开 force reload 自然清除此插件与子模块。原全局 `ghost_hermes_pm` 缓存曾在真实测试中造成 manifest `0.2.0`、`reloaded=True`，而函数体仍旧；本票的 public native test 验证了变更函数体真实生效和 timestamp/size-valid stale `.pyc` 的拒绝。Dashboard 后端是独立 host 的薄 client；它通过重新连接管理桥查询事实，不以自身 cached package 充当 Agent 生效对象。

切换只接受可信 Host 配置中的固定 release path/version/SHA，且文件 inventory 与当前安装一致。不提供通用清理、任意路径安装或热升级承诺。源码切换 intent 先 durable 保存；一个明确受管的原生请求线程通过独立 SDK control socket 发起一次 reload，返回 `outcome_unknown`。旧 `on_unload` 会关掉旧管理桥，原管理 RPC 可以消失。正常 Gateway 上下文重新接入后，Owner 对**同一** operation ID `check`；check 不重放 reload，独立核实实际 loaded target、配置/数据/档案及授权才成为 `switch_verified`。失败可从同一检查点明确 rollback，并在原版本重新接入后核实 `rollback_verified`；gate 仍保留到 Owner 对账后 reenable。

固定 SDK 的 same-key reload 不保证 adapter handlers 已重接，`adapters_rewired` 是数量，工具和 prompt 作用于之后的 session。本插件不修改 SDK、不手调其私有 hook 伪造重接。实际配置的群入口/机器人路由、原进程和资料权限须在当前完整范围的独立证据中确认，缺少证据则阻塞。强制原生 unload、外部 shutdown 和断连记录 `pending_verification`，`execution_stopped=False`；重连后最后的已确认工作仍可查看，不能把监督消失当 stop。

## 可信 Host 配置与证据

`native_maintenance` 包含 `host_home/profile_home/installed_path/releases/files`。`installed_path` 必须是明确 Profile 的 `plugins/ghost-hermes-pm`；release 为固定 `{path, plugin_version, source_digest}`；files 为显式且唯一的 `{id,path,kind,format}`，kind 为 config/data/archive，format 为 file/sqlite。路径必须是 canonical ordinary 文件/目录，文件未知 alias/hardlink 拒绝。SQLite backup 校验 integrity/data_version；普通文件在复制前后核对稳定 bytes。原数据库仍有 WAL/SHM 时 data restore 拒绝，需 Owner 明确关闭原数据服务；不自动清理 WAL。

默认 Host 不接受请求 body 的能力声明。`state_dir/native-maintenance.json` 只引用 `native-maintenance-evidence/` 内有限大小、hash 匹配且新鲜的 trusted Host report。report 绑定原 gateway PID/start_time/home/code identity、SDK 源码 SHA、当前 registry 对象、明确 Profile 范围、operation ID 与原/目标版本。current、handoff、checkpoint、switch、restore 各阶段须有对应的实际覆盖 cases。ACK、ledger、parked/unserved、`active_agents=0` 或历史 report 均不替代当前原执行/在途完整证明。

## 已验证范围与真实门槛

公共离线路径使用实际 SQLite、临时 Git、原 JSONL 执行 peer、原观察 peer和明确人工 native boundary。固定 SDK 场景使用 pristine `bd0affe5e5f723579df8902852f5d0c47795f355` 的真实 loader/control socket/registry/native hook/management server；外部 verifier 仅覆盖拥有的人工 gateway、无模型/外部任务的明确运行范围及显式配置/data/archive 文件。测试副本通过 SDK 公开 build-stamp writer 声明固定 core 构建 metadata；此 metadata 与新插件实际执行 fingerprint、控制效果分开，不是生产版本发布证明。场景实际覆盖旧桥关闭、失 ACK、原 ID 新桥 check、切换失败、真实文件回退、恢复对账、强制卸载及 stale bytecode。

父规格全部已有切片、获准真实群/Profile/服务/仓库、当前模型端点、完整资料/进程/保留保护和正式安装计划继续独立执行。本票不替代前票测试，不操作本人当前任务，不迁移真实资料。Dashboard 的 `release_verified` 仍为 false；没有当前真实验证的能力不能作为已启用发布。本地已批准资源可以只读预检，但原真实模型端点和原 Codex 控制/全资料保护的 unknown/blocked 门槛不会被人工 fixture report 消除。

## 当前入口权限与未决原操作

维护 snapshot 的 `permissions.status/can_manage` 由已验证入口身份生成；只有 Owner 的当前入口得到 true，已登记只读身份为 false。它是显示用事实，不能通过请求 body 授权。Dashboard 在缺失、未核实、非 true、只读或离线权限下隐藏维护控制，预览和提交再次核对权限；Owner 切换为 reader 或刷新后旧预览失效，后端继续拒绝非 Owner。原实际浏览器 reader P1 与修复后独立真实 React 验证均保留。

一致性交接还核对原 Owner 回复 ledger 和通知 ledger 的未知送达，原生生命周期未决 component request、全局验证尚未释放的原 action，以及迁移 preparing/native-state-unverified/cutover-unknown。只读查询不会重发这些原请求。已验证生命周期结果、已经核对结束并释放的原验证 run、尚未开始原生操作的 migration draft 和已完成迁移/回退历史不因此被永久视作在途。每个新增门禁都有公共入口真实反例：unknown 回复/通知、原 stop 已确认后的 lost validation ACK、原 native stop ACK 未核实及原 migration prepare 实际产生人工 artifact 后 lost response；原服务公开 check/reconcile 结束后可以交接，原 run/control 不重播。
