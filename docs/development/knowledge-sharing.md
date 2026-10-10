# Wiki 来源查询与原任务事实补充（[公开查询 Wiki 并补入任务上下文](../../../../issues/25)）

2026-10-09 执行目标修订：当前优先由 Hermes 管理专用 DSH 执行实例，停止 Codex 适配，配套认证组件已撤回；Desktop 手动接入非默认，缺获准独立原生连接时不可观察／接管，见[DSH 修订](../specs/dsh-executor-transition.md)。本文保留的旧 JSONL／stdio 协议 peer、旧执行器收据和既有测试描述是迁移前历史证据，不证明 DSH 通过。业务授权、资料保护、写入与生命周期门槛继续有效，执行相关路径须按 DSH 重新验证。

这是实际来源 adapter、公共管理入口、注册群消息与原会话输入的实施契约。离线演示使用登记的 local 资料和与实际 WikiKnowledge schema 一致的 MCP peer，真实 Lark SDK builder/model、原会话 JSONL peer 与 pristine Hermes SDK 的生产配置启动都有证据。获准原 MCP 的只读预检已核对 initialize/list、wiki_status 和一次 budget=500 context-pack 的结构；公开群／模型／原会话的真实服务验收仍单独记录。

## 授权与来源

`register_knowledge_source(identity, expected_version, source)` 只有已核验本人入口能调用。目录角色、成员身份和 Wiki 自身读取能力不是查询者授权。每个来源登记：

- id/name/provider_ref，以及当前已登记的独立非开发 Wiki Profile 与身份。
- `query_subjects`：真实 identity_ref → 明确 scope_id 列表，不支持由上级或成员资格推导的范围。
- `public_channels`：完整 app、transport tenant、recipient tenant/open_id、群、发送 Profile、允许材料 scope、明确可见主体及真实 Wiki mention ID。
- `wiki_bindings`：query/result 各自完整接收 namespace、已登记 sender Profile/identity、sender tenant/open_id/native IDs，公开 channel 和资料 scope。
- `task_profiles`：允许接收这些已公开、当前必要事实的开发 Profile。没有登记就不补充。

Dashboard `/knowledge` 共用 register/query/supplement 操作，body 不接受 actor/requester/role 或 verifier。普通 Profile 的 `hermes_pm_knowledge` 工具先通过独立 participant bridge；owner token alias 拒绝。工具查询必须关联原任务与明确公开 channel，不能把私有查询结果返回到没有原提问者／共享目标证据的群 agent 上下文。本人经可信 Dashboard 入口可按自己的显式来源范围进行独立查询。

原生 `knowledge_providers` 来自可信配置，接受明确 local reference、登记 root 和 document manifest，或下述原 MCP 完整 corpus reference。`LocalKnowledgeProvider` 不扫描全部知识库，也没有写 API；仅按获准 scope 与查询词读取配置的普通文件，拒绝路径越界、symlink 别名和过大资料。新的来源／超范围必须由本人明确登记，不能从资料正文导入授权。

## 原 Wiki MCP 连接

现有 WikiKnowledge 原连接保持为只读服务，不复制为各 Profile 的记忆库。可信管理 Profile settings 可登记：

```yaml
knowledge_providers:
  mcp:example-knowledge:
    url: http://127.0.0.1:9000/mcp
    corpus_scope_id: example-corpus
    expected_server_info: {name: example-knowledge, version: "0.1.0"}
    expected_tool_description: "Read the synthetic knowledge corpus and return citations."
    credential_ref: native:HERMES_WIKI_TOKEN
```

`credential_ref` 是现有 SDK `agent.secret_scope.get_secret` 的名称引用，仅在 Gateway 的管理 Profile/home/secret-scope 校验通过后解析，按 Bearer 使用并留在 RAM；配置和记录不接收秘密原值。明确无需认证的端点可将该引用设为 null。Owner 的 SourceGrant 必须另行授予**原提问者** `example-corpus`，公开回传还需给具体 channel/binding 登记完整同一范围。它表示该连接的**全部 corpus**，不能拿一个小范围名称冒充可过滤子库。现有工具只接受 `prompt/budget`，没有 requester、scope 或可证明的远端 ACL：适配器仅在实际请求 scope 恰为登记 corpus 时调用，子范围拒绝且不发出网络请求，不以 prompt 伪装访问控制。Owner 登记时冻结 URL/corpus/secret-reference 的非敏感 binding digest；同 reference 改指其他语料须重新登记授权。

