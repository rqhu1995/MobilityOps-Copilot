# 阶段 6：受控重复实验与可读报告

阶段 6 在现有 Python application service 上实现以下闭环：

```text
ExperimentPlan → 全部场景预检查 → 有预算的串行 solve_selected()
→ 保存选择与运行状态 → 重新复核原始证据 → 分组统计 → JSON + Markdown
```

不改变 `ScenarioSpec` 的目标、输入、求解时限或资源要求。报告采用确定性模板，
不调用 LLM。自然语言纵向切片是随后方向，本阶段未启动。

## 显式计划与预算

`ExperimentPlan` 包含一个 `baseline` 和完整的 `variants`。每个 `ExperimentCase`
记录独立 `case_id`、完整 `ScenarioSpec`、backend 要求、重复次数与外部 timeout。
`backend=None` 只允许现有服务的唯一兼容选择；不会尝试多 backend 或改写目标标识。

计划必须明确：

- `max_calls`：最多调用几次 `solve_selected()`，启动前失败也占用该次调用。
- `external_wait_budget_sec`：外部求解等待的预留总额。
- `failure_policy`：`continue` 或 `stop`。`stop` 在任何没有已验证候选的结果后停止，
  包括不可行、无候选超时和启动失败；有候选超时可以继续。

每次调用前，完整预留该条目的外部 timeout，不退回提前结束的余额、不重试、不补跑。
达到任一预算上限，剩余条目标为 `skipped_budget`。预算可以小于整个计划的最坏需求；
预检查通过 `budget_may_skip_runs` 明确提示这个差异。计划数、调用数与候选数分别报告。
调用前再次 preflight 被拒绝的条目不计为 `solve_selected()` 调用。

每个场景支持 1..1000 次重复，整个计划最多 10000 次；这只是 API 的有限性边界，
不构成新批次的运行授权。重复编号是实验索引，不能当成 solver seed。

`ExperimentService.precheck()` 无写入、无进程启动：先使用现有选择规则评估每个场景，
再调用新的 `SolverService.preflight()` 检查 executable/Python、必需输入、数学输入格式、
Gurobi 审计源码散列和每个预定 run ID。它还核对 adapter 的实际外部 timeout 是否与
计划完全一致。任一场景失败，整批不启动求解；执行入口仍保存计划、预检查理由和跳过记录。
`assess()` / `select()` 保持原有纯能力语义，Gurobi preflight 不启动许可证环境。

输入检查复用原有独立验证器的解析规则。运行环境、源码或输入在预检查后仍可能改变，
每次执行前会再检查；每次结果仍按其保存的输入和原始结果独立复核。不同证据签名不合并。

## 完整 Python 示例

仓库中的 [首批计划](../examples/experiment_hgs_6_1.json) 固定为 HGS `6_1`：

| 场景 | 车辆 | 维修人员 | 容量 | 重复 | 内部 / 外部时限 |
| --- | ---: | ---: | ---: | ---: | --- |
| baseline | 1 | 1 | 25 | 3 | 5 / 10 秒 |
| capacity30 | 1 | 1 | 30 | 3 | 5 / 10 秒 |
| two_trucks | 2 | 1 | 25 | 3 | 5 / 10 秒 |

其余参数一致；最多 9 次调用，预留等待总额 90 秒，失败策略为 `continue`。
90 秒不含输入复制、验证、检查点写入和报告耗时；批次另存 `elapsed_sec`。
单次外部 timeout 由现有 adapter 强制执行，不把整批墙钟耗时冒充求解等待预算。

下列代码会启动一批新求解；仅在明确请求执行该批次时使用：

