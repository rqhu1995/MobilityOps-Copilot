# MobilityOps-Copilot

MobilityOps-Copilot 是交通运输优化项目的独立 orchestration 仓库。它将逐步负责场景输入验证、solver 适配、执行管理、原始结果解析、确定性验证，以及后续的解释和 Agent workflow。

## 三仓库关系

| 仓库 | 服务器路径 | 职责 |
| --- | --- | --- |
| MobilityOps-Copilot | `/home/runqiu/MobilityOps-Copilot` | Orchestration、adapter、validation 和标准化输出 |
| BRPWR-HGSADC-SBC | `/home/runqiu/BRPWR-HGSADC-SBC` | 独立的 C++/CMake HGS 启发式 solver |
| BRPWR-Gurobi | `/home/runqiu/BRPWR-Gurobi` | 独立的 Python/Gurobi 精确 MILP solver |

两个 solver 仓库保持独立。本项目通过环境变量引用它们，不复制其完整源码，也不默认修改其核心算法。

## 安装

要求 Python 3.11 或更新的稳定版本。当前服务器的系统 Python 3.12 缺少
`ensurepip/python3.12-venv`，因此使用已经验证过的 `gurobi1301` Python
3.12.13 作为基础解释器来创建项目自己的 `.venv`：

```bash
cd /home/runqiu/MobilityOps-Copilot
/home/runqiu/anaconda3/envs/gurobi1301/bin/python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

新依赖只安装到 `.venv`，不会安装到或升级 `gurobi1301`。在其他已经提供
`venv`/`ensurepip` 的 Python 3.11+ 环境中，也可以使用 `python3 -m venv
.venv`。

如需配置本机路径，可从示例开始：

```bash
cp .env.example .env
```

`.env` 被 Git 忽略。当前代码只读取进程环境变量，不自动加载 `.env`，以避免引入额外依赖；启动命令或后续 CLI 应显式提供这些变量。

## 当前阶段：17，审计证据绑定的 Decision Dossier v1

阶段 17 新增非交互 `--mode dossier`，将阶段 16 的 `verified_complete` 执行审计重新验证后，输出
事实、描述性观察、不能得出的结论、五个决策门和 `auto_execute=false` 的有限下一轮候选。真实
阶段 16 证据包的建议为 `collect_replicates`，因为 baseline/capacity30 当前各只有 `n=1`；候选为
两个 case 各 3 次，但尚未授权执行。完整 **659 项测试通过**。范围、命令和结果见
[阶段 17 文档](docs/audited-decision-dossier.md)。

## 阶段 16：审计绑定的真实 HGS 小批验收

阶段 16 新增 `--mode audited-execution-audit`，把阶段 14 来源 audit、阶段 15 双层确认请求、真实
run 快照、独立验证、确定性报告和父子谱系重新组成一个逐文件散列的闭环。完整 **654 项测试
通过**；真实 baseline/capacity30 各 1 次 HGS 已按明确确认完成，2 个候选均通过独立复核，执行后
审计为 `verified_complete`。范围、固定计划、结果与拒绝条件见
[阶段 16 文档](docs/audit-bound-real-pilot.md)。

## 阶段 15：审计绑定的执行接管

阶段 15 新增 `--mode audited-execution`：它重新审计阶段 14 的来源会话，核对保存 audit、唯一
执行审阅和来源草稿，再以当前 backend 配置重建全计划预检查与一次性执行请求。来源证据、计划、
预检查或运行配置变化都会使旧确认失效。离线 fake 闭环已完成，完整 **650 项测试通过**；阶段 14
真实计划仍未执行，必须取得新的明确授权。范围、命令和证据见
[阶段 15 文档](docs/audit-bound-execution.md)。

## 阶段 14：可审计的真实 Copilot 影子会话

阶段 14 用固定 service 脚本把真实 Gemini 计划路由、字段提取、HGS 只读预检查、执行审阅、
明确取消、证据回答和阶段 13 审计串成一条纵向路径。真实验收已完成 4 次 Gemini 调用，usage
估算 0.187116 HKD；审计状态为 `verified_no_execution`。专用 HGS adapter 硬阻断 `solve()`，
因此没有执行计划中的 solver 调用：

```bash
.venv/bin/python examples/stage14_audited_copilot_pilot.py
```

范围、固定 ID、失败关闭和产物见[阶段 14 文档](docs/audited-copilot-pilot.md)。

## 阶段 13：Copilot 会话离线审计

阶段 12 的会话现在可以从原始本地证据重新验证，并生成逐文件 SHA-256 的确定性
JSON/Markdown 证据包。审计不要求交互终端、Gemini 凭据或 solver 仓库环境变量，也不会
调用模型、solver 或许可证环境：

```bash
.venv/bin/python -m mobilityops \
  --mode audit \
  --copilot-session-id <copilot-session-id> \
  --audit-id <audit-id>