每次查询只执行 MCP 2025-03-26 initialize、initialized、tools/list 及 `get_context_pack(prompt, budget=500)`，核对 exampleKnowledge 0.1.0 与获准只读工具的实际 descriptor/schema；支持 JSON 和 POST SSE 响应。不请求原文窗口，不枚举其他工具、不处理 server instructions、不写原库。HTTP 响应上限 1 MiB，整次查询设 20 秒预算与每次连接/读取超时，清理只针对本次 initialize 返回的 owned session，连接/读取各最多 3 秒且不读取清理响应正文。公共 query/resolve bridge 最多等 30 秒；结果未知先从 snapshot 核对同一 durable query ID，不盲重试或另造 query ID。

context-pack 的 primary/secondary 相关 summary/excerpt 随真实 citation source/start/end 返回。引用区间的单位未独立核实，所以 locator 保留源给出的数值，不宣称是行号。材料 `version=result-snapshot-sha256:…` 是返回包快照摘要，`updated_at` 是本次本地观察时间；可取得的 artifact repoRef/commit/observedAt/updatedAt 在文字中明确标为 **reported artifact version，source revision unverified**，不编造原文件版本。freshness 的 contradicted/stale/archived 优先分类为冲突／过时；显式 fact/inference/suggestion 分类保留，未知编译 artifact 类型按 inference 展示，不能自动补充为事实。无引用、无法核实结果、工具或协议变化、源拒绝及已知秘密仅留下固定状态与原获准范围。

## 实际资料与定位

查询作为单独 `knowledge_queries` 账本保存，不产生开发 Issue 或执行会话。provider 得到原 requester identity 和获准 scope，不把 Wiki 服务主体替换成 requester。结果严格映射 status/materials/actual searched scope/requester/time；未知业务字段不能覆盖请求、自动补充意图或其他任务。

每份材料有稳定 ID、scope、真实相关文字、kind、locator、version、updated_at 和 link_accessible。kind 区分 fact、inference、suggestion、conflict、stale。local adapter 返回实际文件段落、line 定位、完整内容 SHA-256 和文件时间；不拼装静态答案。链接不可取得时仍需允许共享的实际文字，不能只返回 URL。查不到或拒绝说明实际查询范围；不会扩大到私人来源补答案。

来源异常与秘密只保存固定状态／范围，不把错误中的原文或秘密放进群、SQLite、事实输入。来源材料原库保持只读，独立 Wiki 的职责和资料不迁移、不复制成项目全记忆。来源授权撤销后，私有历史材料从响应隐藏；记录不被自动删除。

## 群协议与真实 @

从当前任务发起 query 时，先保存查询意图和逐段 outbox，生成 `资料查询 QUERY_ID` 标记，真实 @ 已登记 Wiki 并回复原任务锚。只有完整 SDK 原始 sender、app/tenant、接收 bot、群、native IDs、已有查询关联及正式 mention 一致时，注册 Wiki 入口才处理该请求。纯文本写 @名字或任意群回复不能假冒 Wiki。

Wiki 以原 requester 权限读取，结果作为固定版本保存；`资料结果 QUERY_ID RESULT_VERSION` 沿实际收到的 query 消息回复、真实 @ 查询发送者。结果回到登记 requester namespace 后再核对原 query parent/root、实际 sender、固定版本和逐段原生发送凭据。query/result 没有恢复开发权或批准权限，[将群内答复送回有效问题与审批](../../../../issues/21) 原人工请求、正式审批与资料路由独立。

本人直接对独立 Wiki 真实 @ 的命令为 `查询 SOURCE_ID SCOPE1,SCOPE2：问题`。身份、接收 app 与原消息先核验；Wiki 回复实际本人，不要求开发任务、不造 Issue，也不启动 DSH。

