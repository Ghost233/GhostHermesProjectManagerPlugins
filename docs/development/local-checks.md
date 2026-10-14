# 本地检查与审查阶段门禁

当前执行端复用同一个 DSH Desktop 后端。原生 Kanban 派发仓库工作，飞书承担收发和人工应答。执行范围见[当前规格](../specs/hermes-development-coordination-v2.md)和[同实例接入决定](../adr/0002-dsh-led-repository-work.md)。

## 本机核心门禁

从准备好的 Python ≥3.11 环境运行 `python tools/local_checks.py`。固定 Hermes SDK 路径由 `HERMES_TEST_SDK_ROOT` 提供；本机 SDK Python 可由 `HERMES_TEST_SESSION_PYTHON` 提供。`DSH_TEST_SDK_ROOT` 指向原 DSH 安装的 Node 包目录，目录包含 `@deepseek-ai/dsh`。默认核心检查和 pytest 定点检查缺少 SDK 时失败。

默认 pytest 范围由 `pyproject.toml` 的 `testpaths` 指定，覆盖共享 Desktop、原生 Kanban 和飞书的核心行为。历史独立运行时用例默认未选中；原失败、超时和清理记录保留，未选中不算通过。其他有价值的回归按相关变更触发，测试数量不作为目标。

入口先核对 Python、固定测试依赖和 Node，再执行 SDK 测试接缝静态检查、Ruff F821、核心 pytest 和 Git diff 检查。pytest 使用首错即停、跳过原因和短临时目录。新增修改先运行定点及受影响验证，复用输入未变且不受修改影响的有效结果。同一输入、环境和命令只保留一个有效长测。

Ruff 版本由 `pyproject.toml` 的 test extra 固定，当前为 `0.15.10`。检查仅选择 [F821](https://docs.astral.sh/ruff/rules/undefined-name/)，捕获未定义名称。SDK 加载、工作区与会话、问答、审批、预算、写入和生命周期仍须通过原生行为验证。`sdk_source_integrity` 核对原 Hermes／DSH SDK 完整源码清单和字节不变。

默认 stdout 是简短状态及 artifact 路径。完整 command、原 exit、terminal 标记、日志 digest、开始和结束源码指纹保存在 `result.json` 和原日志中。`-- COMMAND` 只验证这一条命令，不表示核心门禁已通过。原命令失败保留其 exit；command 为 0，但来源变化、准备不足、取消或缺少 terminal 时，validation 非零。artifact 必须保存在冻结仓库外。提交前，本次修改所需检查须真实通过，并覆盖最终待提交内容。

## 远端 CI

[GitHub Actions](../../.github/workflows/checks.yml) 在 Ubuntu 上执行四项静态检查：

- Python 未定义名称：`python -m ruff check --no-cache --select F821 ghost_hermes_pm tests tools __init__.py`。
- SDK 测试接缝：`python tools/check_sdk_test_seams.py`。
- 当前 DSH Hook 语法：`node --check ghost_hermes_pm/shared_native_hook.mjs`。
- 提交差异的空白错误：`git diff --check HEAD~1 HEAD`。

每项独立显示原始输出和退出结果。远端通过只证明这些静态检查通过；原 SDK 和本机沙箱行为、当前 Hermes／飞书／DSH 实际流程、交付及本地分支同步继续在本机验收。远端 CI 是补充证据，不是本机个人使用的交付或合并硬门禁。

## 双轴审查阶段

双轴审查阶段结束时运行 `python tools/phase_gate.py --manifest <manifest.json>`。三个证据由审查完成后保存，manifest 的固定 source 使用 `tools.check_support.fingerprint` 的完整结果：

- `reports` 恰有 `Spec`、`Standards`，每项含真实报告 `path`、`sha256` 与实际文件 `saved_at_ns`。报告 JSON 明确包含相应 `axis`、`status: "final"` 和相同 `source`；进度消息不是 final artifact。
- `proof` 含保存后的真实 source-proof `path` 和 `sha256`。proof JSON 包含同一 `source`、`captured_at_ns` 及两报告实际 `report_digests`；捕获时间须晚于两个报告保存。
- 门禁同时读取当前 HEAD/tree/status/所有源码文件 SHA，检查它仍等于固定 source，并核对报告及 proof 的实际文件内容和摘要。缺轴、异源、进度、早 proof、摘要不符或晚到 writer 均失败。

通过仅表示显式两轴 final 与保存后同源证据完整，不认证审查质量或阻止任意外部 writer。门禁通过后再开始下一写入阶段；实际平台验收与 PR/main 交付仍按原工单完成。
