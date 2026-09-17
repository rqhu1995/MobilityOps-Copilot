# 阶段 10：证据绑定的结果解释与下一轮实验建议

阶段 10 把重复实验报告变成可直接审阅的决策说明。入口不读取旧报告里的结论作为
事实；每次 `/explain`、`/compare` 或 `/next-plan` 都先调用
`ExperimentReportService.analyze()`，从保存的计划、执行检查点、原始 solver 输出、输入
快照和独立验证器重新构建报告。该入口不加载 Gemini，不启动 HGS/Gurobi，也不探测许可证。

## 三类结论

`DecisionExplanation` 明确分开三类内容：

- `verified_facts`：计划/实际调用数、批次终态、每个 case 的调用数和重新复核通过的候选数；
- `descriptive_observations`：同模型、同输入签名、同搜索条件分组中的最小值、中位数、
  最大值，以及满足可比条件时的“变体中位数减基准中位数”；
- `cannot_conclude`：统计显著性、因果效应、全局最优性、自动部署结论，以及由无候选、
  多签名、搜索条件变化或跨模型导致的具体不可比原因。

每条结构化说明都带证据路径或分组标识。`source_report_sha256` 对本次重新构建的完整
`ExperimentReport` 做规范 JSON 散列；它用于证明说明绑定的是哪一次本地重放结果，不能
替代外部签名或证明所有文件未被协调篡改。

## 离线终端入口

以下命令只需要既有 experiment ID：

```bash
.venv/bin/python -m mobilityops \
  --mode analysis \
  --experiment-id terminal-experiment-stage9-real-acceptance-20260916-v1
```

从仓库根目录启动时，analysis 模式默认读取 `./runs`，不要求 HGS/Gurobi 仓库环境变量。
从其他目录启动时使用 `--runs-dir /home/runqiu/MobilityOps-Copilot/runs` 明确指定绝对路径。

可用命令：

```text
/explain
/compare capacity30 baseline
/next-plan [variant-case-id]
/quit
```

会话保存在 `runs/decision-sessions/<session-id>/`。说明和建议使用独立、递增的 JSON
文件保存；源 experiment 与 run 目录保持只读。`/compare` 只接受计划中已有的变体 case ID。

## 下一轮候选计划

`/next-plan` 生成 `NextExperimentProposal`，不会执行。终端默认绑定最近一次 `/compare`
的变体，也可用 `/next-plan two_trucks` 显式指定；没有选择时拒绝生成，避免静默采用第一个变体。若当前差异可比，它选择该变体，
构造基准和变体各 3 次的确认批次，并把调用上限和等待预算设为计划所需的精确上限。
若唯一问题是搜索条件变化，它先把变体的运行时限、solution policy、seed、外部 timeout
和 backend 要求与基准对齐，同时保留真正的场景变化。

无复核候选、多模型/输入签名或跨 backend 时，服务返回 `plan=null` 和明确 blocker，
不会拼接跨模型统一评分，也不会凭空推荐参数。所有非空候选都有
`auto_execute=false` 和 `requires_new_review_and_confirmation=true`；后续仍须走全计划能力
检查、运行预检查、预算审阅和新的明确确认。

## 离线验收范围

阶段 10 新增 11 项测试，覆盖同口径说明、单变体筛选、候选计划预算、最近比较绑定、搜索条件对齐、
无候选、跨 backend 不可比、终端三条命令和 CLI 分流。测试使用 fake executable 与
fake Python 子进程；解释阶段对 `subprocess.run` 设置失败哨兵，证明建议生成没有启动
solver。没有真实 Gemini、HGS/Gurobi 调用或许可证探测，也没有修改 solver core。
