## Agent skills

### Issue 跟踪器

规格、决策地图和工单保存在 Ghost233/GhostHermesProjectManagerPlugins 的 GitHub Issues。参见 `docs/agents/issue-tracker.md`。

### 分类标签

使用默认分类标签 needs-triage、needs-info、ready-for-agent、ready-for-human、wontfix；Wayfinder 使用独立的 wayfinder:* 标签。参见 `docs/agents/triage-labels.md`。

### 领域文档

使用单上下文：根目录 `GLOSSARY.md` 与按需创建的 `docs/adr/`。参见 `docs/agents/domain.md`。

## GitHub 与 Git

- 所有 GitHub 操作使用 Ghost233。每次需要认证的 gh 业务操作前执行 `gh auth switch --hostname github.com --user Ghost233`，再用 `gh api --hostname github.com user --jq .login` 核验；核验失败时停止 GitHub 操作。
- 远程更新后同步涉及的本地分支，仅允许 fast-forward；结束前核对当前工作区、本地与远端完整提交 hash。
- 不自动 stash、移动、删除、覆盖用户的未提交或未跟踪文件。不使用额外 worktree 或临时集成分支替代当前本地分支。
- Wayfinder 默认只规划。本轮只形成插件规格与决策地图，不安装插件或修改正在运行的 Hermes。
