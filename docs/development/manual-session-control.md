# 本次手动工作接管与归还（[接管并归还本次手动工作](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/20)）

接管是本人对一个已确认原手动会话、当前工作与指定负责人的明确授权。默认 [只观察手动会话并保护仓库占用](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/19) 只观察。原服务、读取范围或控制方法未被实际验证时显示受阻，不能另开 server/thread 或沿相同历史取得控制。

## 本次工作授权

`Manager.take_over_session(identity, request_id, manual_session_id, grant_id, expected_turn_id)` 与令牌 bridge 的同名入口共用授权。只有本人可授予；request 的已受理 Issue、版本、仓库和冻结负责 Profile 定义本次范围。manual_session_id 必须来自 [只观察手动会话并保护仓库占用](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/19) 当前已核实的原服务记录，原 cwd 必须符合本次冻结工作区。稳定 grant_id 不能改绑其他任务、会话或授权者。

持久 control_grants 保存本人、request/冻结范围 digest、指定负责人及身份、source/原 executor、观察代次、独立控制连接代次、endpoint ref、原 thread/current turn 与授权时间。同一原 executor/thread 同时只能有一个 active/pending/suspended 控制负责人。接管仅绑定当前原执行；不向其发送新目标、不创建 thread 或自动启动 turn。

`OriginalControlAdapter` 从 JSONL codec 独立派生，与 `ReadOnlyCodexAdapter` 分开；不把原只观察对象提升为写入对象。原生配置 `codex_manual_control` 显式给 source_id/executable/cwd/environment/service_ref/source_kind/endpoint/endpoint_ref，只构造固定 executable 的 `app-server proxy --sock <明确原 endpoint>`。不查默认 socket、不操作 daemon 生命周期。

连接前要求可信宿主的 `manual-control.json` manifest 指向 `manual-control-evidence/` 固定 SHA-256 report。它引用 `validation-evidence/` 中完整平台写入矩阵、tool 路径、其他执行器覆盖、manual_takeover、[在原会话追加、停止和明确继续任务](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/17) task_control 与 [将群内答复送回有效问题与审批](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/21) human_response 收据。与 [执行一个 Codex Issue 并核对交付结果](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/16) 使用同一完整边界核验，不以只读小范围代替真实开发需求。manual_takeover 收据使用原 thread/read/loaded-list 证据和 owner/single-controller/current-work/return/no-new-thread/no-replay/unsupported-desktop 等实际病例，不能用 thread/start 收据冒充接管。

report/收据绑定固定 binary/config/platform/repository/policy、原 endpoint、原 executor/current control generation、grant/本次目标及指定负责人。fresh 控制必须逐项保持原 permission_profile/policy_digest/runtime_roots。HTTP/群/工具体不能提供 verifier、enabled 或替换证据。解析器核验已有宿主证据，不制造实际 PASS。

## 复用原任务控制与应答

本次记录保存独立 manual origin 和原会话；17/21/18 的控制、刷新和交付通过每条记录的原执行器选择，不把手动会话转给受管 stdio executor。active grant 才能追加、停止、明确继续或正式原请求应答。追加是带 expectedTurnId 的 turn/steer；idle/start 仍要求排他输入能力与完整前置核对。明确继续沿原 thread、新 turn 和旧 stop_records/execution_arrangements。

人工请求沿 [将群内答复送回有效问题与审批](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/21)：仅本人作新决定/审批，指定机器人不能代批。正式 JSON-RPC response 只送原连接、原 request ID/类型、当前原 turn。归还或授权暂停后，待答请求及该 thread 的活句柄过期，不能把旧批准缓存到新授权重放。新明确授权可再次接管同一尚未完成工作，旧范围与基线保留，旧请求不因此复活。

固定原服务、control generation、负责人/目录或当前原 turn 出现冲突时暂停该授权、退为只观察并保留占用，显示需本人核对。`external_actor_coverage` 保持 unknown；不承诺自动识别所有桌面操作。

## 归还与完成

`return_session_control(identity, request_id, grant_id)` 由本人或本次指定控制负责人调用。只撤销当前 grant、使原记录 observe_only 并过期待答句柄；不发 interrupt/stop，不关闭原执行器或回滚文件。正在运行的原执行继续，repository_released 保持原值，后续任务仍等待核实。

工作完成使用 [按仓库排队并交接下一项 Issue](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/18) 既定冻结验收、正式测试/固定版本和完整原 thread/相关 child/background/可信宿主 process coverage。turn end 不等于工作完成；缺证据不交付、不解除占用。核实交付后 grant 标为 completed、控制退为只观察。旧授权不能延到后续 Issue 或其他原会话。

## 统一入口

Dashboard `/task action=takeover|return` 展示同一 control_grants、原执行器与负责关系。本人在唯一关联任务消息下输入 `接管本次工作：<manual记录ID> 回合：<原turnID>`；`归还本次控制` 沿当前 grant 处理，反馈仍引用原任务锚并真实 @。原生 hermes_pm_task 接同样动作字段，participant 入口不能借 owner alias；机器人无法授予本人授权，但当前指定负责人可以控制和归还既有 grant。

## 验证边界

所有当前验证使用公共令牌/API、真实 Python JSONL/proxy-shaped 原服务 peer、合成 Profile/仓库和 pristine SDK registry。wire 证明接管/归还不创建 thread、无归还中断；验证 owner-only、并发唯一负责人、原应答/过期、同 thread 停止/继续、错误 turn/service、重启不继承、缺能力及桌面不支持。合成收据只验证完整性及绑定，不代表真实服务控制已验收。

仍缺获准真实原服务/模型/群、[执行一个 Codex Issue 并核对交付结果](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/16) 完整实际写入边界及 [在原会话追加、停止和明确继续任务](https://github.com/Ghost233/GhostHermesProjectManagerPlugins/issues/17) 当前后台/进程/排他输入覆盖。真实控制保持未启用；没有接触真实原会话、default socket、daemon、.hermes/.codex 配置或秘密，也没有安装、迁移、全局 kill、container 或清理用户改动。
