# 单 Issue DSH 执行（[执行一个 Issue 并核对交付结果](../../../../issues/16)）

2026-10-09 执行目标改为 DSH，停止 Codex 适配，随后确认优先由 Hermes 管理专用 DSH 实例，撤回配套认证组件。本文件描述当前 DSH 契约；切换前的协议替身、原服务探针和测试结果只作历史证据。接口与启用门槛见[DSH 执行目标修订](../specs/dsh-executor-transition.md)。

## 统一入口与持久顺序

`Manager.start_task / refresh_task / verify_task_execution / record_task_delivery` 是管理实例公共接缝。认证 bridge、`hermes_pm_task` 与 Dashboard 调用同一入口，身份取自原认证来源，不接受调用体的 actor、verifier 或 enabled 声明。本人或本次指定负责人仅能操作已冻结任务范围；目录可见性不提供控制权。

飞书本人在唯一关联任务下使用“核验执行能力”“执行”“核对执行”。受理不启动 DSH；公开发送、受理、实际开始、轮次终止和 Issue 交付分别反馈。后台监督只处理已有授权，并沿原群消息锚和真实 @ 回传。

1. 核对受理锚、冻结 Issue／身份／角色／项目／`accepted_dsh_ref`、仓库 fingerprint、准备基线和同仓库占用；目录修正不能默默重派原任务。
2. 核对专用 DSH 实例的明确 home、归属、实际进程、当前连接代次、版本／配置及独立写入、工具和执行覆盖证据；仅使用原生受支持接口，不改原 Desktop。Desktop 手动覆盖另行登记原生连接，不能由专用实例推断。
3. 持久登记 Session 创建意图及稳定任务／操作 ID，再调用原实例 create；响应 ID 即使后续核对失败也保存。
4. 核对实际 Session、workspace、权限边界及控制归属；持久保存 prompt 意图及稳定 `requestId` 后才派发输入。
5. 输入仅包含当前冻结目标、验收、基线、仓库／子仓／产物写集、必要允许共享材料、仓库规则与 Matt 工作流。

响应未知、断线、异常结果、权限不符或落盘失败保留意图、原 Session 和占用。只读对账原 durable 来源，不盲重发或换 ID。DSH prompt 的 requestId 受理去重不能替代插件自己的持久去重，也不能证明未知 create 可安全重发。刷新只读 page／follow／projections，不调用 resume／prompt 来核对状态。

## 状态与交付证据

执行、人工等待、相关子 agent／job、轮次终止、Issue 交付和 PR 状态分别记录。根 Session 结束或普通助手“测试通过”不等于全部执行结束；缺完整事件、后台／子进程覆盖或实际测试证据时不交付、不释放占用。

冻结的原 Markdown checklist 逐项对应，未提供 checklist 时核对原 body。测试必须是原任务、正确 cwd 和固定源码输入上的真实命令与退出码；来源由 DSH 原 durable 工具事件及独立可信测试 runner 验证。没有可核实 exit／输入绑定的普通工具文字不能当正式测试。echo、collect／help／version、复杂 shell 和自定义 runner 不能仅靠标签提升为测试通过。

证据保存实际命令、cwd、退出状态、输出 digest、原服务／代次／Session／turn／调用定位和观测时间，不复制全部输出或秘密。源码变化后的测试绑定 before／after 源码 digest、Git 与产物范围；任务进程不能写管理证据目录。管理状态目录与任务源码或 Git 写集重叠时拒绝启动。

源码交付记录完整提交并核对每个变化文件的 blob，保护既有用户内容；相同 porcelain 状态不能证明 dirty 文件未变化。纯测试或无源码变化不强制新增提交。PR 审查、待合并和已合并单列；明确要求合并才把实际 merged 与本地／远端完整 hash 同步作为交付条件。GitHub 每次业务操作前核验配置账号，交付入口不擅自 merge、push、stash、reset 或清理。

## 原生配置与能力证据

开发 Profile 使用 `connection_refs.dsh`，原生 `dsh_executors` 的 key 与 `service_ref` 相同。默认 `owned_native` 配置要求规范绝对路径 `dsh_home`、现存 `workspace` 和 `runtime_package_root`；可选本机 `node_bin` 和 `timeout`。home 位于 workspace 之外且仅本人可访问，原包目录包含 `@deepseek-ai/dsh/lib/profile-boot.js`。模型和预算沿用专用原生 Profile 配置，分别核验实际加载与可用范围。

Hermes 主插件启动自有 Node 子进程，通过原 `runProfile` 和进程内 Gateway 接入；不注册额外 DSH 插件，不复制原包。`executor_for` 依据冻结 `accepted_dsh_ref`／原 Session service_ref 选择 `Manager.dsh_adapters`；不匹配时拒绝，旧 `dsh_execution` 只在完全匹配时回退。多个项目由同一管理实例监督，不需切换仓库时重载 Hermes；卸载仅清理各自已配置实例及连接。已有远端连接需独立明确登记 `mode: remote`。

