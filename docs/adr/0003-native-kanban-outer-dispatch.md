# 原生 Kanban 负责外层派发

2026-10-10，用户确认复用 Hermes 原生 Kanban，作为唯一的外层派发入口。Kanban 派发仓库级工作，负责人监督 DSH；主插件补充仓库互斥、DSH 接入、提问回送和验收，不保留第二套自动派发队列。停用内置自动拆分，由 DSH 主持 Matt 的详细拆分。

这个选择复用原生持久队列与 Profile 派发，减少两套队列之间的同步。Kanban 可无 WebUI 运行；其 worker 启停不作为 DSH 相关执行已结束的证明。任务与 Issue 的关系见[对应方式](0005-umbrella-issue-and-outer-card.md)，具体验收仍待确定。
