# Hermes 插件与飞书公开协作接口调查

日期：2026-10-06（Asia/Shanghai）。对应 Wayfinder 工单：[核实 Hermes 插件与飞书公开协作接口](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/2)。

本调查回答正式扩展接口和平台约束，不决定 HITL 产品行为。用户已经确认：责任角色与开发/非开发能力分类分开，默认通过飞书群内真实 @ 公开交接；本报告没有用内部 A2A 请求替代这一要求。

## 结论与证据范围

可以通过独立 Hermes 插件实现角色目录、任务记录、飞书消息拦截和外发、后台监督及管理界面；现成接口不等于已经具备完整的任务请求/回传协议。当前飞书官方文档明确支持群内机器人消息和机器人 mention，但必须申请包含机器人消息的接收权限，Hermes 的 `FEISHU_ALLOW_BOTS` 只是平台投递之后的本地过滤开关。[原生插件][H-plugin]、[飞书接收消息][F-receive-data]、[Hermes 飞书设置][H-feishu]

研究已核对源码和官方文档。尚未发送真实群消息、运行 agent、修改配置或验证当前运行中的 gateway；端到端验收仍需独立进行。

| 证据层 | 范围与版本 |
| --- | --- |
| 本机源码基线 | `/Users/ghost233/.hermes/hermes-agent`；官方仓库 origin 为 `NousResearch/hermes-agent`；HEAD 为完整 SHA `bd0affe5e5f723579df8902852f5d0c47795f355`，提交日期 2026-10-02。已实际打开[官方提交页][H-commit]，下文源码链接固定到此提交。 |
| 本机既有工作区修改 | `agent/status_output.py`、`gateway/display_config.py`、`gateway/warning_notifications.py`。未修改它们；本调查的主要接口文件不在该修改列表中。 |
| Hermes 在线文档 | 2026-10-06 实际打开插件、hooks、Profiles、multi-profile gateway、飞书、cron、Dashboard 与 Desktop SDK 官方页面。在线文档持续更新，不能把在线接口自动认定为本机已安装接口。 |
| 飞书发送消息 | 官方 public document portal 返回 `code=0`；`updateTime=1775801451000`，即 2026-04-10 UTC。[发送消息内容][F-create-data] |
| 飞书发送消息内容结构 | 官方 public document portal 返回 `code=0`；`updateTime=1783411335000`，即 2026-07-07 UTC。[内容结构][F-content-data] |
| 飞书接收消息 | 官方 public document portal 返回 `code=0`；`updateTime=1785921394000`，即 2026-08-05 UTC。[接收消息内容][F-receive-data] |

飞书标准文档页面已实际打开，但 HTML 阅读工具返回空正文；随后直接读取同一官方域名的公开 `document_portal/v1/document/get_detail?fullPath=…`，解析 `data.content` 与 `updateTime`。没有使用社区帖、第三方转载或搜索摘要作为结论依据。访问的是公开文档，未读取或使用用户凭据。

## 正式插件接口支持矩阵

