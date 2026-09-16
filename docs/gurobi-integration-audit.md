# 阶段 4A：Gurobi 集成前审计

后续状态（2026-09-14）：阶段 4B 已按本审计完成实现与验收，当前接口见
[Gurobi adapter](gurobi-adapter.md)。下文保留 2026-09-13 的审计事实和当时的方案，
其中“尚未实现”与阶段边界描述属于该历史时间点。

审计日期：2026-09-13。阶段 3 已完成；本轮完成入口、输入、模型语义和结果协议审计，
并验证了在隔离目录调用原有建模函数的可行性。当前产品代码仍只有 HGS adapter。
本文件后半部分是阶段 4B 的实现方案，不表示相关接口已经实现。

## 结论

Gurobi 原有建模函数可由本项目控制的独立 Python 进程复用，无需修改或复制 solver
core。合成小实例的真实探测已通过，现有 Python 环境的所需依赖也可用。

HGS 与 Gurobi 不能直接作为同一个数学模型比较：虽然目标权重和排放系数一致，
但时间索引、维修行驶时间取整、库存更新和不满意度表示存在差异。实测 `6_1`
两个仓库的 14 个对应输入文件逐字节相同，仍有 452 个容量允许的整数库存状态
在两种不满意度计算方式下相差超过 `1e-6`。不得将 Gurobi 的 bound/gap 直接解释
为 HGS 解的质量证明，也尚未证明两种模型之间存在可用于转移下界的松弛关系。

## 审计版本和复用入口

| 仓库 | 审计 HEAD |
| --- | --- |
| MobilityOps-Copilot | `c0636a83b7d701645e489b3c9d2c7c067cc69931`，含尚未提交的阶段 3 工作 |
| BRPWR-HGSADC-SBC | `01a0766e6f3f199d4a850ad9c9478dd024208edf` |
| BRPWR-Gurobi | `93c2f3dc58f962811f22000ba21c7975aaed048d` |

Gurobi 原入口为 `main.py → Solver → addVars/addConstrs/setObj/optimize/save_result`
并调用 `utils.parse_sol_file`。以下入口行为影响直接集成：

| 已核实的入口行为 | 影响与处理方案 |
| --- | --- |
| `parameters.config` 和 `parameters.dataloader` 在 import 时解析参数，后者立即读相对路径数据 | 每次运行必须使用独立子进程，先设置参数与 cwd，再导入；不能在同一解释器中反复换场景 |
| `Solver.__init__` 直接创建默认 `gp.Model()` | 无法在该构造器里传入预先静默启动的环境；由本项目创建 `Env` 和 `Model(env=env)`，再调用原建模函数 |
| `Solver.optimize` 的普通模式强制 `TimeLimit=7200` | 不能用现有入口传递用户求解时限；在模型构建后直接设置 SDK `TimeLimit` 并调用 `model.optimize()` |
| 优化结束后先访问 `objVal`，之后才读取状态，未检查 `SolCount` | 无 incumbent 的超时或不可行结果可能在读取目标时异常；应先保存状态与解数量，再有条件读取候选属性 |
| 日志和结果写往相对的 `resources/logs`、`resources/solutions`；summary 采用追加方式 | 新运行由本项目管理唯一目录与原始产物，不调用现有保存流程 |
| exhaustive callback 在长时间运行后访问 `_log_file`，已审计入口未设置该属性 | 第一版不调用该 callback；不执行长时间运行去触发这一分支 |
| 原 JSON 后处理合并自环、删除零操作，并在部分合并中调用 `int()` | 不作为确定性验证的原始证据；保留未经合并的全部时间索引变量 |

证据：[参数及数据导入](/home/runqiu/BRPWR-Gurobi/parameters/dataloader.py:5)、
[模型创建与优化](/home/runqiu/BRPWR-Gurobi/model/model.py:30)、
[长时间 callback](/home/runqiu/BRPWR-Gurobi/model/callback.py:6)、
[结果后处理](/home/runqiu/BRPWR-Gurobi/utils/utils.py:57)。上述原入口风险来自源码审计；
本轮实际执行的是原建模函数的小实例探测，没有执行这些长期入口分支。

已验证的复用方式：在独立进程创建带显式环境的模型，将它放入具有 `m` 属性的
对象，顺序调用原 `model.variables`、`model.constraints`、`model.objective` 中的
建模函数。探测未调用 `Solver.optimize`、MIP start、callback 或原后处理，未替换
Gurobi SDK 全局函数。搜索策略与原主入口可能不同，数学建模函数保持原样。

## 字段映射与第一版建议

以下为阶段 4B 的具体建议。参数映射以
[parameters/config.py](/home/runqiu/BRPWR-Gurobi/parameters/config.py:12) 为准。

