# GhostHermesProjectManagerPlugins

Hermes 分层协作与 Codex 监督插件。当前阶段为规格规划，尚未实现或安装插件。

- [Hermes 分层协作与 Codex 监督插件决策地图](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/1)：共享的目标、研究与待决问题。
- [首版范围草案](docs/planning/hermes-plugin-scope.md)：已确认的需求、边界与待定的验收场景。
- [领域术语](GLOSSARY.md)：责任角色、Profile 分类与独立助手。
- [Issue tracker 约定](docs/agents/issue-tracker.md)：GitHub 工单、原生依赖与 Wayfinder 操作。

总管、项目总负责人、子项目负责人是责任角色；开发型与非开发型 Profile 是独立的能力分类。总负责人通过 Codex 开发自己的 mono 仓库，并在已指定子负责人交付后进行全局验证；子负责人只完成分配的 GitHub Issue 开发与测试任务。后两层可以合并，Wiki 和个人助手保持独立。默认通过飞书群内真实 @ 公开交接，开发沿用 Codex、Matt Skills 和 GitHub Issue／PR。
