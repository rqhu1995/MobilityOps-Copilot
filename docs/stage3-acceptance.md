# 阶段 3 提交前审查与验收记录

日期：2026-09-11。阶段 3 的 HGS Python 端到端工作流已通过本轮审查和补充真实验收。
当前改动保留在 `codex/hgs-vertical-slice` 工作区，未自动 commit 或 push。

## 审查范围与修复

审查覆盖场景与能力检查、完整参数映射、运行目录隔离、进程时限、原始产物保存、
严格解析、独立输入与数学复核、应用层调用，以及成功/失败状态的一致性。
同时只读核对了 HGS 的库存事件、比例初始化和目标计算代码。

发现并修复一项应用层配置问题：向 `SolverService` 注入不同 `Settings` 的
`HgsBackend` 时，原先会先按 backend 的路径执行，直到 service 验证结果时才发现
运行目录不同。现在构造 service 就会抛出 `ValueError`，不创建运行产物或启动求解。
新增回归测试在修复前失败、修复后通过。

README 同步纠正了两项过时描述：真实多资源验收已完成；Gurobi 的受控许可证和
小型求解检查已获授权并验证可用。当前 `SolverService` 仍只注册 HGS。
本轮没有修改 solver core，也没有新增 adapter、依赖或框架。

## 真实验收矩阵

全部使用同一份 `6_1` 输入数据和已有 HGS executable。车辆容量 25、每辆装卸
60 秒、每辆维修 300 秒。每次求解内部上限 5 秒、外部 timeout 10 秒；每条资源路线
的作业预算单独列在表中。`None` 使用输入原有损坏车数量；显式比例会重新计算初始
损坏车数量，因此不同比例的目标值对应不同初始状态。

下表“实际作业”指在非仓库站点执行至少一项非零操作的车辆/维修人员数量。
所有配置都完整返回了指定数量的路线，包括闲置资源的 `[0, 0]` 路线。

| 验收项 | 配置：车/维修人员 | 实际作业：车/维修人员 | 损坏车比例 | 路线预算（秒） | 报告目标值 | 报告求解耗时（秒） | 结果/复核 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| control | 1/1 | 1/1 | None | 7200 | 104.039 | 1.59983 | succeeded / passed |
| two-trucks | 2/1 | 1/1 | None | 7200 | 104.042 | 1.34023 | succeeded / passed |
| two-repairers | 1/2 | 1/1 | None | 7200 | 104.038 | 1.82982 | succeeded / passed |
| two-each | 2/2 | 1/2 | None | 7200 | 104.038 | 1.03629 | succeeded / passed |
| ratio-030 | 1/1 | 1/1 | 0.3 | 7200 | 103.983 | 1.80187 | succeeded / passed |
| ratio-000 | 1/1 | 1/0 | 0 | 7200 | 104.037 | 0.800767 | succeeded / passed |
| ratio-100 | 1/1 | 1/1 | 1 | 7200 | 103.882 | 1.16832 | succeeded / passed |
| two-each-1h | 2/2 | 2/1 | None | 3600 | 104.116 | 1.5579 | succeeded / passed |

`two-each-1h` 中，两辆车都在站点 3 执行了非零操作，共享库存时序复核通过。
车辆完整用时分别为 2870 和 3190.8 秒；两名维修人员分别为 2000.64 和 0 秒，
均满足 3600 秒预算。`two-each` 中两名维修人员均实际作业，用时分别为
2103.816 和 2000.64 秒。

比例为 `None`、0、0.3、1 时，六个站点复核得到的初始损坏车数量依次为
`[1,5,5,6,3,0]`、`[0,0,0,0,0,0]`、`[3,0,5,0,0,1]`、`[8,0,15,0,0,1]`。
比例 0 的实际输出保留了一条闲置维修路线，验证了显式零值与参数缺省的区别。

每组验收均检查了以下内容：

- 正常退出，`succeeded` / `feasible=True`，有限主指标，bound/gap 为 `None`。
- 实际参数数组与该组配置一致，输入快照及 SHA-256 完整，产物均属于本次 run。
- 原生输出与规范副本逐字节一致；原始日志、标准化结果和 `validation.json` 保存完整。
- 6 个站点的库存时间线、全部路线预算，以及 13 项指标比较通过。
- 保存的 `SolutionResult` 可以重新加载，等于 service 返回值；重复验证结果不变。
- 重用 run ID 被拒绝；该 run 中全部文件的 SHA-256 保持不变。

这些数值是本次运行的观察记录，不是重复运行时要求相等的断言，也不是资源配置
优劣的基准测试。HGS 使用系统时间初始化随机数，并可能在内部上限前结束。

## 本地证据索引

审查目录为 `runs/stage3-review-20260911T143532Z-9602a9c9/`，包含开始状态
`baseline.json`、有界验收脚本 `acceptance.py`、逐组检查与文件散列
`acceptance.json`，以及最终检查记录 `final-verification.json` 和 `pytest.log`。
这些产物由 Git 忽略，不随 checkout 分发。

| 验收项 | `runs/` 下的运行目录 |
| --- | --- |
| control | `hgs-review-control-20260911T143641Z-4cd5fb75` |
| two-trucks | `hgs-review-two-trucks-20260911T143642Z-7c11942c` |
| two-repairers | `hgs-review-two-repairers-20260911T143644Z-27fb61fa` |
| two-each | `hgs-review-two-each-20260911T143645Z-2de5900b` |
| ratio-030 | `hgs-review-ratio-030-20260911T143647Z-237328b6` |
| ratio-000 | `hgs-review-ratio-000-20260911T143648Z-2c8640fd` |
| ratio-100 | `hgs-review-ratio-100-20260911T143649Z-171f8511` |
| two-each-1h | `hgs-review-two-each-1h-20260911T143744Z-1dc99563` |

## 最终检查与阶段边界

完整 pytest：214 项通过。包导入成功，版本为 `0.1.0`。已检查 tracked 和 untracked
文本改动的空白/补丁格式。HGS 原有 executable、`6_1` 数据和既有结果的 SHA-256
未变；本轮开始前的 73 个本地证据文件未变。两个 solver 仓库工作区均保持干净，
三个仓库 HEAD 均未改变：

| 仓库 | HEAD |
| --- | --- |
| MobilityOps-Copilot | `c0636a83b7d701645e489b3c9d2c7c067cc69931` |
| BRPWR-HGSADC-SBC | `01a0766e6f3f199d4a850ad9c9478dd024208edf` |
| BRPWR-Gurobi | `93c2f3dc58f962811f22000ba21c7975aaed048d` |

真实验收仅覆盖 `6_1` 的上述八种配置。其他规模实例、所有多资源组合、长期运行、
数学最优性，以及维修完成时刻和连续装卸过程等物理语义，均没有由本次验收证明。
失败/超时/损坏输入等路径由独立 fake 进程和单元测试覆盖；真实八组都成功，
没有声称制造了真实 HGS 的每一种失败。

下一阶段建议先审计 Gurobi 的调用入口、输入与结果语义，核对两个 solver 的
目标函数、时间单位、库存和维修事件规则，再推进 Gurobi adapter 及统一状态映射。
Gurobi 可用性检查已通过，adapter 开发仍需在进入新阶段时同步更新 `AGENTS.md`。
