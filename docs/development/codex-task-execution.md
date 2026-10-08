# 单 Issue Codex 执行切片（[执行一个 Codex Issue 并核对交付结果](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/16)）

原任务的追加、停止和明确继续见 [在原会话追加、停止和明确继续任务](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/17)。

本切片实现真实自有 stdio app-server adapter 与统一任务入口；协议替身测试不等于获准真实服务验收。当前真实启动能力保持未启用：尚无符合全部写入、工具、连接与真实执行要求的完整验证收据。没有连接原 daemon、读取已有会话、修改真实配置或使用 full access。

## 统一入口与 durable 顺序

`Manager.start_task / refresh_task / verify_task_execution / record_task_delivery` 是管理实例公共接缝。令牌认证 Unix bridge、原生 `hermes_pm_task` 工具及 Dashboard `/task` 调用同一入口；调用体不接受身份、verifier 或 enabled 声明。只有本人或已分配负责人能执行该任务；其他可见目录的角色不能借可见性取得控制。

飞书已关联原任务的本人输入 `核验执行能力`、`执行`、`核对执行` 分别使用这些入口。多个候选仍先澄清。受理本身不启动 Codex。成功启动、核对与交付结果进入既有逐段 outbox，保留原群任务锚和真实 @；已配置 executor 的 Gateway 后台任务核对状态变化并投递待发消息。没有可核实通道时保留投递待核对。

启动顺序为：

1. 确认公开受理锚、冻结负责身份/项目/kind/role及仓库fingerprint仍与当前目录相符、当前负责Profile的local Codex ref与实际adapter一致、逻辑仓库没有其他未释放外层任务，再重新取得本地仓库物理布局。旧记录缺少冻结责任证据时先对账；目录纠正不能默默重新派给新身份或新执行器。
2. initialize/initialized 确认实际自有进程、连接 generation 和来源；验证权限 profile 可选、完整 loaded/list 与当前执行能力证据。
3. 先提交 SQLite `thread_start_intent`，然后 `thread/start`；响应中的 thread ID 即使权限核对失败也先登记。
4. 核对实际 cwd、`canAcceptDirectInput`、固定 CLI、activePermissionProfile 与 runtime roots；提交 thread 绑定及 `turn_start_intent` 后才发 `turn/start`。
5. 当前目标、冻结 Issue 版本及验收、仓库/子仓/产物边界、Matt 工作流、Ghost233 与本地同步约束进入该独立任务会话。

超时、EOF、异常响应、落盘失败或权限不符保留意图/原 thread 和仓库占用。重复 start 不新建线程。更换自有进程会取得新的 generation；相同 CODEX_HOME 或历史不能接替原执行器控制。刷新按已登记 session 的原仓库快照只使用 `thread/read`，目录纠正不改写原仓库占用，也不 resume。完整 JSONL 帧上限 16 MiB；超过上限使监测失联、执行待核实，不能据此宣称停止。bridge 执行操作预算 30 秒；启动应答不明需读取原 durable request，不能盲目重发。

## 状态与交付证据

消息送达 `delivery` 与任务验收 `task_delivery` 是两个字段。执行分别显示 running、waiting_approval、waiting_input、related_execution、turn_ended、unverified。轮次 completed、failed、interrupted 单列；后台命令/相关子会话未终结时不能普通交付。缺少完整 turn items 时不能交付。

交付输入只提供冻结验收项及证据引用，不接受 `passed` / `delivered` 布尔值。每项原 Markdown checklist 按原顺序逐字对应；没有 checklist 时使用原 body。测试引用正式 `commandExecution` item，必须原任务仓库内 completed、exit 0；agentMessage 中的“测试通过”没有证据效力。保存命令、cwd、exit、原 service/generation/thread/turn/item、观察时间和输出 digest，不保存全部输出或秘密。未引用的命令不自动当测试。只直接识别正常 pytest/unittest 执行；echo、collect/help/version、复杂shell或其他自定义runner需可信测试runner收据，不能经 test_item_ids 标签伪造测试。固定文件证据读取允许的普通文件并核对 SHA-256。源码变化后的测试还须有独立可信测试 runner 写入 state_dir/test-evidence 的版本收据，绑定实际 command/output digest、thread/turn/item、相同 before/after 源码 digest、源码/Git只读和授权产物目录；仅在测试后读到某个 HEAD 不证明该版本接受了测试。缺少收据不交付，任务会话自身不能写入管理实例的证据目录；state_dir 位于任务仓库或其Git写集内时在连接前拒绝启动。