| 所需能力 | 本机已确认的接口 | 边界与缺口 | 一手证据 |
| --- | --- | --- | --- |
| 可独立安装的统一插件包 | `plugin.yaml` 与 `register(ctx)`；tools、hooks、slash/CLI commands、skills 可同包 | 插件要启用才运行；通用插件不应冒充模型 provider 或内置 Profile role | [插件官方文档][H-plugin]；[PluginContext 源码][S-context] |
| Profile 名称与路径 | `list_profile_names()`、`get_profile_dir()`、`create_profile()`；`ctx.profile_name` | 名字枚举不读各 Profile 配置；组织层级与能力目录没有现成 schema | [Profile 名字枚举][S-profile-names]；[Profile 创建][S-profile-create]；[当前 Profile][S-profile-context] |
| 责任角色与能力分类 | 插件 settings/state 可保存自有元数据；可向 prompt 注册目录摘要 | 原生 `profile.yaml.role` 目前只接受 `setup`，会授予 backend 能力，不能直接放总管/项目负责人等自定义值 | [role 定义][S-profile-role]；[role 读取过滤][S-profile-meta]；[prompt section][S-prompt] |
| 模型可调用的管理/交接工具 | `ctx.register_tool()`；`ctx.register_command()`；`ctx.register_cli_command()` | 这些接口注册执行入口，不提供任务依赖、请求 ID 或回传状态模型 | [工具注册][S-tools]；[插件 authoring][H-plugin] |
| 群消息入口与直接回复 | `pre_gateway_dispatch(event, gateway, session_store)`；可 `skip/rewrite/allow`；文档明确允许 `gateway.adapters[platform].send(...)` | 在 gateway auth/pairing 前执行，不能将到达该 hook 当作已经授权；支持 async callback | [hook 正式契约][H-hooks]；[本机 fire-site][S-inbound] |
| 飞书原生 API 调用 | `ctx.register_platform_handler("feishu", factory)` 获得 `(lark_oapi_client, adapter)` | adapter 应只读；传来的 native 是 API client，不是事件 dispatcher；缺少正式 `register_feishu_event_handler` | [native handler 契约][H-plugin]；[注册源码][S-platform-factory]；[connect fire-site][S-feishu-connect]；[dispatcher 构造][S-feishu-dispatcher] |
| 定期后台监督 | `ctx.spawn_task(coro)`、`ctx.on_unload(callback)`；现成 cron jobs | asyncio task 需要运行中的 loop；不自动持久化任务恢复信息；没有通用 `ctx.register_cron_job` | [task/cleanup][S-task]；[cron 创建][S-cron-create] |
| 监督状态持久化 | `ctx.state`；原子 JSON，单插件 10 MiB 配额 | Profile-scoped；不是跨 Profile 共享目录或事务性任务队列 | [PluginState][S-state] |
| Web 管理界面 | 同包 `dashboard/manifest.json`、JS bundle、FastAPI `plugin_api.py` | API `/api/plugins/<name>/` 在 Dashboard 进程内；API routes 在启动时挂载，rescan 不等于重新挂载 | [Dashboard 官方文档][H-dashboard]；[实际 router mount][S-dashboard-mount] |
| Hermes Desktop 管理界面 | 同包 `desktop/plugin.js`；可复用插件 backend，`broadcast_plugin_event` 推送更新 | Desktop SDK 与 Web Dashboard SDK不同；这里的 Desktop 是 Hermes Desktop，不是 Codex 桌面 | [Desktop SDK][H-desktop]；[正式事件广播][S-events] |

### 在线文档与本机版本的差异

在线 [Event Hooks 文档][H-hooks] 已列出 `post_gateway_admission`：在正常授权、bot admission、控制消息处理后提供 session/source snapshot；可返回 `handled` 和 `reply` 来消费消息，插件仍承担 durable acceptance 和去重。该接口未出现在本机上述 SHA 的 `VALID_HOOKS` 或 `gateway/run_inbound.py`；本机 pinned [hooks 集合][S-hooks] 不能用这个新契约。本轮没有更新 Hermes。后续实现必须明确选择本机可用契约，或另行确认升级版本后再复核。[本机 hook 注册及未知名称处理][S-hook-register]

`register_hook()` 对未知名称只是警告并存储；注册成功不证明存在 fire-site。因此不能通过注册 `post_gateway_admission`、`gateway_message_delivered` 或虚构 startup hook 来宣称集成已支持。[本机注册行为][S-hook-register]

## 飞书真实 @：发送、接收与关联边界

### 平台支持及必要权限

