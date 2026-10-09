# DSH 重启与失联对账

`Manager.reconcile_task`、认证 bridge、原生工具、Dashboard 与 Gateway 派发前对账共用原任务记录。显示最后确认状态、时间、恢复结果及本人待办；监督不可用不等于执行失败或停止。当前契约见[DSH 修订](../specs/dsh-executor-transition.md)。

## 原状态、实例与代次

先只读核对 SQLite 完整性、schema 2／executor_engine dsh、目录、责任、仓库和持久意图；未知版本、旧执行器状态或绑定冲突不打开相关写入／派发，不就地改名迁移收据。旧原始档案与检查点保留，只读资料不取得控制。

重连证明同一原 DSH 实例、启动事实、来源／endpoint、原 Session／turn、冻结范围和不扩大的物理写集。新客户端 generation 与后端实例身份分开保存。ready／控制基线重置旧 transient 值，历史运行状态不证明当前活动；缺基线／游标／coverage 保留未知和占用，不创建替代 Session。

原执行仍在运行只恢复监督。只有原任务未完成、原工作授权有效、全部根／子 agent／job／后台进程已核实停止、原输入结果明确且不存在主动停止／封存／维护意图时，才允许在原 Session 自动继续。使用稳定输入 ID，先持久意图；未知 create／prompt／答复不盲重放。

## 人工请求与控制

当前 DSH 问题／审批按实际 live 调用及明确生命周期核对。旧批准、旧 pending 句柄、历史提问或投影中的 settled 条目不变成新的审批。continued question 虽可在原系统中恢复 root，插件仍须当前工作与授权成立；停止、完成、只观察或归还后不唤醒。

断线、重启或 grant 暂停使旧回送能力待核实。新 live 请求需要本人一次新决定；原 UI 竞争与未知答复先对账，不重发。归还仍保留正在运行的原执行，重连不续签控制或发送取消。

## 持久生命周期意图

对账先消费 `request.stop`、`stop_records`、项目／Profile lifecycle、archive intent 与 maintenance mode，再访问原服务。archiving、archived、disabled、deactivating、maintenance、restoring 和未知状态均抑制自动继续。未完成的停止意图不能因回合／连接变化消失；已核实旧停止对应后来明确新安排时保留历史关系。

生命周期外部动作仍先持久冻结范围，原执行未核实结束保留占用；父先恢复、子逐个恢复不自动续跑旧工作。状态字段缺省不是原执行、当前授权或未完成证明。

恢复证据绑定当前原实例／连接代次、前 generation、原 Session／turn、冻结范围、仓库 policy、当前控制和带时区的短期有效时间；完整写入／工具／执行覆盖及控制／人工应答独立核对。静态内容和过期报告不能启用。

## 管理资源与验收

管理进程崩溃后，仅在独占 lease、runtime receipt 的 inode／device／uid 正确、原 manager PID 不存在、socket 拒绝连接且期间 inode 未变时回收自己的孤立 socket。别人的资源和未知文件保留；管理桥消失不证明 DSH 执行结束。

DSH 公共路径验收覆盖重连、仍执行只观察、自动继续正负条件、主动停止／封存、未知输入和答复、人工请求竞争、实例／目录冲突、SQLite 损坏、手动已归还以及群／Dashboard 一致。测试实际进程与事件流的中断和重启，核对一次启动、原子执行不变、停止核实后才释放及无旧批准重放。

原 Codex 协议恢复测试仅是历史结果。当前 DSH 实例与 pristine Hermes SDK 的真实场景、相关执行终止、日志隐私和物理边界独立验收，不以合成 provider 报告代替。
