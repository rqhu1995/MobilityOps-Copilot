# 阶段 18：Dossier 绑定的有限重复实验接管

阶段 18 将阶段 17 的 `collect_replicates` 决策证据包接回现有安全执行链。入口不会因为
Dossier、候选文件或自然语言中出现“执行”而启动 solver；它必须重新验证全部来源证据、完成
全计划预检查、展示预算与运行配置，并取得绑定当次审阅的一次性终端确认。

## 数据流

```text
阶段 17 Decision Dossier
→ 重放阶段 16 完整执行审计与原始实验报告
→ 核对 Dossier 指纹、五个决策门和 next-plan
→ 导入完整 ExperimentPlan
→ 全场景能力与运行预检查
→ 展示调用数、等待预算、case 和 backend 配置
→ 生成绑定 Dossier / audit / 候选 / 预检查 / 配置的请求
→ 等待一次性终端确认
→ 串行执行、独立验证、报告和父子谱系
```

## 命令

```bash
.venv/bin/python -m mobilityops \
  --mode dossier-campaign \
  --source-dossier-id <decision-dossier-id> \
  --session-id <new-session-id> \
  --hgs-repo-path /home/runqiu/BRPWR-HGSADC-SBC \
  --gurobi-repo-path /home/runqiu/BRPWR-Gurobi \
  --runs-dir /home/runqiu/MobilityOps-Copilot/runs
```

入口必须运行在交互终端。确认短语形如：

```text
执行 Dossier 重复实验 <12 位审阅指纹>
```

只有终端当次显示的完整短语有效。其他输入安全取消；确认前 Dossier、来源审计、候选文件、
预检查依据或 backend 配置发生任何变化，旧确认失效。已有 session、experiment、report 或请求
目录拒绝覆盖，也不自动恢复、重试或补跑。

## 证据边界

外层 `DossierCampaignRequest` 同时绑定 Dossier 指纹、阶段 16 执行审计清单散列、完整 Dossier
和阶段 11 子请求。子请求继续绑定候选文件字节、规范候选、来源报告、完整计划、全计划预检查及
backend 运行配置。确认后新增 `dossier_campaign_lineage_v1`，把外层请求、子请求、实验 ID 和
重新生成的报告散列连接起来。

证据分别保存到：

- `runs/dossier-campaign-sessions/<session-id>/`：会话、审阅、预检查和状态。
- `runs/dossier-campaign-requests/<experiment-id>/`：Dossier、请求、状态和谱系。
- `runs/experiments/<experiment-id>/`：批次及每次原始 solver run。
- `runs/experiment-reports/<report-id>/`：独立重放生成的 JSON/Markdown 报告。

## 离线验收与真实候选

阶段 18 新增 7 项测试：Dossier/audit/候选/预检查/配置绑定、精确确认、取消、来源篡改、错误确认、
CLI 路由，以及 fake HGS 的 baseline/capacity30 各 3 次完整闭环。fake 批次 6 个候选全部通过
独立复核，保存报告可确定性重放。

阶段 17 的真实 Dossier `stage17-stage16-decision-20260921-v1` 仍对应 baseline/capacity30 各
3 次 HGS、内部/外部 5/10 秒、最多 6 次、等待预算 60 秒、串行且失败继续。阶段 18 的离线开发
不调用真实 Gemini、HGS/Gurobi，不探测许可证，也不修改 solver core。创建本入口或预检查通过
都不构成那 6 次真实 HGS 的执行授权；真实执行仍需单独启动交互会话，并输入当次显示的一次性
确认短语。

2026-09-21 已对该真实 Dossier 做无写入预检查：全部场景通过，backend 仅为 HGS，计划调用
6/6 次、等待 60/60 秒；固定审阅候选的外层请求指纹为
`e408055eaae28ed4653691f1ae6993a32519b4ff19c0cb178d131f4bf26309b9`。检查前后 `runs/` 文件数
均为 1589，两个 solver 仓库保持干净；没有创建 session、request、experiment 或 report。这个
指纹不是终端确认短语，后续真实会话仍会基于当时证据和配置生成新的审阅指纹。
