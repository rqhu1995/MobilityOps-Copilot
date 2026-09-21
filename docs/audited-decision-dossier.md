# 阶段 17：审计证据绑定的 Decision Dossier v1

阶段 17 把阶段 16 的闭环执行审计转成一个面向人类决策审阅的确定性证据包。它重新重放来源
audit 和原始实验报告，分别呈现已复核事实、本批描述性观察、不能得出的结论、决策门状态和一份
有限下一轮候选。生成 dossier 不调用 Gemini 或 solver，也不会自动执行候选。

## 数据流

```text
阶段 16 verified_complete 审计
→ 重新重放来源 session、run、独立验证、报告和谱系
→ 核对保存 audit 与当前重放完全一致
→ 生成事实 / 观察 / 不能得出的结论
→ 评估完整性、执行、可比性、证据强度和下一步五个决策门
→ 生成 auto_execute=false 的有限重复候选
→ 保存 JSON / Markdown Decision Dossier
```

## 决策门

| 门 | 含义 | 状态 |
| --- | --- | --- |
| `evidence_integrity` | 来源审计、报告和谱系已重新验证 | pass/block |
| `execution_completion` | 计划调用和独立复核是否完整 | pass/block |
| `comparison_validity` | 基准与所选变体是否同口径可比 | pass/block |
| `evidence_strength` | 每个相关分组是否至少有 3 个候选 | pass/caution |
| `next_action` | 是否能生成无 blocker 的有限候选 | pass/block |

任何 block 都产生 `review_blockers`；没有 block 但存在 caution 时产生 `collect_replicates`；全部通过
时才产生 `human_decision_review`。这些状态是本地规则，不替代业务负责人最终判断。

## 命令

```bash
.venv/bin/python -m mobilityops \
  --mode dossier \
  --execution-audit-id <verified-execution-audit-id> \
  --dossier-id <new-dossier-id> \
  --variant-case-id <variant-case-id> \
  --runs-dir /home/runqiu/MobilityOps-Copilot/runs
```

入口不要求 TTY、`GEMINI_API_KEY`、`HGS_REPO_PATH` 或 `GUROBI_REPO_PATH`。只有来源实验恰好有一个
变体时可以省略 `--variant-case-id`；零个或多个变体必须显式选择，避免静默选择第一个方案。已有
dossier ID 拒绝覆盖。

产物位于 `runs/decision-dossiers/<dossier-id>/`：

- `dossier.json`：完整证据绑定、决策门、事实、观察、限制和候选。
- `dossier.md`：面向人类审阅的确定性呈现。
- `next-plan.json`：`auto_execute=false` 的阶段 10 兼容候选。

## 阶段 16 真实证据的 dossier

2026-09-21 已生成 `stage17-stage16-decision-20260921-v1`。保存 dossier 与再次只读重放完全一致，
指纹为 `65b5ed5235de27aaa476726392fd0bee52695c9a62ea0d8d4d968f91b249b179`。

五个门依次为 `pass / pass / pass / caution / pass`，因此建议为 `collect_replicates`。caution 的
原因是 baseline/capacity30 各只有 1 个复核候选；现有 -0.004 objective 和 -0.05959 emission
差异只能描述本批运行。

下一轮候选固定为 baseline/capacity30 各 3 次 HGS、内部/外部 5/10 秒，最大 6 次、等待预算
60 秒、串行、失败继续。候选 `auto_execute=false`，尚未做当前运行预检查，也未获得执行授权；
它仍须进入阶段 11/15 的全计划预检查、审阅和新确认。

## 离线验收

阶段 17 新增 5 项测试，完整 659 项测试通过。测试覆盖只读构建、确定性保存、audit 篡改、未知
变体和无 TTY CLI；fake 来源的下一轮候选严格为 6 次、60 秒且不执行。本阶段真实 Gemini、
HGS/Gurobi 调用和许可证探测均为 0，solver core 未修改。
