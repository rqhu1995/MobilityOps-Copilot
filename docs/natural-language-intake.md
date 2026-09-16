# 阶段 7：真实 LLM 到候选场景与显式执行请求

阶段 8 已提供基于这些服务的[终端交互入口](terminal.md)，可直接输入需求、补充澄清、审阅并确认单次执行。

本阶段实现了 Google Gemini `gemini-3.8-flash` 的真实 REST 接入，以及独立于模型的
场景校验、缺失字段澄清、能力评估和执行边界。普通测试使用注入的 HTTP 响应；
真实模型验收由受限示例脚本单独执行，不混入 pytest。

## 当前接口

```text
propose(自然语言, 可选显式基准)
  → Gemini 结构化字段提议 + 原文引用
  → ScenarioSpec 校验 + 缺失/冲突/未支持要求澄清
  → SolverService.select()
  → ready / needs_clarification / incompatible

revise(草稿, 补充自然语言)
  → 重新解释完整消息，保留已有明确字段与未解决要求

prepare_execution(就绪草稿, 运行标识, 外部时限)
  → 一个待审阅的单次 ExperimentPlan + 请求指纹

execute(草稿, 请求, confirmed_request_sha256)
  → 现有 ExperimentService → 独立复核 → JSON / Markdown 报告
```

`propose()`、`revise()` 会请求 LLM，但不启动 solver、不进行运行环境 preflight、
不探测 Gurobi 许可证。`prepare_execution()` 仅生成可审阅请求。
自然语言中的“执行”“跳过验证”不会调用执行接口。

## 候选与澄清规则

`ScenarioExtraction` 只接收有限字段提议和解释问题，不接受函数调用、脚本、路径或
执行动作。每个字段附带 `message_index` 与逐字原文 `evidence`；程序核对引用是否
存在于指定消息，缺少引用或引用错误会阻止执行。

字段仍须通过现有 `ScenarioSpec` 校验：数量必须是合法整数，时间须有限且大于零，
目标与 policy 必须是已有枚举。LLM 不负责改变这些约束，也不能把 backend 能力拒绝
变成成功。缺失字段形成中文问题；同一消息给出多个不同值时要求澄清，不取最后一个。

只有调用者明确传入的 `context=ScenarioSpec(...)` 才可继承字段，来源标记为 `context`。
从消息提取的值标为 `user_text`，领域契约自带的可选默认值标为 `schema_default`。
不会凭空采用历史 `6_1`、容量 25、5 秒时限或 HGS 目标。

`revise()` 传递完整用户消息、显式基准和 backend 要求；后续明确赋值可以修订前值。
如果模型遗漏此前的字段，程序仍保留它们；此前未支持要求也不会因模型遗漏而消失。
特定字段问题可由新消息中的明确赋值解决；无法对应字段的未支持要求，必须由调用者
明确传入 `withdraw_issues=(index, ...)` 撤回，并在新消息中保留用户的撤回原文。

**引用存在与 Schema 合法不证明自然语言解释正确。** 单位转换、否定、约束意图等
仍可能被 LLM 误解，执行前需要审阅完整候选和预算。当前不支持任意运输模型、自动
参数扩展、模型最优性保证或把未知约束悄悄丢弃。

## 显式执行边界

`prepare_execution()` 为就绪草稿构造单次、单 backend、无重试的实验计划。
请求指纹绑定完整草稿（含对话与来源）、场景、选择、内部时限、外部 timeout、
experiment ID 和 report ID。执行时重新计算草稿、能力选择和请求，任一变化都使旧
确认失效。

`execute()` 只接受与该请求完全匹配的 `confirmed_request_sha256`；这是应用调用者
在审阅后传入的显式动作，不从模型输出或普通文本里提取。指纹用于绑定被审阅内容，
不是用户身份认证或密码学签名；当前是本地 Python 服务，没有账号或网络 API。

执行保存 `runs/language-requests/<experiment_id>/draft.json`、`request.json` 和
状态文件。后续调用已有实验服务，沿用全部启动前检查、预算、状态、独立复核与报告。
重复 ID、旧草稿、篡改预算或已存在报告目录会阻止再次执行。

## Gemini 接入与本轮预算

用户指定 Google Gemini `gemini-3.8-flash`，总费用上限 **10 HKD**，凭据环境变量
`GEMINI_API_KEY`。最终通过标准库 HTTPS 调用 **`/v1beta/interactions`**，不引入 SDK
或 Agent 框架。API key 只进入认证请求头，不进入 URL、请求 JSON 或日志，不读取 `.env`。

用户提供的成功 curl 调用已证明同一账号和模型可以生成。此前反复要求验证 fish/账号的
排查方向不正确。项目的旧 `generateContent` 请求失败；改用 Interactions 后，参数对照
进一步确认：最简有界请求可用，加入本项目的 `response_format` 后返回 HTTP 400。
该证据只说明本次参数组合被拒绝，不证明整个结构化输出功能不可用。

