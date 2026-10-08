# 本地检查与审查阶段门禁

提交前从准备好的 Python ≥3.11 环境运行 `python tools/local_checks.py`。固定 SDK 路径仍由 `HERMES_TEST_SDK_ROOT` 提供；本机独立 SDK Python 可由 `HERMES_TEST_SESSION_PYTHON` 提供。入口先检查 runtime、固定 test 依赖与 Node，再按同一顺序执行 F821、现有 required SDK pytest、Dashboard syntax 和 Git diff。具体参数见 `--help`；CI 调用同一入口。

Ruff 仅检查 F821。test extra 固定 [官方 Ruff 0.16.8](https://github.com/astral-sh/ruff/releases/tag/0.16.8)；[官方规则说明](https://docs.astral.sh/ruff/rules/undefined-name/)列明它捕获未定义名称。它不替代原 SDK 行为断言。

默认 stdout 是简短状态及 artifact 路径。完整 command、原 exit、terminal 标记、日志 digest、开始/结束源码指纹在 `result.json` 和原日志中。`-- COMMAND` 只验证这一条定点命令，不表示完整门禁已通过。原命令失败保留其 exit；command 0 而来源变化、准备不足、取消或无 terminal 时 validation 非零。artifact 必须放在冻结仓库外；只有完整模式所有命令真实通过且前后来源一致，才可提交。

双轴审查阶段结束时运行 `python tools/phase_gate.py --manifest <manifest.json>`。三个证据由审查完成后保存，manifest 的固定 source 使用 `tools.check_support.fingerprint` 的完整结果：

- `reports` 恰有 `Spec`、`Standards`，每项含真实报告 `path`、`sha256` 与实际文件 `saved_at_ns`。报告 JSON 明确包含相应 `axis`、`status: "final"` 和相同 `source`；进度消息不是 final artifact。
- `proof` 含保存后的真实 source-proof `path` 和 `sha256`。proof JSON 包含同一 `source`、`captured_at_ns` 及两报告实际 `report_digests`；捕获时间须晚于两个报告保存。
- 门禁同时读取当前 HEAD/tree/status/所有源码文件 SHA，检查它仍等于固定 source，并核对报告及 proof 的实际文件内容和摘要。缺轴、异源、进度、早 proof、摘要不符或晚到 writer 均失败。

通过仅表示显式两轴 final 与保存后同源证据完整，不认证审查质量或阻止任意外部 writer。门禁通过后再开始下一写入阶段；实际平台验收与 PR/main 交付仍按原工单完成。