```

完整重放范围、状态、产物和限制见[阶段 13 文档](docs/copilot-audit.md)。

## 阶段 12：Gemini Decision Copilot

统一入口现可在同一会话中完成“业务目标 → 受校验实验计划 → 证据解释 → 下一轮候选 →
全计划预检查 → 本地明确确认 → 报告回接”。Gemini 只选择有限本地动作和生成证据引用的表述；
它没有 shell、Python、solver 或执行确认能力。启动示例：

```bash
.venv/bin/python -m mobilityops \
  --mode copilot \
  --baseline examples/scenario_6_1.json \
  --backend hgs \
  --budget-hkd 1.5 \
  --max-calls 6
```

从既有实验开始时改用 `--experiment-id <id>`。完整动作、预算、确认和证据契约见
[阶段 12 文档](docs/gemini-decision-copilot.md)。以下章节保留各底层阶段的历史使用和验收记录。

## 阶段 8：终端交互入口

终端现已接通“输入需求 → 澄清 → 审阅场景与预算 → 明确确认 → 单次求解与报告”。
复用现有 Gemini、场景验证、HGS/Gurobi 和报告服务，不新增依赖。默认每会话 LLM 预算
1 HKD、最多 3 次提取；启动后先显示预算，输入 `同意` 才启用模型调用。

在本机仓库根目录使用 fish：

```fish
set -gx GEMINI_API_KEY $GEMINI_API_KEY
.venv/bin/python -m mobilityops --baseline examples/scenario_6_1.json --backend hgs \
    --hgs-repo-path /home/runqiu/BRPWR-HGSADC-SBC \
    --gurobi-repo-path /home/runqiu/BRPWR-Gurobi \
    --runs-dir /home/runqiu/MobilityOps-Copilot/runs
