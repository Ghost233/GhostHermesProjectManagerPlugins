# mono 与子模块的 Codex 写入和测试边界

核实日期：2026-10-06（Asia/Shanghai）。研究工单：[核实 mono 与子模块的 Codex 写入和测试边界](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/11)。范围依据：[确定责任角色、项目与 Profile 的归属和生命周期](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/4#issuecomment-6015801424)。

## 已确认的业务前提

项目总负责人持有自己的 mono 仓库和 Codex 执行，开发该仓库、协调下属任务、更新子模块 gitlink，并在全部已分配子负责人交付后执行全局测试。子项目负责人开发与测试自己项目中明确分配的 GitHub Issue；发现子项目缺陷时按 Issue 交回修复。子模块存在不等于已分配负责人。

这些是已确认的责任边界；本文核实如何落实文件系统和 Git 边界，不重新决定角色、分派或交付策略。

## 研究结论

1. **父仓 gitlink、子仓源码、子仓 Git 元数据是不同对象。** 父仓更新引用的提交，不等于子工作树已切到该提交；子仓自己的 Git 元数据常位于父 `.git/modules/<name>` 中。[Git 子模块定义](https://git-scm.com/docs/gitsubmodules)
2. **只给父目录 `workspaceWrite` 不足以禁止对子源码写入。** 本机 0.160.1 的旧 SandboxPolicy 只有可写 roots，没有嵌套只读规则；新的 permission profiles 可表达精确 read/write 子树，但属于 beta，需实际验收。[本机旧权限类型](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/SandboxPolicy.ts)、[官方权限配置](https://learn.chatgpt.com/docs/permissions)
3. **子仓 commit 需要它自己的 Git 元数据写权限。** 当前默认 workspace-write 保护 `.git` 及 gitfile 指向的目录；所以“源码可写”不代表无需审批即可 add/commit。[官方保护路径](https://learn.chatgpt.com/docs/agent-approvals-security#protected-paths-in-writable-roots)
4. **全局测试的产物写入与源码写入只能按路径区分。** 权限没有“这是测试缓存所以允许”的语义；测试须在只读源码约束下把产物写到允许路径，或暴露确实需要写源码的项目特例。[本机文件系统条目](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/FileSystemSandboxEntry.ts)
5. **全局测试需要稳定的已交付提交组合。** 权限不会阻止其他子负责人同时修改自己有权写入的源码，Git 锁也不是整个测试期间的源码快照锁；跨父子任务的稳定性需要另外验收和确定协作策略。

上面的“可表达”指文档和类型提供能力，不是本轮已证明 macOS 上某个具体 profile 能满足所有 Git、构建和测试动作。

## 证据等级与本机限制

- **官方文档**：实际打开 Git 官方手册、Pro Git 与 OpenAI 权限、app-server 文档。事实后附原始链接。
- **本机静态证据**：CLI `codex-cli 0.160.1`、`git version 2.54.0 (Apple Git-157)`；使用此前在 `/private/tmp/hermes-codex-supervision-0.160.1/` 生成的默认与 experimental schema。
- **尚未实测**：具体 profile 的执行、Git 元数据例外、嵌套模块、全局测试、并发和审批扩大权限的效果。

本轮未启动 app-server、exec 或开发任务，未对真实 mono 仓库运行 Git 或测试，未读取凭据，未创建 branch、worktree 或额外 checkout。`codex sandbox macos --help` 的帮助探测被宿主沙箱以 `sandbox_apply: Operation not permitted` 拒绝，未得到帮助或任何规则验收结果；不据此断言目标权限不可用，也未重试或扩大权限。

协议快照的生成命令与校验值见 [Codex 会话监督与人工应答接口核实](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/blob/5aa03acf19fa62eeb227567329bbce5e5ecb28e7/docs/research/codex-supervision-interfaces.md)。

本机安装版本不代表当前运行 daemon 的版本。默认生成目录名 `stable` 仅表示没有添加 `--experimental`，不是对其中全部 API 稳定性的保证。

## Git 所有权：不能只看目录位置

典型布局中的对象归属如下；真实路径必须解析，不能把 submodule 路径当作 Git 元数据名称。

| 对象 | 常见物理位置 | 逻辑归属与写入作用 |
| --- | --- | --- |
| 父仓跟踪文件与 `.gitmodules` | mono 工作树 | 父仓源码与子模块映射配置；`.gitmodules` 不是 gitlink |
| 父仓 index/tree 内的 gitlink | 父仓 Git directory 的 index；父 commit 的 tree | 父仓记录子 commit object id，模式 `160000`；不包含子仓逐文件内容 |
| 子仓源码工作树 | mono 中的子模块路径 | 子仓项目文件；读它、checkout 它、编辑它是不同操作 |
| 子仓 `.git` 文件 | 子工作树根目录 | 指向实际 git-dir 的 gitfile，通常不是独立 `.git` 目录 |
| 子仓 objects、index、refs、HEAD、logs | 父 `$GIT_DIR/modules/<name>/` | 子仓自己的版本数据；物理位于父目录仍不等于父仓 index/refs |
| 嵌套子模块元数据 | 已解析 Git directory 下的 modules 子树 | 属于进一步嵌套的仓库，不能随授予上层 git-dir 写权限而默认为同一职责 |

来源：[子模块定义和布局](https://git-scm.com/docs/gitsubmodules)、[gitlink 模式示例](https://git-scm.com/book/en/v2/Git-Tools-Submodules)、[Git repository layout](https://git-scm.com/docs/gitrepository-layout)。旧式子模块可能带内嵌 `.git` 目录；未初始化模块可能没有源码工作树，所以 `/mono/.git/modules/<path>` 不能作为万能拼接公式。

如果允许子负责人提交自己的仓库，其所需权限应描述为“自己的工作树及已解析的 Git 元数据”，不是简单要求所有写入都在子源码目录之内。需要辨认其 git-dir、common-dir 与实际 index/object 路径；已有 linked worktree 或路径重定向会影响这些位置。[官方路径解析接口](https://git-scm.com/docs/git-rev-parse)

下列是后续检查时可使用的只读命令示意，本轮没有对用户 mono 仓库执行：

```sh
git -C /actual/child rev-parse --show-toplevel
git -C /actual/child rev-parse --path-format=absolute --git-dir --git-common-dir
git -C /actual/child rev-parse --path-format=absolute --git-path index --git-path objects
git -C /actual/child rev-parse --show-superproject-working-tree
```

## 更新 gitlink 不等于切换子源码

| 操作 | 父仓写入 | 子仓写入 | 对职责边界的含义 |
| --- | --- | --- | --- |
| 父 `git add <submodule-path>` | 父 index 记录子仓当前 HEAD | 不递归暂存子仓改过的逐文件内容 | 需要确认当前 HEAD 恰为应交付 commit；不能把未提交子源码自动当已交付 |
| 父 index 直接记录 `160000,<commit>,<path>` | 修改父 index 的 gitlink | 不物化子工作树 | Git plumbing 能做到只改引用；但指向某 commit 不等于当前测试读到了该 commit 的代码 |
| 父 commit | 父 objects/refs/index 等 | 常规非递归提交不产生子源码 commit | 可记录自己的源码改动和 gitlink；实际 hook 或递归选项仍要核实 |
| `git submodule update --checkout` | 可能涉及初始化配置 | 获取对象、改变子 HEAD/index 并写子工作树，通常 detached HEAD | 它是子目录文件写入；不能归为“只更新父 gitlink” |
| `git checkout --recurse-submodules` | 更新父工作树与状态 | 更新 active 子模块内容并 detach 子 HEAD | 会跨入子仓；不能默默用作只改父仓的实现 |

来源：[Git status 对父暂存与子内容的区别](https://git-scm.com/docs/git-status)、[update-index --cacheinfo](https://git-scm.com/docs/git-update-index)、[Git submodule update](https://git-scm.com/docs/git-submodule)、[Git checkout 的递归行为](https://git-scm.com/docs/git-checkout)。

因此有一个需要明确的行为边界：若“父负责人不改子源码”也禁止为测试 checkout 子工作树，父负责人只能只读已经由获准角色物化的正确版本。若允许“切到已交付 commit”而仍禁止开发编辑，它也需要窄范围授权和无并发写者，不能由“允许更新 gitlink”自行推导。本研究不替用户选择这两种解释。

父级交付引用的 commit 还应能被需要检出的协作者取得。`git push --recurse-submodules=check` 与 `on-demand` 不同：后者会替子仓推送，不能仅因为父需要引用可获取，就把它视为父职责内的默认动作。[Git 官方子模块发布说明](https://git-scm.com/book/en/v2/Git-Tools-Submodules#_publishing_submodule_changes)

## Codex 可以表达哪些边界

### 旧 SandboxPolicy 与新的 permission profiles

本机旧 `SandboxPolicy.workspaceWrite` 有 `writableRoots`、网络开关和 temp 开关，没有“某个嵌套目录只读”的字段。父 root 可写时，不能从该类型推导其子工作树受只读保护；只把 cwd 设成子目录也不是限制整个执行范围。[本机 SandboxPolicy](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/SandboxPolicy.ts)

新的文件系统规则支持 `read`、`write`、`deny`；官方定义更具体路径覆盖更广规则。同一路径冲突时 deny 优先，其次 write、read。它们可以描述父可写、子只读及独立产物目录可写的组合；应按真实已解析路径表达。[官方规则优先级](https://learn.chatgpt.com/docs/permissions)、[本机条目](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/FileSystemSandboxEntry.ts)、[access 枚举](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/FileSystemAccessMode.ts)

这些规则的概念矩阵如下，**不是已经部署的配置**：

| 路径或对象 | 父负责人开发/集成 | 子负责人自己的项目开发 | 父全局测试阶段 |
| --- | --- | --- | --- |
| 父仓自己的源码 | 按已授权范围可写 | 通常只读 | 是否允许额外编辑仍由任务范围决定 |
| 所有嵌入子模块源码 | 只读，包括没有负责人的模块 | 自己仓库可写；它的嵌套仓库仍按边界处理 | 只读，除非已明确允许版本物化操作 |
| 父仓自身 Git 元数据 | Git 操作需要的明确权限 | 不应整块授予 | 正常测试通常不需写，但 Git 检查可能刷新 index |
| 某子仓已解析 Git 元数据 | 不能因位于父 `.git` 就全可写 | 该子仓 add/commit 所需路径 | 通常只读 |
| sibling/嵌套仓库元数据 | 只读或更受限 | 不随自己 git-dir 自动扩大 | 只读 |
| 测试缓存、日志、覆盖率、build 输出 | 单独授权的路径 | 自己测试所需路径 | 单独授权的路径 |

**Git 元数据是特别重要的例外。** 默认 workspace-write 对 `.git` 与 gitfile 指向路径只读。profile 如果对父 `.git` 开放写入，还需对其中属于子仓的 modules 子树维持独立规则；子仓若需要提交，不能通过开放整个父 root 或整个父 `.git` 来获得它的 Git directory。明确覆盖自动保护、目录锁文件和原子替换的组合必须验收。[官方保护路径](https://learn.chatgpt.com/docs/agent-approvals-security#protected-paths-in-writable-roots)

### app-server 的本机接口表面

| 本机类型 | 能证明的能力 | 限制 |
| --- | --- | --- |
| experimental `ThreadStartParams` | `permissions` 选择 named profile；`runtimeWorkspaceRoots` 替换运行时根目录 | 与旧 `sandbox` 互斥；字段出现在 experimental 快照，不代表当前 daemon 已接受。[启动参数](/private/tmp/hermes-codex-supervision-0.160.1/experimental/v2/ThreadStartParams.ts) |
| experimental `ThreadResumeParams`、`TurnStartParams` | 续接或后续回合可选择 profile、覆盖 roots | 后续回合可变更边界；只检查创建时参数不够。[续接参数](/private/tmp/hermes-codex-supervision-0.160.1/experimental/v2/ThreadResumeParams.ts)、[回合参数](/private/tmp/hermes-codex-supervision-0.160.1/experimental/v2/TurnStartParams.ts) |
| experimental `CommandExecParams` | 独立命令可用 `permissionProfile`，或旧 sandboxPolicy | 两者互斥；遗漏时用 server 配置，不是自动继承某个目标 thread 的已确认 profile。[命令参数](/private/tmp/hermes-codex-supervision-0.160.1/experimental/v2/CommandExecParams.ts) |
| `AdditionalFileSystemPermissions` | 请求额外权限可有具体 entries | 这是权限请求/授予结构，不能当作基础策略已被收窄的证明。[额外权限](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/AdditionalFileSystemPermissions.ts) |
| `thread/shellCommand` | 本机注释明确在 sandbox 外以完整访问执行 | 用它运行全局测试不能宣称依然受 thread 子源码只读策略约束。[shell 参数](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/ThreadShellCommandParams.ts) |
| `FsWriteFileParams` | host 文件写 API，传绝对路径 | 类型没有 thread/profile 字段，单凭 schema 不能建立其继承某个 thread 限制的保证。[文件参数](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/FsWriteFileParams.ts) |

官方把 named permission profiles 标为 beta，且它们与旧 `sandbox_mode`/`sandbox_workspace_write` 不叠加；旧设置可能使所选 profile 不生效。macOS 不能兑现的 sandbox 策略应拒绝运行，不能因此假定可退成无限制执行。后续要验收实际生效的权限，而不仅检查拟发送参数。[官方权限配置与执行边界](https://learn.chatgpt.com/docs/permissions)

上述文件系统规则主要约束 sandboxed command execution，不自动成为外部 MCP、host API 或整个管理插件的访问控制。执行路径、审批及重新配置都需纳入验收。[官方范围说明](https://learn.chatgpt.com/docs/permissions#scope-and-enforcement)

## 全局测试：产物、源码和版本组合

OS 权限按照路径允许写入，不能根据文件是否 tracked、ignored、由测试产生来判断它是不是源码。比如构建写 `child/dist`、覆盖率写 `child/coverage`、测试写 snapshot/fixture 或安装脚本改文件，都会触发子目录写入；`.gitignore` 只控制 Git 如何看待未跟踪文件，不是写入授权。[Git ignore 语义](https://git-scm.com/docs/gitignore)、[本机权限条目](/private/tmp/hermes-codex-supervision-0.160.1/stable/v2/FileSystemSandboxEntry.ts)

可以验证“子源码只读、产物目录可写”的路径组合，但不能预先承诺每个项目的测试框架都支持把所有写入移出源码树。若产物必须位于子树，窄路径写例外也会允许修改该目录里的既有文件；需要确认那里没有应保护的用户文件或 tracked 产物，而不能把一个常见目录名直接当授权依据。这是基于路径规则的技术推论。

父全局测试应当识别实际输入版本：父 HEAD/待验收 diff、父 index/目标 tree 的各 gitlink、子工作树 HEAD、子工作树未提交内容，以及测试期间这些输入是否变化。只记父 hash 或只记子“已完成”标志，无法证明测试的是交付组合；运行中的子任务仍可写出混合状态。这是 Git 将 parent tree 与 child worktree 分开保存的直接推论。[Git 子模块模型](https://git-scm.com/docs/gitsubmodules)

全局测试失败还应区分业务断言失败、权限拒绝、缺对象/未初始化子模块、版本不匹配和并发输入改变。权限拒绝不证明子项目代码有缺陷；没有测试真正开始的证据，不应进入“测试已失败所以修代码”的闭环。子项目真实缺陷如何通过 Issue 交回，沿用已确认的职责决定。

## 未提交、未跟踪与并发保护

Git 默认 checkout 会拒绝覆盖某些本地改动，但 force 能丢弃改动和挡路的未跟踪文件；忽略文件还可能在切分支时默认被覆盖。因此“Git 通常会挡住”不能代替本仓库禁止自动 stash/移动/删除/覆盖用户文件的要求。[Git checkout](https://git-scm.com/docs/git-checkout)、[Git submodule 的 force 行为](https://git-scm.com/docs/git-submodule)

检查子状态时需要直接检查各子仓，而不只看父状态的一行摘要。父状态可以因 `ignoreSubmodules` 配置隐藏子脏状态；ignored 文件也需要显式查看。后台监督的 `git status` 默认可能刷新 index 并持锁，应使用 `git --no-optional-locks status` 避免这类可选写入。[Git status 说明](https://git-scm.com/docs/git-status)、[Git 全局锁选项](https://git-scm.com/docs/git#Documentation/git.txt---no-optional-locks)

父和子自己的 index、refs 分属不同仓库，但以下动作仍会发生跨界影响：

- 父暂存 gitlink 时，子负责人可能刚改变 HEAD，导致记录的提交不是前面确认的交付提交。
- 父 `submodule update`/递归 checkout 会更改子负责人正在用的 HEAD、index 和工作树。
- 父测试读取子源码时，子负责人继续编辑、安装依赖或生成 fixture，可能使测试读到混合版本。
- 父对整个 `.git` 授写权限会覆盖子 metadata；子对整个父 root 授权会覆盖父源码。
- 父子测试共用缓存、端口、数据库或外部资源时，文件只读规则不能解决这些资源的竞争。

这些是由布局和权限能力推导的竞争面。schema 没有父子项目提交事务、整个测试时段快照锁或业务 Issue 占用契约；是否串行、怎样冻结已交付版本、如何处理并发新任务，留给 HITL 协作行为决定。

## 下一步验收范围

1. 在明确允许的验收环境验证：父可编辑自身源码，但编辑所有嵌入子源码失败；子可编辑自己项目，但不能编辑父/sibling/嵌套项目源码。
2. 验证父 gitlink-only 更新和父 add/commit 的 metadata 权限；验证子 add/commit 只写自己的 resolved git-dir/common-dir，没有开放整个父 Git directory。
3. 验证旧模式、新 profile、继承、额外 workspace roots、后续回合覆盖及审批扩大权限时的实际边界。
4. 验证全局测试所需产物路径、依赖安装、测试 fixtures 和 hooks；确认权限拒绝能区别于业务缺陷。
5. 验证 gitlink 与实际 checkout 的一致性，以及脏/未跟踪/ignored 用户文件不会被自动处理。
6. 验证父测试期间的子任务/HEAD/gitlink 变动、父子同时 Git 操作和共享测试资源；确认失效结果不会被当已通过验收。
7. 确认“父可更新 gitlink但不改子源码”是否允许版本物化 checkout；不允许时确定由哪个获准动作提供父测试可读取的交付版本。

本报告没有完成上述实验，没有安装或启用新权限配置，也没有替用户选择 checkout、产物目录、并发或失败恢复策略。