| 当前场景字段 | Gurobi 建模输入或 SDK 参数 | 建议约束 |
| --- | --- | --- |
| `instance_id` | `-i / --inst_no` | 使用指定 `resources/datasets/<N>_<id>` 快照 |
| `number_of_stations` | `-S / --n_station` | 不含 depot；站点表顺序映射内部编号 |
| `number_of_trucks` | `-K / --n_truck` | 初版保持严格正整数 |
| `number_of_repairers` | `-R / --n_repairer` 和 `-r True` | 当前域模型只支持正数；无维修人员分支另行处理 |
| `truck_capacity` | `-Q / --truck_capacity` | 统一车辆容量 |
| `loading_time_sec` | `-tl / --t_load` | 注意：这里的 `-tl` 是装卸时间，与 HGS 的 `-tl` 求解时限不同 |
| `repair_time_sec` | `-tr / --t_repair` | 每辆维修时间 |
| `operation_time_budget_sec` | `-NT * -tau` | 增加明确的 backend 时间段长度选项，建议默认 600 秒；预算不能整除时拒绝，不能静默取整 |
| `solver_runtime_limit_sec` | SDK `TimeLimit` | 保留用户时限；单独记录建模时间和整个进程的外部 timeout |
| `broken_bike_proportion` | 原入口没有此参数 | 第一版仅支持 `None`，其他值在启动前拒绝；后续若支持必须保存明确的数据转换记录 |
| `seed` | 原入口只设置 NumPy seed 为实例号，没有传入 SDK Seed | 第一版先明确拒绝非空请求；后续支持 SDK seed 时独立检查范围并记录，不声称墙钟限时结果完全可复现 |
| `objective_profile` | 线性不满意度模型 | 建议新增显式 `gurobi_linear_default`，保留 HGS 的 `eudf_default`；不要静默把旧场景映射为另一种计分方式 |
| `solution_requirement` | 受控 SDK 优化策略 | 两种策略均遵守相同用户时限；第一版不额外设置 first-solution stop，也不复用原入口偏向特定路线的 MIP start |

时间段长度、线程数、求解容差属于 backend 配置，不新增用户无关的 GA 或实验
标签参数。完整、实际生效的配置应写入运行证据。

## 模型差异与数值证据

| 比较项 | HGS 当前模型/复核 | Gurobi 当前建模 |
| --- | --- | --- |
| 基础输入 | 时间矩阵、站点表、`dissat_table`；搜索另用 priority tables | 时间矩阵、站点表、`linear_diss`；不读取 `dissat_table` |
| 维修行驶时间 | 原矩阵 × 1.68 | `ceil(原矩阵 × 1.68)` 秒 |
| 时间结构 | 从零累加行驶和服务时间，无等待 | 时间索引 `t=1..NT`；移动跨越 `ceil(travel/tau)` 个时间段，零距离自环也占一个时间段 |
| 库存时序 | 到站事件原子更新，检查中间库存与不明确的同时到达顺序 | 同一时间段汇总全部资源操作，检查每期库存；不是逐事件或维修完成时刻的库存保证 |
| 时间预算 | 每条路线完整 travel + service，包括最终卸车 | 累计行驶/服务的 upper/lower 约束和 stop-time 变量，结合时间索引流守恒；不能直接把 `t*tau` 当作精确到站时间 |
| Depot 初始可用车 | HGS 输入使用 `INT_MAX`，当前复核按供应 depot 处理 | 初始可用车为 10000，亦参与库存平衡 |
| 不满意度 | 最终整数库存直接查表 | `F_i >= alpha*p_i + beta*b_i + gamma`，同时 `F_i >= 0` |
| 排放 | `2.61*(0.252+0.0003*load)*travel/60*0.42` | `2.61*(0.252*x+0.0003*flow)*travel/3600*25.2`；对应弧上的系数代数一致 |
| 目标权重 | `2*dissat + 0.06*emission + 1e-8*总行驶服务时间` | 权重相同，但组成值与可行域不保证相同 |

证据：[Gurobi 时间取整](/home/runqiu/BRPWR-Gurobi/parameters/dataloader.py:46)、
[库存方程](/home/runqiu/BRPWR-Gurobi/model/constraints.py:7)、
[时间约束](/home/runqiu/BRPWR-Gurobi/model/constraints.py:464)、
[排放及线性不满意度](/home/runqiu/BRPWR-Gurobi/model/constraints.py:697)、
[目标函数](/home/runqiu/BRPWR-Gurobi/model/objective.py:6)。

