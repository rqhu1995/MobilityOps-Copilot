# 阶段 8：终端交互入口

阶段 9 新增批次入口：在相同命令中加入 `--mode experiment`，即可使用
[自然语言实验计划流程](natural-language-experiments.md)。默认 `--mode scenario` 保持本页的
单场景行为；两种模式使用独立会话、预算和确认契约，不能用单场景确认启动实验计划。

在终端完成：**输入需求 → 澄清 → 审阅场景和预算 → 明确确认 → 单次求解与报告**。
入口复用阶段 7 的 `ScenarioIntakeService`、Gemini REST 接入和现有实验/报告服务，
使用标准库，不新增依赖。默认注册 HGS；通过 `--gurobi-python` 显式配置后也注册 Gurobi。

## 启动（fish）

在仓库根目录运行以下命令。前三个变量是路径，不是凭据；第四行把当前 fish
已经设置的 `GEMINI_API_KEY` 导出给 Python 子进程，不显示或复制密钥。
fish 的 `set -g` 仅设置作用域，`set -gx` 同时设置导出属性。

```fish
set -gx HGS_REPO_PATH /home/runqiu/BRPWR-HGSADC-SBC
set -gx GUROBI_REPO_PATH /home/runqiu/BRPWR-Gurobi
set -gx MOBILITYOPS_RUNS_DIR /home/runqiu/MobilityOps-Copilot/runs
set -gx GEMINI_API_KEY $GEMINI_API_KEY
.venv/bin/python -m mobilityops --baseline examples/scenario_6_1.json --backend hgs
```

也可通过 `--hgs-repo-path`、`--gurobi-repo-path`、`--runs-dir` 提供绝对路径，优先于
同名环境配置。不会加载 `.env` 或读取 fish 配置文件。
`--baseline` 是用户显式选择的基准，省略时从空场景开始澄清，不自动补齐实例或资源。
基准示例是 `6_1`、1 车、1 维修人员、容量 25、作业预算 7200 秒、求解时限 5 秒。

`.venv/bin/python -m mobilityops --help` 不需要配置、凭据或交互终端，不调用任何服务。
重新进行 editable install 后也可使用 `.venv/bin/mobilityops`；模块入口无需重新安装。

### 启动 Gurobi 交互

已按上文导出路径和密钥后，使用已有 Gurobi Python 环境及独立目标的基准文件：

```fish
.venv/bin/python -m mobilityops \
    --baseline examples/scenario_gurobi_6_1.json \
    --backend gurobi \
    --gurobi-python /home/runqiu/anaconda3/envs/gurobi1301/bin/python \
    --gurobi-time-interval-sec 600 \
    --gurobi-threads 1
```

`--gurobi-python` 必须是绝对路径，不从当前 Python、环境名称或历史记录推测。
注册和展示能力不导入 `gurobipy`，也不启动许可证环境。`/run` 的只读预检查检查
解释器文件、原建模源码散列、输入数据与预算；实际许可证由确认后的既有受控子进程加载。

| 参数或要求 | 行为 |
| --- | --- |
| `--gurobi-python` | 显式注册已有 adapter；缺少该参数却指定 `--backend gurobi` 时，在创建会话前提示配置错误 |
| `--gurobi-time-interval-sec` | 正整数秒，默认 600；作业预算须整除它，并满足 adapter 的周期数与模型规模限制 |
| `--gurobi-threads` | 1–64，默认 1；记录在审阅与原始运行配置中 |
| `--backend gurobi` | 明确指定 Gurobi，不兼容时拒绝；省略 backend 时仅按能力选择唯一兼容者 |
| `gurobi_linear_default` | Gurobi 的独立目标标识；不会自动改写 HGS 的 `eudf_default`，两种模型的目标值不可直接比较 |

Gurobi 示例仍为 `6_1`、1 车、1 维修人员、容量 25、作业预算 7200 秒、内部时限 5 秒，
采用 `best_available`；默认外部 timeout 10 秒。可以输入 `把容量提高到30，其他沿用基准`，
审阅后执行。非空 seed、损坏车比例和不能被时间段长度整除的作业预算会被拒绝。
时间段长度和线程数由启动参数设置，当前不通过自然语言改写。

## 一次会话怎么操作

