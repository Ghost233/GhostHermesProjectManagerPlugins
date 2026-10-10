# 原 DSH 任务控制（[在原会话追加、停止和明确继续任务](../../../../issues/17)）

当前执行端仍为 DSH，默认优先由 Hermes 管理专用执行实例；Desktop 手动控制非默认，须有获准独立原生连接。配套认证组件已撤回，切换前的协议和控制收据不再用于启用 DSH；完整契约见[DSH 执行目标修订](../specs/dsh-executor-transition.md)。

## 一个控制入口

`Manager.control_task(identity, request_id, action, instruction_id, text=None, expected_turn_id=None)`、认证 ManagementClient、Dashboard 和 `hermes_pm_task` 共用授权。输入不接受 actor、角色、verifier 或 enabled。本人或仍持有冻结责任的指定负责人操作本次工作，目录可见或上级身份不自动取得控制。

稳定 instruction ID 与原任务、动作、内容、预期 turn 和真实身份永久绑定。相同调用返回原记录；不同内容拒绝。未知已发送结果保留原 ID 并只读对账，不能换 ID 重发。飞书“追加：要求”“停止”“继续：要求”“核对执行”先唯一关联原任务，有歧义先澄清。

目录、DSH 引用、服务代次、workspace 或当前 turn 冲突时暂停相关控制并保留占用。旧授权不能转到新仓库／执行器；交付释放后原控制过期。原仓库写入范围与当前控制证据逐项一致，不能用新 policy 扩大旧范围。

## 追加与输入竞态

活动追加须核对原 Session、唯一当前 turn、预期 turn 和有效工作授权，再沿 DSH 原生 prompt／steering 路径提交。受理只表示输入被接受；执行结果来自原事件和后续核对。不能假定原 DSH 接口提供旧协议的 expected-turn 原子保证。

空闲输入和明确继续须核对完整终态、相关执行已结束、原 workspace、上一 turn、当前责任和排他输入路径。在管理写锁内重复 read 只序列化插件自身，不能阻止 Desktop 或其他客户端抢先输入。缺跨客户端原子前置条件或经验证的排他路径时，活动／空闲输入中受影响的能力保持关闭。

先持久保存控制意图和稳定 requestId，再发原 DSH 输入；核对对应 durable 来源、原 Session／turn 和实际执行。受理未知不重放。

## 停止与占用

停止先持久记录操作和明确停止意图，再调用原 Session cancel。响应 accepted／成功不直接标 stopped，也不释放仓库。插件不 kill 全局进程、不关闭共享 DSH 后端、不回滚文件。

停止核实覆盖原根 Session 的完整终态、相关子 agent 的终态、job 全范围及其终态、所有注册 shell 后代／进程路径和相关定时执行。空 job 列表不证明 nohup 或未登记路径已结束；缺页、缺方法、未知覆盖或原实例失联都保持 stopping／unverified 与占用。

全部相关执行核实结束才结束外层任务、保留会话／改动／停止证据并释放该任务占用。停止不等于交付，不关闭 Issue 或合并 PR，原任务不自动续跑。下一项按自己的基线和队列执行。

## 明确继续

原工作需已核实停止、当前明确授权、正确原 Session 和当前准备条件，没有竞争占用。登记新的 execution arrangement／队列 sequence，保留上一停止和原任务关联；原 accepted 时间不能使继续插队。新的输入与原 Session 关联并核对实际新 turn，旧停止记录保持历史终态。

当前可信证据独立覆盖 active append、idle input、cancel、stop verification、explicit continue、wrong turn、duplicate／unknown instruction、disconnect、相关子 agent／job／进程、跨客户端 exclusive input。证据绑定原实例、连接代次、仓库与 policy；HTTP、群、工具和任务进程不能上传或改写它。

## 验证与剩余门槛

DSH 合成 Remote 测试与未经修改 Hermes SDK 验证持久意图先于发送、公共入口权限、误 turn、重复／未知结果、原 Session 继续、停止覆盖和文件保留。真实原实例还须验证跨客户端竞争、实际子执行终止、物理写入、模型及群反馈。原协议测试属于历史行为依据，不能标成 DSH 运行通过。
