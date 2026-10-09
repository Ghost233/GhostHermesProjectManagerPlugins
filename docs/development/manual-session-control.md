# 本次 DSH 手动工作接管与归还（[接管并归还本次手动工作](../../../../issues/20)）

本人对当前原 DSH 会话、工作范围和指定负责人的明确授权才产生接管，权限默认[只观察](manual-dsh-observation.md)。Desktop 接入非默认，没有获准独立原生连接时不可观察／接管；由 Hermes 管理的专用实例、只读历史、克隆或另一个 SDK 运行时不能取得原 Desktop 控制。

## 工作授权与原实例

`take_over_session` 绑定已核实 manual Session、冻结 Issue／仓库／负责人、稳定 grant ID 和预期 turn。只有本人授予；同 ID 不改绑。原 workspace 必须符合冻结范围，不另建 Session 或自动发送新目标。

持久 control grant 保存真实授权者、任务与范围摘要、指定负责人、原服务／实例、观察和控制连接代次、endpoint ref、原 Session／current turn 及时间。同一原 Session 只有一个 active／pending／suspended 控制负责人。DSH Session writer lock 不提供这项本人授权，也不能证明其他 Desktop 客户端不会插入输入。

配置与运行证据独立核验。控制接口需覆盖原 Session 读取、追加、停止、明确继续、问题／审批以及全部物理写入、工具与相关执行边界；只读证据或合成报告不能开启接管。当前 turn／权限／workspace／policy 与原记录不符时暂停授权，保留只观察和占用。

## 复用原任务控制

有效当前 grant 才能沿原 DSH Session 的正式方法追加、停止、明确继续及答复原请求。预期 turn、稳定操作 ID、持久意图、跨客户端排他输入和未知不重放沿[任务控制](dsh-task-control.md)执行。缺少真实排他保障关闭受影响输入，不以管理写锁冒充。

问题和审批沿[人工应答](dsh-human-responses.md)。只有本人作新决定／批准，机器人只可答复授权内已定事实。continued question 可 resume／新 turn，因此仍需原工作、实际执行和控制条件；归还或暂停后不能复活旧问题或缓存批准。

原服务／generation／负责身份／目录／turn 冲突暂停控制并说明待本人核对；不承诺自动识别所有桌面操作。新明确接管只承接尚有效本次范围，旧审批不随授权复活。

## 归还与完成

`return_session_control` 由本人或本次指定负责人撤销当前 grant，退为只观察并过期相关待答句柄；不发 cancel、stop，不关共享后端，不回滚文件。原执行继续，后续同仓库任务仍等待完整终止核实。

完成须满足冻结 Issue 验收、测试／固定源码与工作区交接、原根和全部相关子 agent／job／进程覆盖。turn end 不等于交付；缺证据不释放占用。核实交付后 grant completed，后续工作不能复用旧授权。

Dashboard、原生工具和唯一关联任务下的“接管本次工作”“归还本次控制”使用同一公共入口，不能通过 actor／Owner alias 伪造授权。反馈沿原任务引用与真实 @。

## 验收

合成 DSH Remote 与未经修改 Hermes SDK 验证本人授权、唯一负责人、无新 Session、归还无 cancel、原请求一次应答、错误 turn／instance、重复、重启及缺能力拒绝。有获准独立原生连接时，真实原 DSH Desktop Session 的接管／归还、输入竞争、写入和相关执行覆盖须分别证明；缺连接的 Desktop 范围明确不可用，不以专用实例验收代替。历史 Codex 测试不构成当前验收。
