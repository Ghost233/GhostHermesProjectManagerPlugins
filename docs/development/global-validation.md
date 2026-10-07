# mono 稳定组合全局验证（[验证 mono 交付组合并按 Issue 返工](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/27)）

`Manager.global_validation(identity, action, details)`、令牌 bridge 的 `ManagementClient.global_validation(action, details)`、Dashboard `POST /global-validation` 与 `hermes_pm_global_validation` 共用一个权威管理实例。身份来自原入口；请求不接受 actor、权限或测试通过声明。原生 participant 工具只允许本人的既定 mono 职责，不能借 Owner credential 或运行 child 物化。

## 固定本轮交付

`plan` 明确原 mono 请求、完整父 commit、本次相关已交付 child 请求及路径、已配置测试 ID。child 固定验收、测试、遗留工作树与 PR 条件来自原交付证据；未交付、错误负责人、缺少固定源码版本或未满足必要合并条件会拒绝。清单不会自动吸收 future task。父树中的 gitlink 必须等于 child 交付 commit。

```json
{
  "request_id": "原 mono 请求 ID",
  "mono_commit": "0123456789abcdef0123456789abcdef01234567",
  "children": [{"request_id": "本次 child 交付请求 ID", "path": "packages/child"}],
  "test_ids": ["integration"]
}
```

完全相同的 plan 返回原轮，不因重复群消息再建验证。确需重试已结束的组合，plan 增加 `retry_of`，指向同一 mono 原任务的已结束轮。新 child 修复请求或父 commit 则形成新的固定清单。`unassigned` 单列父树中未在本轮 child 清单内的实际子模块；核对其物化版本和边界，不自动建负责人。

## 独立准备和实际输入

总负责人开发只涉及自己的 mono 源码与 gitlink。验证入口只读取 child，不执行 checkout、stash、reset、清理或源码编辑。已由 child 提供正确物化版本时，记录只读准备已结束；child HEAD 不匹配时必须由独立获准的准备动作物化。

Owner `prepare` 必须给出同一轮、相同 child 请求/路径/commit 清单：

```json
{
  "validation_id": "原验证轮 ID",
  "children": [{"request_id": "本次 child 交付请求 ID", "path": "packages/child", "commit": "0123456789abcdef0123456789abcdef01234567"}]
}
```

Owner 准备走原宿主的独立 materializer；先保存精确授权和执行意图。未提交或未跟踪 child 内容、mono 自身未提交文件会阻止物化，文件原样保留。原动作及相关执行结束、实际 child commit/工作树和未修改 mono 源码均核实后才显示 ready。准备响应未知保留该轮占用，不重复 checkout，不运行测试；必须核对原动作。

`start` 取得父及相关 child 的规范 Git common-dir 验证占用，并保存原 mono request ID。已运行的其他受管任务、手动活动或未知观察范围会受阻；执行器原观察源在取得占用前刷新，插件不自动中断或接管它们。新受管任务显示 `validation_waiting`，后台派发不会尝试启动它们。mono 原任务的自身占用与验证占用独立保存。

测试前实际重新读取仓库绑定、物化 HEAD/tree、分支、必要工作树状态、所有非产物源码（包括 ignored source）、父/child Git 元数据、冻结验收/交付和来源责任。产物根必须是明确登记的 mono 子目录，不能与子源码或 Git 元数据重叠。未知子模块版本、目录别名、边界变化和 dirty workspace 均显示受阻。不能仅凭 gitlink、cwd、prompt 或 full-access 声称正确或隔离。

## 原宿主能力和测试证据

管理实例的 `global_validation_host` 是可信宿主接缝，不来自群消息、HTTP 或 model-facing args。宿主提供：

- `verify_boundary(round)`：实际宿主 ID/代次、runner 配置 digest、精确输入 digest、源码/Git 只读、明确产物根，以及平台、工具、进程、preexisting-hardlink 和持续输入变化观察证据。
- `start(round)`：返回实际原 run ID、同一宿主/代次及输入绑定；不接受从请求正文传来的 shell command。
- `read_result(run_id)`：原测试和相关执行终止证据、每个配置测试 ID 的 argv/cwd/exit code/输出 digest、允许产物文件 digest，以及缺陷目标和关联测试。
- `read_input_changes(round)`：原输入观察源返回本轮关联变化事件；即使手动修改又恢复旧字节，也不能只用相等的首尾 digest 宣称稳定。快照/check 继续核对原观察源。
- `find_run(round)`：启动响应未知时只核对原 run；不另开执行器、不重放启动。
- 独立获准物化的 `prepare(round, authorization)`：精确 child 操作范围和原动作终止收据；响应未知时 `find_preparation(round, authorization)` 只查询原动作，不重放物化。

`verified-original-host` 的能力还必须有管理状态目录内不可被任务写入的固定 digest 收据，覆盖父/child 源码和 Git 写拒绝、允许产物写入/逃逸拒绝、既有 hardlink 写拒绝、全部工具/进程路径约束及输入变化观察完整性，并绑定本轮输入和宿主/runner 版本。只检查源码目录或一次 SBPL 探针不足以启用该能力。