源码变化需要当前固定提交与工作区交接核对，逐一核对变化文件在该 commit 中的 blob，保留原有用户内容。相同 porcelain 状态不能把既有 dirty 文件的新改动变成已提交源码。纯测试或无源码变化不要求新增提交/PR。实际 PR 读取独立显示 awaiting_review、awaiting_merge、merged；冻结验收明确以“Merge…”或“合并…PR”等列出合并要求时，未合并不交付，还须核验实际 base 分支本地与远端完整 hash。其他合并措辞由本人明确冻结为验收条目，不能从普通文本猜测任务要求。GitHub 只读 source 在每个认证业务命令前 switch Ghost233 并核验实际 login；不符停止。交付模块自身不 push、merge、stash、reset 或清理用户内容。

## 原生配置与验证收据

未配置 `codex_stdio` 时完全不启动进程。它只来自可信原生配置，要求显式 `command`、`cwd`、`environment`、`service_ref`（local:…）。environment 直接传入，不复制宿主环境；注册的 CODEX_HOME 必须与 initialize 返回一致。请只在获准测试服务及独立目录配置，勿复制真实 .codex 配置、凭据或会话作试验。

可信宿主 validator 在 state_dir 的 `codex-validation.json` 引用 `validation-evidence/` 内固定 hash 收据。HTTP、群和工具体不能上传或替换 validator。仅写入 profile 名、passed 字符串或当前 generation 不能开启能力。四类收据为 platform_enforcement、tool_paths、task_start、manual_execution_coverage，每类绑定实际 service/generation、binary/config digest、platform、仓库 fingerprint、policy digest。

- 平台矩阵必须同时证明 mono 根级源码、Git index/commit/gitlink及授权产物可写，并拒绝 child源码/真实Git、祖先rename、原子替换、symlink、预存/new hardlink、未授权位置、测试阶段源码/Git与子进程逃逸，保护 hash不变。
- 各模型文件、shell/git、测试、Code Mode、MCP、动态工具、外部filesystem/process/thread-shell路径必须受同一约束或明确 disabled。
- 获准实际启动收据必须覆盖 initialize/initialized、profile/list、thread/start、turn/start、thread/read，固定真实 thread/turn、profile和roots。
- 手动/其他执行器覆盖必须明确完整且没有竞争执行；当前 owned loaded/list 本身不能代表全部桌面或CLI活动。

收据解析只核对已有可信 validator 产物的完整性和当前适用范围；它不能把未执行探针变成验收证据。每次启动再次验证，没有收据或任何 FAIL / unknown 都保持关闭。

## 已知真实验收门槛

三条调查路线尚不能满足生产写入边界，不能据此启用控制：

1. Mac nested sandbox 的运行探针未验证；已有无推理握手 smoke 只证明 framing/初始化/只读方法。
2. 固定 Linux broad mono write + child read 允许保护祖先 rename；文件级窄写集无法支持正常 Git，目录级窄写集无法正常修改 root 源码/index。不能以只读小范围代替本产品开发需求。
3. Mac 整体自有进程外层 Seatbelt 的有界矩阵多数成功，但预存 mono hardlink alias 可以写穿 child 源码并改变保护 hash，结果 BOUNDARY_FAILED。部分操作拒绝不等于整体边界成立。