| 条件 | 当前官方契约 |
| --- | --- |
| 应用身份向群发送 | 应用启用机器人能力并发布；机器人在群内且有发言权限；发送 API 为 `POST /open-apis/im/v1/messages`。应用身份需 `im:message` 或 `im:message:send_as_bot` 等文档列出的发送权限；群自定义 webhook bot 不适用该 API。[发送消息][F-create-data] |
| 接收用户与其他机器人 @ 当前机器人的群消息 | 订阅接收消息 v2.0，即 `im.message.receive_v1`；申请 `im:message.group_at_msg.include_bot:readonly`。[接收消息][F-receive-data] |
| 接收群内所有用户与其他机器人消息 | `im:message.group_msg.include_bot:read`；不包含当前机器人自己的消息。[接收消息][F-receive-data] |
| 原有用户消息权限 | `im:message.group_at_msg` / `im:message.group_at_msg:readonly` 以及 `im:message.group_msg` 仅用户消息，不能据此推断 bot-to-bot 投递。[接收消息][F-receive-data] |
| 应用类型限制 | 上述两个 `include_bot` 权限在官方权限表的 `support_app_types` 标为 `custom`。不能推断商店应用同样可用。[接收消息][F-receive-data] |
| Hermes 本地 admission | `FEISHU_ALLOW_BOTS=mentions` 只允许明确 @ 当前 bot 的 peer bot 消息；`all` 允许 peer bot 消息；默认 `none` 丢弃。还检查自身身份、self echo 和 group policy。[admission 源码][S-feishu-admit] |

因此，飞书权限/订阅和 Hermes admission 是两个条件。只改 `FEISHU_ALLOW_BOTS` 不能补足飞书服务器未投递的事件。当前官方支持已得到文档确认，但本机各应用的实际授权、版本发布状态、群成员身份未检查，不能宣称端到端已经可用。[接收事件官方文档][F-receive-data]、[本机 admission][S-feishu-admit]

### 真实 mention 与展示文本

文本消息支持 `<at user_id="真实ID">显示名</at>`；post 的 `at` 节点与 post/md 的飞书扩展 mention 也有正式格式。普通文本的 `@名字` 不等于身份绑定。真实 ID 要有效；text/post 的 `user_id` 语法与卡片 Markdown 的 `id` 语法不同。官方接收事件的 `mentions[].mentioned_type` 区分 `user` 与 `bot`，说明目标机器人 mention 有可观测身份字段。[发送内容结构][F-content-data]、[接收事件][F-receive-data]

本机 Feishu `.send(content)` 自动选 text 或 post/md，没有单独的结构化 Feishu mentions 参数；但这不意味着它必然无法发送真实 mention：官方当前 text 和 post/md 均定义了身份标签语法。应验证构造的标签是否经过 Hermes 格式化、拆分及 fallback 后保真，而不能仅因缺少 `mentions=` 参数就否定支持。[outbound payload][S-feishu-outbound]、[发送过程][S-feishu-send]、[F-content-data]

正式平台 factory 还可取得 active lark_oapi client，通过插件自己构造 payload 调发送 API；无需据此认定必须 monkey-patch 核心。与此同时，通用 `send_message_tool` 的结构化 `mentions` 当前只支持 WhatsApp，不能把它的参数契约套到 Feishu。[native handler][S-platform-factory]、[实际 client 传递][S-feishu-connect]、[send_message mentions 边界][S-send-message]

机器人的身份 ID、目标 mention ID 和 Profile ID不是同一概念。发送 API 的官方 ID说明强调用户 `open_id` 属于应用作用域；本机 adapter 的自身 bot identity 也区分 open_id、user_id、name。因此跨应用目录不能只按显示名或未经核验的 ID 复用；bot ID发现与互相 mention 的实际映射应在多 bot 验收中记录。[ID类型说明][F-create-data]、[bot identity][S-feishu-identity]

### 可关联的消息坐标