```python
from pathlib import Path
from uuid import uuid4

from mobilityops.config import Settings
from mobilityops.domain.experiment import ExperimentPlan
from mobilityops.services.experiment_service import ExperimentService
from mobilityops.services.experiment_report import ExperimentReportService
from mobilityops.services.solver_service import SolverService

project = Path("/home/runqiu/MobilityOps-Copilot")
settings = Settings(
    hgs_repo_path=Path("/home/runqiu/BRPWR-HGSADC-SBC"),
    gurobi_repo_path=Path("/home/runqiu/BRPWR-Gurobi"),
    runs_dir=project / "runs",
)
plan = ExperimentPlan.model_validate_json(
    (project / "examples/experiment_hgs_6_1.json").read_bytes()
)
experiments = ExperimentService(SolverService(settings))
experiment_id = f"hgs-repeat-{uuid4().hex}"
precheck = experiments.precheck(plan, experiment_id=experiment_id)
print(precheck.model_dump_json(indent=2))  # 尚未启动求解

batch = experiments.execute(plan, experiment_id=experiment_id)
report = ExperimentReportService(settings).write_report(
    experiment_id, report_id=experiment_id,
)
print(batch.status)
print(settings.runs_dir / "experiment-reports" / experiment_id / "report.md")
```

单独重放不需要可用的 solver 或许可证，也不触发求解：

```python
reports = ExperimentReportService(settings)
fresh = reports.analyze(experiment_id)  # 只读，无报告写入
reports.write_report(experiment_id, report_id=f"replay-{uuid4().hex}")
```

