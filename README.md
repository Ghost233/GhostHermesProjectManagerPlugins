# GhostHermesProjectManagerPlugins

Hermes 分层协作与 Codex 监督插件。首版规格与决策地图已定稿，尚未实现或安装插件。

- [首版插件规格](docs/specs/hermes-plugin-v1.md)：已确认的实现契约、管理入口、生命周期与验收矩阵。
- [Hermes 分层协作与 Codex 监督插件决策地图](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/1)：规划目标、研究与决定索引。
- [需求与范围记录](docs/planning/hermes-plugin-scope.md)：形成规格的已确认需求及边界。
- [架构图](docs/diagrams/hermes-plugin-v1-architecture.svg)与[可编辑 Mermaid 源文件](docs/diagrams/hermes-plugin-v1-architecture.mmd)。
- [领域术语](GLOSSARY.md)：责任角色、Profile 分类与独立助手。
- [Issue tracker 约定](docs/agents/issue-tracker.md)：GitHub 工单、原生依赖与 Wayfinder 操作。

总管、项目总负责人、子项目负责人是责任角色；开发型与非开发型 Profile 是独立的能力分类。总负责人通过 Codex 开发自己的 mono 仓库，并在已指定子负责人交付后进行全局验证；子负责人只完成分配的 GitHub Issue 开发与测试任务。后两层可以合并，Wiki 和个人助手保持独立。默认通过飞书群内真实 @ 公开交接，开发沿用 Codex、Matt Skills 和 GitHub Issue／PR。

总管在入口群与各项目群之间公开转接，任务围绕起始消息引用回复，结果按上级汇总。mono 封存时父子 Profile 一起封存；恢复时先恢复总负责人，子 Profile 逐个恢复，旧执行不自动续跑。

每次明确分派的工作使用独立 Codex 会话，同仓库外层任务串行排队。手动会话默认只观察，明确接管限本次工作，归还控制保留正在运行的执行；停止结束外层任务并保留改动，重启后先对账再决定是否继续。总负责人不自行 checkout 子仓，全局验证期间暂缓相关仓库的新外层任务。

监督按 Issue 验收核对交付，执行和 PR 状态分别显示。有活动任务时每 15 分钟汇总，审批和阻塞问题立即通知；疑似停滞先核查、失联先对账。群内答复须关联仍有效的原请求并符合控制权限，审批由本人明确作出，敏感答案在原 Codex 界面处理。

资料通过公开真实 @ 按来源范围共享，Wiki 返回相关材料及来源，开发流程不自动改写原资料库。Codex 接收当前任务所需的允许共享上下文，各角色在验收后保存自己的项目记忆，个人偏好按本人明确范围更新；旧档案只读查询不恢复旧入口。

首版提供飞书聊天管理与 Hermes Web Dashboard，同机运行 Hermes 管理和 Codex 执行端。一个管理实例使用独立 SQLite 保存全局协调状态；登记的旧档案与封存资料永久保护，迁移、升级和恢复均核对检查点。规格列明 16 组离线与真实环境验收场景，实施时须分别证明接口、身份及仓库边界生效。
