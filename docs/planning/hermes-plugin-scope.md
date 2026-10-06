# Hermes 分层协作与 Codex 监督插件：首版范围草案

状态：需求输入已确认；实现规格仍需解决决策工单。本文件不是已实现功能清单。

规范地图为 [Hermes 分层协作与 Codex 监督插件决策地图](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/1)。决定写入各工单解决评论，地图只维护索引；本文件在规格定稿工单中收敛为可交接的实现规格。

## 已确认的需求

### 责任角色与 Profile 分类

- 总管是通常沟通的入口，负责跨项目分配任务和汇总。
- 项目总负责人持有自己的 mono 仓库，通过 Codex 进行该仓库的开发、集成和全局测试，并管理自己下属的 Profile。
- 总负责人可更新 mono 仓库记录的子模块提交引用，不修改子仓源码。子负责人只完成明确分配的 GitHub Issue 中开发与测试任务。
- 子负责人由用户明确指定，不按 submodule 数量自动创建；每份子 Profile 长期绑定一个具体项目，本次任务完成后保留供同项目后续分配。
- 已指定子负责人全部交付后，总负责人再次执行全局验证。已分配子项目的缺陷以明确的 GitHub Issue 交回子负责人修复，再验证；未指定负责人的子模块问题交由用户决定。
- 项目总负责人和子项目负责人可合并，组织关系不要求固定三层；合并后同一 Profile 直接负责具体项目的开发与验证。
- 责任角色与开发型／非开发型 Profile 分开定义，不用层级强制决定能力。
- 开发型 Profile 向 Codex 发送提示词、监督会话并汇报，实际开发由 Codex 执行。
- 非开发型 Profile 可负责调度、资料或个人事务；这一分类本身不额外限制个人助手的通用工具。
- ConsoWiki、Ghost个人助手是独立助手，不放入项目责任层级。
- 总管管理全局组织目录；项目总负责人管理自己的下属，跨总负责人移交由总管处理。同一项目调整上级时保留子 Profile、记忆与历史。

### 创建、迁移、封存与恢复

- 为插件新建专用 Profile 和新飞书机器人，原 Profile 与原机器人停用归档。
- 迁移长期记忆、人设职责与通用偏好；模型与工具按新职责核对，Codex 执行配置由新插件重新配置。旧聊天历史保留为可查询归档，Wiki 保留原资料库连接。
- 新项目另建 Profile，不将原项目 Profile 改绑。恢复封存项目时继续使用原 Profile。
- 封存停止该项目范围的新任务，立即请求中断该范围的执行并保留改动；中断尚未确认时保持封存处理中和监督职责。
- 封存总负责人只影响其 mono 项目，不自动停止或封存下属子 Profile。旧助手归档保留资料与历史，不自动删除或回滚代码。

### 公开协作与资料查询

- 默认入口和交接发生在飞书群，使用真实 @，协作过程对参与者可见。
- 用户可以直接与总管、开发负责人、Wiki 或个人助手沟通。
- 开发负责人可以直接向 Wiki 请求资料，无需自己遍历大量资料库。
- 请求／答复如何关联、怎样避免重复启动与互相 @ 循环、何时逐级汇总，仍需确定。

### Codex 监督与人工应答

- 首版只接入 Codex。
- 既监督插件启动的任务，也允许登记用户手动启动的 Codex 会话。
- 手动会话默认只观察；用户明确接管后，才追加要求、中断或恢复。
- 定期检测会话结果、运行状态、可能的卡死、待回答问题和人工介入需求，并总结回复。
- Codex 需要用户回答时，支持在飞书群提示并把用户回复送回原任务。
- 普通问题与执行审批须分别考虑；可靠状态证据、通知频率、超时语义与回复失效条件仍待决定。
- 汇报需区分执行轮次结束、实现交付、PR 待审查与合并结果。

### 工作流与已有环境

- 开发沿用 Codex 的原生配置、仓库约定、Matt Skills 和 GitHub Issue／PR。
- Hermes 监督执行，不重新建立一套 Matt 工单拆分或内部开发流程。
- 使用现有本地仓库；只有用户明确要求时才 clone。
- 项目资料随其长期绑定的子 Profile 保留，总负责人保管自己的协调资料。各层共享、传入 Codex 和执行结果回写的具体协议仍待决定。
- 通过插件统一管理角色和执行监督；新助手迁移、旧历史查询与旧入口停用的技术支持边界，以及安装升级过程仍需核实。

## 研究与待决问题

| 名称 | 类型 | 要得到的产物 |
| --- | --- | --- |
| [核实 Hermes 插件与飞书公开协作接口](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/2) | AFK Research | 当前正式接口、一手证据、限制与实测缺口 |
| [核实 Codex 会话监督与人工应答接口](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/3) | AFK Research | 会话、事件、问题和审批的可观测与控制边界 |
| [确定责任角色、项目与 Profile 的归属和生命周期](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/4) | HITL Grilling | 角色、项目、Profile、记忆和封存的归属规则 |
| [确定飞书群内公开交接与消息关联协议](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/5) | HITL Grilling | 可见消息样例、关联、去重与交接终止规则 |
| [确定 Codex 会话登记、控制与接管规则](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/6) | HITL Grilling | 观察与控制授权、占用、并发及重启对账规则 |
| [确定监督状态、汇报频率与人工介入闭环](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/7) | HITL Grilling | 状态证据、通知与群回复回送行为 |
| [确定资料查询、上下文传递与记忆回写边界](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/8) | HITL Grilling | Wiki 查询、上下文范围和记忆归属规则 |
| [核实助手迁移、历史查询与旧入口归档接口](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/10) | AFK Research | 新决定所需的正式接口、版本与技术边界 |
| [核实 mono 与子模块的 Codex 写入和测试边界](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/11) | AFK Research | 新决定所需的正式接口、版本与技术边界 |
| [锁定首版插件规格、管理入口与验收边界](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/9) | HITL Grilling | 可交接实现的最终规格与验收场景 |

原生子工单、阻塞与分配状态由 GitHub 维护；本表是问题范围说明，不是状态副本。

## 规格定稿时应覆盖的验收场景

1. 一个项目采用三层负责人且总负责人有自己的 mono 执行，另一个项目合并后两层；只为明确指定的项目建立子负责人，独立助手仍可直接联系。
2. 开发负责人在群内真实 @ Wiki，获得有来源的相关答案并将必要内容送入 Codex。
3. 同一群有两项并行执行，进展、问题和答复能关联到正确任务，重复消息不重复启动开发。
4. 手动会话只观察；明确接管和归还时能核对控制归属。
5. 区分长命令、等待用户、审批、失联和疑似停滞；群内回答能送回仍有效的原请求。
6. 插件或 Gateway 重载／重启后对账已有任务，保留 Profile 记忆与用户工作区内容。
7. 执行结束后按实际证据汇报 PR、测试、审查及本地同步状态。
8. 子 Issue 交付后集成子模块引用并执行全局验证；失败时沿 Issue 交回修复，各负责人源码写入保持在自己的仓库范围。
9. 新 Profile 与新机器人迁移知识和偏好，旧历史保持可查询；封存 mono 总项目时其下属仍能继续活动，恢复保留原绑定。

具体预期输出和通过标准由相关工单确定后补齐，当前没有声称已通过这些场景。

## 当前范围之外

- 本轮编码、安装、上线或更改现有 Hermes／飞书／Codex 运行环境。
- 首版支持其他开发 CLI。
- 重建 Matt 的内部执行与审查流程。
- 未经明确要求替换仓库或接管用户手动会话。