```

输入 `把容量提高到30，其他沿用基准`，审阅候选后用 `/run` 检查并展示单次请求。
只有输入该请求的确认码才执行；示例内部上限 5 秒、外部 timeout 10 秒、无重试。
`/review` 查看当前草稿，`/quit` 保存后退出。完整命令、fish 环境配置、证据目录与
退出码见 [终端使用说明](docs/terminal.md)。帮助入口 `.venv/bin/python -m mobilityops --help`
不需要凭据或环境配置。

HGS 首版开发验收 **534 项普通测试通过**，含 **36 项新增终端测试**。端到端测试使用 fake HTTP 与 fake executable，当时没有新增真实 LLM/solver 调用；
真实终端启动与取消路径也已检查。每会话最多执行一次，不自动恢复或重试。

2026-09-16 用户随后完成完整真实终端验收：1 次 Gemini 提取、1 次确认后的 HGS
求解，结果 `succeeded / feasible=True`，候选通过独立复核；目标值 104.04，求解器
耗时 1.41653 秒，LLM usage 估算 0.013878 HKD。只读重放与保存报告一致，详见
[终端真实验收](docs/terminal.md#用户完成的真实终端验收)。

Gurobi 终端现可通过 `--gurobi-python /home/runqiu/anaconda3/envs/gurobi1301/bin/python`
显式注册；使用 `--backend gurobi` 和 `--baseline examples/scenario_gurobi_6_1.json`。
可配置时间段长度（默认 600 秒）和线程数（默认 1），它们与完整执行请求一起纳入
终端确认指纹。预检查不启动许可证环境；确认后沿用已有周期模型独立复核和报告。
完整命令与参数边界见 [Gurobi 终端用法](docs/terminal.md#启动-gurobi-交互)。

Gurobi 终端扩展后完整 **571 项测试通过**，本次新增 **37 项**。本机 Python、
已审计源码与 `6_1` 数据通过只读预检查；该开发验收阶段未新增真实 LLM/solver 调用或许可证探测。
见 [扩展验收](docs/terminal.md#gurobi-终端扩展验收)。

用户随后完成真实 Gurobi 终端验收：1 次 Gemini 提取、1 次确认后的 Gurobi 求解，
返回 **`timeout_with_candidate / feasible=True`**，8657 项独立检查通过。目标值
104.163310039134，best bound 103.794436993005，gap 约 0.354130%，仅属于本次周期模型。
约 5 秒内部时限结束后正常退出；未触发 10 秒外部终止。原始响应、配置确认及结果报告
已只读重放复核，详见 [Gurobi 真实终端验收](docs/terminal.md#用户完成的真实-gurobi-终端验收)。

### 已完成阶段 7：自然语言需求与显式执行请求

阶段 7 已实现 Gemini `gemini-3.8-flash` 的 REST 接入、带原文来源的候选字段、
缺失/冲突字段澄清、现有能力评估，以及绑定完整草稿和预算的单次执行请求。
`ScenarioIntakeService.propose()` / `revise()` 只生成和修订候选；
`prepare_execution()` 提供可审阅请求，`execute()` 才调用已有实验与报告服务。

最终接入使用 Gemini **Interactions API**：提示中要求 JSON，本地严格校验；凭据仅通过
`GEMINI_API_KEY` 环境变量认证。2026-09-16 完整 **498 项测试通过**，六个真实案例
全部通过，覆盖参数修改、缺失时限与补充、未支持时间窗、HGS seed 拒绝和 2 辆车。
保存的原始响应、候选与执行请求已重新复核。本批 usage 估算 **0.146778 HKD**；
包含此前失败、参数对照及手动验证预留，累计保守预留 **3.74096 HKD < 10 HKD**，
预留不等于实际账单。六案例提取阶段的真实 solver 调用 **0 次**。随后用户确认容量 30 的单次请求，
已完成 **1 次真实 HGS 执行并通过独立复核**：目标值 104.039，求解器耗时 1.27922 秒，
无新增 LLM 调用。见 [执行报告](runs/stage7-confirmed-execution-20260916/report.md)。
详见 [阶段 7 文档](docs/natural-language-intake.md) 与
[受限验收示例](examples/stage7_gemini_acceptance.py)。

### 已完成阶段 6：受控重复实验与可读报告

阶段 6 已完成显式 `ExperimentPlan`、全部场景启动前检查、有界串行执行、逐次选择与
状态保存、重新复核后的描述统计，以及可追溯的 JSON / Markdown 报告。
`ExperimentService` 复用现有 `SolverService`；`ExperimentReportService` 从保存证据
重新验证、按场景/模型/搜索条件分组，失败、无候选和不可比结果单独呈现。

2026-09-15 本轮完整验收 **418 项测试通过**。首批真实 HGS `6_1` 为基准、容量 30、
2 辆车三个场景各 3 次，共 9 次；每次内部上限 5 秒、外部 timeout 10 秒，等待预算
90 秒，批次实际耗时约 11.90 秒。9 个候选全部通过独立复核，三个场景的目标值
最小值/中位数/最大值均为 104.038 / 104.039 / 104.040；这仅表示本批未观察到
中位数差异，不证明资源变化没有价值。只读重放得到相同统计与报告字节，531 个旧
产物及两个 solver 仓库保持原样。另重放 3 个历史 Gurobi 终态，本轮未新增 Gurobi 求解。

完整 API、失败策略、预算定义、状态表及验收链接见 [受控重复实验](docs/experiments.md)，
固定首批计划见 [experiment_hgs_6_1.json](examples/experiment_hgs_6_1.json)。

### 阶段 5 的能力选择与情景分析

阶段 5 已完成 `SolverService.assess()`、`select()`、`solve_selected()` 和
`ScenarioAnalysisService.compare()`。应用层可解释哪些 backend 已注册、哪些约束
被拒绝，并只选择唯一兼容的 backend 或用户指定的兼容 backend；运行环境与许可证
仍由执行阶段检查。分析保存结果时重新复核证据，同模型情景生成观测差值，跨模型
或没有可行候选的结果明确拒绝比较。完整调用示例、比较规则及真实验收见
[能力选择与情景分析](docs/scenario-analysis.md)。

阶段 4B 已实现 Gurobi adapter，并通过原有建模函数完成受控求解、原始结果保存、状态映射和独立周期模型验证。项目 Python 环境负责 orchestration 和验证；Gurobi 在显式配置的既有 Python 环境中运行，无需修改 solver core 或向本项目安装 SDK 依赖。

Gurobi 使用独立的 `gurobi_linear_default` 目标标识，HGS 保持 `eudf_default`。两个 solver 的时间、库存和不满意度语义不同，不能将 Gurobi 的 bound/gap 用于证明 HGS 解的质量。当前接口、完整调用示例、状态表、验证范围和真实验收见 [Gurobi adapter](docs/gurobi-adapter.md)；模型差异的数值依据见 [集成前审计](docs/gurobi-integration-audit.md)。

### 已完成的 HGS 端到端集成

阶段 3 已实现以下 Python 工作流：

```text
ScenarioSpec → capability/constraint check → HGS argv → preflight
→ 隔离执行 → 原始 artifact → parser → 结构与数学复核 → SolutionResult
```

- `SolverService` 默认注册 HGS；显式注入配置好的 `GurobiBackend` 后也可选择 Gurobi。原有 `solve()` 显式指定 backend，新 `solve_selected()` 支持按兼容性唯一选择并返回理由。
- 指定 `seed` 会在创建运行目录和启动进程前拒绝；两个 solution policy 都保留用户提供的 runtime。
- HGS 在本次 run 的独立工作目录运行；所需 instance 文件复制到该 run，solver 源码和 solver 仓库不被修改。
- 同时设置内部时限和外部 timeout。默认 grace period 为 5 秒；超时、非零退出、缺失/损坏结果都有明确状态和原始证据。
- 验证 station ID、资源数量、路线边界、操作数量、车辆库存及容量、结果状态和 artifact 归属；`SolutionResult` 支持 JSON round-trip。
- 成功结果还必须通过输入快照的 SHA-256 检查，以及站点库存时序、每条路线完整用时、不满意度、排放和目标值的独立复核。详细时间线与比较容差写入 `validation.json` 和 `solver_metadata.validation`。
- HGS 的 `best_bound` 和 `optimality_gap` 始终为 `None`。

这里的“确定性”指输入、参数映射、解析、状态和验证规则。HGS 从系统时间初始化随机数，没有 seed CLI，因此路线和目标值可能变化。独立复核采用 HGS 的到站时原子更新库存、固定行驶时间、无等待语义，不模拟服务过程中的逐辆转移或维修完成后才能使用车辆的时刻，也不证明最优性。这些模型边界会随成功结果返回为 warnings。

## 通过 Python API 运行

从仓库根目录使用 `.venv/bin/python` 执行以下 Python 代码。默认调用已有的 `/home/runqiu/BRPWR-HGSADC-SBC/build/main`，不会自动编译。路径不同的环境应调整 `Settings`；也可使用 `Settings.from_env()` 读取上述三个环境变量，代码不会读取 `.env`。

```python
from pathlib import Path
from uuid import uuid4