| 坐标 | 平台 / 本机现状 |
| --- | --- |
| 发送者身份 | 官方事件有 sender_id 和 `sender_type=user|bot`；Hermes source 保留 user_id/name/alt ID 和 `is_bot`。[F-receive-data]、[S-source]、[S-feishu-normalize] |
| mention 身份 | 飞书原始事件有结构化 mentions、ID、name、`mentioned_type`。Hermes内部 `FeishuMentionRef` 只有 name/open_id/is_all/is_self，规范化后将其他 mention 写为 `[Mentioned: …]` 文本；MessageEvent 没有独立 mentions 字段。[S-feishu-mentions]、[S-message-event] |
| message / chat / thread / reply | Hermes保留 message_id、chat_id、native thread_id、parent/root reply ID。`root_id` 不被误作 topic thread_id。[S-feishu-normalize] |
| 发送返回 | SendResult 可带成功状态和 message_id；飞书发送响应还列出 message_id/root_id/parent_id/thread_id/mentions。[S-feishu-send-result]、[F-create-data] |
| 去重 | 飞书明确要求用 message_id 而非 event_id 做幂等；发送 UUID 在1小时内至多成功一条。Hermes飞书入站还有24小时 TTL去重。[F-receive-data]、[F-create-data]、[H-feishu] |
| 任务请求/回传 | 上述都是消息证据，平台没有提供本插件业务的 request_id、parent task、责任归属、完成/失败协议。消息 API成功与 peer agent 接受/任务完成是不同事实；此点是接口边界推论，不是已决定的产品行为。[S-message-event]、[S-feishu-send-result] |

`pre_gateway_dispatch` 的 `event.raw_message` 当前可接触原始 SDK 对象，且文档允许 gateway adapter side-channel send；该 hook 是强权限 in-process 入口。`gateway_platform_event` 的稳定 observer 契约不提供 raw SDK/adapter，不能把后者限制误写为“所有插件无法读取 raw”。结构化 mention 的稳定插件快照、任务接收确认与普通自动回复的出站消息关联仍需要实现/验收；没有现成全平台 `gateway_message_delivered` hook。[pre hook 契约][H-hooks]、[本机 MessageEvent][S-message-event]、[pinned observer 边界][S-observer-boundary]

Feishu API client 和 EventDispatcherHandler 是分开的对象，connect factory 到来时 dispatcher已经创建。官方通用表称 Feishu可用于 API calls/event routing，但本机没有专门的 Feishu事件 registrar；不能从这句话推导出可以安全替换已构造的 private dispatcher。[dispatcher][S-feishu-dispatcher]、[client prepare][S-feishu-client]、[H-plugin]

## Profile 隔离与共享目录

原生 Profile 是不同 Hermes home，隔离 SOUL、记忆、会话、skills、cron 与 state.db；官方明确不应让两个独立 agent进程共同写同一个 home。多 Profile可以由单个 multiplex gateway服务，消息路由、输出 adapter、凭据和存储仍属于各自 Profile；共享进程不意味着共享记忆或权限。[Profiles][H-profiles]、[multiplex gateway][H-multi]

`ctx.state` 动态解析当前 Profile的 `plugin-data/<namespace>/state.json`，不是进程全局数据库。原生 `profile.yaml` metadata列出的 description/display_name/role也没有组织树或开发能力字段。若多 Profile访问同一个组织目录，需要明确目录拥有者、读取方式和单写入/并发更新边界；这是接口要求产生的待决问题，本报告不指定由谁拥有目录。[PluginState][S-state]、[原生 metadata][S-profile-meta]

`gateway.profile_routes` 可按接收 bot所属 `bot_profile`、platform、chat/thread和 user_id选执行 Profile；它解决入口路由，不表达总管→项目→子项目责任关系。Profile重命名也有原生 previous_names metadata；目录引用怎样跟随重命名应在规格中决定。[ProfileRoute][S-route]、[ProfileInfo][S-profile-info]

## 长期监督、重启及定时能力

