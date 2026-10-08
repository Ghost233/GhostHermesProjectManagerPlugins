## 个人使用范围与资料边界

- 插件仅供本人在自己的 Mac 上使用，不要求适用于所有环境。未经明确要求，不扩展跨平台兼容或通用部署能力。
- 验收以本机完整测试、实际 Hermes／飞书／Codex 闭环，以及合并后本地主工作区同步和最终验证为准。远端 CI 与跨环境复现不是交付或合并硬门禁，不因等待远端 CI 阻塞可开展的本机工作。
- 受管个人项目的真实相关信息不得硬编码或写入插件源码、测试夹具、版本化文档、AGENTS.md、公开 Issue／PR 或普通日志，包括真实项目名称、仓库地址、本地路径、群／机器人／应用／用户标识、凭据、私有资料与聊天内容。
- 运行必需的最少真实绑定仅保存在仓库外受限的本机配置或状态中，通过配置注入。源码、示例与测试使用合成占位数据；日志仅输出脱敏状态或不含真实项目信息的引用。

## Agent skills

### Issue 跟踪器

规格、决策地图和工单保存在本机配置指定的 GitHub Issue 跟踪器。参见 `docs/agents/issue-tracker.md`。

### 分类标签

使用默认分类标签 needs-triage、needs-info、ready-for-agent、ready-for-human、wontfix；Wayfinder 使用独立的 wayfinder:* 标签。参见 `docs/agents/triage-labels.md`。

### 领域文档

使用单上下文：根目录 `GLOSSARY.md` 与按需创建的 `docs/adr/`。参见 `docs/agents/domain.md`。

## GitHub 与 Git

- 所有 GitHub 操作使用受限本机配置指定并授权的账号。每次认证业务操作前执行账号切换，再读取实际 login 并与配置账号比较；不符则停止。具体命令参见 `docs/agents/issue-tracker.md`。
- 远程更新后同步涉及的本地分支，仅允许 fast-forward；结束前核对当前工作区、本地与远端完整提交 hash。
- 不自动 stash、移动、删除、覆盖用户的未提交或未跟踪文件。不使用额外 worktree 或临时集成分支替代当前本地分支。
- 首版规格与决策地图已定稿。实施遵循已批准的 GitHub 子工单、Implement Spec、TDD、双轴审查与 Retro；正式安装、开通机器人和迁移真实资料按具体计划另行执行。