Gurobi 还明确限制：全期从站点取出的可用车不超过初始可用车相对目标的盈余，
不允许向非 depot 卸损坏车，取走与修复的损坏车合计不超过初始数量，所有维修
人员合计至多一次从其他节点进入每个站点；每名维修人员第一期须离开 depot。
这些限制见 [装卸约束](/home/runqiu/BRPWR-Gurobi/model/constraints.py:240) 和
[路线及到访约束](/home/runqiu/BRPWR-Gurobi/model/constraints.py:363)。当前 HGS
独立验证器并未声明检查全部这些限制；本轮没有据此声称 HGS 搜索器在所有情形
下都允许相反操作。

本轮枚举 `6_1` 每站所有 `usable >= 0, broken >= 0, usable+broken <= capacity`
的整数库存，比较查表值与 `max(0, max(alpha*usable+beta*broken+gamma))`：

| 内部站点 | 枚举状态数 | 差异大于 `1e-6` 的状态数 | 最大绝对差 |
| --- | --- | --- | --- |
| 1 | 300 | 22 | 0.027507 |
| 2 | 406 | 41 | 0.000371666667 |
| 3 | 820 | 15 | 0.012354 |
| 4 | 1176 | 13 | 0.0218575 |
| 5 | 820 | 356 | 0.184963615385 |
| 6 | 595 | 5 | 0.0005245 |
| 合计 | 4117 | 452 | 0.184963615385 |

例如站点 5 在 `(usable=5, broken=21)` 时，查表为 `59.298054`，线性下界包络
为 `59.113090384615376`。这只是容量允许的库存点，不是已经证实可由当前
`6_1` 路线到达的状态；不能用这张表推断实际最优目标差。观察到的差异也不能
单独证明完整 Gurobi 模型是 HGS 模型的松弛。

另一个直接差异：depot 到站点 1 的车辆时间为 561.1 秒，维修人员在 HGS 中为
942.648 秒，在 Gurobi 中为 943 秒。全矩阵的维修取整最大增量为 0.96 秒。

对于提前停止的 Gurobi incumbent，`F_i` 可能高于线性包络，因为它受下界约束
而不是等式约束。未来验证应检查该不等式，并用实际 `F_i` 重算原始模型目标；
可以另报收紧后的包络分数，但不能悄悄替换 `ObjVal` 后继续附上原 `MIPGap`。

## 原始结果、状态与独立验证方案