| 机制 | 已证实能力 | 不应自动推断 |
| --- | --- | --- |
| `ctx.spawn_task` | 在运行中的 asyncio loop创建受监督 task，卸载或 force reload取消；`on_unload`注册清理。[S-task] | 不自动持久化调度；进程退出后的恢复、leader ownership及外部Codex是否仍活着要另行记录/检查。 |
| connect factory / async pre hook | Feishu factory在 connect的运行loop调用；async pre hook由gateway loop await。[S-feishu-connect]、[H-hooks] | 同步 `register()`任意加载路径都有loop；收到第一条消息后启动的任务就等于冷启动即监督；这些尚未验收。 |
| reload / reconnect | 新插件平台callback可late rewire；本机base按native client和(plugin,qualname)去重。在线文档明确已经wired的native handler没有通用un-wire，disable后可能需要gateway重启。[S-native-wire]、[H-plugin] | 插件后台task取消意味着platform SDK handler也被全部移除；失联重建native client后旧引用继续有效。 |
| cron jobs | `create_job`接受script/no_agent/monitor_script/origin/deliver/workdir；monitor输出不变可抑制agent run。[S-cron-create] | 每次检测都要LLM；该参数已决定通知频率或卡死阈值。 |
| script-only watchdog | 定时运行脚本、stdout直发；空stdout静默；非零退出/timeout告警。脚本必须位于拥有job的 `$HERMES_HOME/scripts/`。[H-cron] | 可直接把插件目录任意文件作为cron script；自动继承全部Hermes provider凭据。 |
| cron scheduler provider | 独立collector接受 `register_cron_scheduler`；provider有start/stop/recovery/fire_due。[S-cron-collector]、[S-cron-provider] | 这是通用 `PluginContext` job registrar；安装项目管理插件必须替换scheduler。 |
| Hermes会话/approval/stream/kanban hooks | 可以观察Hermes自己会话结束、人工审批和worker状态。[S-hooks] | 自动获得外部Codex的turn结束、需要用户回答、卡死或退出状态。外部适配器需要自己的证据。 |
| `ctx.inject_message` | 向已有Hermes durable session_key注入消息；host接受不等于turn完成；需要相应grant。[S-inject] | 创建任意新群路线，或替代真实公开群交接。 |

以上支持把常驻监督与低成本定时检查做在插件/现有scheduler上，但具体选择、频率、restart ownership、通知规则及人工干预行为仍是后续规格决定，不能在本研究中替用户回答。

## 管理界面入口

Web Dashboard插件可提供独立页面与shell slots，FastAPI router在`/api/plugins/<name>/`后提供管理API；请求受Dashboard正常auth和enabled状态检查。Python后端运行在Dashboard进程内，可以导入Hermes模块，但不是稳定的远程全Profile控制平面。API routes启动时挂载，增改backend需要验证重启路径。[H-dashboard]、[S-dashboard-mount]

Hermes Desktop可在同一包放`desktop/plugin.js`并复用Dashboard backend；其SDK与Web Dashboard不同。Python正式广播接口`broadcast_plugin_event`面向该进程连接的Desktop客户端，不能据此声称跨所有机器/gateway必达。管理界面是飞书操作入口的补充；用户是否需要Desktop、Web或两者属于尚未解决的产品选择。[H-desktop]、[S-events]

## 留待实现前或实现阶段验收的问题

这些问题的定义已经清楚；研究未发送实际消息，因此不能用文档核验替代运行验收。

1. **多bot真实 @链路**：两个企业自建应用都在同一群；目标bot发布并获`include_bot`接收权限；发送者构造有效target identity；验证text与post/md在格式化、拆分、fallback后都有原始`mentions[].mentioned_type=bot`与正确ID，且只目标角色接单。
2. **请求/应答证据**：保留source、chat/thread、request消息ID、reply parent、sender和target；验证接受确认、结果与发送成功分别可观察。未知target、错误群、未授权bot、重复message、重发UUID和跨thread回传的判定需要规格定义。
3. **native生命周期**：cold start无需用户先发消息即可启动监督；disconnect/reconnect、插件disable/force reload与gateway restart后不残留重复任务/client引用；不能仅检查`register()`成功。
4. **Profile/共享目录**：多Profile状态不会串scope，当前profile切换、Profile合并/重命名和并发写目录可恢复；目录拥有者与写权限要在HITL决策中明确。
5. **外部Codex证据**：从正式Codex接口取得session、turn、工作目录、活动/停止/待用户输入/审批等证据；Hermes自己的hooks不能作为外部Codex状态替身。卡死判定和可自动采取的动作应由其他工单确定。
6. **管理面与版本**：确定Dashboard/CLI/飞书管理入口；验证backendreload、授权边界和持久化状态。若决定依赖在线新增hook，先明确最低Hermes版本并重测，不能默认更新当前Hermes。

