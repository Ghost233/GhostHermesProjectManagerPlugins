# 维护、停用与恢复

统一管理入口 `maintenance(action, details)` 提供 `enter`、`check`、`checkpoint`、`switch`、`rollback`、`deactivate`、`reenable`。Dashboard 和已核实飞书本人入口调用同一权威实例，普通 Profile、模型凭据和消息正文中的身份自称不提供本人维护权限。

## Dashboard 用户路径

1. 打开「维护、停用与恢复」，核对实际加载的插件／SDK 版本、固定源码摘要、原服务与代次、核实时间、通知健康和未启用能力。管理实例离线显示最后核实资料；原生强制卸载或外部停机的事件仍是 `pending_verification`，不代表原执行已停止。
2. 选「进入维护」，填写稳定操作 ID；需要升级时填写已经登记的目标版本 JSON，例如 `{"id":"release-v2","plugin_version":"0.2.0","source_digest":"<实际64位摘要>"}`。预览会展示当前目录版本、完整 Profile 范围和原实际版本。本人审阅后确认，冻结原计划。维护暂停新执行与队列派发，已有监督仅在可用时继续。
3. 对同一操作 ID 选「核对维护」，查看原任务、相关后台执行、手动会话及在途请求。仅在原界面处理手动只观察执行，再用真实会话 ID 明确标记「本人已处理」；服务还会独立核实，不因本人标记就假定已停止。无法安全交接时保持阻塞并按计划待办处理，不假定热升级。
4. 无活动回合、相关执行或未处理在途请求且原生覆盖完整时，选「建立维护检查点」。核对配置、资料、档案、授权和目录备份证据。`checkpoint_verified` 才是检查点核实结果，未知版本、数据库或原服务不可用时不能据此切换。
5. 选「切换维护版本」，重新预览当前目录版本和完整范围后确认。版本字段更新或原生 `accepted` 只是观测／受理；只有原计划的 `switch_verified` 和对应配置、资料、档案与授权证据表示已核实切换。
6. 失败时本人明确选「回退维护」。只有 `rollback_verified` 才表示恢复证据通过。回退保留当前权威授权、任务／停止意图、队列和仓库改动，不 reset、clean 或自动启动旧任务；监督健康需要重新核对。
7. 对原操作 ID 选「重新启用」，审阅最新版本和完整范围。服务先对账原工作和在途请求，只有 `reenabled` 才解除维护安排；明确停止的任务保留停止状态，旧工作不自动续跑。

主动停用使用「主动停用」及稳定操作 ID，并审阅当前版本和完整范围。实例只按已有有效控制授权请求中断，核实原执行与相关后台结束后才显示 `deactivated`／`disabled`；手动只观察由本人处理，改动和排队工作保留。后续重新启用遵循同一原计划的对账路径。

每次刷新都作废旧维护预览，需要重新审阅后批准；不会根据刷新后的 Profile 范围偷偷扩大先前审批。任何响应丢失都保留原操作 ID并显示结果待核实，撤销旧控制审批。读取快照或明确 `check` 原计划，不重新提交控制、不生成新 ID 来代替未知结果。

## 飞书本人路径

在已核实的总管入口群，本人真实 @ 已登记总管，并用下列明确命令加 JSON 决定。原消息的 sender、租户、app namespace、群、真实 mention、Gateway 原 source 授权及实际收件机器人必须通过核验；配置里的总管责任范围也必须匹配。JSON 只描述本人当前操作及范围，不授予新身份。

| 命令 | 统一操作 | JSON 必要内容 |
| --- | --- | --- |
| `进入维护` | `enter` | 稳定 `operation_id`、当前 `expected_version`、完整排序 `expected_profile_ids`、实际 `expected_release`；升级时另带已登记 `target_release` |
| `核对维护` | `check` | 原 `operation_id`，必要时真实 `handled_manual_session_ids` |
| `建立维护检查点` | `checkpoint` | 原 `operation_id`，必要时真实 `handled_manual_session_ids` |
| `切换维护版本` | `switch` | 原 `operation_id`、新审阅的当前 `expected_version`、完整排序 `expected_profile_ids` |
| `回退维护` | `rollback` | 同上 |
| `主动停用` | `deactivate` | 稳定 `operation_id`、当前 `expected_version`、完整排序 `expected_profile_ids`、实际 `expected_release` |
| `重新启用` | `reenable` | 原 `operation_id`、新审阅的当前 `expected_version`、完整排序 `expected_profile_ids` |

`expected_release` 精确包括 `plugin_version`、`source_digest`、`sdk_version`、`sdk_source_digest`，取自当前已核实实际来源；不能用猜测版本代替。示例：`@总管 核对维护 {"operation_id":"原审定维护ID"}`。入口不会补齐或扩大消息里遗漏的目录版本／Profile 范围。结果关联原群原消息锚，回复原计划 ID、实际状态和本人待办；收件或回复送达不等于维护成功。

## 当前验收范围

离线公共测试使用真实管理入口、HTTP 路由和飞书原事件语义，外部原生维护、Codex、飞书及 React／Dashboard SDK 边界使用受控夹具。完整获准的真实版本、控制、消息、资料边界、备份与恢复验收须分别取得证据。Dashboard 的 `release_verified` 保持由权威实际核实结果决定，离线测试或静态版本显示不开放发布能力；正式安装、开通现有机器人和真实资料迁移需要具体安排。
