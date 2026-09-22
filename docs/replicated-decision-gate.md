# 阶段 19：Replicated Decision Gate

阶段 19 对阶段 18 完成后的 Dossier 重复实验进行无交互、只读的闭环审计，并从本次重复实验
自身的报告生成新的决策证据包。历史 `n=1` 证据保留在父 Dossier 谱系中，不静默并入新实验的
统计分母。

## 数据流

```text
阶段 18 已确认会话
→ 核对终端 session / events / review / confirmation
→ 重放父 Dossier 和阶段 16 audit
→ 核对外层 DossierCampaignRequest 与阶段 11 子请求
→ 重放 experiment、6 个原始 run、独立验证和报告
→ 核对 Dossier → request → experiment → report 谱系
→ 输出逐文件 SHA-256 campaign audit
→ 以本次 baseline/capacity30 各 n=3 生成 replicated dossier
→ 重新评估五个决策门
```

## 命令

```bash
.venv/bin/python -m mobilityops \
  --mode campaign-decision \
  --dossier-campaign-session-id <completed-session-id> \
  --audit-id <new-campaign-audit-id> \
  --dossier-id <new-replicated-dossier-id> \
  --variant-case-id capacity30 \
  --runs-dir /home/runqiu/MobilityOps-Copilot/runs
```

该入口不要求 TTY、Gemini key 或 solver 路径，不调用外部进程。audit 保存到
`runs/dossier-campaign-audits/<audit-id>/`，新 Dossier 保存到
`runs/replicated-decision-dossiers/<dossier-id>/`；已有 ID 拒绝覆盖。

只有批次 `completed`、调用数等于计划数、所有候选重新独立复核通过、双层请求和完整谱系一致时，
campaign audit 才能成为 `verified_complete`。新 Dossier 的证据强度只读取本次 campaign 分组；
相关分组最小 `n >= 3` 时该门为 `pass`，否则仍为 `caution`。

fake HGS 验收中 baseline/capacity30 各 3 次，6 个候选全部通过，刷新后的五门均为 `pass`，因此
状态进入 `human_decision_review`。这只是离线验收结果，不是对真实阶段 17 候选的业务结论。

## 真实 campaign 审计与 Dossier

2026-09-22，阶段 19 对真实阶段 18 会话 `stage18-stage17-hgs-20260922-v1` 完成无交互重放。
audit `stage19-stage18-hgs-audit-20260922-v1` 为 `verified_complete`：计划的 6 次调用全部完成，
6 个候选全部再次通过独立复核，203 个文件进入清单。清单 SHA-256 为
`a9b1b5b63cc48fe227a4a04390836a847a102dab7566d2621ce514de0a68c707`，保存 audit 再次只读重放
一致。

replicated dossier `stage19-stage18-decision-20260922-v1` 只使用本次 campaign 的 baseline 与
capacity30 各 `n=3` 分组，五个决策门全部为 `pass`，确定性建议为 `human_decision_review`。
Dossier SHA-256 为 `1582ac3aefac3ef3ab7ec2d090713554c2af6ba985aca1ed20d280c0418026e9`，再次只读重放
一致。本阶段没有新增 Gemini 或 solver 调用、不探测许可证，也不修改 solver core。
