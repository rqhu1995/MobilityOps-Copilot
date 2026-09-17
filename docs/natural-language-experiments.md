# 阶段 9：自然语言实验计划

阶段 9 把阶段 7/8 的受控自然语言入口连接到阶段 6 的重复实验服务：

```text
自然语言 → 带逐字引用的字段提议 → 本地重建完整 ExperimentPlan
→ 澄清与全场景能力检查 → 全计划运行预检查
→ 绑定计划、预算、标识、预检查和运行配置的明确确认
→ 现有串行执行 → 独立重放验证 → JSON / Markdown 报告
```

模型输出只是不可信提议。自然语言中的“执行”“确认”或“跳过检查”不会调用 solver；
只有终端显示完整审阅后输入当前 `执行实验 <12位确认码>`，才会进入执行服务。

## 启动实验计划模式

路径、基准和凭据的配置规则与[阶段 8 终端入口](terminal.md)相同。下面的命令会进入交互，
本身不调用模型或 solver；输入 `同意` 后，普通文字才会消耗一次已授权的模型额度。

```fish
set -gx HGS_REPO_PATH /home/runqiu/BRPWR-HGSADC-SBC
set -gx GUROBI_REPO_PATH /home/runqiu/BRPWR-Gurobi
set -gx MOBILITYOPS_RUNS_DIR /home/runqiu/MobilityOps-Copilot/runs
set -gx GEMINI_API_KEY $GEMINI_API_KEY
.venv/bin/python -m mobilityops \
    --mode experiment \
    --baseline examples/scenario_6_1.json \
    --backend hgs
```

示例需求：

```text
以 baseline 为基准，比较 capacity30 容量提高到30和 two_trucks 车辆增加到2辆；
每个场景3次，使用HGS；内部5秒、外部10秒，最大9次、总等待预算90秒，失败后继续。
```

case ID 必须是安全且唯一的 ASCII 标识。基准由 `--baseline` 显式提供；模型只提议用户
明确说出的变体和覆盖值，本地程序从该基准重建每个完整 `ScenarioSpec`。省略基准文件时，
必须通过自然语言提供完整基准字段，缺失项会阻止执行。

## 草稿、澄清与预算

提取结果分别声明一个 baseline、有限 variants 和字段提议。每个 case、字段或未支持要求
都带 `message_index` 与原文逐字引用；引用不在对应消息、同一轮值冲突、重复/不安全 ID、
模糊范围或未支持要求都会形成澄清。引用存在只证明来源可定位，不证明模型理解正确，
因此终端始终显示完整计划供人工审阅。

`backend`、重复次数、外部 timeout 可以作为全计划值，也可以对具体 case 显式覆盖。
最大调用数、总等待预算和 `continue` / `stop` 失败策略必须明确。自然语言入口要求预算
覆盖全部预定调用：

```text
planned_calls ≤ max_calls
Σ(case.repetitions × case.external_timeout_sec) ≤ external_wait_budget_sec
```

这比阶段 6 的底层 API 更严格。阶段 6 允许显式小预算并把剩余条目标为
`skipped_budget`；阶段 9 不会把自然语言中的预算矛盾自动解释为允许跳过。

LLM 预算与 solver 预算分开显示。LLM 预算限制提取次数、token 与 HKD 预留；solver
预算限制调用次数和外部等待秒数。任何一方的剩余额度都不会转给另一方，也不会自动续费。

## 全计划预检查与确认

草稿先对每个完整场景调用无副作用的 `SolverService.select()`。输入 `/run` 后，程序再对
全部场景调用 `ExperimentService.precheck()`，检查每个预定 run ID、adapter 路径、输入、
时限和已审计源码条件。预检查不启动 solver、模型调用或 Gurobi 许可证环境；任一场景失败，
第一个 solver 都不会启动。

执行请求包含完整计划、experiment/report ID、全部预检查记录和涉及的 backend 运行配置。
请求与终端审阅各有 SHA-256 指纹。计划修订、证据变化、预检查变化、executable/Python 路径、
Gurobi 时间段或线程数变化，都会使旧确认失效。每个实验 ID 只允许执行一次；已有实验、报告、
run 或请求目录均拒绝覆盖。

确认后直接调用现有 `ExperimentService.execute()`，按 case 和 repetition 串行执行；不重试、
补跑、切换 backend 或崩溃恢复。随后调用 `ExperimentReportService.write_report()`，从原始证据
重新验证候选，并按同场景、同模型/输入签名和同搜索条件分组。跨 backend 结果分别显示为
不可比，不进入统一优劣排名。

## 状态与证据

```text
runs/terminal-sessions/<session-id>/
  session.json
  turn-001-input.json / turn-001-draft.json
  review-001-request.json
  review-001-precheck.json
  review-001-execution.json
  state.json / events.jsonl
runs/llm/terminal-experiment-<session-id>/
  call-001/request.json / response.json / experiment-extraction.json
runs/language-experiment-requests/<experiment-id>/
  draft.json / request.json / state.json
runs/experiments/<experiment-id>/
runs/experiment-reports/<report-id>/report.json、report.md
runs/<experiment-id>-c001-r0001/ ...
```

