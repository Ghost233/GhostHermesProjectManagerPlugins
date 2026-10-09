# DSH 手动实例只观察（[只观察手动会话并保护仓库占用](../../../../issues/19)）

观察本人明确登记、具有获准独立原生连接的项目与手动 DSH 实例，不获得控制权。默认开发使用 Hermes 管理的专用 DSH 实例；Desktop 手动观察／接管属于非默认接入，没有连接时明确不可观察／接管与覆盖未知。只读历史、同 home、克隆或另一个 SDK 运行时不代表原执行实例，不能由专用实例推断 Desktop 活动。当前契约见[DSH 执行目标修订](../specs/dsh-executor-transition.md)。

## 登记与公共入口

`Manager.register_observation_source(identity, registration)` 由本人登记稳定 id、source kind、project IDs 和 adapter ref；同 ID 不静默换服务。`refresh_manual_sessions`、认证 ManagementClient、Dashboard 与 `hermes_pm_observe` 共用可见项目范围和 snapshot。飞书“核对手动会话”沿唯一原任务关联及真实 @ 反馈，后台无新变化不重复发消息。

可信原生配置提供原 HTTP／WebSocket 接入、服务与 endpoint 引用以及原生认证材料；公共操作不能指定 URL、cookie、命令、verifier 或 enabled。配置发现不等于来源登记、权限授予或运行启用。

## 严格只读

插件客户端的只观察模式只调用实际已验证的 Session list、page／follow、projections 与相关只读子 agent／job 范围。禁止 create、resume、prompt、cancel、队列修改、审批答复、userQuestions.answer、归档／模型／配置变更。收到人工问题也不自动取得答复授权。

HTTP 单次调用与 `/api/remote.mux` 的逻辑流使用同一已核验来源；ready generation、基线、durable 游标和 transient frames 分开处理。business／协议失败保持失败，物理失联按当前原服务对账再重连。完整页、游标缺口、跨代、畸形记录或重复范围不能被静默忽略。

卸载关闭插件自己的逻辑流、订阅、HTTP／WebSocket 客户端和受管任务，不关闭共享后端或中断手动执行。秘密、URL／cookie、真实 Profile／项目／用户信息和帧内容不进普通日志；不改变宿主全局 logger。

## 身份和实际范围

能力证据绑定当前原 DSH 实例、进程启动事实、版本／配置摘要、来源与 endpoint、连接 generation、明确项目范围及实际只读病例。相同 Session ID 或历史不替代原实例身份证明。缺当前证据保持 unknown，不写临时 PASS 或另开服务来制造成功。

Session list 是存储／projection 可见范围，不能推出全部活动执行。核对真实 workspace 的 Git common directory 后才读取本项目历史；不复制 preview、消息、命令输出或秘密。子 agent、job、shell 后代、定时任务及其他明确登记实例分别说明已覆盖和未知部分。

- active：当前原实例基线／实时事件核实活动。
- inactive_verified：原根会话、相关子 agent／job、工具和可信进程覆盖均核实结束。
- last_known：只有历史或最后已知状态，不推断其他实例结束。
- unknown／conflict：身份、generation、分页、执行覆盖或布局不完整，保留最后核实时间和占用。

完整历史须核对 DSH page／follow 的全部必要 durable 事件和压缩前材料覆盖。尾页存在或 UI 显示摘要不证明原文已恢复；未经授权的 fork／子 agent 来源不随父身份自动开放。

## 仓库等待

已发现活动、历史／未知未核实执行和范围覆盖缺失阻塞同逻辑仓库的新外层任务。linked worktree 和别名不绕过串行；其他逻辑仓库可以继续。启动、追加和明确继续前重新核对来源；不能用新连接空列表释放旧活动占用。

观察不提供接管。明确本次工作授权和独立实际控制证据满足后才经[手动控制](manual-session-control.md)接入原 Session，不能把观察对象自动升级为控制对象。

## 验证与证据范围

DSH Remote 合成测试与 pristine Hermes SDK 检查所有出站只读、原实例／generation、错误身份、分页／压缩缺口、相关执行、断线、卸载／重载与仓库占用。Desktop 有获准独立原生连接时，实际原实例的只读列表／事件和作用域须单独验证；没有连接时该范围保持不可用与未知，不开发认证导出／发布／导入器来绕过它。一次成功读取不证明全部来源、控制、写入或群内闭环。

切换前的原 Codex 只读探针、Unix-WebSocket 和内核 peer 身份证明保留历史记录；不继续提供该协议兼容，也不将其纳入 DSH 能力凭据。
