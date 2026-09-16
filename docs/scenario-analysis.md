# 阶段 5：显式能力选择与情景分析

阶段 5 已在现有 Python 服务上完成能力评估、带理由的选择、选择后求解，以及
保存结果的只读情景分析。两套 solver core、模型和既有调用接口保持原样。

## 能力评估与选择

`SolverService.assess(scenario)` 返回每个已知 backend 的 `BackendAssessment`：

- `registered` 表示当前 service 是否配置了该 adapter。
- `compatible` 表示该 adapter 的约束检查是否全部通过；未注册时为 `None`。
- `eligible` 只有已注册且约束检查通过才为 `True`。
- `constraints` 保留逐项处理记录，`reasons` 列出拒绝原因。
- `runtime_readiness` 始终为 `not_checked`；这里不读取 executable、输入或许可证，
  不启动子进程，不创建目录。执行前仍由原有 adapter 做 preflight。

`select(scenario, backend=None)` 返回可序列化的 `SelectionDecision`。显式指定
backend 时，只接受该 backend；省略时，仅在恰好一个 backend 兼容时选择它。
没有候选或存在歧义时 `selected_backend=None`，保留所有评估和具体原因。
不根据 backend 顺序、历史目标值或 `best_available` 猜测谁更好。

`solve_selected(scenario, run_id=..., backend=None)` 重新选择后调用现有 `solve()`，
返回 `SelectedSolution(decision, result)`。无法选择时抛出含 `.decision` 的
`BackendSelectionError`，不会启动 solver。执行期间沿用原有约束、preflight、
timeout、原始产物和独立验证规则；执行失败不尝试切换模型。

HGS 的 `eudf_default` 和 Gurobi 的 `gurobi_linear_default` 分别选择相应模型。
两种 policy 都保留输入的求解时限；`best_available` 不承诺取得最优解。
Gurobi 仍需显式注入已配置解释器的 adapter。其 capability 声明能报告 bound/gap，
不保证每次运行都会得到这些数值。

## 完整 Python 使用示例

从仓库根目录通过 `.venv/bin/python` 使用下列代码。示例执行两次内部上限 5 秒、
外部上限 10 秒的 HGS `6_1`；车辆容量是显式构造的情景变化。

```python
from pathlib import Path
from uuid import uuid4

from mobilityops.config import Settings
from mobilityops.domain import ScenarioSpec
from mobilityops.services.solver_service import SolverService
from mobilityops.services.scenario_analysis import ScenarioAnalysisService
from mobilityops.solvers.gurobi.backend import GurobiBackend
from mobilityops.solvers.gurobi.contracts import GurobiOptions

project = Path('/home/runqiu/MobilityOps-Copilot')
settings = Settings(
    hgs_repo_path=Path('/home/runqiu/BRPWR-HGSADC-SBC'),
    gurobi_repo_path=Path('/home/runqiu/BRPWR-Gurobi'),
    runs_dir=project / 'runs',
)
service = SolverService(
    settings,
    gurobi_backend=GurobiBackend(
        settings,
        options=GurobiOptions(
            python_executable=Path('/home/runqiu/anaconda3/envs/gurobi1301/bin/python'),
        ),
    ),
)
baseline = ScenarioSpec.model_validate_json(
    (project / 'examples/scenario_6_1.json').read_bytes()
)
variant = ScenarioSpec.model_validate({
    **baseline.model_dump(),
    'scenario_name': '6_1 capacity 30',
    'truck_capacity': 30,
})
for scenario in (baseline, variant):
    decision = service.select(scenario)
    print(decision.model_dump_json(indent=2))
    if decision.selected_backend is None:
        raise ValueError(decision.reason)

first = service.solve_selected(baseline, run_id=f'baseline-{uuid4().hex}')
second = service.solve_selected(variant, run_id=f'capacity30-{uuid4().hex}')
report = ScenarioAnalysisService(settings).compare(
    baseline_run_id=first.result.run_id,
    variant_run_ids=(second.result.run_id,),
)
print(report.model_dump_json(indent=2))

# 新建独立报告目录；不可覆盖已有分析，也不回写被分析的运行。
output = settings.runs_dir / f'analysis-{uuid4().hex}'
output.mkdir()
(output / 'baseline-selection.json').write_text(first.decision.model_dump_json(indent=2))
(output / 'variant-selection.json').write_text(second.decision.model_dump_json(indent=2))
(output / 'analysis.json').write_text(report.model_dump_json(indent=2))
```

若要运行 Gurobi，显式读取 `examples/scenario_gurobi_6_1.json`，再调用同一选择接口。
这表示用户选择另一套模型；系统不会自动把 HGS 场景的目标标识改成 Gurobi。
`solve_selected()` 的选择记录随返回值提供，由调用者保存；adapter 仍按原有格式
保存每次求解的 `solution.json`。多个场景依次运行，某次失败时已完成的运行仍保留。

## 比较规则

`ScenarioAnalysisService.compare(baseline_run_id=..., variant_run_ids=(...))`
仅读取 `Settings.runs_dir` 中的规范文件，要求至少一个变体且 run ID 不重复。
它无需配置 solver executable 或 Gurobi SDK，也不调用许可证环境。

