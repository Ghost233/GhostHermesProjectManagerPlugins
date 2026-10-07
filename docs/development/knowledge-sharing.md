# Wiki 来源查询与原任务事实补充（#25）

这是实际来源 adapter、公共管理入口、注册群消息与原会话输入的实施契约。离线演示读取登记的合成 local 资料，真实 Lark SDK builder/model 与自有 JSONL peer 都有证据；尚未安装或验收真实 Wiki／群／模型服务，不能将这些夹具作为实际连接通过。

## 授权与来源

`register_knowledge_source(identity, expected_version, source)` 只有已核验本人入口能调用。目录角色、成员身份和 Wiki 自身读取能力不是查询者授权。每个来源登记：

- id/name/provider_ref，以及当前已登记的独立非开发 Wiki Profile 与身份。
- `query_subjects`：真实 identity_ref → 明确 scope_id 列表，不支持由上级或成员资格推导的范围。
- `public_channels`：完整 app、transport tenant、recipient tenant/open_id、群、发送 Profile、允许材料 scope、明确可见主体及真实 Wiki mention ID。
- `wiki_bindings`：query/result 各自完整接收 namespace、已登记 sender Profile/identity、sender tenant/open_id/native IDs，公开 channel 和资料 scope。
- `task_profiles`：允许接收这些已公开、当前必要事实的开发 Profile。没有登记就不补充。

Dashboard `/knowledge` 共用 register/query/supplement 操作，body 不接受 actor/requester/role 或 verifier。普通 Profile 的 `hermes_pm_knowledge` 工具先通过独立 participant bridge；owner token alias 拒绝。工具查询必须关联原任务与明确公开 channel，不能把私有查询结果返回到没有原提问者／共享目标证据的群 agent 上下文。本人经可信 Dashboard 入口可按自己的显式来源范围进行独立查询。

原生 `knowledge_providers` 来自可信配置，只接受明确 local reference、登记 root 和 document manifest。`LocalKnowledgeProvider` 不扫描全部知识库，也没有写 API；仅按获准 scope 与查询词读取配置的普通文件，拒绝路径越界、symlink 别名和过大资料。新的来源／超范围必须由本人明确登记，不能从资料正文导入授权。

## 实际资料与定位

查询作为单独 `knowledge_queries` 账本保存，不产生开发 Issue 或执行会话。provider 得到原 requester identity 和获准 scope，不把 Wiki 服务主体替换成 requester。结果严格映射 status/materials/actual searched scope/requester/time；未知业务字段不能覆盖请求、自动补充意图或其他任务。

每份材料有稳定 ID、scope、真实相关文字、kind、locator、version、updated_at 和 link_accessible。kind 区分 fact、inference、suggestion、conflict、stale。local adapter 返回实际文件段落、line 定位、完整内容 SHA-256 和文件时间；不拼装静态答案。链接不可取得时仍需允许共享的实际文字，不能只返回 URL。查不到或拒绝说明实际查询范围；不会扩大到私人来源补答案。

来源异常与秘密只保存固定状态／范围，不把错误中的原文或秘密放进群、SQLite、事实输入。来源材料原库保持只读，独立 Wiki 的职责和资料不迁移、不复制成项目全记忆。来源授权撤销后，私有历史材料从响应隐藏；记录不被自动删除。

## 群协议与真实 @

从当前任务发起 query 时，先保存查询意图和逐段 outbox，生成 `资料查询 QUERY_ID` 标记，真实 @ 已登记 Wiki 并回复原任务锚。只有完整 SDK 原始 sender、app/tenant、接收 bot、群、native IDs、已有查询关联及正式 mention 一致时，注册 Wiki 入口才处理该请求。纯文本写 @名字或任意群回复不能假冒 Wiki。

Wiki 以原 requester 权限读取，结果作为固定版本保存；`资料结果 QUERY_ID RESULT_VERSION` 沿实际收到的 query 消息回复、真实 @ 查询发送者。结果回到登记 requester namespace 后再核对原 query parent/root、实际 sender、固定版本和逐段原生发送凭据。query/result 没有恢复开发权或批准权限，#21 原人工请求、正式审批与资料路由独立。

本人直接对独立 Wiki 真实 @ 的命令为 `查询 SOURCE_ID SCOPE1,SCOPE2：问题`。身份、接收 app 与原消息先核验；Wiki 回复实际本人，不要求开发任务、不造 Issue，也不启动 Codex。

outbox 每段先登记 UUID/意图，再发真实 Lark post/@/reply；保存 delivered/failed/unknown 及实际 message/chat/parent/root/thread 字段。配对错误的 transport 在 claim 前拒绝。发送结果未知或重启留下 sending 时保留 unknown，不换 UUID 重放。重复 query ID 固定 requester、问题、scope、任务和意图；相同请求返回原版本结果，变更内容拒绝。

## 原受控任务补充

`query_knowledge` 可由原已授权 requester 明确 `auto_supplement` 意图；创建时冻结原 thread/turn/service/generation/control 与当前执行安排。结果中的指令和字段不能开启此意图。公开结果完全核实后，`supplement_knowledge` 仅引用实际允许的相关 fact，保持当前冻结 Issue 的目标／验收／版本及原仓库边界。

输入明确标为 untrusted source data，并以 JSON 引用实际材料和来源；源指令不变成新授权、执行审批或新的范围。inference/advice/conflict/stale 保留为材料；冲突和过时不自动选边。完整记忆、历史和未经要求的原资料不随输入附带。

发送前再次正式读取原线程。只接受仍 active 且唯一 inProgress 原回合，使用 #17 `control_task append` 与真实 expectedTurnId；不得借 idle 路径创建新回合。query/result 派生稳定 knowledge instruction ID，持久意图先于 RPC。重复与 unknown 保留原 ID，既不重发，也不换 ID 重试。

任务已结束、stopping/stopped、已交付／释放、仅观察、控制已归还、thread/turn/generation/安排改变、通道未知或资料迟到时，只展示 materials_only／blocked 原因；不 steer/start/resume。明确后续工作沿新的受理或已有普通控制契约处理。#26 记忆回写不属于本票。

## 展示与验证

Dashboard 展示来源授权、实际 requester/scope、原任务关联、fact/inference/suggestion/conflict/stale、定位／版本／时间、原群 result 锚、逐段凭据与补充状态。当前来源登记／查询／补充有 HTTP API，页面是展示；不将未执行的 UI 按钮点击称为已验证。

`tests/test_knowledge.py` 从公共 token/API 与实际原始 Lark text/post 事件驱动 readonly local provider、真实 SDK builder 和 stdio peer，覆盖查询／@回传／原任务 facts 一条链，另有权限拒绝、上级/Wiki 无替代权、重复、scope 撤销、资料字段注入、命名空间伪装、实际拒绝／冲突／秘密、idle/错回合/停止/归还/只观察/失联矩阵。`knowledge_smoke_runner.py` 使用 pristine SDK registry 和独立 participant bridge，拒绝真实 .hermes/.codex 与网络，验证工具拒绝私有上下文替代及稳定原会话补充。

真实验收仍需获准 Source/Wiki 连接、app/tenant/群与真实 @/消息关联、原服务模型预算及 #16/#17/#18 的全部写入／控制／后台边界。生产 gate 保持关闭，不重复已失败权限研究、不降级 full access、不读取真实旧 Wiki 作测试，不自动安装／开机器人／迁移资料。