1. **确认 LLM 预算。** 屏幕显示模型、次数、费用上限、数据发送范围和本地留存；输入
   `同意` 才启用提取。默认 1 HKD、最多 3 次；每次保守预留 0.21888 HKD，三次合计
   0.65664 HKD。可用 `--budget-hkd`（不超过 10）与 `--max-calls`（不超过 6）收紧或
   显式调整。费用先耗尽时会提前停止，不自动增加额度；每次启动是新的独立预算，
   不重置或合并阶段 7 的历史账本。
2. **输入需求。** 例如 `把容量提高到30，其他沿用基准`。程序显示全部字段、字段来源、
   相对基准的变化、能力选择和待澄清问题。继续输入补充说明会形成新版本，每轮提取
   消耗一次额度；查看命令不消耗额度。
3. **审阅执行请求。** 输入 `/run`，先检查 executable、实例、计划时限和运行 ID；
   检查通过后显示完整场景、所选 backend、内部时限、外部 timeout、一次调用预算和运行配置。
   示例的内部/外部时限是 5/10 秒；外部由所选 adapter 的退出余量计算，CLI 两套 adapter 均默认 5 秒。
4. **明确确认。** 输入屏幕显示的 `执行 <12位确认码>`。确认绑定完整请求和所选 backend 的运行配置；
   普通 `yes`、空输入和自然语言中的“执行”均不会启动求解。其他输入取消本次确认；
   修改需求应回到 `需求或命令 >` 输入。每次修改都需要重新审阅、重新确认；
   Python/executable 路径、时间段长度、线程数或运行路径变化后，旧确认同样失效。
5. **查看结果。** 程序显示实际状态、通过独立复核的候选数、单样本描述统计及
   JSON/Markdown 报告的绝对路径。Gurobi 有通过复核的候选时，还展示本次周期模型的
   best bound 和 optimality gap；没有候选或复核失败时不展示候选指标。每会话最多执行一次，之后结束。

## 命令

| 输入 | 行为 | 新增 LLM 调用 |
| --- | --- | --- |
| 普通文字 | 新需求或补充说明，生成新草稿 | 1 次提取 |
| `/review`、`/help` | 查看当前候选或帮助 | 0 |
| `/run` | 生成请求、预检查、等待针对当前请求的确认 | 0 |
| `/withdraw 1 撤回时间窗要求` | 按当前显示编号明确撤回未支持要求，并重新提取 | 1 次提取 |
| `/quit` | 保存会话后退出 | 0 |

每次提取先发一次 `countTokens`，计数通过再发一次 `interactions`；默认每个 HTTP 请求
超时 30 秒，无自动重试。每次最多 16000 输入 token、4096 输出 token（含思考）；
沿用已有定价有效期检查和账本锁。失败保留已预留额度，预留及 usage 估算不等于账单。

## 证据与退出状态

所有产物在 `MOBILITYOPS_RUNS_DIR` 下；会话 ID 默认由 UTC 时间和随机后缀组成，
可用 `--session-id` 明确指定，但重复 ID 会拒绝，不覆盖已有目录或预算。

```text
runs/
  terminal-sessions/<session-id>/
    session.json                   # 显式基准、backend、预算和边界
    turn-001-input.json            # 输入和撤回动作，模型调用前保存
    turn-001-draft.json            # 已校验候选、来源与澄清
    review-001-request.json        # 完整执行计划与指纹
    review-001-execution.json      # 请求 + backend 运行配置 + 审阅指纹
    review-001-precheck.json       # 执行前检查
    state.json / events.jsonl      # 最新状态和追加事件
  llm/terminal-<session-id>/        # 每次 HTTP 请求、响应与预留账本
  language-requests/terminal-<session-id>/
  experiments/terminal-<session-id>/
  experiment-reports/terminal-<session-id>/report.json、report.md
  terminal-<session-id>-c001-r0001/ # 原始 solver 输出和独立复核
```

执行确认写入状态后才调用原服务。缺失字段、未支持要求、不兼容 backend 和失败的
预检查都阻止执行；新的模型调用失败时立即结束会话，不能退回旧候选继续执行。
EOF、Ctrl-C 保留当前状态，重启不会自动重试模型或 solver，也不会自动恢复确认。
进程被强制终止时，以最近持久化检查点为准，需要检查执行证据，不能把未写入终态
理解为没有调用过 solver。