### 最终请求与校验

实际请求只包含 `model`、`input`、`store: false` 和
`generation_config.max_output_tokens: 4096`。`input` 包含固定提取规则、JSON schema
和序列化用户数据。JSON 由提示要求，**服务端不强制该 schema**；程序仍以严格 JSON
解析、`ScenarioExtraction`、原文引用和 `ScenarioSpec` 校验拒绝错误输出。没有静默
降级、自动重试、工具调用或模型自动执行授权。

响应只接受 `status: completed`、指定模型、一个 `model_output` 和文本内容。
`thought` 的签名或摘要不参与候选解析；工具调用、额外步骤、截断、未完成、缺失用量、
无效 JSON 或越界用量均阻止成功。原始响应保留用于复核，不把生成成功等同于场景兼容。
`revise()` 每次发送完整用户消息和显式基准，作为独立提取请求；不依赖服务端会话或
`previous_interaction_id`，不重放模型的思维步骤。

### 计数与价格口径

Interactions 没有本项目可用的专用计数接口。生成前使用 `models.countTokens`，
投影完整输入文本（含固定规则、schema 和用户数据），再加 **1024 token 余量**，
总量不得超过 **16000**。这是启动前保守检查，不声称与 Interactions 的最终计费计数
完全一致。返回 usage 再复核输入和输出上限；超限会停止预算后续调用。

每次最多 4096 输出 token，包含 thinking；不设置未经本次成功请求验证的 thinking
级别。每个 HTTP 请求配置 30 秒网络 timeout，验收启动进程额外设置 390 秒总上限。
禁用重定向和 HTTP 自动重试。失败保存 HTTP/API 状态及移除凭据和 URL 后的错误文字，
不保存错误响应原文、认证头或任意网络异常文字。

价格依据在 2026-09-16 核对：输入每百万 token **0.75 USD**，输出含 thinking
**3.75 USD**，有效至 2026-12-31。预算使用 **8 HKD/USD** 保守系数：

```text
单次预留 = (16000 × 0.75 + 4096 × 3.75) / 1,000,000 × 8
         = 0.21888 HKD

旧失败与协议对照：11 次预留 = 2.40768 HKD
最终六案例：       6 次预留 = 1.31328 HKD
用户手动验证：              0.02000 HKD（单独保守预留）
累计：                      3.74096 HKD < 10 HKD
```

响应费用估算使用 `usage.total_input_tokens`、`total_output_tokens` 和
`total_thought_tokens`；thinking 单独加到可见输出，不遗漏计费。估算不是 Google
账单。旧失败缺少 usage 时记为未知，保留完整预留，不退预算补跑。

每份预算使用独立目录、不可覆盖的 ID 和文件锁。新 client 不能重置账本；旧
`generateContent` 预算只读，不能用升级接口的方式重新消耗其剩余额度。最终验收示例
在创建新预算前汇总已有阶段 7 账本及手动验证预留，超过用户总上限即拒绝启动。
通用 `create_budget()` 本身不代替 Google 账号级限额，新增预算仍须在已授权范围内。

官方依据：