模型原始响应、每版输入与草稿、预检查、执行请求、确认状态、批次和报告均可追溯；凭据只
进入认证请求头，不写入产物。Ctrl-C 保存最近状态，已完成尝试仍可由底层报告服务只读重放，
但终端不会自动继续未完成计划。

全部计划条目完成并通过重放复核时退出码为 `0`；部分失败、超时无候选、不可行、验证失败、
预算跳过或未完成仍会生成可用报告，但退出码为 `1`。确认前取消/EOF 为 `0`，参数错误为 `2`，
Ctrl-C 为 `130`。

## 离线开发验收

本阶段使用 fake HTTP 和可快速返回的 fake HGS executable 完成 3 个场景 × 每场景 3 次
的端到端验收：内部 5 秒、外部 10 秒、最多 9 次、总等待预算 90 秒、失败策略 `continue`。
九个 fake 候选均经过独立复核，报告按三个 case 各得到 `n=3`，随后从保存证据只读重放得到
相同结构化报告。测试还覆盖缺少重复次数、预算冲突、重复/不安全 ID、伪造引用、HGS seed、
Gurobi 目标错配、混合 backend、全计划预检查拒绝、自然语言执行注入、计划和运行配置变化、
两种超时、部分失败、Ctrl-C 与确定性报告重放。

本轮完整 **592 项普通测试通过**，阶段 9 新增 **21 项**。开发验收没有真实 Gemini、
HGS 或 Gurobi 调用，没有许可证探测，也没有修改两个 solver 仓库。

## 真实验收计划与完成记录

以下固定计划已由用户审阅并明确确认执行：

| 项目 | 固定值 |
| --- | --- |
| Gemini | `gemini-3.8-flash` 最多 1 次提取；最多预留 0.21888 HKD；无重试 |
| 场景 | HGS `6_1` baseline、capacity30、two_trucks |
| 重复 | 每场景 3 次；总计 9 次；失败后继续 |
| 时限 | 每次内部 5 秒、外部 10 秒；总外部等待预算 90 秒 |
| session ID | `stage9-real-acceptance-20260916-v1` |
| budget ID | `terminal-experiment-stage9-real-acceptance-20260916-v1` |
| experiment/report ID | `terminal-experiment-stage9-real-acceptance-20260916-v1` |

run ID 为上述 experiment ID 加 `-c001-r0001` 至 `-c003-r0003`。如果任一 ID 已存在、
一次模型提取未形成 ready 计划、全计划预检查失败或审阅内容与表格不一致，本批停止，不追加
模型调用或 solver 运行。该批只能验证真实交互与受控执行流程，并给出这九次运行的描述性观测；
不能证明自然语言提取的一般准确率、资源变化的因果效应、统计显著性或最优性。

2026-09-16 完成会话 `stage9-real-acceptance-20260916-v1`。首次启动在任何外部调用前发现
CLI 将精确的 0.21888 HKD Decimal 预留与二进制 float 直接比较而误判不足；修复为十进制
字符串比较，加入边界回归检查并重新运行 592 项测试后，才使用原固定 ID 启动真实验收。

Gemini 实际调用 1 次，生成 `ready` 草稿；预留 0.21888 HKD，usage 估算 0.119862 HKD。
全计划预检查三个场景均通过，确认指纹绑定 HGS executable、完整场景、9 次调用和 90 秒预算。
随后 9 次 HGS 均 `succeeded`，无重试或补跑；9 个候选均通过重新复核，批次实际耗时
12.373584243 秒。

| 场景 | n | objective 最小 / 中位 / 最大 | dissatisfaction 最小 / 中位 / 最大 | emission 最小 / 中位 / 最大 |
| --- | ---: | --- | --- | --- |
| baseline | 3 | 104.038 / 104.038 / 104.042 | 51.7808 / 51.7808 / 51.7808 | 7.93847 / 7.93847 / 7.99806 |
| capacity30 | 3 | 104.04 / 104.04 / 104.042 | 51.7808 / 51.7808 / 51.7808 | 7.97731 / 7.97731 / 7.99806 |
| two_trucks | 3 | 104.038 / 104.04 / 104.04 | 51.7808 / 51.7808 / 51.7808 | 7.93847 / 7.97731 / 7.97731 |

两个变体相对基准的 objective 中位数观测差均为约 0.002，dissatisfaction 差为 0，
emission 差为约 0.03884。小样本与 HGS 波动使这些数值只能描述本批运行，不能解释为容量或
车辆变化的因果效果。保存的 JSON 报告经只读 `analyze()` 重放后完全一致；296 个证据文件的
聚合散列在重放前后相同。两个 solver 仓库保持不变，本次没有 Gurobi 调用或许可证探测。

[Markdown 报告](../runs/experiment-reports/terminal-experiment-stage9-real-acceptance-20260916-v1/report.md) ·
[JSON 报告](../runs/experiment-reports/terminal-experiment-stage9-real-acceptance-20260916-v1/report.json) ·
[终端状态](../runs/terminal-sessions/stage9-real-acceptance-20260916-v1/state.json)
