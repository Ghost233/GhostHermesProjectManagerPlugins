# GhostHermesProjectManagerPlugins

Hermes 分层协作与 Codex 监督插件。当前阶段为规格规划，尚未实现或安装插件。

- [Hermes 分层协作与 Codex 监督插件决策地图](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/1)：共享的目标、研究与待决问题。
- [首版范围草案](docs/planning/hermes-plugin-scope.md)：已确认的需求、边界与待定的验收场景。
- [领域术语](GLOSSARY.md)：责任角色、Profile 分类与独立助手。
- [Issue tracker 约定](docs/agents/issue-tracker.md)：GitHub 工单、原生依赖与 Wayfinder 操作。

总管、项目总负责人、子项目负责人是责任角色；开发型与非开发型 Profile 是独立的能力分类。总负责人通过 Codex 开发自己的 mono 仓库，并在已指定子负责人交付后进行全局验证；子负责人只完成分配的 GitHub Issue 开发与测试任务。后两层可以合并，Wiki 和个人助手保持独立。默认通过飞书群内真实 @ 公开交接，开发沿用 Codex、Matt Skills 和 GitHub Issue／PR。

总管在入口群与各项目群之间公开转接，任务围绕起始消息引用回复，结果按上级汇总。mono 封存时父子 Profile 一起封存；恢复时先恢复总负责人，子 Profile 逐个恢复，旧执行不自动续跑。

每次明确分派的工作使用独立 Codex 会话，同仓库外层任务串行排队。手动会话默认只观察，明确接管限本次工作，归还控制保留正在运行的执行；停止结束外层任务并保留改动，重启后先对账再决定是否继续。总负责人不自行 checkout 子仓，全局验证期间暂缓相关仓库的新外层任务。