| 退出码 | 含义 |
| --- | --- |
| `0` | 正常退出/取消/EOF，或已生成报告且有通过复核的成功或有候选超时结果 |
| `1` | LLM/运行失败，或报告中没有通过复核的成功/有候选超时结果 |
| `2` | 启动参数、路径、文件或终端条件错误 |
| `130` | Ctrl-C 中断 |

`state.json` 的 `reported` 表示报告已生成。实际运行状态另存 `outcome`，仍区分
`succeeded`、`timeout_with_candidate`、`timeout_no_candidate`、`failed`、
`validation_failed` 等；没有候选时不填入零值。Markdown 报告链接到逐次运行证据。

`review-001-execution.json` 的 `review_sha256` 绑定请求与运行配置，当前终端确认码取其
前 12 位；状态中的 `confirmed_review_sha256` 保存完整确认值。内层请求保留原有
`request_sha256`，供 `ScenarioIntakeService` 再次验证。历史 HGS 首版会话只有请求指纹，
原文件保持不变；不将历史确认自动升级成新审阅确认。

## 验收与范围

2026-09-16 完整 **534 项普通测试通过**，其中新增终端测试 **36 项**，耗时 12.22 秒。
终端测试使用 fake HTTP 与隔离的 fake executable，覆盖从交互到独立复核报告
的流程，以及取消、澄清、撤回、旧确认拒绝、预算耗尽、失败、重复 ID 和中断。
上述开发验收未新增远端 LLM 或真实 solver 调用；阶段 7 的六个真实 Gemini 案例和一次真实
HGS 执行仍是已有底层服务的验收证据，见 [阶段 7 文档](natural-language-intake.md)。

实际 PTY 终端启动与取消路径已通过：
[本机会话状态](../runs/terminal-sessions/20260916T085520Z-1927e626/state.json)。
本次在预算确认前取消，没有创建 LLM 预算或启动求解。该本地产物被 Git 忽略。

### Gurobi 终端扩展验收

2026-09-16 完整 **571 项普通测试通过**，其中本次新增 **37 项**，全量耗时 14.13 秒。
Gurobi 终端测试复用已有原始结果 fixture 和 fake Python 子进程，覆盖从 CLI 参数、
fake HTTP 提取到确认、实际子进程桥接、独立周期模型复核和 JSON/Markdown 报告的流程。
另覆盖两种超时、不可行、许可证错误、复核失败、能力拒绝、源码/数据/解释器缺失、
配置变化导致旧确认失效，以及注册 Gurobi 后 HGS 仍可执行。

本机已有 Python 路径、Gurobi 已审计源码和 `6_1` 数据已通过只读预检查，内部 5 秒、
外部 10 秒匹配。此检查不启动许可证环境；上述开发验收阶段真实 LLM、solver 调用均为 0，
没有用离线测试替代真实许可证/求解可用性验证。55 个既有终端证据文件与两个 solver
仓库状态在只读检查前后保持不变。

[本机配置和证据检查汇总](../runs/stage8-gurobi-terminal-20260916T092024Z-1ac3955e/summary.json) ·
[预检查明细](../runs/stage8-gurobi-terminal-20260916T092024Z-1ac3955e/precheck.json)

### 用户完成的真实终端验收

2026-09-16 用户在 fish 中完成会话 `20260916T090049Z-c25da014`：确认 1 HKD／最多
3 次提取预算，输入“把容量提高到30，其他沿用基准”，审阅后确认一次 HGS 执行。
实际调用 Gemini 1 次、HGS 1 次，无重试；只有容量从 25 变为 30，其余场景字段保留。

结果为 `succeeded / feasible=True`，1 个候选通过独立复核。目标值 104.04，
不满意度 51.7808，排放指标 7.97731；求解器耗时 1.41653 秒，批次耗时约 1.45342 秒。
内部上限 5 秒、外部 timeout 10 秒。LLM 预留 0.21888 HKD，按返回 usage 及已保存
定价估算 0.013878 HKD；估算不等于实际账单。

