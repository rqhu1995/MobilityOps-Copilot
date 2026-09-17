# 阶段 11：下一轮实验候选的离线接管与明确确认

阶段 11 把阶段 10 保存的 `NextExperimentProposal` 接入现有的确定性预检查、确认、
串行执行和独立报告链路。候选 JSON 是不可信输入，不是执行授权；该入口不调用 Gemini。

## 启动

从仓库根目录运行：

```bash
.venv/bin/python -m mobilityops \
  --mode next-experiment \
  --proposal runs/decision-sessions/<review-session>/001-next-plan.json
```

HGS 候选沿用 `HGS_REPO_PATH`、`GUROBI_REPO_PATH` 和 `MOBILITYOPS_RUNS_DIR`。
若候选明确要求 Gurobi，还必须用 `--gurobi-python` 注册已审计 adapter；可同时提供
`--gurobi-time-interval-sec` 和 `--gurobi-threads`。不能用 `--backend` 改写候选选择。

## 三步安全边界

1. 入口严格解析候选，重新调用 `ExperimentReportService.analyze()` 复核来源实验，核对
   `source_report_sha256`，再按候选中的唯一变体重新生成阶段 10 建议并要求完全一致。
2. 完整 `ExperimentPlan` 进入 `ExperimentService.precheck()`；全部场景通过后展示计划/上限
   调用数、所需/上限等待预算、失败策略、逐场景 backend 与时限，以及完整 backend 配置。
3. 请求指纹绑定规范候选散列、候选文件字节散列、来源实验/报告散列、experiment/report ID、
   完整计划、预检查和运行配置。只有输入界面显示的一次性确认短语会启动串行执行。

`auto_execute=true`、`requires_new_review_and_confirmation=false`、任意 blocker、空计划、
多个变体、来源证据变化、计划篡改、预检查失败或已有产物路径都会在确认前拒绝。
确认提示出现后，候选文件即使只改变空白、来源证据、预检查结果或 backend 配置发生变化，
旧确认也会失效。候选或其他文本中的“执行”不构成确认。

## 产物与执行语义

会话证据保存在 `runs/next-experiment-sessions/<session-id>/`，包含 `session.json`、
`precheck.json`、`request.json`、`review.json`、`state.json` 和事件日志。确认后请求快照保存在
`runs/next-experiment-requests/<experiment-id>/`；实验批次、逐次 run 和报告继续使用既有目录。

执行保持阶段 6/9 的语义：按 case/repetition 串行，无自动重试、补跑、恢复、backend 切换
或预算扩大。报告从原始结果和输入快照重新独立复核；失败或无候选不补零，小样本不支持
显著性、因果效应或最优性结论。预检查只读取本地可执行文件、审计源码和输入，不启动 solver
或许可证环境。

## 离线开发验收

阶段 11 新增 13 项测试，完整 **616 项测试通过**。覆盖候选/来源篡改、陈旧散列、
`auto_execute`、blocker、空计划、全场景预检查失败、错误确认、候选文件变化、backend 配置变化、
请求散列不匹配和 CLI 分流。fake HGS 端到端案例在精确确认后执行基准与变体各 3 次，
6 个候选均通过独立复核，保存报告只读重放一致。

本次开发验收调用真实 Gemini、HGS、Gurobi 和许可证环境均为 **0**，没有修改 solver core。
阶段 10 当前生成的真实 6 次 HGS 候选没有被自动执行；如需真实验收，必须另行审阅其来源散列、
计划、6 次调用、60 秒外部等待预算、backend 配置和新 experiment ID，再给出明确确认。