outbox 每段先登记 UUID/意图，再发真实 Lark post/@/reply；保存 delivered/failed/unknown 及实际 message/chat/parent/root/thread 字段。配对错误的 transport 在 claim 前拒绝。发送结果未知或重启留下 sending 时保留 unknown，不换 UUID 重放。重复 query ID 固定 requester、问题、scope、任务和意图；相同请求返回原版本结果，变更内容拒绝。

## 原受控任务补充

`query_knowledge` 可由原已授权 requester 明确 `auto_supplement` 意图；创建时冻结原 thread/turn/service/generation/control 与当前执行安排。结果中的指令和字段不能开启此意图。公开结果完全核实后，`supplement_knowledge` 仅引用实际允许的相关 fact，保持当前冻结 Issue 的目标／验收／版本及原仓库边界。

输入明确标为 untrusted source data，并以 JSON 引用实际材料和来源；源指令不变成新授权、执行审批或新的范围。inference/advice/conflict/stale 保留为材料；冲突和过时不自动选边。完整记忆、历史和未经要求的原资料不随输入附带。

发送前再次正式读取原线程。只接受仍 active 且唯一 inProgress 原回合，使用 [在原会话追加、停止和明确继续任务](../../../../issues/17) `control_task append` 与真实 expectedTurnId；不得借 idle 路径创建新回合。query/result 派生稳定 knowledge instruction ID，持久意图先于 RPC。重复与 unknown 保留原 ID，既不重发，也不换 ID 重试。

任务已结束、stopping/stopped、已交付／释放、仅观察、控制已归还、thread/turn/generation/安排改变、通道未知或资料迟到时，只展示 materials_only／blocked 原因；不 steer/start/resume。明确后续工作沿新的受理或已有普通控制契约处理。[用已知资料答复并回写项目记忆](../../../../issues/26) 记忆回写不属于本票。

## 展示与验证

Dashboard 展示来源授权、实际 requester/scope、原任务关联、fact/inference/suggestion/conflict/stale、定位／版本／时间、原群 result 锚、逐段凭据与补充状态。当前来源登记／查询／补充有 HTTP API，页面是展示；不将未执行的 UI 按钮点击称为已验证。

`tests/test_knowledge.py` 从公共 token/API 与实际原始 Lark text/post 事件驱动 readonly local provider、真实 SDK builder 和 stdio peer，覆盖查询／@回传／原任务 facts 一条链，另有权限拒绝、上级/Wiki 无替代权、重复、scope 撤销、资料字段注入、命名空间伪装、实际拒绝／冲突／秘密、idle/错回合/停止/归还/只观察/失联矩阵。`test_wiki_mcp.py` 在公共 token/Manager/client 与实际 HTTP/SSE peer 处核对完整 corpus、连接换绑、协议/工具/引用拒绝、慢响应、秘密与不重试，并驱动原有真实 SDK @/回传/原 active turn 补充链。`wiki_mcp_smoke_runner.py` 用固定 pristine Hermes SDK 的真实 PluginContext/settings/secret scope/Gateway hook 构建生产 provider，普通加载不连服务，原 Unix bridge 线程可使用 RAM 凭据，关闭仅清理自己的资源。`knowledge_smoke_runner.py` 使用 pristine SDK registry 和独立 participant bridge，拒绝真实 .hermes/.codex 与网络，验证工具拒绝私有上下文替代及稳定原会话补充。

真实验收仍需获准 Source/Wiki 连接、app/tenant/群与真实 @/消息关联、原服务模型预算及 [执行一个 Issue 并核对交付结果](../../../../issues/16)/[在原会话追加、停止和明确继续任务](../../../../issues/17)/[按仓库排队并交接下一项 Issue](../../../../issues/18) 的全部写入／控制／后台边界。生产 gate 保持关闭，不重复已失败权限研究、不降级 full access、实际原 Wiki 仅按当次本人授权做有界只读核验，不自动安装／开机器人／迁移资料。
