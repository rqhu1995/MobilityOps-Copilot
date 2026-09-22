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

## 真实两遍式决策审阅完成记录

2026-09-22，入口重新验证 replicated dossier `stage19-stage18-decision-20260922-v1` 后生成请求
SHA-256 `aaf7154ad07e50bbcf0f0418df0c71d79516d4a8eb1e8865cae5de652834fb68`。用户输入一次性确认
`启用 Decision Copilot v2 aaf7154ad07e`，会话 `stage20-stage19-gemini-20260922-v1` 随后固定
调用 `gemini-3.8-flash` 两次，未使用剩余两次上限，也没有重试。

第一遍简报提出 `adopt_variant`；第二遍复核判断该建议把描述性结果提升成了采用 capacity30 的
业务结论，与确定性的 `human_decision_review` 边界以及“不能证明统计显著性、因果效应或全局
最优性”的限制冲突，因此返回 `rejected`。最终会话状态为 `review_rejected`。这是协议预期的
失败关闭：没有形成采用或部署决定，输出仍为 `human_approval_required=true`、
`auto_execute=false`，且不会自动修订或发起第三次调用。

两次调用共使用 6,880 个输入 token、519 个可见输出 token、3,023 个 thinking token，总计
10,422 token；usage 估算 0.147540 HKD，预留 0.43776 HKD，低于 1 HKD 上限。provider 工具
调用和 solver 调用均为 0；没有调用 Gurobi、探测许可证或修改 solver core。简报 SHA-256 为
`e9e3065fe5759888f0e7fc53a26ceab7e2ef095d8df6338ee063f0754305eee3`，复核 SHA-256 为
`c4165d09656dc03a098277df3142e6f1f36a3b399b6993905eb54aba98fc6588`。
