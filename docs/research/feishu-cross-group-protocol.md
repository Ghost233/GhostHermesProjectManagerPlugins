# 飞书跨群引用、消息链接与身份关联接口调查

日期：2026-10-06（Asia/Shanghai）。Wayfinder 工单：[核实飞书跨群引用、消息链接与身份关联接口](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/12)。

协议前提：[确定飞书群内公开交接与消息关联协议](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/5#issuecomment-6017769799)。总管在入口群与全部项目群，项目负责人留在项目群，助手按需入群；各群保留任务起始消息引用，使用统一关联。生命周期采用[确定责任角色、项目与 Profile 的归属和生命周期的最新修订](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/4#issuecomment-6017855378)：mono 封存父子一起；恢复只父，子逐个恢复，不自动续 Codex。本研究不另选 HITL 策略。

复用已发布证据：[Hermes 插件与飞书公开协作接口调查][R-hermes]、[助手迁移、历史查询与旧入口归档接口调查][R-migration]。前序报告仅复用 API 事实；产品生命周期以前述最新修订为准。本机源码基线沿用前序已核实的 Hermes 完整 SHA `bd0affe5e5f723579df8902852f5d0c47795f355`；本轮只补读发送、回复、身份和群成员相关源码。未运行 Git/gh，未读取真实配置/凭据、创建群或机器人、邀请成员、发送消息或改变运行状态。

## 事实结论

飞书 create 支持向目标群发送新消息，reply 只绑定原 `message_id` 所属会话，forward 是另一个正式跨目标发送入口。跨群不能共享同一条 native reply tree：各群的本地起始消息 ID、源群/源消息引用和统一业务关联需分别保留。平台发送成功只证明创建/回复成功，不能证明目标 assistant 已接单。机器人入群及人工回复身份也需独立核对，消息 ID 或显示名本身不授予权限。[发送][F-create]、[回复][F-reply]、[转发][F-forward]、[身份概述][F-identity]

**消息链接生成契约未建立**：已检查的 create/reply/get 响应没有稳定的用户可点击 `message_link` 字段；当前官方 Python SDK message resource没有列出相应 generator。本研究没有找到可核验的正式“给 message_id 生成消息链接”API或完整 AppLink构造契约，因此不拼造 URL。各群原消息 ID/来源引用有正式事实基础；自动生成可点击链接仍是需求与后续验收项。[获取消息响应][F-get]、[官方 SDK message resource][F-sdk-message]

## 官方 API、权限和群边界

以下官方页面均实际打开；标准 HTML页面未返回正文时，直接读取同域 public `document_portal/v1/document/get_detail?fullPath=…`，以 `code=0` 的 `data.content` 核对。只采用官方文档和第一方 SDK，未用搜索摘要或社区帖作结论。

| 接口 | 现行能力与权限 | 对协议的边界 |
| --- | --- | --- |
| `POST /im/v1/messages` | 指定 `receive_id_type` 和 `receive_id`；应用身份需 `im:message` 或 `im:message:send_as_bot` 等文档列出的权限；bot在目标群且有发言权限 | 在目标群 create产生新的message_id；源群引用可以作为内容/插件关联信息，不能作为另一群的native reply anchor。[发送消息][F-create] |
| `POST /im/v1/messages/{message_id}/reply` | 基于原消息；同样要求发送权限、bot在原群且有发言权限；`reply_in_thread=true`以话题形式回复 | request没有独立目标chat_id；原消息在哪个会话，reply就绑定哪个会话。不能传别群消息ID来“引用到目标群”。[回复消息][F-reply] |
| `POST /im/v1/messages/{message_id}/forward` | 原消息＋目标receive_id；bot在目标群并有发言权限；目标用户在可用范围；不支持再次转发合并转发内的子消息 | 可转发到其他目标，但响应是新message_id，需保留两端关联；不能把forward当同一reply tree，也不能推断任意app可读取源消息。[转发消息][F-forward] |
| `GET /im/v1/messages/{message_id}` | 应用启用机器人能力；bot必须在消息所属群 | 知道ID不等于可读；其响应有message/chat/thread/root/parent/sender/mentions，没有已核实的message-link生成字段。[消息详情][F-get] |
| `POST /im/v1/chats/{chat_id}/members` | `im:chat` 或 `im:chat.members:write_only`；邀请bot的member ID类型为`app_id`。调用者和被邀请bot均需机器人能力 | 操作者须在群；群若限制仅owner/admin加人，调用者须符合该限制，或满足创建群bot的`im:chat:operate_as_owner`条件。最多一次邀请5个bot，加入后群内bot总数不超过15。[邀请成员][F-invite] |
| `PATCH /im/v1/chats/{chat_id}/members/me_join` | 将token对应操作者加入群；`im:chat` 或 `im:chat.members:write_only` | 只支持公开群；内部群要求同租户。助手知道项目群ID不等于能自助加入私有群。[主动入群][F-join] |
| `GET /im/v1/chats/{chat_id}/members/is_in_chat` | `im:chat`、`im:chat:readonly` 或 `im:chat.members:read`等；返回token对应用户/bot是否在群 | 检查的是调用token的actor，不能用总管token冒充目标助手已经在群。内部群要求同租户。[成员状态][F-in-chat] |
| `GET /im/v1/chats/{chat_id}/members` | 操作者在群；有成员读取权限，按选定user ID类型返回列表 | **不返回机器人成员**；不能用普通群成员列表证明某个助手bot已加入。[成员列表][F-members] |

入群请求的 `code=0`也不应被直接当成所有目标已经可用：响应包含 `invalid_id_list`、`not_existed_id_list` 和等待群主/admin审批的 `pending_approval_id_list`。实际成员身份和发言能力应由独立证据确认，审批如何呈现沿用已定协议。[邀请响应][F-invite]

官方文档修订值：create `1775801451000`、reply `1775801452000`、get `1775808289000`（均2026-04-10 UTC）；invite `1741868620000`（2025-03-13 UTC）；me_join `1737081995000`（2025-01-17 UTC）；is_in_chat `1735820031000`（2025-01-02 UTC）；members list `1730257096000`（2024-10-30 UTC）。这些是读取时仍由官方返回的现行内容，不代表本轮运行验收日期。

## 关联与去重：各群锚点和统一业务关联

| 证据 | 可以证明 | 不能据此推断 |
| --- | --- | --- |
| 发送响应message_id | 平台创建了指定发送结果的消息 | 目标bot接收到事件、Hermes admission通过、任务被接受或完成 |
| chat_id＋当地起始message_id | 某群内任务的native回复/话题锚点 | 另一群可以使用这个ID作为自己的reply anchor |
| root_id/parent_id/thread_id | 消息在其原会话的回复/话题坐标 | 跨群业务父子任务关系、全局request/任务唯一性 |
| 业务统一关联记录 | 按已定协议连接入口群和各项目群的本地锚点/回传 | 飞书会自动维护业务状态、跨群复制删除/编辑或接单确认 |
| 平台uuid | create/reply相同uuid在1小时内至多成功一条；reply不填uuid不去重；forward相同uuid对同一目标1小时内最多一次 | 永久幂等、message_id等于uuid、超过窗口不会重复、不同群发送共用uuid就安全 |
| 入站message_id | 官方接收消息文档要求用message_id而非event_id做业务幂等 | 已完成人工身份核对、任务状态转移或重启恢复 |

一手来源：[create][F-create]、[reply][F-reply]、[forward][F-forward]及[前序真实mention/事件与关联研究][R-hermes]。接单/人工回答/结果回传需要按已定协议记录独立事件；API发送成功不替代这些事件。

## 人工和机器人身份：跨应用作用域

| 标识 | 正式作用域 | 协议注意点 |
| --- | --- | --- |
| 用户open_id | 同一用户在不同app不同；官方明确不能把app A取得的open_id直接用于app B | 全局人工回复身份不能仅比较不同app回调的open_id字符串；在每个app侧需要正确的ID绑定。[用户身份][F-identity] |
| 用户union_id | 同一应用服务商提供的多个app间统一；应用服务商是组织概念 | 不能推断任意开发商app的union_id相同；需核对实际组织/服务商边界。[用户身份][F-identity] |
| 用户user_id | 用户在企业/租户内的身份；同一用户不同企业不同 | 租户必须参与核对；取得user_id还受字段权限影响，不能假定所有事件都有值。[用户身份][F-identity]、[发送ID/字段权限][F-create] |
| bot app_id | 公开应用的唯一标识；邀请bot进群用它 | 不等于真人user_id，也不等于真实mention填入的bot open_id。[邀请成员][F-invite]、[本机bot identity][S-bot-identity] |
| bot sender/mention身份 | 接收事件区分sender_type=user/bot和mentioned_type=user/bot；本机自身bot open_id由`/bot/v3/info`识别，peer bot名由basic_batch查 | bot ID的跨app映射需实际多app核对；不能把“用户open_id作用域规则”直接当作已验证的bot映射，也不能凭名字认定peer profile。[插件与公开协作研究][R-hermes]、[本机 bot 身份][S-bot-identity]、[peer bot 查询][S-peer-name] |

本机 `_resolve_sender_profile` 优先将tenant user_id（否则open_id）写入SessionSource.user_id，将union_id写入user_id_alt；session participant默认优先alt ID。这个映射改善会话关联，但normalized source没有显式表达所有原始ID类型、接收app和tenant细节。人工回复需核对受信任adapter/回调来源、实际sender_type和作用域身份；正文里的姓名、@展示标签、任务号、转述或引用不能单独作为授权证据。[sender mapping][S-sender]、[session participant][S-participant]、[原始接收/mention边界研究][R-hermes]

真实@依赖有效身份标签，text/post与card语法有差异；跨群交接仍需目标bot在目标群、`include_bot`接收权限和Hermes admission。前序报告已核实这些条件，本轮不重复应用创建指南，也不替用户选择人工回答策略。[插件与公开协作研究][R-hermes]

## 本机 Hermes 发送约束

| 正式入口 / 实际行为 | 对本协议的影响 |
| --- | --- |
| 插件`pre_gateway_dispatch`可通过gateway adapter side-channel send；platform factory可获得active lark_oapi client | 插件可用已有client构造正式API；native send/get与helper invite仍需相应权限。它们不是业务级群白名单或身份授权的替代物。[插件与公开协作研究][R-hermes] |
| `FeishuAdapter.send(chat_id, content, reply_to, metadata)` | 一旦有reply_to，优先调用原message_id的reply接口；即使chat_id传B而reply_to属于A，也不会变成对B的跨群回复。[Hermes 路由实现][S-send-raw] |
| thread metadata与reply anchor | reply_in_thread取决于thread_id；源码在无reply anchor时尝试`receive_id_type=thread_id` create。当前create文档枚举仅列open_id/union_id/user_id/email/chat_id，**该fallback的正式平台契约未建立**，须验证，不能据源码存在就宣称官方支持。[Hermes 路由实现][S-send-raw]、[发送消息][F-create] |
| 内容拆分 | send把长内容分chunks，只返回最后一次响应的message_id | 如任务起始消息被拆分，不能假定返回ID是第一段起始锚；每群锚点需要验收其实际内容/顺序。[Hermes send][S-send] |
| retry uuid | `_send_raw_message`每次create/reply构造新的uuid4；retry循环重新调用它；`.send`没有调用者uuid参数 | 现成Hermes重试没有保持同一次发送的稳定平台uuid，不能继承平台1小时去重保证。插件若需要已定协议的稳定重发证据，须验证发送路径和自身记录。[Hermes 路由实现][S-send-raw]、[Hermes 重试实现][S-retry] |
| reply target丢失/撤回 | 普通群可能降级create新消息；topic中跳过顶层fallback避免新topic | send成功也可能没有原reply引用；结果必须检查实际response/新message_id，不用chat参数推断关联保真。[Hermes 重试实现][S-retry] |
| bot进退群事件 | adapter现有handler记录日志并清理chat metadata cache | 没有现成插件业务目录的join-success事件契约或自动更新“助手已可接单”；需要API/事件证据验收。[进退群事件][S-membership-event] |

SendResult主要提供`success`、`message_id`和raw response，不是跨群任务账本。以上是固定Hermes版本的限制；研究没有改driver或声称已经通过传输重试测试。[SendResult][S-send-result]

## 尚需多 App、多群验收

1. 两个app、至少入口/项目两个群：每群起始消息真实可见；回传命中当地anchor，同时能关联同一业务任务；reply不会被源群message_id误路由。
2. 普通群与topic：原消息撤回/删除、权限不足、create/reply fallback以及长消息拆分后，关联记录仍反映实际发送结果；不把最后chunkID当首段锚。
3. 重发及不确定网络结果：稳定uuid的实际调用路径、1小时内重复、超窗口、跨群/不同内容和重新启动后的去重分别验证；发送成功与接单事件分别成立。
4. 助手加入项目群：公开群self-join、私有群invite、owner/admin限制、pending审批、bot数量限制及目标bot自己的is_in_chat；普通成员列表过滤bot的影响。
5. 同一真人分别回复两个app：open_id不同情况下user_id/union_id/tenant映射正确；不存在字段或不同租户/服务商不能被误认。真实bot sender与@目标也有受信任app/Profile绑定。
6. 可点击消息链接：**自动生成契约尚未建立**；后续若提供正式文档/API或明确允许的链接取得方式，再验证跨群读者是否有原消息可见权限。ID来源记录本身不能宣称链接可点击或跨群可读。
7. 封存与恢复：群成员仍可见不等于助手在运行；按照最新生命周期决定验证父子封存和逐个恢复，不因再次入群/收到回传自动续Codex。具体执行接口沿用其他工单，本研究不扩展生命周期实现。

事实调查完成；跨群协议的需求边界和未验收项已建立，没有执行任何真实群操作。

[R-hermes]: https://github.com/Ghost233/GhostHermesProjectManagerPlugins/blob/900dd9fc8b1901caa551459777e79bab42d8ddde/docs/research/hermes-plugin-interfaces.md
[R-migration]: https://github.com/Ghost233/GhostHermesProjectManagerPlugins/blob/05b5645344ed8f4e1dd93060eb82b7b89db3d253/docs/research/profile-migration-archive.md
[F-create]: https://open.feishu.cn/document_portal/v1/document/get_detail?fullPath=%2Fserver-docs%2Fim-v1%2Fmessage%2Fcreate
[F-reply]: https://open.feishu.cn/document_portal/v1/document/get_detail?fullPath=%2Fserver-docs%2Fim-v1%2Fmessage%2Freply
[F-forward]: https://open.feishu.cn/document_portal/v1/document/get_detail?fullPath=%2Fserver-docs%2Fim-v1%2Fmessage%2Fforward
[F-get]: https://open.feishu.cn/document_portal/v1/document/get_detail?fullPath=%2Fserver-docs%2Fim-v1%2Fmessage%2Fget
[F-invite]: https://open.feishu.cn/document_portal/v1/document/get_detail?fullPath=%2Fserver-docs%2Fgroup%2Fchat-member%2Fcreate
[F-join]: https://open.feishu.cn/document_portal/v1/document/get_detail?fullPath=%2Fserver-docs%2Fgroup%2Fchat-member%2Fme_join
[F-in-chat]: https://open.feishu.cn/document_portal/v1/document/get_detail?fullPath=%2Fserver-docs%2Fgroup%2Fchat-member%2Fis_in_chat
[F-members]: https://open.feishu.cn/document_portal/v1/document/get_detail?fullPath=%2Fserver-docs%2Fgroup%2Fchat-member%2Fget
[F-identity]: https://open.feishu.cn/document_portal/v1/document/get_detail?fullPath=%2Fhome%2Fuser-identity-introduction%2Fintroduction
[F-sdk-message]: https://raw.githubusercontent.com/larksuite/oapi-sdk-python/v2_main/lark_oapi/api/im/v1/resource/message.py
[S-send]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L1652
[S-send-raw]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L3747
[S-retry]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L3926
[S-send-result]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L3789
[S-bot-identity]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L3497
[S-peer-name]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L3310
[S-sender]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L3248
[S-participant]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/gateway/session.py#L676
[S-membership-event]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L2134
