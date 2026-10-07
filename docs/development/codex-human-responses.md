# 原请求人工应答切片（[将群内答复送回有效问题与审批](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/21)）

本切片支持当前受管 Codex 0.160.1 stdio 连接发来的人工请求，并保留实际服务、连接代次、原 thread/turn/item/approval 与原 RPC ID。离线 JSONL peer、人工宿主收据及 pristine Hermes SDK smoke 只验证插件行为。没有获准真实执行服务或群内实际审批验收；生产人工答复仍须独立的当前宿主收据，真实写入与控制 gate 不因此开启。周期汇总与提醒留给 [定期汇总并提醒待处理请求](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/23)。

`Manager.answer_human_request(identity, request_id, human_request_id, reply_id, response)` 是公共答复入口。令牌 bridge、Dashboard `/task` 的 `action=answer`、原生 `hermes_pm_task` 共用它。本人身份来自既有认证入口；可见任务或受分配的机器人不等于本人新决定，participant 原生工具不能批准本人请求。调用体不接受 actor、权限、验证器或启用声明。`reply_id` 是稳定 ID，与原请求、本人和答复永久绑定；重复只读原记录，换 ID 不能重放未知或已送回的答复。

入站 ServerRequest 与出站 RPC 分表，数值和字符串 ID 保留类型。刷新消费通知时不会丢弃原请求参数；只有连接内存表保有原始 pending 句柄。答复前再读取同服务原 thread，核对身份、有效任务控制、仓库边界、同代连接、原回合及 pending 句柄；写锁与读表锁内最多送回一次正式 `{id:原ID,result:正式回应}`。原界面先处理、结束回合、归还控制、停止/交付/释放任务、断线或重启后都不能从历史或持久摘要重建旧批准。

普通 `item/tool/requestUserInput` 使用 `isBlocking` 区分普通阻塞与非阻塞问题；不使用已废弃的超时自动批准、拒绝或停止。`isSecret` 和 `isOther` 按固定 schema 的省略默认值 false 处理。任何秘密 metadata、敏感内容或配置中的已知秘密，使整个请求仅显示安全定位；问题内容与答案不进入群、任务记录或日志。不能为独立 stdio 服务编造桌面链接，目前定位是实际 service/thread/item 与“原客户端界面需人工打开”，URL 为 null。

命令、文件及权限的正式请求归为审批。user-input 的题干、header、选项标签及说明中涉及命令、执行、网络、权限或批准的内容也不能作普通事实答复；未支持的该类请求只定位原界面。动态工具、凭据刷新、attestation、旧审批和 MCP elicitation 同样不伪造方法。实验性 additionalPermissions/availableDecisions 出现时先定位原界面，本切片不假定它们的运行能力。普通自然语言问题仅从原当前回合最近 agentMessage 的实际内容观察，回答通过 [在原会话追加、停止和明确继续任务](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/17) `append` 的 expected-turn/idle-input 控制语义；它不成为虚构 RPC，不允许以自然语言批准执行。

普通结构化答复为 `{"answers":{"原题ID":["答案"]}}`，正式回送转为每题 `{answers:[...]}`。原题 ID 集合、选项及自由输入标记均须匹配。自然语言题固定答案 ID `answer`。

审批答复须为 `{"decision":"accept|decline|cancel","operation_id":"原操作完整摘要SHA","scope":"turn"}`，具体操作与 scope 在 Dashboard 和即时提示中供本人核对。普通“继续”“好的”不表示批准。首版不支持 acceptForSession、策略修改或 session 权限。权限审批额外提供显式 `permissions` JSON 子集；除原请求子集外，路径必须在原 session 实际仓库内，写入不能覆盖或逃逸到只读子仓库。原任务缺少网络扩权证据时拒绝 enabled 网络；未核验的 entries/glob 形状交原界面。不得增加原请求之外的路径、网络或字段。拒绝权限返回空 permissions 和 turn scope。fileChange 必须展示并绑定当前原 item 的具体路径、kind 与 patch；内容变化使旧操作过期，缺 patch 不能批准。grantRoot 的 session 权限效果尚不稳定，交原界面处理。

群内本人通过已登记且核验过的入口/项目群回复：

- `回答 原人工请求完整ID：答案`，单题可省略 ID，但仍需唯一关联；多题使用 Dashboard。
- `批准 原人工请求完整ID 操作 原操作完整ID 范围 turn`，或者同格式 `拒绝`。
- 权限批准末尾加 `权限 {明确批准的JSON子集}`。

已登记并核验的总管（steward）入口群可路由本人答复到唯一原请求；它只路由人类决定，原任务的项目、负责人、服务和执行范围均保持原关联。总管缺少开发能力时不会因此接开发请求。明确完整人工请求 ID 可以跨已登记入口群与项目群关联。开发负责人群未指明 ID 时只在原 app、租户、群、本人和负责范围内匹配；总管入口由本人可见的所有待答请求核对唯一目标；引用问题提示或任务起始消息可缩小候选。多个待答请求持久登记澄清，绝不选最近任务。关联后的反馈沿原任务 outbox 与真实 @ 回传；入口群回复也收到本消息下的处理反馈。不同机器人/app 的总管通道不借用原项目机器人发送 outbox；原项目提示由原绑定通道处理。秘密答复被拒绝且内容不保存。

状态分为人工答案 `reply.received`、原连接发送 `reply.sent`、原请求 `resolution` 与 `execution_result`。本地写入送回不等于服务已处理；`serverRequest/resolved` 可能表示答复或清理，只使请求 resolved，不声称批准成功或任务交付。执行结果只来自原 thread/read 中匹配 item 的实际终态。未知或未确认结果先读取原记录/核对原服务，禁止盲目重发；Dashboard 保留提交 ID，仅提供核对入口。

宿主证据目录可在 [执行一个 Codex Issue 并核对交付结果](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/16) 四类 hashed 收据与可选 [在原会话追加、停止和明确继续任务](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/17) task_control 之外提供 `human_response` 收据。它绑定当前二进制、配置、平台、仓库、policy、service/generation，要求实际 `thread/read`、四类支持的服务请求、`serverRequest/resolved`，原 thread/turn 与原连接正式应答证明，以及 question、nonblocking、command/file/permission approval、owner_only、wrong_request、duplicate、resolved_race、disconnect、secret、unknown_no_replay 的实际 PASS。解析器只核对已有真实证据，不把合成收据文字变成实际能力。

`tests/test_questions.py` 使用独立 `questions_fixture_server.py` 真实 JSONL 子进程、令牌 Unix bridge、HTTP 与已核验群入口验证原请求保留、正式应答、方向相同 ID、数字/字符串类型、分类/秘密、明确审批范围、权限子集、原 UI 竞争、过期/停止/控制归还、身份拒绝、断线/重启/重复、歧义与自然语言原回合追加。测试只使用合成占位资料。真实服务、原 daemon、原会话、真实配置与秘密、正式安装和机器人开通均未操作。

`questions_smoke_runner.py` 在 pristine SDK 的真实 registry 上验证 participant 无权冒充本人，Owner Dashboard 正式答复经 token bridge 回到原 JSONL 请求；lark-oapi 1.6.8 的真实 Builder 验证显式审批的引用与真实 @。无 thread 对应的未知请求只给 Owner 显示原服务定位，不保存 params 或凭据。群内人工反馈先持久登记原消息 namespace 与 UUID，未知发送和原消息重放不会再发送。
