# Issue 跟踪器：GitHub

仓库：Ghost233/GhostHermesProjectManagerPlugins。规格、决策地图和工单保存在 GitHub Issues，使用 gh CLI。

## 身份核验

每次需要认证的 gh 业务操作前，先切换 Ghost233，再验证实际登录为 Ghost233。GH_TOKEN/GITHUB_TOKEN 覆盖登录时也必须符合这一身份。失败时停止后续 GitHub 操作。

## 基本操作

- 创建：`gh issue create --repo Ghost233/GhostHermesProjectManagerPlugins --title '<名称>' --body-file '<正文文件>'`。
- 读取：`gh issue view <number> --repo Ghost233/GhostHermesProjectManagerPlugins --comments`。
- 列出：`gh issue list --repo Ghost233/GhostHermesProjectManagerPlugins --state open --json number,title,body,labels,assignees,url`。
- 评论：`gh issue comment <number> --repo Ghost233/GhostHermesProjectManagerPlugins --body-file '<评论文件>'`。
- 标签和认领：`gh issue edit <number> --repo Ghost233/GhostHermesProjectManagerPlugins --add-label '<标签>' --add-assignee Ghost233`。
- 解决：先发布解决评论，再关闭 issue；证据与资产只链接，不粘贴大段原始输出。

多行正文和评论保存为 UTF-8 文件，使用 --body-file；不将正文直接插入 shell 命令。

## 把 Pull Request 作为分类入口

把 PR 作为请求入口：no。

## 寻路操作

- 地图是带 wayfinder:map 标签的单个 issue；保留 Destination、Notes、Decisions so far、Not yet specified、Out of scope。
- 子工单使用 GitHub 原生 sub-issues。若服务确认不支持，再使用带名称链接的正文约定。
- 阻塞优先使用原生 issue dependencies。添加 blocked_by 关系时使用阻塞项的数字 database id，不使用 issue number 或 node_id。
- 工单类型：wayfinder:research、wayfinder:prototype、wayfinder:grilling、wayfinder:task；工单声明 AFK 或 HITL。
- frontier 是开放、无开放阻塞项且未分配的地图子工单，按子工单顺序领取。开始工作前分配给 Ghost233，分配就是占用声明。
- 人类可读内容始终使用带链接的工单名称，不使用裸编号。
- 决定写在解决评论中，地图仅追加一行摘要和名称链接。开放工单从原生子工单查询，不在地图正文重复维护列表。
- Research 结果保存为仓库中的 Markdown，标注一手来源，并由研究工单留下文件与研究分支指针。