以下状态映射为阶段 4B 设计；产品中尚未注册 Gurobi。
先读取 `Status`、`SolCount`、`Runtime`，再按可用性读取 `ObjVal`、`X`、`ObjBound`
和 `MIPGap`。不读取不存在的 incumbent 属性，不把 SDK 的无穷大哨兵写成有限
数学下界。状态定义依据 [Gurobi 官方状态文档](https://docs.gurobi.com/projects/optimizer/en/current/reference/numericcodes/statuscodes.html)，
属性含义依据 [官方模型属性文档](https://docs.gurobi.com/projects/optimizer/en/current/reference/attributes/model.html)。

| SDK/进程观察 | 建议标准化状态 | `feasible` | 原始解与指标 |
| --- | --- | --- | --- |
| OPTIMAL，存在 incumbent，独立验证通过 | `succeeded` | true | 保留原 ObjVal、合法 bound/gap、原始变量及验证报告；最优性仅针对该模型和求解容差 |
| TIME_LIMIT，存在 incumbent，独立验证通过 | `timeout` | true | 保留已验证候选及可用 bound/gap；明确终止原因是时限 |
| TIME_LIMIT，没有 incumbent | `timeout` | null | 目标、路线、gap 为空；仅保留确实可用的有限 bound |
| INFEASIBLE，正常返回 | `infeasible` | false | 保留原始状态；不伪造路线、目标或 gap；这不是 HGS/现实问题不可行的证明 |
| INF_OR_UNBD / UNBOUNDED | `failed` | null | 保留诊断，不能当作已证明不可行 |
| INTERRUPTED、NUMERIC 或不支持的终止原因 | 第一版 `failed` | null | 保留原始候选用于诊断，不自动宣称成功 |
| 进程外部超时、异常退出、证据不完整 | `timeout` / `failed` | null | 保留已有原始产物，不根据半写入文件推广可行性 |
| 候选独立验证失败 | `failed` | null | 保留原始候选及错误，规范结果不推广该路线/目标 |

当前域模型允许 `timeout` 与 `feasible=True` 同时存在，但未来验证分派必须覆盖
**所有含可行候选的结果**，不能只在 `status=succeeded` 时复核。现有 validator
中的逐站卸车顺序规则源于 HGS；Gurobi 的每期汇总库存应进入独立的 backend
验证分支，不得直接套用 HGS 原子到站验证器。

未来原始证据至少保存：完整 scenario/backend options、源文件及输入 SHA-256、
导入模块路径、SDK 版本、实际参数、进程阶段及用时、状态/解数量、`.sol`、全部
未合并的时间索引弧与操作、站点与车辆库存、F/排放/辅助时间变量、SDK 容差及
质量属性。普通日志保持静默；仅将白名单属性和数值写入 JSON。SDK 允许显式
环境传入模型，见 [Env 文档](https://docs.gurobi.com/projects/optimizer/en/current/reference/python/env.html)。

确定性验证必须从本次输入快照重建：每资源每期弧选择与移动流、装卸/维修和
库存平衡、容量、初始库存累计限制、维修到访限制、所有时间 upper/lower/stop
约束，以及 F 下界、排放、操作时间和总目标。整数转换使用已记录的整数容差，
不能直接 `int()` 截断。保留时间段、自环和库存变量作为权威证据；通用路线只能
是附带的显示投影，不能丢失语义后再作为验证依据。SDK 的违约量为零是辅助
证据，不能替代独立验证，也不等于真实连续作业可行性。

## 实际探测与证据

使用现有 `/home/runqiu/anaconda3/envs/gurobi1301/bin/python`，已加载 Gurobi
13.0.1、NumPy 2.5.1、pandas 3.0.3。项目 `.venv` 没有新增这些依赖。
所有探测使用 `-B`、独立 cwd 和一个显式静默环境；SDK 自行加载许可证，脚本
不读取许可证或 `.env`。原生 stdout/stderr 在初始化前被屏蔽，输出仅为白名单
JSON。整个探测进程外部上限 15 秒，各次优化上限不超过 2 秒。

| 实际探测 | SDK 状态 | 解数量 | 说明 |
| --- | --- | --- | --- |
| 单变量二元最大化 | OPTIMAL (2) | 1 | 目标 1，下界 1，gap 0 |
| 二元变量被要求等于 2 | INFEASIBLE (3) | 0 | 未读取 incumbent 属性 |
| TimeLimit=0 的单变量模型 | TIME_LIMIT (9) | 0 | 用于直接触发 SDK 边界；不是允许零时限的 ScenarioSpec |
| 原建模函数，合成 1 站点、4 时间段、1 车/1 维修人员 | OPTIMAL (2) | 3 | 165 变量、245 约束；目标及 bound 均为 8.0331762396，gap 0 |

最后一组初始可用/损坏库存为 1/1，容量 3，往返单程车辆时间 60 秒，维修
单程取整为 101 秒，采用合成下界 `F >= -2*usable + 3*broken + 10`。
结果在第 2 期卸一辆可用车并维修一辆车，最终库存为 3/0，F=4。
排放合计 0.55281366；车辆行驶/服务为 120/120 秒，维修行驶/服务为 202/300 秒，
手算目标 `2*4 + 0.06*0.55281366 + 1e-8*742 = 8.0331762396`，与 SDK 一致。
SDK 报告 ConstrVio、BoundVio、IntVio 均为零。

该结果证明原建模函数可复用，不表示完整 Gurobi adapter、`6_1` 实例求解、
大型模型授权容量、时限下已有 incumbent、所有状态分支或独立周期验证器已经
通过验收。未执行原主入口、exhaustive callback 或原 JSON 后处理。

本地证据根目录：`runs/stage4a-audit-20260913T082102Z-eaf16585/`。

- `baseline.json`：三个仓库状态、项目已有改动、67 个 solver 文件及 310 个旧证据文件的散列。
- `compare_inputs.py`、`comparison-inputs/`、`input-comparison.json`：输入快照和整数库存枚举。
- `probe.py`、`tiny-probes/`：合成数据、命令、结果 JSON、原始 `.sol`/`.lp`、进程记录。
- `tiny-verification.json`：合成案例的独立库存、组成值与目标复算。
- `pytest.log`、`final-verification.json`：项目回归与仓库完整性检查。

## 阶段 4B 的交付顺序

1. 同步更新阶段边界，明确允许 Gurobi adapter；增加独立 objective profile、时间段配置和能力拒绝规则。
2. 实现受控子进程桥接：预先验证并复制数据、静默显式环境、禁用 bytecode、直接调用已审计建模函数、记录来源及原始证据。
3. 实现严格的原始变量解析、SDK 状态映射和独立周期模型验证；复核通过后才能返回可行候选。
4. 在 `SolverService` 显式注册 Gurobi，覆盖最优、有解超时、无解超时、不可行、异常退出、损坏证据和模型差异拒绝；普通 pytest 使用 fixtures/fake。
5. 先验收合成可手算实例，再运行受控真实 `6_1`。只有证明模型可比后，才开展跨 solver 的质量比较或自动选择。

阶段 4A 保留 `AGENTS.md` 中 Gurobi adapter 的实现禁令，交付审计与具体实现
方案。本轮三个仓库 HEAD 均未改变，两个 solver 工作区及受审计文件保持原状，
已有 310 个本地证据文件未变；完整项目回归 214 项通过。没有 commit 或 push。