后续只读复核重新解析原始模型响应、构造草稿与执行请求、核对确认指纹，并从原始
结果和输入快照重做独立复核。重算报告与保存报告完全一致；55 个相关证据文件的
散列以及两个 solver 仓库状态在本次只读复核前后保持不变。复核未调用模型或 solver。

[原始运行报告](../runs/experiment-reports/terminal-20260916T090049Z-c25da014/report.md) ·
[独立验证记录](../runs/terminal-20260916T090049Z-c25da014-c001-r0001/validation.json) ·
[只读验收结果](../runs/stage8-terminal-acceptance-20260916T090351Z-b262b3a6/summary.json)

这次验证终端完整成功路径；澄清、拒绝、取消和中断的终端分支仍由上述离线测试覆盖。
报告内“相对基准变化：无”指单次实验以候选本身作为基准；自然语言入口相对于载入
场景的容量变化保存在会话草稿中。本次没有重新求解容量 25 场景，不生成两种容量的效果差值。

### 用户完成的真实 Gurobi 终端验收

2026-09-16 用户在 fish 中完成会话 `20260916T092552Z-12512c5a`：确认 1 HKD／最多
3 次提取预算，Gemini 提取 1 次，将容量从 25 改为 30，其余基准字段保留；随后确认
含运行配置的审阅指纹，执行 Gurobi 1 次，无重试。配置为时间段长度 600 秒、线程数 1、
内部时限 5 秒、外部 timeout 10 秒，目标 `gurobi_linear_default`。

结果 `timeout_with_candidate / feasible=True`，1 个候选通过 8657 项独立周期模型检查。
目标值 104.163310039134，不满意度 51.869688，排放指标 7.0639506189；求解器报告
best bound 103.794436993005、optimality gap 0.00354129535621（约 0.354130%）。
bound/gap 仅属于本次 Gurobi 模型，由求解器报告；独立验证复核候选可行性和目标值，
没有重新证明 best bound。不能把本次目标值与 HGS 结果直接排列优劣。

Gurobi 原始状态为 `9`，求解器耗时约 5.00112 秒；子进程正常退出，`returncode=0`、
`timed_out=false`，未触发 10 秒外部终止。子进程耗时约 5.50875 秒，批次约 5.64715 秒。
状态准确保留为有候选超时，不提升为已证最优。LLM 预留 0.21888 HKD，usage 估算
0.015648 HKD；估算不等于实际账单。

后续只读复核从原始模型响应重建草稿、完整请求及含配置的审阅指纹，核对确认与
实际运行 options，再从原始结果和输入快照重新独立验证。重算报告与保存报告一致；
本次 47 个证据文件、此前 HGS 会话 55 个证据文件以及两个 solver 仓库状态在复核
前后保持不变。复核没有新增模型调用、求解或许可证探测。

[原始运行报告](../runs/experiment-reports/terminal-20260916T092552Z-12512c5a/report.md) ·
[独立验证记录](../runs/terminal-20260916T092552Z-12512c5a-c001-r0001/validation.json) ·
[只读验收结果](../runs/stage8-gurobi-terminal-acceptance-20260916T093122Z-4b9a4825/summary.json)

至此，两套 backend 的终端完整执行链路均有真实验收证据；Gurobi 本次覆盖有候选超时
路径，其他终端分支仍以各自已记录的离线测试和历史验收范围为准。

当前为单会话、单次 HGS 或 Gurobi 的交互入口；没有 Web UI、会话恢复、
自动重试或批次调度。输入/响应保存在本地，避免在需求中粘贴凭据。
候选合法不保证自然语言理解正确；请审阅每个字段和模型边界。单次求解不证明
最优性或资源变化的因果效应。

## 离线结果审阅模式

已有重复实验可用独立的只读入口重新解释：

```bash
.venv/bin/python -m mobilityops --mode analysis --experiment-id <experiment-id>
```

从仓库根目录启动时默认读取 `./runs`；从其他目录启动时用 `--runs-dir` 指定绝对路径。
该模式不要求 HGS/Gurobi 路径，也不需要 `--baseline`、`--backend`、LLM 预算或 solver 配置。`/explain` 展示完整
实验的三类结论，`/compare <variant-case-id> baseline` 聚焦一个变体，
`/next-plan [variant-case-id]` 为最近比较或显式指定的变体保存有界候选计划，`/quit` 退出。
没有已选择变体时拒绝生成，避免静默选择计划中的第一个变体。每条分析命令都重新复核原始证据；候选计划不会自动
执行，仍需新的预检查、预算审阅和明确确认。完整契约见
[阶段 10 文档](evidence-based-explanations.md)。

