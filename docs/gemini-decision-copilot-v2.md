# 阶段 20：Gemini Decision Copilot v2

阶段 20 将一个已确定性重放的 replicated dossier 转成两遍式决策简报。Gemini 第一遍生成简报，
第二遍独立检查证据 ID、数字、确定性 gate、因果/最优性措辞和执行边界。本地服务在每遍之后再次
严格校验；拒绝结果不会自动修订或重试。

## 固定协议

```text
replicated dossier
→ 重放 campaign audit 和实验报告
→ 构造编号 evidence（lineage / gate / fact / observation / limitation）
→ 展示 gemini-3.8-flash、最多 4 次、预算 ≤ 1 HKD
→ 等待一次性 Gemini 启用确认
→ 第 1 次调用：DecisionBrief
→ 本地证据、数字和 gate 校验
→ 第 2 次调用：DecisionBriefReview
→ 本地复核结果校验
→ 保存报告；human_approval_required=true / auto_execute=false
```

最大额度固定为 4 次，是故障关闭上限；正常工作流固定只调用 2 次。失败不重试、不续费，剩余额度
不会自动使用。模型没有 shell、Python、网络工具、solver 工具或确认工具，provider tool call 也被
协议拒绝。

## 命令与确认

```bash
.venv/bin/python -m mobilityops \
  --mode decision-copilot-v2 \
  --replicated-dossier-id <replicated-dossier-id> \
  --session-id <new-session-id> \
  --max-calls 4 \
  --budget-hkd 1 \
  --runs-dir /home/runqiu/MobilityOps-Copilot/runs
```

入口先只读验证 Dossier，再展示模型和预算。只有输入当次显示的：

```text
启用 Decision Copilot v2 <12 位请求指纹>
```

才创建预算并调用 Gemini。其他输入安全取消，不创建预算。确认绑定 Dossier、campaign audit、报告、
全部 evidence、模型和预算；确认后任一证据变化都会拒绝。

确定性 Dossier 为 `review_blockers` 时，模型只能返回 `review_blockers`；为
`collect_replicates` 时只能返回 `collect_more_evidence`；只有
`human_decision_review` 才允许提出 `adopt_variant`、`retain_baseline` 或继续收集证据。无论模型
建议为何，输出始终要求人类批准且 `auto_execute=false`。

数字必须存在于所引 evidence payload；未知或重复 evidence ID、凭空数字、越过 gate、批准但仍
列有 unsupported claims 等情况全部失败关闭。产物保存在
`runs/decision-copilot-v2-sessions/<session-id>/`，原始模型证据保存在对应 `runs/llm/` 预算目录。

fake Gemini 已覆盖正常两遍审阅、确定性 gate 越界、无来源数字、错误确认和终端取消；正常路径
恰好产生两次 `countTokens + interactions`，provider 工具调用和 solver 调用均为 0。

本阶段没有真实 replicated dossier，因为真实阶段 18 的 6 次 HGS 尚未确认执行。因此没有调用
真实 Gemini，也没有创建真实 Gemini 预算。真实调用需要先有阶段 19 `verified_complete` audit
和 replicated dossier，再输入当次启用短语。