1. [Interactions REST 契约](https://ai.google.dev/api/interactions-api)：请求、步骤、状态及 usage 字段。
2. [Thinking 与输出上限](https://ai.google.dev/gemini-api/docs/thinking#token-limits-and-max_output_tokens)：输出上限包含 thought tokens。
3. [Gemini 标准层价格](https://ai.google.dev/gemini-api/docs/pricing#gemini-3.8-flash)：输入与包含 thinking 的输出价格。
4. [Token 计数](https://ai.google.dev/api/tokens)：计数投影使用的接口；它不提供本项目 Interactions 精确账单保证。
5. [结构化输出](https://ai.google.dev/gemini-api/docs/structured-output)：官方描述的功能；本次测试的参数组合未通过，不用于最终请求。

## fish 与验收示例

fish 的 `set -g` 指定当前 shell 全局作用域，`-x` 才导出给子进程。已验证通过 fish
启动并导出变量后，Python 能访问凭据；不需要展示密钥。见 [fish set 文档](https://fishshell.com/docs/current/cmds/set.html)。

```fish
set -gx GEMINI_API_KEY "$GEMINI_API_KEY"
/home/runqiu/MobilityOps-Copilot/.venv/bin/python /home/runqiu/MobilityOps-Copilot/examples/stage7_gemini_acceptance.py
```

**本轮六案例已完成。** 示例固定预算 ID 为 `stage7-gemini-interactions-final-20260916`，
再次运行会拒绝，不能删除账本后重跑。此前的 [恢复脚本](../examples/stage7_gemini_recovery.py)
仅保留作历史实现证据；旧预算不再接受生成。

## 普通 Python 调用

```python
from mobilityops.services.gemini_intake import GeminiBudgetConfig, GeminiScenarioInterpreter
from mobilityops.services.scenario_intake import ScenarioIntakeService
from mobilityops.services.solver_service import SolverService

# 新预算必须对应已经明确授权的范围；本轮已完成，不按示例另开预算。
GeminiScenarioInterpreter.create_budget(
    settings, budget_id="my-approved-budget",
    config=GeminiBudgetConfig(budget_hkd=1.31328, max_calls=6),
)
interpreter = GeminiScenarioInterpreter(settings, budget_id="my-approved-budget")
intake = ScenarioIntakeService(SolverService(settings), interpreter=interpreter)
draft = intake.propose("把基准场景的容量改为30，其余不变。", context=baseline)

# 只生成请求，不运行 solver。
if draft.status == "ready":
    request = intake.prepare_execution(
        draft, experiment_id="explicit-single-run", report_id="explicit-single-report",
        external_timeout_sec=10,
    )
    print(request.model_dump_json(indent=2))

# 仅在调用者收到针对完整 request 的明确执行要求后调用：
# receipt = intake.execute(draft, request, confirmed_request_sha256=request.request_sha256)
```

## 2026-09-16 最终验收

**498 项普通测试通过，六个真实 Gemini 案例全部通过。** 阶段 7 共新增 80 项测试：
场景与执行边界 27 项，Gemini 接入、预算、响应验证和示例 53 项。普通测试使用 fake
HTTP/fake executable，不请求真实 LLM 或依赖本机许可证。

| 案例 | 预期与实际状态 | 复核结论 |
| --- | --- | --- |
| 容量提高到 30 | `ready` | 容量 30，其余基准参数保留 |
| 缺少求解时限 | `needs_clarification` | 形成求解时限澄清问题 |
| 补充 5 秒时限 | `ready` | 求解 5 秒，作业预算仍为 7200 秒 |
| 新增时间窗 | `needs_clarification` | 未支持要求保留并阻止执行 |
| HGS 指定 seed=42 | `incompatible` | seed 保留，由能力检查拒绝 |
| 车辆增加到 2 辆 | `ready` | 2 辆车，其余基准参数保留 |

保存的原始响应已在离线重放中重新解析，通过来源、候选、澄清、能力决策和指纹复核；
待审阅执行请求重新构造后完全一致。六案例 usage 估算共 **0.146778 HKD**。旧证据
散列与两个 solver 仓库状态保持不变。本轮真实 solver 调用 **0 次**；执行后报告链路
沿用已有服务，并由 fake executable 测试覆盖。

主要证据（被 Git 忽略，不是普通测试依赖）：

- [最终 Markdown 报告](../runs/stage7-intake-final-20260916/report.md) 与 [JSON 报告](../runs/stage7-intake-final-20260916/report.json)。
- [六案例计划](../runs/llm/stage7-gemini-interactions-final-20260916/plan.json)、[验收结果](../runs/llm/stage7-gemini-interactions-final-20260916/acceptance.json) 与 [账本](../runs/llm/stage7-gemini-interactions-final-20260916/ledger.json)。
- [已确认执行请求](../runs/llm/stage7-gemini-interactions-final-20260916/reviewed-execution-request.json)：容量 30，HGS，内部 5 秒、外部 10 秒、1 次；六案例验收时尚未执行，随后按用户确认完成，见下节。
- [最简参数成功对照](../runs/llm/stage7-gemini-interactions-20260916/minimal-probe/result.json) 与 [增加 response_format 后失败的对照](../runs/llm/stage7-gemini-interactions-20260916/schema-probe/result.json)。
- [此前失败阶段报告](../runs/stage7-intake-review-20260916/report.md)：保留原结论和当时证据，不覆盖为成功。

六个案例验证这条工作流，不构成任意自然语言提取准确率、统计显著性或因果结论。
Schema 合法和引用存在仍不能证明语义解释正确；实际执行前需要审阅候选、模型边界和预算。

## 用户确认后的单次真实求解

2026-09-16 用户明确要求执行已展示请求后，`ScenarioIntakeService.execute()` 重新核对
草稿和请求指纹，沿用实验服务完成预检查、1 次 HGS 求解、独立复核与报告。未调用
LLM，未修改场景或时限，未重试。结果 `succeeded / feasible=True`：目标 104.039，
不满意度 51.7808，排放指标 7.94776，求解器报告耗时 1.27922 秒，批次耗时约 1.31 秒。

本次原始输入与库存时序、路线预算和 13 项指标比较均通过检查。两个 solver 仓库
状态以及 166 个受保护文件散列保持不变。单次结果不用于容量改善的因果结论或最优性证明。

[执行 Markdown 汇总](../runs/stage7-confirmed-execution-20260916/report.md) ·
[执行 JSON 汇总](../runs/stage7-confirmed-execution-20260916/summary.json) ·
[独立复核](../runs/stage7-gemini-interactions-final-20260916-reviewed-c001-r0001/validation.json)
