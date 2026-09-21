# 阶段 16：审计绑定的真实 HGS 小批验收

阶段 16 把阶段 14 的真实 Gemini 影子计划、阶段 15 的明确执行接管和执行后的确定性证据重放
组成一个闭环。开发部分只使用 fake HGS；真实小批执行仍需要针对下列完整计划的单独明确授权。

## 完整闭环

```text
阶段 14 已审计、未执行的真实 Copilot 计划
→ 阶段 15 重新审计、当前预检查与一次性终端确认
→ 两个 HGS case 有界串行执行
→ 逐 run 输入快照与独立结果验证
→ 确定性实验报告
→ 阶段 16 重放来源 audit、双层请求、确认、谱系、run 与报告
→ 逐文件 SHA-256 清单和闭环审计报告
```

阶段 16 审计不需要 TTY、Gemini key 或 solver 仓库路径，不调用网络、solver、许可证环境或
外部子进程。它不信任阶段 15 保存的完成状态，而是重新验证：

1. 阶段 15 session/state/event、一次性确认和审阅指纹。
2. 阶段 14 来源会话与保存 audit，以及来源计划、请求、预检查和 backend 配置绑定。
3. audit-bound 请求、内部语言实验请求和两层 reported 状态。
4. 原始实验批次、逐 run 输入/结果、独立验证、保存报告和父子谱系。
5. 所有相关执行证据的路径、大小和 SHA-256 规范清单。

任一文件缺失、篡改、使用符号链接、越界、状态矛盾或跨层散列不一致都会使整个审计失败。

## 当前待确认的真实计划

来源会话：`stage14-shadow-20260918-v1`；来源 audit：
`stage14-shadow-audit-20260918-v1`。计划固定为：

| case | backend | 次数 | 内部上限 | 外部 timeout |
| --- | --- | ---: | ---: | ---: |
| baseline | HGS | 1 | 5 秒 | 10 秒 |
| capacity30 | HGS | 1 | 5 秒 | 10 秒 |

总上限 2 次 HGS、总等待预算 20 秒、串行、失败策略 `continue`，不重试、不补跑、不新增 Gemini
调用、不调用 Gurobi、不探测许可证。该表和阶段 14 文件都不是执行授权；必须先由阶段 15 入口
展示当前预检查、配置和确认散列，再由用户明确输入当次确认短语。

## 执行和闭环审计命令

获得真实小批授权后，先运行：

```bash
.venv/bin/python -m mobilityops \
  --mode audited-execution \
  --copilot-session-id stage14-shadow-20260918-v1 \
  --audit-id stage14-shadow-audit-20260918-v1 \
  --session-id stage16-stage14-hgs-20260918-v1 \
  --hgs-repo-path /home/runqiu/BRPWR-HGSADC-SBC \
  --gurobi-repo-path /home/runqiu/BRPWR-Gurobi \
  --runs-dir /home/runqiu/MobilityOps-Copilot/runs
```

只有终端显示的 `执行审计计划 <12位散列>` 可以确认。执行结束后运行只读闭环审计：

```bash
.venv/bin/python -m mobilityops \
  --mode audited-execution-audit \
  --audited-execution-session-id stage16-stage14-hgs-20260918-v1 \
  --audit-id stage16-stage14-hgs-audit-20260918-v1 \
  --runs-dir /home/runqiu/MobilityOps-Copilot/runs
```

审计产物写入 `runs/audited-execution-audits/<audit-id>/report.json` 和 `report.md`。已有 audit ID
拒绝覆盖。

## 离线开发验收

阶段 16 新增 4 项测试，完整 654 项测试通过。fake Gemini 只建立与阶段 14 同形状的来源证据，
fake HGS 完成两个 case 共 2 次执行；随后从原始运行快照重新验证报告并生成闭环清单。谱系和原始
run 篡改均被拒绝，非交互 CLI 不依赖 solver 环境。

本阶段离线开发的真实 Gemini、HGS/Gurobi 调用和许可证探测均为 0，solver core 未修改。开发
完成时真实小批尚未执行，只有新的明确授权才能越过阶段 15 的终端确认边界。

2026-09-18 已对上述固定 ID 做一次只读生产就绪检查：来源 audit 重新验证通过，两个 HGS case
的当前全计划预检查为 `allowed=true`，计划/上限均为 2 次，需要/预算均为 20 秒；生成的请求指纹
为 `4bb228109e65707d9608b984907b09f2a546f227735dfa60af5bb919820cf017`。该检查没有创建 session
或 run，没有启动 solver 或探测许可证；指纹只是当前快照，不是执行确认，任何证据或配置变化都
必须重新生成。

## 真实小批完成记录

2026-09-21 用户原样确认 `执行审计计划 a51d346ef1f8`。确认前请求指纹、审阅指纹、来源 audit、
计划、当前预检查和 backend 配置均与上述快照一致。会话
`stage16-stage14-hgs-20260918-v1` 随后严格按计划串行调用 HGS 2 次：

| case | 状态 | 独立复核 | objective | dissatisfaction | emission | solver runtime |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| baseline | succeeded | verified_candidate | 104.042 | 51.7808 | 7.99806 | 1.59896 秒 |
| capacity30 | succeeded | verified_candidate | 104.038 | 51.7808 | 7.93847 | 1.48551 秒 |

批次状态 `completed`，实际调用 2/2 次，预留等待 20/20 秒，批次实际耗时约 3.16711 秒；没有
重试或补跑。capacity30 减 baseline 的本批观测差为 objective -0.004、dissatisfaction 0、
emission -0.05959。每个 case 仅 `n=1`，这些只是描述性观测，不证明因果效应、统计显著性或
最优性。

执行后审计 `stage16-stage14-hgs-audit-20260918-v1` 为 `verified_complete`，80 个文件进入规范
清单；保存审计与再次只读重放完全一致。清单指纹为
`4d34a94247e63261beb1b50554121bcc7cdc8ea04d07389f74788fad1aaea328`，子报告指纹为
`876b8674ca2c9d7be3c8953f7d352165a73643b28c23bded987de670872b7054`，谱系指纹为
`bc446c5a562ac4a7bd3b1f04207656158fe14b6ec84e7d53a2555cb1244ea710`。本次新增 Gemini 和
Gurobi 调用、许可证探测均为 0，两个 solver 仓库保持干净。
