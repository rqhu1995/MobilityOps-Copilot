# 阶段 4B：Gurobi adapter 与独立周期验证

2026-09-14 实现并验收。阶段 4A 的[语义审计](gurobi-integration-audit.md)继续作为
模型依据；本文件记录现在已实现的接口、限制和实际证据。两个 solver core 均保持原样。

## 调用方式

已提供复用本 adapter 的[终端交互入口](terminal.md#启动-gurobi-交互)，可显式配置
Python 路径、时间段长度和线程数，审阅确认后执行一次求解与独立报告。以下 Python API 继续可用。

项目 `.venv` 不需要安装 gurobipy、NumPy 或 pandas。父进程处理契约、解析和验证，
子进程使用明确配置的既有 Gurobi Python 环境。仅在注入 Gurobi backend 时注册
该 backend；默认 service 仍可单独运行 HGS，无需有效 Gurobi 环境。

```python
from pathlib import Path
from uuid import uuid4

from mobilityops.config import Settings
from mobilityops.domain import BackendName, ScenarioSpec
from mobilityops.services.solver_service import SolverService
from mobilityops.solvers.gurobi.backend import GurobiBackend
from mobilityops.solvers.gurobi.contracts import GurobiOptions

root = Path("/home/runqiu/MobilityOps-Copilot")
settings = Settings(
    hgs_repo_path=Path("/home/runqiu/BRPWR-HGSADC-SBC"),
    gurobi_repo_path=Path("/home/runqiu/BRPWR-Gurobi"),
    runs_dir=root / "runs",
)
backend = GurobiBackend(settings, options=GurobiOptions(
    python_executable=Path("/home/runqiu/anaconda3/envs/gurobi1301/bin/python"),
    time_interval_sec=600,
    threads=1,
    timeout_grace_sec=5.0,
))
scenario = ScenarioSpec.model_validate_json(
    (root / "examples/scenario_gurobi_6_1.json").read_bytes()
)
result = SolverService(settings, gurobi_backend=backend).solve(
    scenario, backend=BackendName.GUROBI, run_id="gurobi-" + uuid4().hex,
)
print(result.model_dump_json(indent=2))
```

可显式设置 `GurobiBackend(..., instance_directory=<绝对目录>)` 读取本地自定义
数据；目录仍须包含场景对应的 station/time/linear 文件。此选项用于合成案例和
数据接入，来源路径写入证据，数据在运行前复制到独立快照。它不改变建模函数。

## 能力与入口约束

- Gurobi 只接受 `objective_profile="gurobi_linear_default"`；HGS 仍只接受
  `eudf_default`。不会把 HGS 示例静默转成 Gurobi 场景。
- 第一版对非空 `seed` 和 `broken_bike_proportion` 明确拒绝，包括值为 0 的请求。
- 作业预算必须能被显式时间段长度整除；不向上或向下取整。支持 2..1000 个
  时间段，并限制 `(N+1)^2 * T * (4*K+R) <= 100000`，在建模前拒绝过大配置。
- 两种 solution policy 都保留用户 runtime，不额外设置 first-solution stop。
- Python 路径必须绝对且可执行；source/input 文件必须存在。检查八个实际导入
  源文件的 SHA-256，只有阶段 4A 已审计的版本可启动。source 变化需要新审计，
  不能通过普通配置跳过该检查。
- 子进程运行 `python -B worker.py`，显式 cwd、stdout/stderr 捕获、有限 timeout，
  不使用 shell。父进程外部上限为 runtime + grace，grace 为 `(0,60]` 秒，外部
  上限最多一天；包含 SDK 初始化、建模、优化和保存，超时会杀死并回收子进程。
- 子进程在 import SDK 和启动 license 环境前静默标准输出/错误，创建显式空
  `Env`、关闭日志再启动。凭据由 SDK 加载，不读取或记录 `.env`、许可证值或
  环境变量。异常仅输出类型、阶段和可用的数字错误码。
- 直接调用原有变量、约束和目标建模函数；不调用原 `Solver.optimize`、原 MIP
  start、exhaustive callback 或有损 JSON 后处理。不写外部仓库或 bytecode。

`GurobiConstraintError.records` 包含完整能力决定；路径、源码、数据检查错误抛
`GurobiPreflightError`。输入完整性/形状检查在创建输入快照后、启动子进程前执行，
无效快照会留下 `validation.json` 诊断并保留已占用 run ID。任何已占用 ID 都不可
覆盖。产物 I/O 错误抛出带 run 路径的 `ArtifactError`，不虚构保存成功。

## 产物与状态

```text
runs/<run_id>/
├── scenario.json
├── options.json                  # interpreter、time grid、threads、grace
├── command.json                  # 父进程实际 argv 数组
├── request.json                  # builder argv、runtime、源路径及 SHA-256
├── process.json                  # 输入/worker 散列、cwd、returncode、wall time
├── work/resources/datasets/N_i/  # 固定输入快照
├── stdout.log / stderr.log       # 正常 Gurobi worker 下为空
├── worker-phase.json             # license/import/build/optimize/save/completed
├── worker-result.json            # 原始 SDK 状态、指标、全部变量和模块来源
├── native.sol                    # 有 incumbent 时保存原生结果
├── validation.json               # 独立复核或未执行原因
└── solution.json                 # 标准化结果
```

JSON 产物使用临时文件后原子替换。先保存原始结果，才解析及规范化；候选被拒绝
时仍保留原始 JSON 和 `.sol`。SDK 异常可能只产生安全错误 JSON，进程被外部杀死
可能没有完整原始结果；缺失路径不会被伪造。

| 原始结果 | 标准状态 | `feasible` | 候选与指标处理 |
| --- | --- | --- | --- |
| OPTIMAL，有解且通过独立验证 | succeeded | true | 保存原目标、合法 bound/gap；最优性只针对该模型及 SDK 容差 |
| TIME_LIMIT，有解且通过独立验证 | timeout | true | 保留可行候选、目标、合法 bound/gap |
| TIME_LIMIT，无解 | timeout | null | 不读/填 incumbent 属性；仅保留可用有限 bound |
| INFEASIBLE | infeasible | false | 不返回路线、目标或 gap；不可行性是 SDK 对该模型的报告 |
| INF_OR_UNBD、UNBOUNDED、INTERRUPTED、NUMERIC 等其他状态 | failed | null | 保留原始状态和候选证据，不推广可行性 |
| 外部超时、异常退出、原始产物缺失/损坏 | timeout / failed | null | 保留已有证据，不使用半写入或不可信候选 |
| 候选复核失败 | failed | null | 保存明确诊断，规范结果中不包含被拒绝的路线或目标 |

SDK 无穷大或属性不可用时写 `null`。有解时先保存 `native.sol`，再保存全部变量。
求解线程数、`MIPGap=1e-5`、`FeasibilityTol=1e-6`、`IntFeasTol=1e-5` 均显式设置。
因此 SDK 的 OPTIMAL 可能仍有不大于目标容差的非零 gap；不会将其改写为零。
Gurobi 时间上限允许少量结束处理开销，实测 5 秒上限的 Runtime 为 5.001559 秒。
状态含义和时间属性参考[官方状态说明](https://docs.gurobi.com/projects/optimizer/en/current/reference/numericcodes/statuscodes.html)
及[TimeLimit 说明](https://docs.gurobi.com/projects/optimizer/en/current/reference/parameters.html#parameter.TimeLimit)。

## 独立验证内容

父进程验证不 import gurobipy，也不依赖 SDK 提供的约束违约报告。所有带可行
候选的结果都验证，包括 `timeout / feasible=True`。重复验证重读本地原始证据，
不信任 metadata 中已有的 `passed`。验证不会重新打开 solver 仓库。

先检查 scenario、options、实际 builder 请求、审计源码标识、输入散列和模块来源。
原生 `.sol` 支持可选模型名称头，但变量不得缺失或重复；它必须与原始 JSON 的
完整变量集合、变量值和目标一致。JSON 拒绝重复字段与非有限数字。

验证器从时间网格、资源集合和输入建立预期的完整变量集合，检查非负性、二元
上界和整数性，随后独立检查：

- 每期站点可用/损坏库存平衡、非负性和站点容量，包括 depot 库存平衡。
- 车辆可用/损坏库存的弧流、初始与末期库存、单类及总容量。
- 各类操作与资源到访关联，depot 和非 depot 的损坏车操作限制。
- 全期可用车取出盈余上限、损坏车取走与修复总量上限、维修人员累计到访限制。
- 每资源首期出发、末期返库、跨时间段流守恒和每期至多一条弧。
- stop-time 指示变量、自环、停工转移与累计时间 upper/lower 的定义和预算。
- 每条弧的排放、每资源操作用时、不满意度线性包络下界及实际模型目标。
- SDK bound 不超过该 incumbent、有限 gap 与原目标/bound 一致，以及 OPTIMAL
  与所设置的求解 gap 容差一致。下界本身来自 SDK，不是验证器重新证明的下界。

约束检查允许 `1e-6` 加浮点运算的小量 ULP 余量，整数检查允许 `1e-5`。保留
原始浮点值，整数转换仅用于通过验证的路线投影，不能直接截断来修复违反约束
的原始解。候选的 F 可以高于线性包络；验证器保留这个 slack 和原目标，不用
收紧后的分数冒充 SDK ObjVal。

`TruckRoute` / `RepairRoute` 是按时间段排序的操作投影，保留自环重复站点；
`validation.json` 中的每期弧、库存和时间变量证据是周期语义的依据。**不把期号
转换成声称精确的到站时刻**，不套用 HGS 的逐站卸车顺序规则，也不证明连续
装卸/维修完成可用性或与 HGS 的模型等价。

## 实际验收与覆盖范围

证据目录：`runs/stage4b-20260914T070011Z/`。普通 pytest 不调用真实 Gurobi，
使用小型原生候选 fixture、fake Python 进程及数值篡改覆盖失败与验证路径。

| 真实验收项 | 状态 / 可行性 | 目标 | bound | gap（比例） | 独立约束检查 |
| --- | --- | --- | --- | --- | --- |
| 合成 1 站点 | succeeded / true | 8.0331762396 | 8.0331762396 | 0 | 453 项通过 |
| 合成 1 站点、2 名维修人员 | infeasible / false | null | null | null | 无候选，不执行候选复核 |
| 合成实例，`1e-9` 秒求解上限 | timeout / null | null | null | null | 无候选，不执行候选复核 |
| 真实 6_1，5 秒求解上限 | timeout / true | 104.14143246938093 | 103.80007873300737 | 0.0032777899081993683 | 8657 项通过 |
| 合成 2 站点、2 车 / 2 维修人员 | succeeded / true | 16.0442678416 | 16.044212623999993 | 0.000003441578048446106 | 1477 项通过 |

第一项与阶段 4A 的手算目标一致。第二项的不可行性来自该模型要求两名维修人员
首期均离开 depot，同时限制每站合计只进入一次；不把这个结论推广为现实问题
不可行。第三项用于稳定触发无 incumbent 的时限分支，不是常规求解推荐时限。
最后一项演示了 SDK 在配置容差内报告 OPTIMAL，但 bound 和目标不完全相等。

每项均检查 JSON 保存/重载一致、独立验证幂等、静默日志，以及重复 run ID 拒绝
且全部文件散列不变。真实 `6_1` 的内部/外部上限为 5/10 秒；未为了获得更低 gap
延长此次运行。实际目标值不作为以后随机/限时运行必须相等的断言。

| 验收项 | `runs/` 下的 run ID |
| --- | --- |
| 合成最优 | `gurobi-tiny-optimal-20260914T071530Z-e1ee4b1e` |
| 合成不可行 | `gurobi-tiny-infeasible-20260914T071531Z-d9df088d` |
| 合成无解超时 | `gurobi-tiny-no-incumbent-20260914T071531Z-3c5f0e4c` |
| 真实 6_1 | `gurobi-6-1-20260914T071532Z-326ec303` |
| 合成多资源 | `gurobi-two-stations-multi-20260914T071820Z-28efb79c` |

阶段 5 已补充[带理由的 backend 选择与同模型情景分析](scenario-analysis.md)，保留本文的 adapter 调用方式。

未实现跨 solver 最优性比较、seed、损坏车比例转换、无维修
人员模式、CLI/Agent/API/UI，或更大实例的真实验收。所有运行产物仍由 Git 忽略。