后续必须在明确获准的测试仓库/服务、模型/预算与工具集合上证明完整可用开发边界、测试阶段写集、真实 thread/turn 启动、事件/等待/结束/结果，以及原群发送/受理。当前不会降级到 full access，也不会把协议替身、收据形状验证或静态 schema 作为实际验收。

## 离线验证

从公共令牌 bridge 与核验飞书入口驱动真实 Python 子进程 JSONL peer。peer 在 turn/start 时读取同一已提交公共业务快照，并记录原服务事件，证明 durable 顺序；覆盖分片/通知穿插、null profile、未知启动不重放、同仓库竞争、原 generation、更长启动链、过大帧失联、等待/轮次结束、正式测试证据、纯测试交付、PR/合并/同步独立状态、静态能力声明拒绝及原群引用/@。这些明确是 synthetic fixtures。

使用项目 test extra 和指定 pristine SDK fixture：

```sh
HERMES_TEST_SDK_ROOT=<isolated-sdk> HERMES_REQUIRE_SDK_SMOKE=1 python -m pytest -q --basetemp=/tmp/hermes-task-tests
node --check dashboard/dist/index.js
```

使用短 basetemp 避免 macOS Unix socket 路径长度限制。真实能力验收报告与静态/离线结果分别保存；真实群与服务证据缺席仍列为 acceptance gap。

## 合并验收解释

受理时从原 Issue 冻结明确的合并义务。`must/必须` 的合并操作、状态展示、可选及禁止合并分别核对；仅出现 `merged/合并` 不添加义务。历史关键词版本的义务按原已冻结 body 纠正解释，原验收文本不自动替换为后来 Issue 内容。必要 merge 仍要求真实 merged PR 与 local/remote 同步；明确禁止的实际 merged 结果不能交付。条件、冲突或不能唯一解释的条目清楚返回 `needs_clarification` 并保持未交付。本人可在原任务通过 `append`，或在原群唯一关联的任务回复中明确解释原条件，例如“本任务测试即可交付，PR 无需合并”。该输入仍经原 `control_task` 的身份、权限、服务、代次与预期回合核对，实际 RPC 受理后单独保存解释及任务、原验收、授权和消息来源；不改原 Issue、冻结 scope 或验收文字。多个不明确条目须引用原条目逐字说明，不能猜测其目标。交付时只使用当前授权仍有效的最新明确解释，批准合并仍要求真实 merged 与同步证据；普通状态展示、建议、其他身份、其他任务和过期输入不能解除门禁。解释只决定原条件验收，不能取得执行审批或扩大仓库边界；消息收到、送回原会话与最终交付分别核对。 只识别原任务当前明确的决定句，如“本任务批准合并 PR”“本任务不批准合并 PR”“本任务 PR 不是必须合并／无需合并”。尚未批准、报告、讨论和引用材料保持待澄清，不从提到“批准”推导决定；同条输入中的原条件逐字引用只用于唯一目标关联，其他引用不能拼成新的决定。明确决定与附带的状态展示分别处理。 Markdown 反引号或波浪号围栏的完整正文均为材料；关闭围栏须同种字符且长度足够，未关闭的块保留至输入末尾。材料块以独立占位保留边界，块内决定文字不会提升为本人答复；块外明确决定仍经原入口核对，原条件逐字引用只用于关联。

## 已提交业务快照

`ManagementClient.read_snapshot(committed=True)` 与 `Manager.read_snapshot(..., committed=True)` 使用独立只读连接返回已提交的项目、Profile 和任务视图；`SnapshotReader` 为相同可信入口的独立只读读者。身份、角色、项目可见性与默认快照共用规则，敏感值按已有公开材料规则过滤。结果明确标记 `committed_snapshot`、观测时间及 `execution=unverified`，不能当作当前执行、停止或授权验证。该读取不对账、写库、发 RPC 或启动服务；默认快照行为保持。原服务 fixture 在输入 RPC 到达时用此视图保留 thread/control/stop intent-before-send 断言，不访问物理表或私有 payload。
