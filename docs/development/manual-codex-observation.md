# 原 Codex 服务只观察（[只观察手动会话并保护仓库占用](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/19)）

本切片发现本人明确登记的项目及原执行服务中的手动会话。它读取当前服务、可读取的历史和实际读取范围，不获得控制权。`daemon`、`independent_cli`、`desktop` 分别登记与验证；一个来源通过不代表另两个来源可用。

## 接缝与登记

- `Manager.register_observation_source(identity, registration)`：只有本人登记稳定 `id`、`kind`、`project_ids` 和 `adapter_ref`。来源不能通过更改同一 ID 静默改绑。
- `Manager.refresh_manual_sessions(identity, scope=None)`：按调用者可见项目读取，返回与 Dashboard 相同的 snapshot。
- 令牌 bridge 的 `ManagementClient` 省略身份。HTTP `/observations` 使用 `action=register|refresh`。原生 `hermes_pm_observe` 只接受可选 `scope`，使用 participant credential；不能借 owner alias。
- 飞书 `核对手动会话` 须唯一关联当前请求，沿原任务消息和真实 @ 反馈。没有新变化的后台轮询不会重复生成群反馈。

登记例子：

```json
{"id":"project-daemon","kind":"daemon","project_ids":["mono"],"adapter_ref":"local:approved-original-daemon"}
```

HTTP、群与原生工具不能指定命令、endpoint、verifier、启用能力或原执行器身份。原生宿主设置 `codex_observation` 是适配器配置列表，`manual_sources` 可保存上述明确登记；不从配置扫描结果猜关联。

## 原 endpoint 与只读传输

原生配置必须显式提供 `executable`、`cwd`、`environment`、`service_ref`、`source_kind`、`endpoint`、`endpoint_ref`。执行器只构造：

```text
<fixed executable> app-server proxy --sock <explicit registered original endpoint>
```

它不开新 app-server，不查默认 socket，不操作 daemon start/stop/restart。继承的 JSONL codec 只用于插件自己创建的 proxy transport 子进程；其 PID 不是原执行器身份，关闭 proxy 也不代表手动执行结束。

`ReadOnlyCodexAdapter` 的出站白名单只允许 initialize/initialized、thread/read、thread/list、thread/loaded/list、thread/backgroundTerminals/list 及 thread/turns/list、thread/items/list。thread/start/resume/fork、turn/start/steer/interrupt、审批或 server request 的 JSON-RPC result 都在写入前拒绝。收到手动审批请求不答复、不把它登记为插件可批准请求。

## 当前能力证据

连接前要求可信宿主在管理实例外部写集之外提供 `codex-observation.json` manifest，引用 `observation-evidence/` 的固定 SHA-256 收据。管理实例位于观察项目源码/Git 写集中时不读取原 endpoint。收据绑定固定 binary/config/endpoint digest、当前 connection generation、平台、original executor ID、source kind 与 endpoint ref，逐个声明已实际通过的读取方法及 original endpoint、只读帧、原请求路由、缺失范围与断线病例。声明的方法缺自己的实际病例时不启用。

解析器核对已有宿主证据，不执行探针，不把 PASS 文字变成实际能力。不存在当前 hashed 收据时，配置来源显示 unknown 且不打开 proxy。`initialize.codexHome`、userAgent、原始 thread ID 或相同历史均不能代替原执行器证明。独立 CLI 或桌面没有获准可接 endpoint 时保持未知，不另开服务读取相同历史来制造原连接。

## 实际读取范围与状态

读取 loaded/list 的完整分页，仅代表该已连接原执行器当前加载的线程。读取 thread/list 时显式给出已验证的 sourceKinds、分别查询 archived=false/true，使用 useStateDbOnly=true；不调用默认 scan-and-repair，也不把数据库列表当所有执行。所有来源的 global_execution_coverage 都显示 unknown。

先读取 metadata 确定真实 cwd 的 Git common directory，匹配明确项目范围后才取得完整 turn history。只保存必要 thread ID、原执行器/generation、source kind、cwd/逻辑仓库、状态、控制权限和时间；不复制 preview、消息、命令输出或秘密。

状态分别为：

- `active`：同代原服务 loaded 线程明确 active。
- `inactive_verified`：同代 loaded 线程及其完整回合/工具/相关子线程、完整后台分页和可信宿主 runtime coverage 均核实结束。
- `last_known`：只能读取历史或当前线程不在 loaded 范围内；不推断其他服务停了。
- `unknown`：读取缺失、分页不完整、相关执行/覆盖未知或原连接不可用。保留最后已知状态和时间。

分页历史在支持时使用只读 turns/list 取得完整 itemsView。缺失支持时保留未知；从不 resume 来读历史。遇到来源 identity/generation、项目布局或 thread cwd 冲突时，原来源显示 conflict，旧记录与占用保留，不用新连接的空列表释放旧执行。

## 仓库等待与监督

已发现活动、历史/未知未核实执行及来源覆盖缺失阻塞同 Git common directory 的新插件外层任务，符号路径和 linked worktree 不绕过等待。其他逻辑仓库可继续。start、append 和明确 continue 前再次只读核对已登记的相关来源；原服务冲突时停止受影响的新自动执行。观察没有接管入口，不预实现 [接管并归还本次手动工作](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/20)。

Gateway 在其受管生命周期内轮询原来源，再尝试 [按仓库排队并交接下一项 Issue](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/18) 已授权队列；卸载或入口失效后停止轮询并关闭插件自己的 proxy transport。群和 Dashboard 共用保存的状态、范围、最后核实、observe_only 及队列原因。[执行一个 Codex Issue 并核对交付结果](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/16) 的启动能力仍独立要求完整实际写入与手动执行覆盖证据；一次观察不能替代跨客户端的排他执行保障。

## 验证与真实缺口

公共令牌桥、HTTP/群及原生 SDK registry 使用合成的原服务 JSONL/proxy-shaped peer 验证全出站帧只读、跨仓库与别名、重启/相同历史不能替换原执行器、unsupported/分页/历史缺口、相关子执行与后台、当前 hashed receipt 门槛。它们都是 synthetic fixtures。

当前没有本人获准的测试 daemon、独立 CLI 或桌面 endpoint，也没有它们的真实读取、原请求路由、宿主 runtime coverage 或群验收收据。三个真实来源的能力分别未知；不读取真实 .hermes/.codex、原会话、默认 socket 或秘密，不运行真实 daemon/模型服务探针，不宣称真实只观察已验收。