from mobilityops.config import Settings
from mobilityops.domain import BackendName, ScenarioSpec
from mobilityops.services.solver_service import SolverService

project = Path("/home/runqiu/MobilityOps-Copilot")
settings = Settings(
    hgs_repo_path=Path("/home/runqiu/BRPWR-HGSADC-SBC"),
    gurobi_repo_path=Path("/home/runqiu/BRPWR-Gurobi"),
    runs_dir=project / "runs",
)
scenario = ScenarioSpec.model_validate_json(
    (project / "examples/scenario_6_1.json").read_bytes()
)
result = SolverService(settings).solve(
    scenario,
    backend=BackendName.HGS,
    run_id=f"hgs-6-1-{uuid4().hex}",
)
print(result.model_dump_json(indent=2))
```

上例只执行 HGS。已有 executable 位于其他绝对路径时，可显式构造 `HgsBackend(settings, executable=..., timeout_grace_sec=5.0)` 并通过 `SolverService(settings, hgs_backend=...)` 注入。注入的 HGS 或 Gurobi backend 都必须使用与 service 相同的 `Settings`，配置不一致会在创建 service 时抛出 `ValueError`，避免执行与结果验证使用不同路径。

每次 run 保存 `scenario.json`、实际 argv 数组 `command.json`、原始字节 `stdout.log` / `stderr.log`、原生 `Solutions/...txt`、其逐字节副本 `solver-result.txt`、含输入及 executable SHA-256 的 `process.json`、独立复核报告 `validation.json`，以及验证后的 `solution.json`。运行目录还保留所需的输入快照。所有文件都在被 Git 忽略的 `runs/<run_id>/` 下。

`validation.json` 在成功时记录每辆车和维修人员的到达/离开时间、各站点的库存变化、最终库存及指标比较。例如 `6_1` 的完整输出包含 13 项比较：3 项主指标、4 项汇总用时、6 项站点不满意度。比较容差按 HGS 的六位有效数字计算。输入损坏、库存矛盾、路线超时、指标不一致或会影响可行性的同时到达顺序不明确，都使结果返回 `failed` / `feasible=None`；原始候选结果仍保留。报告会区分复核失败与因进程/解析失败而未执行复核。

run ID 仅允许 ASCII 字母、数字、`.`、`_`、`-`，首字符必须为字母或数字，长度不超过 200。重复 ID 会抛出 `HgsPreflightError`，不会覆盖原结果。约束拒绝抛出 `ConstraintRejectedError`；输入/配置问题抛出 `HgsPreflightError`（非法 ID 为 `ValueError`）；artifact 写入失败抛出带运行目录的 `ArtifactError`。已经启动的进程失败或解析/验证失败返回 `failed`，外部超时返回 `timeout`，可行性未知时为 `None`。没有结果文件不等于数学不可行。

完整字段映射、目录布局、状态语义与验证限制见 [docs/solver-contract.md](docs/solver-contract.md)。

## 验证证据与尚未实现

普通 pytest 使用固定输入/输出 fixture、mock、独立 fake executable 和可手算实例，不启动真实 HGS 或 Gurobi。阶段 4B 的 312 项测试保持通过，覆盖参数与能力拒绝、路径及源码检查、输入与原始结果损坏、最优/有解超时/无解超时/不可行、子进程回收、写入失败、每类周期变量的篡改、独立预算检查、F 包络松弛、证据重放和 service 配置一致性。阶段 5 新增 44 项能力选择与情景分析测试，阶段 5 完整测试共 356 项通过；阶段 6 新增 62 项实验测试，本轮共 418 项通过。

2026-09-10 已在本机通过一次真实 `6_1` smoke test：内部上限 5 秒，外部上限 10 秒；HGS 正常提前结束，报告 runtime `1.03258` 秒，objective `104.04`，dissatisfaction `51.7808`，emission `7.96796`。返回 `succeeded` / `feasible=True`，车辆与维修路线均成功解析，bound/gap 均为 `None`。这些数值只记录本次运行，不作为重复运行的精确断言。

首次纵向切片产物为 `runs/hgs-6-1-20260910T150133Z-9fffd7ee/`，验收记录为 `runs/stage3-smoke-acceptance.json`。2026-09-11 使用新验证器回放该结果也通过，原有产物保持不变。

2026-09-11 的新真实验收产物为 `runs/hgs-math-6-1-20260911T135733Z-6df16b93/`，独立复核状态 `passed`。HGS 报告 objective `104.042`，独立复算为 `104.04162346259199`，差异在六位有效数字舍入容差内；车辆完整路线用时为 `5537.1` 秒，维修路线为 `3666.144` 秒，均低于 7200 秒操作预算。内部/外部运行时限仍为 5/10 秒，实际报告 runtime `1.39315` 秒。验收记录及旧结果回放报告在 `runs/hgs-math-development-20260911T134914Z/`，并验证了 JSON round-trip、验证幂等、原生输出字节一致和重复 ID 拒绝。

2026-09-11 的提交前审查补充了 8 组真实 `6_1` 验收，全部通过：基准、2 truck、2 repairer、2 truck + 2 repairer、显式损坏车比例 0/0.3/1，以及 3600 秒作业预算下的 2 truck + 2 repairer。最后一组有两辆车实际作业并共同服务站点 3；另有一组两名维修人员实际作业。全部使用 5 秒内部求解上限和 10 秒外部 timeout，并通过 13 项指标比较、库存时序、路线预算、JSON round-trip、验证幂等和重复 ID 不覆盖检查。详细矩阵、审查修复与本地产物索引见 [阶段 3 验收记录](docs/stage3-acceptance.md)。

被忽略的本地运行产物不会随 Git checkout 分发。真实验收目前只覆盖 `6_1`，没有覆盖更大规模实例、长期运行或所有多资源组合。

本机已有 `gurobi1301` 环境中的 Gurobi 13.0.1 已通过许可证、小型模型和阶段 4B 真实求解验证。环境与集成边界记录在 `AGENTS.md`，原有 HGS 运行方式继续可用。

2026-09-13 的阶段 4A 探测证据保留在 `runs/stage4a-audit-20260913T082102Z-eaf16585/`。2026-09-14 的阶段 4B 已进一步通过应用层验收合成最优、不可行、无解超时、多资源以及真实 `6_1` 有解超时。`6_1` 在 5 秒内部上限下返回 `timeout / feasible=True`：目标 `104.14143246938093`，bound `103.80007873300737`，gap `0.0032777899081993683`（约 0.328%），8657 项独立约束检查通过。产物在 `runs/gurobi-6-1-20260914T071532Z-326ec303/`，汇总在 `runs/stage4b-20260914T070011Z/`。

Gurobi 调用须使用 [examples/scenario_gurobi_6_1.json](examples/scenario_gurobi_6_1.json) 的独立目标标识，明确配置 `GurobiOptions(python_executable=...)` 并注入 `SolverService(settings, gurobi_backend=...)`。第一版拒绝非空 seed 和损坏车比例；作业预算必须整除显式时间段长度。每期完整变量与原生 `.sol` 一并保存，候选验证包含库存、弧流、装卸/维修限制、时间预算、排放和线性不满意度。详见 [Gurobi adapter 调用方式](docs/gurobi-adapter.md#调用方式)。

2026-09-14 的阶段 5 通过新选择接口完成 2 次真实 HGS `6_1` 和 1 次 Gurobi 合成求解，
并只读重放 12 个历史运行。HGS 资源/库存变体可生成观测差值；HGS 与 Gurobi 的
`6_1` 返回不可比且不计算差值。验收产物在 `runs/stage5-20260914T075303Z/`。

尚未实现自动改写场景、按历史目标值推荐 solver、并行/分布式批次调度、自动恢复或调参、solver 自动编译、跨 solver 最优性比较、超出已审计模型语义的物理过程验证、LLM/Agent SDK、RAG、多 Agent、HTTP API、Web UI、数据库、消息队列或云部署。

## 测试

```bash
.venv/bin/python -c "import mobilityops; print(mobilityops.__version__)"
.venv/bin/python -m pytest
git diff --check
```
