# 阶段 12：Gemini Decision Copilot v1

阶段 12 把自然语言实验计划、既有证据解释、下一轮候选、全计划预检查和明确执行确认放入
同一个会话。Gemini 只选择有限动作并生成证据引用的表述；确定性 Python 服务仍拥有计划校验、
能力判断、预检查、执行请求、确认、solver 调用和结果复核的全部权限。

## 启动

新业务目标从显式基准开始：

```bash
.venv/bin/python -m mobilityops \
  --mode copilot \
  --baseline examples/scenario_6_1.json \
  --backend hgs \
  --budget-hkd 1.5 \
  --max-calls 6
```

从既有实验开始证据对话：

```bash
.venv/bin/python -m mobilityops \
  --mode copilot \
  --experiment-id <saved-experiment-id> \
  --budget-hkd 1.5 \
  --max-calls 6
```

入口仍要求 `HGS_REPO_PATH`、`GUROBI_REPO_PATH` 和 `MOBILITYOPS_RUNS_DIR`。只有需要
Gurobi 计划时才用 `--gurobi-python` 显式注册该 adapter。凭据只从导出的
`GEMINI_API_KEY` 读取，不写入会话、请求、响应或预算账本。

## Gemini 可以选择的动作

模型每轮只能返回 `gemini_copilot_decision_v1`，其 `action` 必须属于本地当轮公布的白名单：

| action | 本地行为 | Gemini 调用 solver |
| --- | --- | --- |
| `draft_experiment` | 调用受引用校验的实验字段提取器，再由本地契约重建计划 | 否 |
| `explain_experiment` | 从原始运行证据重新构建确定性解释 | 否 |
| `propose_next_experiment` | 为明确变体生成 `auto_execute=false` 的有限候选 | 否 |
| `prepare_execution_review` | 全场景预检查并生成绑定配置的请求，随后由本地终端等待确认 | 否 |
| `answer` / `clarify` | 引用当前会话证据回答，或询问一个缺失信息 | 否 |

Provider 返回的 function/tool call、未知动作、当前状态不可用的动作、未知或重复 evidence ID 均被拒绝。
证据回答存在数字时，数字必须能在引用证据或用户原文中找到；该检查限制数字幻觉，但不把 LLM
表述提升为新的验证事实。

## 确认和执行边界

`prepare_execution_review` 不是执行。它复用阶段 9 或阶段 11 的确定性服务，展示完整计划、
调用和等待预算、所有场景预检查、backend 配置、请求指纹与审阅指纹。只有用户逐字输入当次
显示的 `执行 Copilot 实验 <12位散列>` 才能继续；Gemini 决策、用户原始消息中的“执行”、
普通 `yes` 或旧确认都不能启动 solver。

确认后程序再次生成完整请求。计划、来源证据、预检查或运行配置变化时旧确认失效。执行仍按
case/repetition 串行，不自动重试、补跑、切换 backend、扩大预算或恢复中断任务；结果继续经过
独立验证和确定性报告。完成后同一会话可继续解释证据或生成下一轮候选。

## 会话证据与谱系

会话保存在 `runs/copilot-sessions/<session-id>/`，包括模型输入、Gemini 决策、本地计划草稿、
解释、候选、执行审阅、状态和事件日志。Gemini 预算继续使用 `runs/llm/<budget-id>/` 的不可重置
账本。确认后执行沿用现有 experiment、run、report 和 request 目录。

成功执行还生成 `lineage.json`，绑定父实验（如有）、计划来源、请求指纹、审阅指纹、子实验和
重新计算的子报告散列。这是阶段 12 内部安全模块，不允许模型改写。

## 开发与验收边界

离线开发新增 15 项测试，完整 **631 项测试通过**。fake Gemini/fake HGS 覆盖统一会话中的
计划起草、两场景预检查、精确确认、2 次串行执行、独立报告、结果解释和谱系；另覆盖自然语言
“执行”不确认、预检查失败、阶段 10 候选在同会话接入阶段 11、动作白名单、证据 ID、数字约束
和剩余额度不足时的动作收缩。

用户授权的真实 Gemini 验收固定为 `gemini-3.8-flash`、最多 6 次 interactions、预算上限
1.5 HKD；六个案例只验证动作协议、状态约束和证据引用，真实 solver 调用与许可证探测均为 0。
验收脚本为 `examples/stage12_gemini_copilot_acceptance.py`，固定预算 ID，已存在时拒绝重开。
2026-09-18 已使用固定预算 ID 完成 6 次真实 `gemini-3.8-flash` interactions：预留
1.31328 HKD，usage 估算合计 0.085188 HKD。前 5 个动作案例直接通过；第 6 个证据回答包含正确
引用和事实数字，但本地校验器把行首列表序号和已验证的 `evidence-001` 标签误判为无来源数字。
修复后保存的原响应离线重放通过；没有重试或第 7 次远端调用。验收证据位于
`runs/llm/stage12-gemini-copilot-20260917-v1/`，真实 solver 调用和许可证探测均为 0，两个 solver
仓库保持不变。任何真实 solver 计划仍需后续针对完整请求单独授权。
