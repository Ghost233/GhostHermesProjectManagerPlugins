# Issue 跟踪器：GitHub

规格、决策地图和工单保存在本机配置指定的 GitHub Issue 跟踪器，使用 gh CLI。真实仓库与账号保留在仓库外受限配置；本文件仅保存操作约定。文档中的 Issue 链接相对于当前跟踪器仓库，保留原工单及评论定位。

## 身份核验

从受限本机配置读取授权账号和跟踪器仓库，分别放入任务变量 `pm_github_account` 与 `pm_tracker_repository`。每次需要认证的 gh 业务操作前均重新切换并核对实际登录；环境 token 覆盖登录时也必须符合配置身份。配置缺失或核验不符时停止后续操作。

```bash
: "${pm_github_account:?缺少本机授权账号}" "${pm_tracker_repository:?缺少本机跟踪器仓库}"
gh auth switch --hostname github.com --user "$pm_github_account" >/dev/null 2>&1 || exit 1
pm_actual_login=$(gh api --hostname github.com user --jq .login 2>/dev/null) || exit 1
[ "$pm_actual_login" = "$pm_github_account" ] || exit 1
```

上述核验须紧接每次业务命令之前执行。以下命令使用这些已从本机配置读取并核验的变量，正文与证据只包含脱敏状态、合成示例或本机引用。业务返回值在内存或仓库外受限状态中核对，普通日志只报告脱敏状态，不打印真实仓库、账号或返回正文。

## 基本操作

- 创建：`gh issue create --repo "$pm_tracker_repository" --title '<名称>' --body-file '<正文文件>'`。
- 读取：`gh issue view <number> --repo "$pm_tracker_repository" --comments`。
- 列出：`gh issue list --repo "$pm_tracker_repository" --state open --json number,title,body,labels,assignees,url`。
- 评论：`gh issue comment <number> --repo "$pm_tracker_repository" --body-file '<评论文件>'`。
- 标签和认领：`gh issue edit <number> --repo "$pm_tracker_repository" --add-label '<标签>' --add-assignee "$pm_github_account"`。
- 解决：先发布解决评论，再关闭 issue；证据与资产只链接，不粘贴大段原始输出。

多行正文和评论保存为 UTF-8 文件，使用 --body-file；不将正文直接插入 shell 命令。

## 把 Pull Request 作为分类入口

把 PR 作为请求入口：no。

## 寻路操作

- 地图是带 wayfinder:map 标签的单个 issue；保留 Destination、Notes、Decisions so far、Not yet specified、Out of scope。
- 子工单使用 GitHub 原生 sub-issues。若服务确认不支持，再使用带名称链接的正文约定。
- 阻塞优先使用原生 issue dependencies。添加 blocked_by 关系时使用阻塞项的数字 database id，不使用 issue number 或 node_id。
- 工单类型：wayfinder:research、wayfinder:prototype、wayfinder:grilling、wayfinder:task；工单声明 AFK 或 HITL。
- frontier 是开放、无开放阻塞项且未分配的地图子工单，按子工单顺序领取。开始工作前分配给本机配置的授权账号，分配就是占用声明。
- 人类可读内容始终使用带链接的工单名称，不使用裸编号。
- 决定写在解决评论中，地图仅追加一行摘要和名称链接。开放工单从原生子工单查询，不在地图正文重复维护列表。
- Research 结果保存为仓库中的 Markdown，标注一手来源，并由研究工单留下文件与研究分支指针。