正式 native 可配置 `global_validation_host={host_id,generation,runner,watcher,tests,environment}`：`runner` 是获准已存在的原宿主隔离 runner 完整 argv 前缀，`tests` 将固定测试 ID 映射到固定 argv 后缀，公开操作不能带 shell command；`watcher` 是已存在的原生连续观察程序 argv（例如 `fswatch -0 -r`），宿主附加准确父子 worktree 与 Git common-dir，读取 NUL 分隔的原输入路径事件。`environment` 明确独立环境。插件自带 `NativeGlobalValidationHost` 和单独 `native_global_worker` 实际执行获准 child 固定版本准备及测试进程，查询原 preparation/run/result，无未知启动重放；检测相关进程组未结束时保持 unverified。

独立宿主必须在 `global-validation-host.json` 为确切 validation ID 登记位于 `validation-evidence/` 的文件引用与 SHA256。原证明绑定本轮 input digest、宿主代次、整个 runner 配置、runner/watcher 实际二进制 SHA256 与完整 enforcement matrix；原持续观察在独立 Owner prepare 结束后建立，prepare 返回实际固定输入 digest。原证明还必须匹配 `validation-native/` 下该轮 watch-binding 的实际 `watcher_pid` 与随机 `input_watch`，并给出原能力探针结束后的 `watch_event_cursor`；其后的所有输入事件都使本轮失效。原持续观察实际连接须保持存活，重新启动的观察器不能证明旧轮覆盖。当前 Mac 物理边界反例仍未被解决，因此本机无这些真实原证明时 start 保持 blocked。测试的 `synthetic-fixture` 宿主执行真实合成 Git/测试，但边界收据是受控替身；其通过仅属于该合成范围，不启用生产能力。既有 Mac preexisting-hardlink 反例、全工具/进程路径保护、真实模型服务和真实群发送仍须原能力验收。正式安装、机器人开通和资料迁移仍另案。

## 结束、失效和返工

`finish` 核对原 run。测试运行中、相关执行未结束或原宿主失联显示 running/unverified，保留占用。启动响应未知持久化原意图；`reconcile` 只查询同一原宿主/代次的实际 run，不能用替换执行器或空列表证明结束。

原观察源关联变化事件，或实际源码、ignored source、父/child commit、Git 元数据、冻结交付/职责、仓库/产物布局或 runner 配置变化使本轮失效。失效一经观察即持久化；恢复旧字节不会复活旧 pass。公共快照和 Dashboard 自动撤销旧完成状态。重启后原持续输入观察宿主不可用时，即使 Git/源码首尾快照相同也降为 unverified、整体完成为 false，历史成功不能冒充当前完整观察。通过、失败、已核实结束后的失效或准备受阻释放本轮验证占用；尚未结束的 mono 原任务继续保持自身占用。未知正在运行的原动作不能被当作结束释放。

`rework` 带原验证 ID、测试证据中的目标及可读的明确 GitHub repair Issue URL。管理实例只通过可信 Ghost233 source 核对已有 Issue，不在此入口创建网络工单。子缺陷生成既有职责群内的真实 mention 工作交接，保存原 Owner 来源、原 mono 目标、失败验证和旧 child 请求关系；独立接收才新建 child 任务。重复返工仍是一项。mono 缺陷回原 mono 任务自行修；未分配模块交 Owner 决定；环境、权限和版本准备问题显示 blocked。

child 修复按原 Issue 交付和工作区交接契约提交新固定版本。总负责人更新 gitlink 并创建新组合验证，旧验证的占用不会阻塞 child 返工。`complete` 要求 mono 原验收与固定交付满足、当前稳定组合全局测试通过、旧明确 child repair Issue 的交付已纳入新清单。Owner 为原未分配模块明确登记负责 Profile 后，匹配原 repair Issue 的新固定 child 交付可闭合该 Owner 决策；环境/权限问题由同一 mono 负责人按明确 repair Issue 交付，并在新组合核对固定版本和原边界证据后闭合。普通 child 结果和 mono 单次交付始终不会自动声称项目整体完成。

## 群与 Dashboard

Owner 通过唯一关联原 mono 起始消息发送 `全局验证 <plan|prepare|start|finish|reconcile|check|rework|complete>：<JSON>`。plan 的 request ID 或其他动作的 validation ID 必须属于这条原任务。Dashboard 同样核对具体 JSON 操作，展示本轮版本、实际输入/Git 元数据、准备、测试输出/产物 digest、占用和返工关系。原生 participant 工具排除 prepare。

公开进度、返工材料与完成消息引用原任务；总负责人 summary、独立接收后的总管 Owner summary 保留验证 ID、固定版本、测试、占用和修复关系。没有当前全局完成证据时 `whole_project_complete=false`。证据变更后已发的历史消息保持可核对，当前视图不继续宣传失效的完成。

`tests/test_global_validation.py` 在获准的合成仓库中演示原公开 mono 目标、child 交付、实际测试失败、明确 Issue 返工、真实 Lark SDK mention/消息构建及独立 native 接收、真实 child 源码修复 commit 和 unittest、父 gitlink/自身 unittest、重验、child 结果/总负责人 summary/总管回复原 Owner 消息，以及排队、失效、手动只观察、未分配模块和未知响应重启对账。mandatory 原生 SDK smoke 使用固定 pristine SDK 和 artificial home，不访问真实 Profile、配置、认证、聊天或仓库。