## 接管下一轮实验候选

阶段 10 保存的非空候选可进入独立的一次性确认入口：

```bash
.venv/bin/python -m mobilityops \
  --mode next-experiment \
  --proposal runs/decision-sessions/<review-session>/001-next-plan.json
```

入口不调用 LLM，也不接受 `--backend` 覆盖候选。它重新复核来源实验散列，要求候选与当前
确定性建议完全一致，再对完整计划做全场景能力与运行预检查。终端展示调用/等待预算、逐场景
backend 和时限、运行配置、候选/请求/审阅散列。只有终端当次显示的
`执行下一轮实验 <12位散列>` 才能启动执行；候选中的文字不能确认。

候选文件、来源证据、计划、预检查依据或 backend 配置在确认前变化时，旧确认失效且不调用
solver。会话证据位于 `runs/next-experiment-sessions/`，确认后的请求快照位于
`runs/next-experiment-requests/`。完整拒绝条件、产物和离线验收见
[阶段 11 文档](confirmed-next-experiments.md)。

## Gemini Decision Copilot

阶段 12 用一个会话编排实验起草、证据解释、下一轮候选和执行审阅：

```bash
.venv/bin/python -m mobilityops \
  --mode copilot \
  --baseline examples/scenario_6_1.json \
  --backend hgs \
  --budget-hkd 1.5 \
  --max-calls 6
```

也可用 `--experiment-id <id>` 从既有实验开始。每条自然语言先产生一个受限 Gemini 路由决策；
若选择起草或修订计划，字段提取会再消耗一次调用。`/state` 查看确定性状态和已预留预算，
`/quit` 保存退出。Gemini 没有 shell、Python、solver 或确认工具。

当模型选择执行审阅时，本地服务才做全场景预检查，并显示绑定请求与运行配置的一次性确认短语。
自然语言中的“执行”不确认；确认前任何计划、证据、预检查或配置变化都会拒绝执行。完成后会话
可继续解释结果或生成下一轮候选。协议、动作、证据规则和产物见
[阶段 12 文档](gemini-decision-copilot.md)。

## Copilot 会话离线审计

阶段 13 提供非交互只读入口，对阶段 12 保存的会话及关联实验做完整证据重放：

```bash
.venv/bin/python -m mobilityops \
  --mode audit \
  --copilot-session-id <copilot-session-id> \
  --audit-id <audit-id>
```

从仓库根目录启动时默认读取 `./runs`；其他位置使用 `--runs-dir`。该模式不要求 TTY、
`GEMINI_API_KEY`、`HGS_REPO_PATH` 或 `GUROBI_REPO_PATH`，也不接受计划、backend 或 solver
参数。它不会调用 Gemini、solver、许可证环境或任何外部子进程。

输出写入 `runs/copilot-audits/<audit-id>/report.json` 和 `report.md`。已有审计 ID 拒绝覆盖；
使用不同 audit ID 重放相同源证据会得到相同内容和清单指纹。验证层次和状态含义见
[阶段 13 文档](copilot-audit.md)。

## 可审计的真实 Copilot 影子验收

阶段 14 的固定脚本通过 service API 驱动真实 Copilot 会话，在执行确认处始终返回“取消”，并用
硬阻断 adapter 保证即使会话边界发生回归也不会启动 HGS：

```bash
.venv/bin/python examples/stage14_audited_copilot_pilot.py
```

该入口最多调用 `gemini-3.8-flash` 4 次、预算 1 HKD，不注册 Gurobi、不探测许可证；结束后自动
运行阶段 13 审计。完整固定输入、失败策略和产物见[阶段 14 文档](audited-copilot-pilot.md)。

2026-09-18 真实影子会话的 4 次调用全部成功，usage 估算 0.187116 HKD；确认固定取消，阶段 13
审计为 `verified_no_execution`，实际 solver 调用和许可证探测均为 0。
