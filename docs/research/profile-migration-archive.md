# 助手迁移、历史查询与旧入口归档接口调查

日期：2026-10-06（Asia/Shanghai）。Wayfinder 工单：[核实助手迁移、历史查询与旧入口归档接口](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/10)。

前置决定：[确定责任角色、项目与 Profile 的归属和生命周期](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/4#issuecomment-6015801424)。总负责人有自己的 mono 仓库与 Codex，负责全局测试；子负责人明确指定、只做分配 Issue，任务结束保留 Profile；迁移创建新 Profile 和新飞书机器人，旧 Profile/机器人归档；迁移长期记忆、人设和偏好，执行配置重建，旧聊天可查询，Wiki 原资料库连接保留；仅封存总负责人的 mono 时不级联封存子 Profile。本调查只验证这些决定所需的接口事实，不增加 HITL 产品决定。

## 结论与源版本

原生 Hermes 支持新建 Profile、复制人设/精选记忆，以及通过显式 `session_search(profile=旧Profile)` 只读查询旧数据库。named Profile 的 `gateway stop` 可以持久停用该 Profile 的机器人和 cron，同时保留其数据；它不是业务“项目封存”，也不是外部 Codex 中断。没有原生 `hermes profile archive` 子命令。用户要求的选择性迁移、旧→新来源映射、业务角色查询授权和档案保留保证，仍需插件表达与验收。[新建/克隆源码][P-create]、[跨 Profile 查询][P-search-resolve]、[停用源码][P-gateway-lifecycle]、[原生 Profile 命令定义][P-cli]

| 证据层 | 范围 |
| --- | --- |
| 本机固定版本 | `/Users/ghost233/.hermes/hermes-agent`，HEAD `bd0affe5e5f723579df8902852f5d0c47795f355`，提交日期 2026-10-02；前序研究已验证对应 [NousResearch 官方提交][H-commit]。本轮再次只读核对 SHA，未更新代码。 |
| 已有证据起点 | [Hermes 插件与飞书公开协作接口研究](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/blob/900dd9fc8b1901caa551459777e79bab42d8ddde/docs/research/hermes-plugin-interfaces.md)。沿用已核实的插件/飞书/管理面入口，避免重做同一调查。 |
| 官方在线文档 | 2026-10-06 实际打开 Profiles、Persistent Memory、Sessions、Profile Commands、Profile Distributions、multi-profile gateway 及 CLI reference。源码链接固定到本机 SHA；在线文档的新能力单独标明。 |
| 飞书群历史来源 | 实际打开[获取会话历史消息页面][F-history]，HTML 阅读器未给正文，随后直接读取同一官方域名的[公开文档内容接口][F-history-data]；`code=0`、`updateTime=1775808289000`，即 2026-04-10 UTC。 |
| 未执行事项 | 未读取真实 config、`.env`、凭据、SOUL、记忆或聊天；未实际 create/clone/export/import/delete/stop/start，未创建飞书应用或发送消息，未改变运行状态。 |

## 原生迁移能力与用户决定的对应关系

| 类目 | 原生能力 | 迁移边界 / 不能盲目复制的内容 | 一手证据 |
| --- | --- | --- | --- |
| 新 Profile | `create_profile()` 创建独立 home；空白创建种子配置、默认 SOUL、空目录和凭据文件 | 新建 Profile 不是自动创建飞书应用；运行中的 multiplexer 会发现/服务新 Profile，所以配置发布与启用需要可控 | [create][P-create]、[layout/通知 host][P-layout]、[Profiles][H-profiles] |
| 人设 | Profile root 的 `SOUL.md` 是原生 persona；`--clone` 会复制它 | 修改后的 persona 要在新 session 生效；不能把执行配置或系统 prompt 快照当 persona 迁移 | [clone 文件集合][P-clone-sets]、[SOUL 加载][P-soul] |
| 长期精选记忆 | `memories/MEMORY.md`、`memories/USER.md`；clone 默认复制这两个文件 | 两个文件分别包含知识/环境事实和用户偏好，不代表全部历史；现有内容可能引用旧工作目录、旧任务或旧角色，身份连续性与这些引用的正确性要验证 | [clone 文件集合][P-clone-sets]、[Memory 官方文档][H-memory] |
| 偏好 | 原生 USER.md 承载沟通风格、期望等；它随精选记忆复制 | `preferences/` 目录可出现在 export 中，但不等于全部偏好都有统一 schema；模型/工具/sandbox/审批配置也不应自动作为偏好迁移 | [Memory][H-memory]、[export 根集合][P-export-sets] |
| 记忆加载时机 | 精选记忆在 session 开始时读取为 frozen prompt snapshot | 复制完成后已有正在运行的 session 不自动刷新为新记忆；文件存在不等于配置已启用该记忆 target | [MemoryStore 加载][P-memory-load]、[Memory][H-memory] |
| 外部 memory provider | config clone 按 `<provider>/`、`<provider>.json` 约定复制 active provider 配置；provider 可能需要同包插件代码 | 复制 provider 配置不等于复制服务端记忆，也不保证新 assistant identity 独立。官方特别提示某些 Hindsight embedded 配置仍共享 daemon/bank；需要核对 provider namespace/identity | [provider clone helper][P-memory-provider]、[Profiles][H-profiles] |
| Wiki 原资料库连接 | native config/插件/MCP 配置可描述原端点、目录、库或 server；clone 保留非通道配置 | 用户已要求保留原资料库，不应把资料库复制成另一个库或丢弃连接。Wiki 知识库与助手自身 memory bank 不是同一对象。本轮未检查实际 Wiki provider/endpoint，不能声称已接通；连接定义须在重建执行配置时保留并重新验证授权 | [bootstrap clone][P-bootstrap]、[独立 provider 配置][P-memory-provider]、[Profiles][H-profiles] |
| 执行配置 | native `--clone` 同时复制 `config.yaml`、`.env`、model/tool 配置、skills、plugins | 与“执行配置重建”不等价；默认只剥离消息通道，仍保留 provider/tool keys 和模型设置，不能直接作为本产品迁移命令 | [clone 文件集合][P-clone-sets]、[bootstrap][P-bootstrap]、[通道剥离][P-clone-publish] |
| 旧聊天 | 旧 Profile 保留原 `state.db`，可显式只读查询；new Profile 无需接管旧会话的 live route | 查询保留旧 Profile 来源信息，不等于把旧 session 转为新 Profile 的 live 执行上下文；用户已选择“旧聊天可查询” | [session_search profile 参数][P-search-schema]、[目标 DB 只读打开][P-search-resolve] |
| 新飞书机器人 | Hermes 飞书 setup 支持 scan-to-create 或手工配置新应用；每个 Profile 拥有自己的 adapter 身份 | 不能把 old app_id/token/allowlist/配对记录搬到新 Profile 来冒充新机器人；应用 onboarding 与 Profile 创建分别验收 | [飞书官方 Hermes 指南][H-feishu]、[clone channels 契约][P-create] |
| 插件业务状态 | 通用插件 state 原生按 Profile 隔离 | 旧→新替代关系、archived 状态、归档时间、来源查询范围没有原生迁移 schema；不能盲目复制旧进程 PID、lease、未完成任务或监督 cursor 来宣称恢复 | [前序插件研究](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/blob/900dd9fc8b1901caa551459777e79bab42d8ddde/docs/research/hermes-plugin-interfaces.md)、[PluginState][P-plugin-state] |

`MEMORY.md` 和 `USER.md` 只承担精选记忆；原生 session archive、messages compaction、外部 memory provider、Wiki 知识库是不同存储层。保留身份记忆不能自动证明保留了全部旧聊天或外部知识。[Memory][H-memory]、[Sessions][H-sessions]

### 创建、clone、export/import 与 backup 的区别

| 入口 | 本机实际行为 | 对本任务的事实含义 |
| --- | --- | --- |
| 空白 create | 新 home；不复制旧会话、旧 credentials 或 provider-specific state | 有新身份容器，但没有选择性记忆迁移和旧→新引用关系 |
| `--clone` / `--clone-from` | config、`.env`、SOUL、精选 MEMORY/USER、skills、plugins、active 外部 provider 配置；默认剥离通道 | 提供现成复制机制，范围比用户要求更广；需要选择性迁移而非直接依赖其默认范围 |
| `--clone-all` | 大范围 copytree；排除 `state.db` 及 WAL/SHM、sessions、backups、snapshots、checkpoints、cron；去除 runtime markers、setup role 和 single-use OAuth 复制件 | 名称中的 all 不等于复制历史；不能当作完整档案/数据库备份。复制已存在的 OAuth refresh grants 会导致多个 owner，原生已专门剥离这类数据 |
| `profile export` | 新 tar.gz；default 通过根 allow-list，排除 state.db 等 runtime；named 更广地复制目录；固定版本排除 auth.json、`.env`、bot-desktop，筛选文本执行 secret redact | default 和 named 导出范围不同；default 导出不包含 canonical 聊天 DB。named 导出可包含 state.db，但这一路源码是 copytree，不是 SQLite backup snapshot |
| `profile import` | 要求一个顶层目录和未占用的新名字；不能覆盖已有 Profile，也不能导入为 default；去除 PM runtime roots 与 setup role后发布目录 | 不会按本任务选择长期记忆与执行配置；不能视作迁移协议，也不能自动重建飞书机器人/业务授权 |
| `hermes backup` / snapshot | 备份实现对 `.db` 使用 SQLite `backup()` 的 WAL-safe snapshot；有独立恢复处理和 runtime排除 | 和 Profile portable export不是同一路。恢复验收需要记录实际备份 scope、完整性及版本；本研究没有执行备份或恢复 |

各行依据：[创建接口][P-create]、[clone 基本集合][P-clone-sets]、[clone-all 排除集合][P-clone-exclusions]、[clone-all OAuth/runtime清理][P-clone-all]、[export/import 实现][P-export-import]、[export redaction][P-export-scrub]、[WAL-safe backup][P-backup-sqlite]、[backup zip 选择][P-backup-zip]。

**版本限制**：2026-10-06 在线 Profile Distributions文档列出了更广的 export credential-store 排除集合，例如 `home/`、provider JSON、token stores、配对记录等；本机该 SHA 的 `_EXPORT_CREDENTIAL_FILES` 只有 `auth.json`、`.env`、`bot-desktop`。不能把在线新排除保证直接套在已安装版本。文档也有“content is not scanned”字样，而本机实现明确对筛选文本 force-redact；个人记忆/聊天语义并不因此被删除。执行配置重建不应依赖 portable export来筛选所需身份数据。[在线 export 范围][H-distributions]、[本机 exclusions/export][P-export-import]、[本机 redact][P-export-scrub]

## 旧历史可以怎样查询

| 查询入口 | 能力与必要来源信息 | 边界 |
| --- | --- | --- |
| native tool discovery | `session_search(profile="old", query="关键词")`，目标 Profile `state.db` read-only；FTS/CJK等搜索返回真实历史片段；压缩归档 message可被搜索 | 旧 Profile要仍被识别且 DB在原路径；不能只传裸 session_id期待自动跨 Profile扫描。查询不启动旧 agent |
| native read / scroll | `profile=old, session_id=…`，或 `@session:old/id`；长会话read返回head/tail，scroll按message anchor继续 | 单次read不是全历史：本机read用get_messages默认 `include_compacted=False`、`include_ancestors=False`；需要 discovery/scroll或完整展示接口覆盖旧压缩内容 |
| REST 查找/阅读 | `/api/sessions/search?q=…&profile=old`；`/api/sessions/{id}/messages?profile=old&include_compacted=true`，支持分页，内部包含compression ancestors | 普通列表默认排除session archived；完整展示要分页并显式包含compacted内容，不应只展示默认最新500条然后宣称读完 |
| 跨 Profile管理列表 | `/api/profiles/sessions?profile=all&archived=include`；枚举包含parked/standalone Profile，标注owner Profile | 列表是元数据，不等于原聊天原文；只读聚合不唤醒旧bot |
| 文本档案 | sessions export有JSONL、Markdown/QMD、HTML；Markdown/QMD/HTML可含display history，`--lineage logical`合并压缩lineage | machine round-trip JSON/JSONL默认只live上下文，不能当作包含全部压缩旧turn的完整可读档案 |

一手来源：[跨 Profile resolver/参数][P-search-resolve]、[tool dispatch][P-search-dispatch]、[tool schema][P-search-schema]、[read/head-tail][P-search-read]、[get_messages默认][P-message-read]、[FTS含compacted行][P-state-search]、[REST search][P-rest-search]、[REST messages][P-rest-messages]、[Profile聚合][P-profile-sessions]、[sessions export][P-sessions-export]、[Sessions文档][H-sessions]。

### 查询授权不是由 native profile 参数自动提供

`session_search` 的跨 Profile resolver只做 name规范化、name验证、Profile存在检查，然后read-only打开目标DB；没有总管/项目/子负责人角色校验、项目归属白名单、迁移来源白名单或`allowed_profiles`业务配置。`role_filter`是message roles（user/assistant/tool），不是组织责任角色；`source`过滤是检索范围，不是业务授权。[resolver][P-search-resolve]、[dispatch][P-search-dispatch]、[schema][P-search-schema]

原生Profile存储隔离和明确profile参数能防止裸ID误落入其他store，但不代替本插件的权限规则。业务授权需在插件交接/历史查询入口确认，并防止模型绕过约束直接调用开放的native跨Profile查询；本研究不替用户决定哪些角色可以查哪些资料。Dashboard正常auth同样不能自动证明具备项目级资料来源授权。[native scoped read][P-search-read]、[前序管理面研究](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/blob/900dd9fc8b1901caa551459777e79bab42d8ddde/docs/research/hermes-plugin-interfaces.md)

### 保存的 Hermes会话与飞书完整群历史不同

Hermes canonical `state.db`保存它实际处理/记录的会话；`sessions/`中的旧JSONL或save文件不是当前搜索索引。默认飞书 mention gate及按sender分会话意味着不能由这些DB推出“该群所有消息都已归档”。旧 Profile本地档案、飞书远端聊天和外部Codex历史必须注明来源。[Sessions][H-sessions]、[Hermes飞书入口][H-feishu]

飞书有独立 `GET /im/v1/messages`历史接口：bot需在目标群；应用身份读取群消息还需对应读取权限及 `im:message.group_msg`；普通群 `container_id_type=chat`只返回topic根消息，topic内回复需按`thread`容器另查。成员可见历史/话题设置会限制新bot，包括加入前话题不可见的 `230073`错误。创建新bot不等于自动获得所有旧群历史。本机跨Profile `session_search`查的是保留DB，不依赖旧bot还在线或新bot已取得这些远端权限。[飞书历史API][F-history-data]、[本地resolver][P-search-resolve]

## 归档、停用、删除和恢复的边界

| 操作 | 已证实行为 | 对保留和接单的影响 |
| --- | --- | --- |
| named Profile `gateway stop`（multiplex） | 先写 `gateway.parked`，请求host unserve；marker跨host restart保留，Profile数据和cron定义不删除 | 停该Profile bot/cron，其他Profiles继续；若immediate stop未确认，CLI明确报告并由下次rescan处理，不能只看请求发出就认为已经停妥 |
| named Profile `gateway start` | 删除parked marker，请host服务；restart对已parked且无独立gateway的Profile也可能转成start | 是原生可恢复方式；归档业务标记是否允许恢复需由插件已定生命周期约束，不能让周期rescan意外恢复旧入口 |
| default `gateway stop` | 操作host gateway而非单个named卫星；default的parked marker被忽略 | 会影响该host服务的其他Profile；不能为单mono项目封存停default multiplex host |
| standalone Profile stop | 生命周期仍指向自己的进程/service，不写multiplex parked marker | 不能把named/multiplex park契约套在standalone部署；需验收实际topology |
| sessions archive | 软隐藏session及compression lineage，messages保留；可由Desktop/Dashboard反归档 | 搜索仍可找到，但不关闭Profile、不停止机器人或Codex，也不是永久保留承诺 |
| Profile delete | 停service/backend、设置tombstone、清理identity、删除Profile数据 | 不适合用户已决定的旧Profile保留归档；无法将“目录删掉”算作还可native查询 |
| mono项目封存 | 原生Profile API不包含业务项目树/mono负责人关系 | 不自动封存子Profile；也不应自动停总负责人所有服务。业务mono状态与Profile运行状态需分别记录，不级联决定已经由用户确认 |

一手来源：[park/unpark源码][P-gateway-lifecycle]、[Profile枚举][P-profile-serving]、[unserve teardown][P-unserve]、[multiplex官方文档][H-multiplex]、[session soft archive][P-session-archive]、[Profile delete][P-delete]、[原生命令定义][P-cli]。

Profile停用清理Hermes adapter连接、缓存agent、scoped MCP/DB/log handles；没有外部Codex session/turn句柄中断契约。除非Codex适配器另外证实停止/取消，park不能作为Codex已停止的证据；是否存在未完成外部任务和如何处置要在相应生命周期工单处理。[unserve实现][P-unserve]、[前序监督边界](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/blob/900dd9fc8b1901caa551459777e79bab42d8ddde/docs/research/hermes-plugin-interfaces.md)

### 旧飞书机器人仍在群中的事实限制

Feishu `disconnect()`关闭WebSocket/Webhook与reconnect、清理pending batches并释放app lock；没有在该路径调用远端删除应用或移出群。因此停车是停止本机接单，不是对飞书群成员和历史消息做删除。旧名称/成员仍可能可见，用户仍可@旧入口；没有运行的callback就不能承诺旧bot会自动回复迁移说明。要保持“不接新任务但可告知迁移去向”，需要独立入口行为，不能在事实研究中默认启动旧assistant。[Feishu disconnect][P-feishu-disconnect]

归档旧bot的飞书平台显示、菜单、旧card callback、应用范围和群成员状态与Hermes parked是不同层；本调查没有访问实际飞书后台或变更这些设置，未验证一个通用“飞书bot archive”API。恢复旧Profile会重新使用其保留的bot配置，未确认业务归档约束前不能据native start把恢复当普通运行。[disconnect][P-feishu-disconnect]、[park/start][P-gateway-lifecycle]

## 保留策略：soft archive不保证永久可查

本机默认 `sessions.auto_prune=true`、`retention_days=90`；对已结束、未pinned且超过保留窗口的session做删除。自动maintenance调用`prune_sessions()`没有传`archived=False`，底层默认`archived=None`意味着两类都可匹配；**已soft-archived、已ended的旧历史仍可能被自动prune删除**。这与手工CLI prune默认跳过archived是不同调用路径，不能用CLI帮助文字替自动维护作保证。[默认配置][P-retention-defaults]、[自动调用][P-auto-prune]、[tri-state筛选及pinned排除][P-prune-filter]、[手工CLI过滤][P-manual-prune]

原生pin是durable keep，默认prune/auto-archive避开pinned；停止named gateway减少其startup/ticker活动，但不禁止Desktop/CLI/显式清理操作，也不把Profile目录变成immutable档案。只读历史query本身不启动旧agent，不等于所有其他后台都不会改旧store。[pin/归档源码][P-session-archive]、[prune-filter][P-prune-filter]、[停用范围][H-multiplex]

因此，“旧聊天可查询”的实现验收要同时覆盖档案存储存在、完整性、retention/pin或独立备份保证、可恢复路径、来源授权和压缩历史读取。报告不自行修改retention、不替用户选择保存年限或自动删除行为；但归档实现不能忽略上述默认删除机制。

## 恢复证据与后续验收

| 验收点 | 必须能核对的事实 |
| --- | --- |
| 身份连续性 | 源/目标canonical Profile名称、各自home、SOUL/精选记忆迁移清单和校验值；新session确实加载目标记忆，执行配置未以整包旧clone冒充重建 |
| 新旧入口分离 | old/new Feishu app/bot identity、目标群；旧Profile停用被确认，新bot确实是新身份，旧token和配对记录未使新bot冒充旧身份 |
| 保留与查询 | 旧DB/档案可读，session owners与origin routes保留；已压缩/archived内容能分页查询，查询授权符合前置决定，查询不唤醒旧agent |
| 数据恢复 | 固定Hermes版本、备份scope、文件校验与SQLite完整性；不要把named live copytree tar当作已验收的WAL-safe数据库snapshot；实际restore需要另行授权/验证 |
| 外部连接 | Wiki连接的端点/目录/库对象不变，重建后授权有效；助手memory namespace是否独立按provider实际能力核对；本研究没有读取真实连接信息 |
| 封存不级联 | mono业务封存只改变指定项目状态；子Profile保留；不为单项目停止defaulthost，Profile park与外部Codex停止分别有证据 |
| 归档恢复 | parked跨host重启有效；native start/restart确实会恢复运行，插件归档状态不得被普通rescan或历史query绕过；旧bot仍在群时实际行为符合规格 |

本工单的事实调查完成。尚未创建或迁移任何Profile/机器人；没有执行恢复试验。待做的是产品生命周期/查询授权的既定规格落地及以上运行验收，而不是再把此Research当作HITL决定。

[H-commit]: https://github.com/NousResearch/hermes-agent/commit/bd0affe5e5f723579df8902852f5d0c47795f355
[H-profiles]: https://hermes-agent.nousresearch.com/docs/user-guide/profiles
[H-memory]: https://hermes-agent.nousresearch.com/docs/user-guide/features/memory
[H-sessions]: https://hermes-agent.nousresearch.com/docs/user-guide/sessions
[H-distributions]: https://hermes-agent.nousresearch.com/docs/user-guide/profile-distributions
[H-multiplex]: https://hermes-agent.nousresearch.com/docs/user-guide/multi-profile-gateways
[H-feishu]: https://hermes-agent.nousresearch.com/docs/user-guide/messaging/feishu
[F-history]: https://open.feishu.cn/document/server-docs/im-v1/message/list
[F-history-data]: https://open.feishu.cn/document_portal/v1/document/get_detail?fullPath=%2Fserver-docs%2Fim-v1%2Fmessage%2Flist
[P-create]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/profiles.py#L1335
[P-cli]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/subcommands/profile.py#L18
[P-clone-sets]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/profiles.py#L35
[P-clone-exclusions]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/profiles.py#L77
[P-clone-all]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/profiles.py#L1237
[P-bootstrap]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/profiles.py#L1313
[P-clone-publish]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/profiles.py#L1387
[P-layout]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/profiles.py#L1408
[P-memory-provider]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/profile_memory_config.py#L39
[P-soul]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/agent/prompt_builder.py#L1627
[P-memory-load]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/tools/memory_tool_store.py#L133
[P-export-sets]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/profiles.py#L160
[P-export-import]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/profiles.py#L2178
[P-export-scrub]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/profiles.py#L2200
[P-backup-sqlite]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/backup_sqlite.py#L31
[P-backup-zip]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/backup.py#L488
[P-plugin-state]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/plugins_state.py#L134
[P-search-resolve]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/tools/session_search_tool.py#L434
[P-search-read]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/tools/session_search_tool.py#L447
[P-search-dispatch]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/tools/session_search_tool.py#L577
[P-search-schema]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/tools/session_search_tool.py#L769
[P-message-read]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_state_messages.py#L1480
[P-state-search]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_state_search.py#L1062
[P-rest-search]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/web_routers/sessions.py#L284
[P-rest-messages]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/web_routers/sessions.py#L685
[P-profile-sessions]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/web_routers/profiles.py#L214
[P-sessions-export]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/sessions_cmd.py#L322
[P-gateway-lifecycle]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/gateway_profile_lifecycle.py#L30
[P-profile-serving]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/profiles.py#L1095
[P-unserve]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/gateway/run_profile_reconcile.py#L214
[P-session-archive]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_state_sessions.py#L911
[P-delete]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/profiles.py#L1779
[P-feishu-disconnect]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/plugins/platforms/feishu/adapter.py#L1545
[P-retention-defaults]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/config_defaults.py#L2259
[P-auto-prune]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_state_maintenance.py#L449
[P-prune-filter]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_state_maintenance.py#L199
[P-manual-prune]: https://github.com/NousResearch/hermes-agent/blob/bd0affe5e5f723579df8902852f5d0c47795f355/hermes_cli/sessions_cmd.py#L691
