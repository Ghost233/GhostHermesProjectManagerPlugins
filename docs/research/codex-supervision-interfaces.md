# Codex 会话监督与人工应答接口核实

核实日期：2026-10-06（Asia/Shanghai）。对应决策工单：[核实 Codex 会话监督与人工应答接口](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/3)。

## 结论与证据等级

Codex app-server 协议提供会话启动、续接、追加输入、中断、事件流和人工请求。它有足够的协议表面支撑开发型 Profile 的监督适配器，但本次没有运行任务，也没有验收飞书到 Codex 的闭环。[官方 app-server 文档](https://learn.chatgpt.com/docs/app-server)

本文区分三类证据：

- **官方文档**：已实际打开的 OpenAI 页面，用于说明公开接口与公开限制。
- **本机静态证据**：安装的 CLI 帮助及其生成的 TypeScript schema，证明 0.160.1 的类型和命令表面；不能代替 RPC 行为测试。
- **尚未验收**：运行时互操作、重连、桌面可见性、人工应答竞争等；不能从方法存在推导成功。

本轮只核实技术事实。监测周期、人工介入条件、谁可以审批、合并层级的行为和群内回复形式，仍属于 HITL 决策。

## 本机版本与复现边界

`command -v codex` 返回 `/Users/ghost233/.local/bin/codex`，解析后的安装文件是 `/Users/ghost233/.codex/packages/standalone/releases/0.160.1-aarch64-apple-darwin/bin/codex`；`codex --version` 返回 `codex-cli 0.160.1`。

仅执行帮助、版本读取和协议生成。未执行 `thread/start`、`thread/resume`、`turn/start`、审批回复、真实 `codex exec`，未读取登录或凭据文件，未修改现有 Hermes 或 Codex 会话。

可复现的生成命令：

```sh
codex app-server generate-ts --out /private/tmp/hermes-codex-supervision-0.160.1/stable
codex app-server generate-ts --experimental --out /private/tmp/hermes-codex-supervision-0.160.1/experimental
```

两次生成均退出 0，分别产生 734 和 875 个 TypeScript 文件。目录名 `stable` 只是“未加 `--experimental`”的快照名，不代表其中所有类型都已稳定或不受运行时 gating。比如 `ToolRequestUserInputParams` 在默认输出中存在，但类型注释仍标记 `EXPERIMENTAL`。[本机输入请求类型](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ToolRequestUserInputParams.ts)

帮助和生成命令均出现无法创建 PATH aliases 的警告，未影响生成结果。只读 `codex app-server daemon version` 因沙箱禁止连接本机 control socket 而失败。因此 **运行中的 daemon 版本和桌面使用的后端版本未知**，不能用安装的 CLI 版本替代它们；也不能据此断言 daemon 未运行。

主要快照的 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `stable/ClientRequest.ts` | `e2761659dcbdc94e98768fbbd97987a55334393b670aaf9412b54a779f3b13fa` |
| `stable/ServerRequest.ts` | `30880f1a8da876eec30600ba27560d05ad0db5c645e14564c9c79a2429274f06` |
| `stable/ServerNotification.ts` | `459d76defb8b368e906b2afbdd14f5ea8cae33eabdd28de097fd703c6f38c4ad` |
| `stable/v2/ThreadResumeParams.ts` | `ceeedf2cfda691faf97432ee29135837f4cc0e7a9f3e9dc97db2379c63f73aa4` |
| `stable/v2/ToolRequestUserInputParams.ts` | `0e3063b70a99b815e170b3c157416924315308a1c8af201dcc2f3fb459e4144a` |
| `stable/v2/ThreadItem.ts` | `047df9febb13b40263fb5b0abe244171c4dcb5e83229bbf9efba31abae840746` |
| `experimental/ClientRequest.ts` | `f21e6307c8c2c7f2b5a32d434cdca238e3ec19b71a1f40b60f8c305289860436` |

快照在临时目录，不作为发布物；后续可以按同一版本重新生成。实现绑定应以所连接 server 的版本和实际协议验收为准。

## 会话与执行控制

以下方法均见 0.160.1 的 [ClientRequest](/private/tmp/hermes-codex-supervision-0.160.1/stable/ClientRequest.ts)。

| 所需能力 | 本机协议证据 | 可以成立的结论与边界 |
| --- | --- | --- |
| 启动插件任务 | `thread/start` 返回 `thread`；`turn/start` 传 `threadId` 与 `input` | 先保留返回的 thread id，再启动一轮执行；创建 thread 本身不是任务已经运行或完成。[启动参数](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ThreadStartParams.ts)、[回合参数](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/TurnStartParams.ts) |
| 延续已有历史 | `thread/resume` | 参数注释区分从持久历史加载与重入当前 server 已运行的 thread；优先使用 thread id。不是任意跨进程抢占能力。[续接参数](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ThreadResumeParams.ts) |
| 给运行中的回合追加要求 | `turn/steer`，必传 `expectedTurnId` | 前置条件必须匹配当前活动回合；不是创建新回合，不支持随附 model/cwd/sandbox 等覆盖。[追加参数](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/TurnSteerParams.ts) |
| 给空闲 thread 提供下一项输入 | `turn/start` | 这是会改变会话的执行请求。不要用它代替只观察；也不要将不确定是否收到的请求盲目重发。[回合参数](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/TurnStartParams.ts) |
| 中断回合 | `turn/interrupt`，传 `threadId`、`turnId` | RPC 接受与收到该 turn 的 `interrupted` 终态需要分别确认；不能据此推导所有后台子进程都已终止。[中断参数](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/TurnInterruptParams.ts)、[回合状态](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/TurnStatus.ts) |
| 登记手动会话 | `thread/list`、`thread/read`、`thread/loaded/list` | 支持读取与枚举。`thread/list` 的默认 sourceKinds 偏交互来源；需要 exec/appServer 等来源时应显式处理，不能只用默认列表当全集。[列表参数](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ThreadListParams.ts) |
| 辨认 thread 与 session | `Thread.id`、`sessionId`、`parentThreadId`、`forkedFromId` | thread 和同一会话树的 session 不一定同 id；汇报、应答与中断需要使用实际目标 thread/turn。[Thread](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/Thread.ts) |

本机还存在实验性 `thread/queue/add`、`list`、`update`、`delete`、`reorder`、`start`。CLI `codex queue --help` 描述为给已有会话排队消息。这是“稍后执行”的接口表面，不能自动等同于 `turn/steer` 的即时追加。[实验方法表](/private/tmp/hermes-codex-supervision-0.160.1/experimental/ClientRequest.ts)、[队列参数](/private/tmp/hermes-codex-supervision-0.160.1/experimental/v2/ThreadQueueAddParams.ts)

## 插件任务、手动会话与桌面边界

| 场景 | 已建立的证据 | 未建立的保证 |
| --- | --- | --- |
| 插件连接自己管理的 app-server | 0.160.1 帮助支持 stdio、Unix socket、TCP WebSocket；有线程与回合控制协议 | 本轮未启动 server，没有连接、认证或真实推理验收 |
| 手动 CLI 使用共享 daemon | 本机 `codex --help` 有 `agents`（浏览共享 daemon 会话）、`--no-daemon`；`app-server proxy --help` 可代理到运行中的 control socket | 不能假设每个手动会话都在同一个 daemon：`--no-daemon`、remote endpoint、不同用户或 home 都可能不同；当前 socket 接入未验证 |
| 对已登记手动会话只观察 | `thread/read` 可只读持久历史；官方明确它不 resume、不订阅 | 默认只观察的业务权限需要插件自行约束；协议连接本身仍有执行方法，不等于服务端只读 ACL |
| 观察活跃手动会话 | 与其实际 server 共用连接时，schema 的 `thread/resume` 支持 rejoin 已运行 thread | 独立新 server 读取同一磁盘历史，不证明它能看到另一个 server 的实时状态或操控其 turn；resume 不能默认当纯只读订阅 |
| 独立 server 创建任务后在桌面可见 | 官方记录桌面与 IDE 的项目内共享 | 没有建立任意第三方 app-server 任务自动进入当前桌面 sidebar、实时可见或可由桌面无缝接管的契约 |

桌面与 IDE 共享是明确的特定客户端行为；共享配置或历史路径不能推出共享活动 executor。[官方客户端设置](https://learn.chatgpt.com/docs/developer-settings#ide-extension-sync)

官方把 app-server 与 WebSocket transport 标为实验能力；这会影响版本锁定和兼容验收。[官方协议与传输说明](https://learn.chatgpt.com/docs/app-server#protocol)、[官方命令说明](https://learn.chatgpt.com/docs/developer-commands)

## 事件、轮询与状态证据

0.160.1 的 [ServerNotification](/private/tmp/hermes-codex-supervision-0.160.1/stable/ServerNotification.ts) 包含 thread 状态、turn 生命周期、item 生命周期、文本增量、命令输出增量、工具进度、人工请求已解决通知等。`initialize` 的 `optOutNotificationMethods` 按精确方法名抑制通知；不要把被抑制的事件当作执行未发生。[初始化能力](/private/tmp/hermes-codex-supervision-0.160.1/stable/InitializeCapabilities.ts)

当前导出的 ClientRequest 没有独立 `thread/subscribe` 或 `pendingRequests/list` 方法。官方说明 start/resume 后接收该 thread 的事件，read 不订阅；所以纯读取轮询与进入活动会话订阅需要分别评估。[官方事件说明](https://learn.chatgpt.com/docs/app-server#events)、[本机方法表](/private/tmp/hermes-codex-supervision-0.160.1/stable/ClientRequest.ts)

| 观察 | 证据 | 应避免的推断 |
| --- | --- | --- |
| 正在执行 | thread `active`；turn `inProgress`；item 有 started 而无 completed | 不证明工作有有效进展，也不证明卡死。[线程状态](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ThreadStatus.ts)、[回合状态](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/TurnStatus.ts) |
| 等待人工审批 | `activeFlags` 含 `waitingOnApproval`，结合实际请求 | 没有请求 payload 时不能重建待审批动作或提交决定。[活动标记](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ThreadActiveFlag.ts) |
| 等待用户输入 | `waitingOnUserInput`，或输入请求的 `isBlocking=true` | 存在非阻塞问题时不能把整个任务一律报成停工。[活动标记](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ThreadActiveFlag.ts)、[输入请求](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ToolRequestUserInputParams.ts) |
| 长命令 | commandExecution 有 `status`、`processId`、`aggregatedOutput`、`exitCode`、`durationMs`；输出 delta 有 thread/turn/item id | 无输出可能是安静长命令；时间字段不能证明卡死。`durationMs` 在运行中也可能尚无值。[Item](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ThreadItem.ts)、[输出事件](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/CommandExecutionOutputDeltaNotification.ts) |
| 无事件 | 传输没有新消息；导出的普通方法表没有周期性任务 heartbeat 契约 | 不能区分安静执行、网络断连、客户端停止读取、事件被过滤或服务阻塞；需要额外证据，阈值属于后续产品决策。[方法表](/private/tmp/hermes-codex-supervision-0.160.1/stable/ClientRequest.ts)、[通知表](/private/tmp/hermes-codex-supervision-0.160.1/stable/ServerNotification.ts) |
| 客户端连接断开 | EOF/WebSocket close 或传输错误 | 只是监督通道不可用；不等于远端 turn 失败或已停止。这是传输与执行分离的推论，仍需断连验收 |
| 推理请求错误 | ErrorNotification 有 `willRetry`；TurnError 有 error info | 有重试的错误通知不应直接变成业务失败；终态需要另核实。[错误事件](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ErrorNotification.ts)、[错误类型](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/CodexErrorInfo.ts) |
| 空闲或未加载 | thread `idle`、`notLoaded` | idle 不是任务验收完成；notLoaded 只说明该 server 的运行时加载状态，不能推导别处无活动。[线程状态](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ThreadStatus.ts) |
| 回合终态 | turn `completed`、`failed`、`interrupted`，有时间与 error 字段 | completed 只说明一轮执行结束；答非所问、提出问题、没有验收或尚有排队工作都可能如此。[Turn](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/Turn.ts) |
| final answer item | agentMessage 可带 `phase=final_answer` | provider 不保证 phase 一致；phase 未知不是完成依据，final answer 也不是产品验收。[MessagePhase](/private/tmp/hermes-codex-supervision-0.160.1/stable/MessagePhase.ts) |

实验 schema 另提供 `thread/backgroundTerminals/list`，含 `itemId`、`processId`、`osPid`、CPU、内存等。这有助于补充长命令证据，但这些指标可为 null，且只针对 loaded thread；不能提升为固定“卡死检测 API”。[后台终端类型](/private/tmp/hermes-codex-supervision-0.160.1/experimental/v2/ThreadBackgroundTerminal.ts)

轮询可以使用 `thread/read`、`thread/list`、`thread/loaded/list`；官方将 `thread/turns/list`、`thread/items/list` 标为实验，长历史要处理分页与后端不支持的情况。通知导出的空 item 列表也不能当“没有工具活动”。事件、补读与业务验收是不同证据来源。[官方线程读取说明](https://learn.chatgpt.com/docs/app-server#threads)、[本机分页参数](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ThreadTurnsListParams.ts)

## 普通问题和执行审批

### 结构化用户输入请求

0.160.1 `item/tool/requestUserInput` 的 server request 带 RPC `id`，params 带 `threadId`、`turnId`、`itemId`、`questions`、`isBlocking`、`autoResolutionMs`。每个问题有 `id`、`header`、`question`、`isOther`、`isSecret` 与可选 options。回应是按问题 id 映射的 answers。[ServerRequest](/private/tmp/hermes-codex-supervision-0.160.1/stable/ServerRequest.ts)、[请求参数](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ToolRequestUserInputParams.ts)、[问题类型](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ToolRequestUserInputQuestion.ts)、[回应类型](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ToolRequestUserInputResponse.ts)

示意回应（不是对现有任务的实际调用）：

```json
{"id":"original-rpc-request-id","result":{"answers":{"question-id":{"answers":["用户实际答案"]}}}}
```

本机类型注释要求用 `isBlocking` 判断是否阻塞，并将 `autoResolutionMs` 标为 deprecated；官方页面目前仍着重描述 timeout。实现时要按 server 版本适配，而不是以文档中的旧字段独自推断等待行为。本次没有测过 blocking/nonblocking 的真实模型调用。[本机参数](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ToolRequestUserInputParams.ts)

此外，本机 `agentMessage` 可以有 `delivery="async"` 与 `questions`（只有 title 和 options）。这能提示非阻塞问题的存在，却不是上述带 RPC id、question id 的请求格式。没有建立其通用跨客户端应答契约，不能凭这些字段伪造一个 requestUserInput 回应。[Item](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ThreadItem.ts)、[异步问题](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/AsyncUserInputQuestion.ts)

如果只是 agentMessage 自然语言提问，则不存在可据以回复的 server RPC request id。回答属于用户新输入：运行中可评估 steer，空闲时可评估 turn/start；语义识别、是否需要立即回答和是否允许自动恢复由后续工单决定。[追加参数](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/TurnSteerParams.ts)、[回合参数](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/TurnStartParams.ts)

### 审批请求

| 请求 | 本机 params/回应 | 对群内闭环的技术约束 |
| --- | --- | --- |
| `item/commandExecution/requestApproval` | thread/turn/item、`kind`、`startedAtMs`、可选 approvalId、reason、command、cwd、network context；回应 `{decision: ...}` | `kind` 区分 `command` 与 `writeStdin`；同 item 可有不同 approvalId，不能只按 item 去重。网络 context 带 host/protocol，需要保留被审批对象。[请求](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/CommandExecutionRequestApprovalParams.ts)、[kind](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/CommandExecutionApprovalKind.ts) |
| `item/fileChange/requestApproval` | thread/turn/item、startedAtMs、reason、grantRoot；回应 `{decision: ...}` | `grantRoot` 类型注释标 UNSTABLE，且注释明确是否被兑现尚不确定，不能承诺会话级目录授权一定生效。[请求](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/FileChangeRequestApprovalParams.ts) |
| `item/permissions/requestApproval` | thread/turn/item、environment、cwd、requested permissions；回应 granted permissions、scope | 权限回应的结构不同于普通答案或 command decision。[请求](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/PermissionsRequestApprovalParams.ts)、[回应](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/PermissionsRequestApprovalResponse.ts) |

command decision 包括 accept、acceptForSession、decline、cancel，以及策略修订；file decision 有前四种。一次允许和长期授权有不同意义，不能将普通群聊的“好/继续”自动解释为扩大权限。[命令决定](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/CommandExecutionApprovalDecision.ts)、[文件决定](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/FileChangeApprovalDecision.ts)

连接器的副作用审批也可能通过 user-input 请求呈现，因此不能只按方法名把 `requestUserInput` 全部判成普通问题。MCP 还可发起 `mcpServer/elicitation/request`；这类请求同样需要按原始 payload 和回应格式处理。[官方连接器审批说明](https://learn.chatgpt.com/docs/app-server#approvals)、[本机 MCP 请求](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/McpServerElicitationRequestParams.ts)

实验版本的命令审批还导出 `availableDecisions` 和 `additionalPermissions`。默认快照没有这些字段；不要假设每次审批都有相同候选项。[实验审批参数](/private/tmp/hermes-codex-supervision-0.160.1/experimental/v2/CommandExecutionRequestApprovalParams.ts)

### 回送原任务与请求失效

人工回应是 server request 的 JSON-RPC response，不是把答案随便 `turn/start` 到相同 thread。需要保留原 RPC id 及 thread/turn/item 对应关系，并在原请求的有效 transport 上返回正确 result 类型。RPC id 是 string 或 number，不能用某个问题 id 或 item id 替代。[RequestId](/private/tmp/hermes-codex-supervision-0.160.1/stable/RequestId.ts)、[ServerRequest](/private/tmp/hermes-codex-supervision-0.160.1/stable/ServerRequest.ts)

`serverRequest/resolved` 带 threadId 与 requestId，表明请求已解决或清除；它不是“批准成功执行”的结果。用户输入请求也可能在 turn 开始、完成或中断时被清除。迟到的群回复必须先确认仍有相应请求；请求清除后不能复用旧 id。[已解决通知](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ServerRequestResolvedNotification.ts)、[官方人工请求说明](https://learn.chatgpt.com/docs/app-server#approvals)

因此闭环验收需要验证“收到有效请求 → 展示正确问题/动作 → 实际用户回应 → 对应请求仍有效 → 回送 → 请求 resolved → 后续执行结果”。谁能回应、秘密问题如何展示、多人抢答、超时或缺席如何处理，仍待 HITL 决策；本研究没有替用户选择这些行为。

## 断连、接管与重启恢复

| 边界 | 已建立的能力 | 未验收的部分 |
| --- | --- | --- |
| 客户端重连同一存活 server | 每个新 connection 需初始化；可按 thread id resume；本机 resume 注释说明 rejoin 已运行 thread | pending RPC 请求是否重放、原 id 是否可用、旧客户端是否仍可回答、多客户端抢答如何收敛，公开资料和类型未建立保证 |
| server 进程重启 | 官方提供保存 thread id 后启动新进程、重新 initialize、resume 的恢复方式 | 这恢复对话历史，不保证原在途 turn、命令、approval callback 或 stdin 会话重启后继续。[官方恢复流程](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server) |
| 从另一个 server 接管手动任务 | 安装版本具有共享 daemon/proxy/remote CLI 表面 | 独立 app-server 对同一持久历史 resume 是否会产生双执行、文件竞争或不同权限，未验收；不能承诺无缝接管 |
| 监督器意外退出 | 导出的 read/history 方法可用于补读 | schema 没有持久化事件流 sequence/resume token 或 pendingRequests/list 契约；仅凭历史 item 不能重建仍等待回复的 RPC id |
| 追加请求发出后丢失 response | 本机有 clientUserMessageId、steer 的 expectedTurnId | 字段存在不证明所有重试都幂等；需要检查历史和验收重发语义，避免重复任务。[追加参数](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/TurnSteerParams.ts)、[回合参数](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/TurnStartParams.ts) |

上表涉及“没有保证”是本次研究的证据边界，不是声称 Codex 一定不支持。恢复策略需要在隔离、明确授权的验收环境确认，不能拿用户正在运行的任务实验。

## CLI 非交互方式的适用范围

`codex exec --json` 输出 JSONL 事件，支持按 session id `codex exec resume`；最终消息也可通过 `--output-last-message` 落盘。它适合预设权限的批处理或简化的输出监督，JSONL 格式与 app-server 的双向 RPC 不是同一协议。[官方非交互文档](https://learn.chatgpt.com/docs/non-interactive-mode)

本机 `codex exec --help` 验证这些选项存在，但本研究没有启动 exec。该帮助没有 app-server 的 server-request 回应接口，所以仅以 exec/stdout 不能证明普通输入、审批与群回复回送的交互闭环成立。不能通过对 stdin 追加文本或匹配“Approve?”等终端文本替代正式 RPC 协议。

## 下一阶段必须验收的技术问题

1. 用已知版本的测试 server 验证启动、活动回合追加、空闲回合追加、中断及 business 验收分离。
2. 验证当前 CLI shared daemon、手动 `--no-daemon` 与桌面各自的会话来源、历史读取、运行状态和接入能力。
3. 验证只观察能否满足状态汇报；需要 resume 才能订阅时，确认其配置、请求路由和运行状态副作用。
4. 验证 blocking/nonblocking 普通问题、自然语言问题、command/writeStdin/network/file/permissions 审批的真实 payload 与答复。
5. 验证迟到回复、重复回复、桌面与插件同时回应、turn 被中断、传输断连、监督器重启与 server 重启。
6. 验证安静长命令、活跃但无事件、短暂推理重试、进程退出与后台 terminal，能否分别给出有证据的状态。
7. 验证绑定版本差异：isBlocking、experimental gating、分页历史、pending request 重放、客户端消息重试语义。

这些是待确定的验收范围，不是本轮已经完成的测试或实现方案。
