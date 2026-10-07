# 仓库队列与工作区交接（#18）

队列按规范的 Git common directory 串行外层任务，符号路径与 linked worktree 共用同一顺序。其他逻辑仓库可以同时运行自己的任务会话。Matt 子线程属于原外层占用，结束核对会遍历它们，不把内部并行再登记成外层请求。

`accept_request` 保存冻结目标、验收、Issue URL/更新时间、规范仓库以及递增的 `queue.sequence`。受理、排队、占用、工作区交接受阻与交付分别保存。`start_task` 是明确执行要求：前项未释放时保存真实调用者和等待原因，返回 `repository_busy`；已核实停止或交付后，管理实例与 Gateway 监督循环只派发这些已有执行授权、已准备且仍符合基线的请求。未请求执行的受理不会自动开跑。超时、连接更换或未知后台不会按时间释放占用。

## 明确准备

`Manager.prepare_task(identity, request_id, plan)`、令牌 bridge 的 `ManagementClient.prepare_task(request_id, plan)`、Dashboard `/task` 的 `action=prepare` 与原生 `hermes_pm_task` 共用只读核对。负责人按本 Issue、明确依赖及仓库约定形成安排，无须本人为每项重复批准。

```json
{
  "branch": "main",
  "commit": "0123456789abcdef0123456789abcdef01234567",
  "dependencies": [],
  "issue_updated_at": "2026-10-07T00:00:00Z"
}
```

`commit` 是完整本地提交，空 Git 仓库可显式为 `null`；分支仍须明确。`dependencies` 是本管理实例已交付请求的 ID，基线必须包含这些固定交付版本。当前 checkout 含前项未合并源码交付时必须明确声明该依赖，或由获准的仓库准备步骤提供另一基线。

插件不 checkout、不创建运行任务的额外 worktree，也不 stash、移动、清理、删除、覆盖用户文件。分支/提交不符或遗留文件未明确确认时，保存 `preparation.workspace`、路径和 digest 并等待。确需保留未提交内容时，plan 的 `workspace_digest` 必须精确匹配显示的 `source_digest`；保存逐文件 digest，启动前再次核对，普通交付不能把这些用户文件的新内容当作原内容。准备之后任何分支、提交、源码或 porcelain 状态变化都阻止开跑。

飞书输入须唯一关联原任务起始消息：`确认基线：main <完整 SHA>`；可加 `依赖：<请求ID,请求ID>` 与 `保留：<workspace digest>`。空仓库的 SHA 可显式写 `unborn`。Dashboard 显示相同 plan、遗留路径、队列原因与状态。原生工具不接受 actor、权限或启用能力声明。

## 释放与明确继续

普通交付在原执行及冻结验收证据之外，核对原 thread 的全部完整终态、同逻辑仓库已加载线程、Matt receiver 子线程、每个相关 thread 的全页后台列表与当前可信宿主的 process coverage。方法缺席、分页缺口、子执行未终结或进程覆盖未知时保持原占用。已核实交付保存完整源码提交（无源码变更也记录现有 HEAD）、源码 digest、工作区状态、遗留内容与执行结束证据；PR 审查与合并状态单列。

停止遵守 #17 的 `stop_records` 和原执行核实契约。明确继续登记新的 `execution_arrangements` 与新的队列 sequence/安排时间，关联原 request/thread/上一停止，排在当前待办之后；旧 `accepted_at` 不能让继续插队。排队的同一 instruction ID/内容返回 `queued`，变更内容拒绝；核实 idle、有效责任、原服务代次、当前准备及排他输入收据后才能对原 thread 发新 turn。旧停止证据保留，未知输入不重放。

发现服务已加载的手动或未知执行时，按其真实 cwd 的 Git common directory 核对并保存 `queue.external_occupancy`。linked worktree 或子目录路径不能绕过外层串行。更换连接代次不能用新服务空列表释放旧占用；需要原服务核实或明确对账。

## Issue 来源

`refresh_task_source` 从可信 `delivery_source.read_issue` 只读取得原 Issue；实际 GitHub source 在每次业务读取前 switch Ghost233 并核验 login。保存当前定位、与冻结目标/验收的 unified diff 及版本记录。`accepted_scope`、执行 prompt 和交付验收不随来源变化改写。来源不可读显示 unverified；新要求须通过原控制授权明确追加或新请求。飞书 `核对Issue来源`、Dashboard `action=source` 与原生工具显示相同差异。

## 验证边界

公共令牌 bridge 驱动真实合成 JSONL 子进程，覆盖持久 FIFO/失联占用、双任务推进、两个逻辑仓库同时活动、错误基线/遗留文件保留、明确继续按新安排排队、同 common-dir alias 的手动执行、冻结 Issue 来源差异。真实 Git 核对全部在合成仓库中进行。

上述协议 peer 和测试 verifier 明确是 synthetic fixtures。#16 的完整生产写入/工具边界、获准真实模型服务、当前宿主执行覆盖和真实群发送仍缺实际证据，生产启动/控制 gate 继续关闭。队列实现不把 RPC 形状、合成收据或 UI 状态变成真实能力验收。