## 可供工单记录的事实答案

独立Hermes插件已有足够的正式入口支撑组织元数据、群消息拦截/API外发、后台task/cron与管理界面，无需先假定修改核心。当前飞书官方支持bot-to-bot群消息；真实公开 @同时依赖有效mention身份、`include_bot`权限/事件订阅及Hermes本地admission。原生Profile.role不承载自定义组织角色，state默认隔离；任务关联、重启恢复与端到端mention保真仍需规格和验收。在线新增`post_gateway_admission`不在本机固定版本中。研究完成，产品行为与实现尚未完成。

[H-commit]: https://github.com/NousResearch/hermes-agent/commit/bd0affe5e5f723579df8902852f5d0c47795f355
[H-plugin]: https://hermes-agent.nousresearch.com/docs/developer-guide/plugins
[H-hooks]: https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks
[H-profiles]: https://hermes-agent.nousresearch.com/docs/user-guide/profiles
[H-multi]: https://hermes-agent.nousresearch.com/docs/user-guide/multi-profile-gateways
[H-feishu]: https://hermes-agent.nousresearch.com/docs/user-guide/messaging/feishu
[H-cron]: https://hermes-agent.nousresearch.com/docs/user-guide/features/cron
[H-dashboard]: https://hermes-agent.nousresearch.com/docs/user-guide/features/extending-the-dashboard
[H-desktop]: https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk
[F-create-data]: https://open.feishu.cn/document_portal/v1/document/get_detail?fullPath=%2Fserver-docs%2Fim-v1%2Fmessage%2Fcreate
[F-content-data]: https://open.feishu.cn/document_portal/v1/document/get_detail?fullPath=%2Fserver-docs%2Fim-v1%2Fmessage-content-description%2Fcreate_json
[F-receive-data]: https://open.feishu.cn/document_portal/v1/document/get_detail?fullPath=%2Fserver-docs%2Fim-v1%2Fmessage%2Fevents%2Freceive
[F-create]: https://open.feishu.cn/document/server-docs/im-v1/message/create
[F-content]: https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json
[F-receive]: https://open.feishu.cn/document/server-docs/im-v1/message/events/receive
[S-context]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/plugins.py#L231
[S-tools]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/plugins.py#L457
[S-profile-names]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/profiles.py#L426
[S-profile-create]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/profiles.py#L1335
[S-profile-context]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/plugins.py#L400
[S-profile-role]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/profiles.py#L94
[S-profile-meta]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/profiles.py#L868
[S-profile-info]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/profiles.py#L613
[S-prompt]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/plugins.py#L998
[S-inbound]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/gateway/run_inbound.py#L111
[S-platform-factory]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/plugins.py#L858
[S-feishu-connect]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L1528
[S-feishu-dispatcher]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L1429
[S-feishu-client]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L3913
[S-feishu-admit]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L3383
[S-feishu-identity]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L246
[S-feishu-mentions]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L932
[S-feishu-normalize]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L2616
[S-feishu-outbound]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L3621
[S-feishu-send]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L1652
[S-feishu-send-result]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L3789
[S-message-event]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/gateway/platforms/event.py#L45
[S-source]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/gateway/session.py#L66
[S-send-message]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/tools/send_message_tool.py#L267
[S-hooks]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/plugins.py#L109
[S-hook-register]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/plugins.py#L974
[S-observer-boundary]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/website/docs/user-guide/features/hooks.md#L1312
[S-route]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/gateway/profile_routing.py#L55
[S-state]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/plugins_state.py#L134
[S-task]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/plugins.py#L410
[S-native-wire]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/gateway/platforms/base.py#L2308
[S-cron-create]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/cron/jobs.py#L1812
[S-cron-collector]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/cron_providers/__init__.py#L89
[S-cron-provider]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/cron/scheduler_provider.py#L136
[S-inject]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/plugins.py#L604
[S-dashboard-mount]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/web_server_dashboard.py#L805
[S-events]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/plugin_events.py#L1