Gurobi 批次复用同一接口，但场景必须使用 `gurobi_linear_default`，且向 `SolverService`
显式注入 `GurobiBackend`。见 [Gurobi 配置](gurobi-adapter.md#调用方式)。本阶段新增真实
实验仅使用 HGS；Gurobi 的批次分支通过 fake executable 和历史证据覆盖。

## 状态与证据保存

目录布局如下；experiment ID、report ID 和 run ID 都必须安全且不能覆盖已有目录。

```text
runs/experiments/<experiment_id>/
  plan.json
  precheck.json
  batch.json
  attempts/00001.json ...       # 每次选择、是否调用、状态、耗时、诊断
runs/<experiment_id>-c001-r0001/ # 现有 adapter 的完整原始运行证据
runs/experiment-reports/<report_id>/
  report.json
  report.md
```

每次调用前保存 `running` 检查点，结束后保存终态。选择记录包含完整请求与全部能力
评估。实际调用再次使用 `solve_selected()`，返回决策必须与保存决策一致。整个批次
串行执行，不自动恢复，不切换模型。

| 状态 | 含义 | 是否可进入指标统计 |
| --- | --- | --- |
| `succeeded` | adapter 返回通过验证的可行解 | 重新复核通过后可以 |
| `timeout_with_candidate` | 超时但有已验证候选，目前来自 Gurobi 内部时限 | 重新复核通过后可以 |
| `timeout_no_candidate` | 超时且没有被 adapter 接纳的候选 | 不可以 |
| `infeasible` | Gurobi 对本次周期模型报告不可行 | 不可以 |
| `constraint_rejected` / `preflight_failed` | 能力或运行前检查拒绝 | 不可以 |
| `start_failed` / `failed` | 启动、进程、解析或其他调用失败 | 不可以 |
| `validation_failed` | adapter 或服务拒绝候选的独立复核 | 不可以 |
| `pending` / `running` / `interrupted` | 未完成或执行中断 | 不可以 |
| `skipped_precheck` / `skipped_budget` / `skipped_failure` | 全批检查、预算或失败策略导致未调用 | 不可以 |

HGS 外部 timeout 不会从部分输出提升候选。验证失败不表示数学不可行。
捕获 `KeyboardInterrupt` / `SystemExit` 后保存中断状态并重新抛出；此前完成的运行
可以报告。硬退出可能只留下 `running` 状态，重放明确视为未完成，不猜测进程结果。
检查点/原始 artifact 写入失败一律停止，保留现有证据，不继续发起调用。
检查点使用临时文件和原子替换，批次与单次记录不一致时拒绝报告，不自动修补。

## 复核、分组与报告

`ExperimentReportService` 先交叉核对计划、完整运行序列、预检查、选择、各次检查点
和预算账目，再通过 `ScenarioAnalysisService.observe()` 重读每次的场景、原始结果、
输入快照与过程证据，重新执行独立验证。它不信任旧 `passed` 标记或旧报告指标。
复核还要求实际场景、backend、状态和外部 timeout 与计划及选择一致。

报告同时保留执行状态与本次证据复核状态。单次证据损坏或缺失时，该条目单独排除，
其余完整运行继续报告；批次计划或检查点互相矛盾时，整个报告拒绝生成。

候选按 **case、完整场景（忽略名称）、模型/输入签名、搜索条件** 分组；每组输出
objective、dissatisfaction、emission 的 `n`、最小值、中位数、最大值。失败和无候选
不补零。backend、模型实现、输入散列、验证语义、Gurobi 周期长度或搜索配置不同
时不混合样本。bound/gap 保留在单次结果，不统计或跨运行转移。

只有基准和变体各有一个有效组、模型口径一致且搜索条件相同，才生成“变体中位数
减基准中位数”。搜索条件变化另列 `search_conditions_changed` 并省略差值；无候选、
多签名组或跨模型返回 `not_comparable`。同一批次的各组仍可分别呈现。

Markdown 包含完整计划入口、场景变化、状态分母、逐组统计、观测差异、限制和每次
运行的直接证据链接。JSON 还保存完整签名、读入文件的 SHA-256 和重新计算的验证结果。
重放不会改写旧 `validation.json`；报告中的原执行验证链接只用于追溯当时记录。
相同证据的重放产生相同 JSON 与 Markdown 字节，新报告必须使用独立目录。

HGS 的六位有效数字舍入范围不是统计置信区间；连续 3 次运行也不默认相互独立。
这些小样本只描述本批观测，不推断显著性、因果效应、资源投入的一般价值或最优性。
HGS 到站时原子库存更新与 Gurobi 周期模型的语义限制继续适用。

## 本轮验收

2026-09-15 本轮完整测试 **418 项通过**：历史 356 项加 62 项实验测试。新增覆盖
计划重校验、全批预检查、调用/等待预算耗尽、混合终态、失败策略、中断和写入失败、
选择变化、重复 ID、证据/检查点损坏、符号链接、统计分母与奇偶中位数、输入签名
漂移分组、跨 backend 拒绝比较、有候选/无候选超时和确定性报告重放。普通 pytest
使用 fixture、mock 和 fake executable，不依赖真实产物、SDK 或许可证。

真实 HGS 首批严格执行上述 9 次计划，全部 `succeeded / feasible=True`，9 个候选
全部通过独立复核。完整外部等待预留 90 秒；批次实际耗时 **11.901342172 秒**，
首次报告生成另耗时约 0.118 秒。没有重试或额外求解。

| 场景 | 有效候选 n | 目标最小值 | 目标中位数 | 目标最大值 | 不满意度中位数 | 排放中位数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 3 | 104.038 | 104.039 | 104.040 | 51.7808 | 7.94776 |
| capacity30 | 3 | 104.038 | 104.039 | 104.040 | 51.7808 | 7.94776 |
| two_trucks | 3 | 104.038 | 104.039 | 104.040 | 51.7808 | 7.94776 |

两个变体相对基准的三个指标中位数差均为 0。容量 30 的排放范围为
7.93847..7.96796，另两个场景为 7.93847..7.97731；所有不满意度打印值均为
51.7808。这些数值只描述本批小样本，不证明加大容量或增加车辆没有价值。

验收证据保存在被忽略的 `runs/`：

- 实验与逐次选择：`runs/experiments/stage6-hgs-20260915T150009Z-10309bc8/`。
- 最终可读报告：[report.md](../runs/experiment-reports/stage6-hgs-20260915T150009Z-10309bc8-final/report.md)。
- 完整结构化报告：[report.json](../runs/experiment-reports/stage6-hgs-20260915T150009Z-10309bc8-final/report.json)。
- 初次验收记录：[acceptance.json](../runs/stage6-acceptance-20260915T150009Z-10309bc8/acceptance.json)，同目录保存脚本、前置检查、旧产物散列和历史 Gurobi 重放。

重放重新完成独立复核，连续生成的报告 JSON/Markdown 字节一致；531 个旧文件散列、
两个 solver 仓库状态及所记录的输入/源码/executable 散列保持一致。另只读复核了
3 个 Gurobi 历史运行：合成最优、有不可行证明、无候选超时。本轮没有新增 Gurobi
求解或探测许可证；Gurobi 的有候选超时等批次分支由 fake 测试覆盖。

这些真实产物不随普通 Git checkout 分发；缺少 `runs/` 不影响测试。