每个运行重新读取 `scenario.json`、`solution.json` 和 `process.json`。HGS 还核对
argv、重新解析原始结果、检查完整输入散列和 executable 散列记录的一致性，并进行
独立数学复核。Gurobi 重新核对原始周期变量、原生 `.sol`、配置、建模源码散列及输入，
调用现有周期模型验证器。旧的 `validation: passed` 标记会丢弃后重算。
路径逃逸、符号链接、文件损坏或缺失证据导致异常并停止本次分析，不输出部分比较。
分析不修改原来的 `validation.json` 或任何历史文件。

报告包含每次运行的场景、复核后的结果、用于分析的证据 SHA-256、模型签名、搜索
条件，以及每个变体相对于基准的比较：

| `kind` | 含义 | 指标差值 |
| --- | --- | --- |
| `same_scenario_observations` | 同一运输场景的观测；名称或搜索条件可以不同 | 允许 |
| `scenario_observations` | 同一模型和输入口径下，资源、容量、服务时长、作业预算或 HGS 损坏车比例发生显式变化 | 允许，列出变化字段 |
| `not_comparable` | 候选未通过复核，或 backend、目标标识、实例、输入散列、实现散列、验证语义、Gurobi 周期长度不同 | 禁止；返回原因 |

比较采用保守的输入字节一致性规则：即使只调整输入文件格式，数学输入散列不同也
拒绝比较。HGS 的 BCRF/BCRFR 搜索辅助文件同样校验完整性，但它们的变化列为
搜索条件变化；模型输入签名采用独立数学验证器实际检查的 station/time/dissat 文件。
Gurobi 改变作业预算可以形成同一周期长度下的情景，改变周期长度则视为不同模型口径。

`scenario_changes` 列运输场景参数变化；`search_changes` 单独列求解时限、policy、
seed 声明及可记录的搜索环境变化。场景名称保留在各 observation 中，不作为数学变化。
签名中的 HGS executable 散列来自本次 run 的记录，不重新要求历史 executable 存在。
散列与独立重算证明本地证据一致性，不提供对所有文件被协调修改的密码学认证。

`deltas` 只包含 objective、dissatisfaction、emission，方向统一为 **变体减基准**。
每个值保留两次原始报告值。HGS 同时给出六位有效数字的合计舍入范围及是否超出该
范围；这不是统计置信区间。Gurobi JSON 没有 HGS 文本舍入，该范围记为 0；依然受到
数值求解和验证容差限制。两者都不根据单次差异宣称资源投入的因果效应或最优性。

`timeout / feasible=True` 且通过周期复核的 Gurobi 候选可以参与同模型比较。
无候选超时、失败或模型不可行的结果不产生指标差值。bound/gap 只保留在各自
`result` 内，报告不计算跨运行的 bound/gap 差值，也不比较两个 backend 的速度。

## 验收（2026-09-14）

阶段 5 新增 44 项普通测试，连同原有 312 项，共 356 项通过；使用 fake executable
和已有最小 fixture，不调用真实 solver。
覆盖无副作用选择、未注册/不兼容/歧义、显式拒绝且不 fallback、真实 preflight 错误
传播、模型重校验、两种 backend 的证据回放、损坏/缺失/符号链接/伪造验证标记、
不同输入和模型签名、搜索条件变化、有解超时与无候选状态，以及 JSON round-trip。

本机通过新选择接口执行了 3 次受控真实求解：

| 场景 | run ID | 状态 | 本次 objective |
| --- | --- | --- | ---: |
| HGS `6_1`，容量 25 | `hgs-selection-baseline-2bf30b6fd005` | succeeded / true | 104.039 |
| HGS `6_1`，容量 30 | `hgs-selection-capacity30-134d4d0afc3d` | succeeded / true | 104.039 |
| Gurobi 合成 1 站点 | `gurobi-selection-tiny-3c0845f669cd` | succeeded / true | 8.0331762396 |

两次 HGS 的打印目标值相同，只表示本次没有观察到目标值变化，不证明容量没有价值。
新的 Gurobi 合成结果与阶段 4B 的相同场景通过同模型比较。

另外只读重放了 12 个历史运行：8 个 HGS `6_1`、1 个 Gurobi `6_1`、3 个 Gurobi
合成终态。7 个 HGS 变体均通过同模型情景比较；HGS/Gurobi `6_1` 的比较明确返回
`not_comparable`，没有目标差值；Gurobi 不可行和无候选超时也没有指标差值。

例如历史基准目标为 104.039；2 车 / 2 维修人员且预算缩短为 3600 秒的变体为
104.116，观测差值 +0.077；损坏车比例显式设为 0.3 的变体为 103.983，观测差值
−0.056。这些是不同资源/库存条件下的单次观测，不作因果或算法优劣结论。

验收脚本、选择记录、3 份分析 JSON、基线及最终检查保存在被 Git 忽略的
`runs/stage5-20260914T075303Z/`。历史报告和两个 solver 仓库保持不变。
截至阶段 5，尚未实现自然语言输入、自动生成情景、批次调度、统计重复实验、跨模型
统一重评分、CLI、LLM/Agent SDK、API/UI 或新基础框架。2026-09-15 阶段 6 已增加
有界串行重复实验和 JSON/Markdown 报告，见 [重复实验文档](experiments.md)。