群、工具和 Dashboard 不能上传 cookie、连接目标、运行命令、映射或能力收据，不通过认证 URL 发布或导入器取得连接。插件包及本机安装复制清单包含 `ghost_hermes_pm/owned_native_host.mjs`；该自有文件纳入插件源码指纹，原 Hermes／DSH SDK 全部源码另外按 `sdk_source_integrity` 核对不变。

当前 hashed 能力证据绑定 DSH 服务身份、启动／连接代次、实际二进制／配置／平台、仓库 fingerprint 和写入 policy。至少独立核对平台 enforcement、全部 tool paths、实际 task start 及手动／其他执行覆盖；静态 PASS、preset 名称或相同 home 不启用执行。

- 平台允许自身源码、Git index／commit／gitlink 和授权产物；拒绝子源码／Git、祖先 rename、原子替换、symlink、已有／新 hardlink、越界及子进程逃逸。
- 文件工具、shell／git、测试、代码执行、MCP、动态工具和外部 filesystem／process 入口遵守同一边界或明确关闭。
- 实际任务证据覆盖实际专用实例 create／prompt、原 Session、workspace、输入受理、事件、终态和结果；独立登记的手动实例分别证明，不冒充 Desktop 覆盖。
- 完整手动执行覆盖不能从空 Session 列表或原 Session writer lock 推导；相关子 agent、job、shell 后代及其他已登记来源分别核对。

每次启动重新核验。当前未通过的日志隐私、物理写入与运行覆盖继续保持门槛，不降级 full access，不把源码接口、合成收据或旧执行器测试当真实 DSH 验收。

## 验证与缺口

使用合成项目、原始 DSH Remote／事件语义及未经修改 Hermes SDK 验证公共入口、持久顺序、重复／未知输入、队列竞争、身份拒绝、测试／交付及卸载／重载。真实验收再核对获准原 DSH 实例、模型／预算、测试仓库、全部工具、相关执行与群内往返；执行器变化后重新跑最终本机门禁。

已有 Mac hardlink 写穿保护内容的反例仍需解决；部分拒绝不证明整体边界成立。测试报告绑定最终源码、环境、命令和真实退出码；强制清理与超时不算通过。

## 合并验收解释

受理时从原 Issue 冻结明确的合并义务。`must/必须` 的合并操作、状态展示、可选及禁止合并分别核对；仅出现 `merged/合并` 不添加义务。历史关键词版本的义务按原已冻结 body 纠正解释，原验收文本不自动替换为后来 Issue 内容。必要 merge 仍要求真实 merged PR 与 local/remote 同步；明确禁止的实际 merged 结果不能交付。条件、冲突或不能唯一解释的条目清楚返回 `needs_clarification` 并保持未交付。本人可在原任务通过 `append`，或在原群唯一关联的任务回复中明确解释原条件，例如“本任务测试即可交付，PR 无需合并”。该输入仍经原 `control_task` 的身份、权限、服务、代次与预期回合核对，实际 RPC 受理后单独保存解释及任务、原验收、授权和消息来源；不改原 Issue、冻结 scope 或验收文字。多个不明确条目须引用原条目逐字说明，不能猜测其目标。交付时只使用当前授权仍有效的最新明确解释，批准合并仍要求真实 merged 与同步证据；普通状态展示、建议、其他身份、其他任务和过期输入不能解除门禁。解释只决定原条件验收，不能取得执行审批或扩大仓库边界；消息收到、送回原会话与最终交付分别核对。 只识别原任务当前明确的决定句，如“本任务批准合并 PR”“本任务不批准合并 PR”“本任务 PR 不是必须合并／无需合并”。尚未批准、报告、讨论和引用材料保持待澄清，不从提到“批准”推导决定；同条输入中的原条件逐字引用只用于唯一目标关联，其他引用不能拼成新的决定。明确决定与附带的状态展示分别处理。 Markdown 反引号或波浪号围栏的完整正文均为材料；关闭围栏须同种字符且长度足够，未关闭的块保留至输入末尾。材料块以独立占位保留边界，块内决定文字不会提升为本人答复；块外明确决定仍经原入口核对，原条件逐字引用只用于关联。

## 已提交业务快照

`ManagementClient.read_snapshot(committed=True)` 与 `Manager.read_snapshot(..., committed=True)` 使用独立只读连接返回已提交的项目、Profile 和任务视图；`SnapshotReader` 为相同可信入口的独立只读读者。身份、角色、项目可见性与默认快照共用规则，敏感值按已有公开材料规则过滤。结果明确标记 `committed_snapshot`、观测时间及 `execution=unverified`，不能当作当前执行、停止或授权验证。该读取不对账、写库、发 RPC 或启动服务；默认快照行为保持。合成 DSH 服务 fixture 在输入 RPC 到达时用此视图保留 Session/control/stop intent-before-send 断言，不访问物理表或私有 payload。
