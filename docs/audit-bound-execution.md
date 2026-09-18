# 阶段 15：审计绑定的执行接管

阶段 15 将阶段 14 已保存、已审计但未执行的 Copilot 计划接回现有安全执行链路。它不信任
旧的“通过”标记，也不把 Gemini 输出、审计报告或文件中的“执行”文字当成授权。

## 数据流

```text
阶段 14 Copilot 会话 + 阶段 13 audit
→ 重新重放完整 Copilot 审计
→ 核对保存 audit 与当前重放完全一致
→ 核对唯一执行审阅、完整计划和来源草稿
→ 用当前 solver 配置重新做全计划预检查
→ 生成新的 audit-bound 请求与终端审阅指纹
→ 等待一次性终端确认
→ 有界串行执行、独立验证、报告与父子谱系
```

任一来源文件、审计清单、草稿、执行审阅、计划、预检查依据或 backend 配置变化，都会使旧
请求或确认失效。接管请求绑定来源会话 ID、audit ID、audit 清单散列、来源审阅和请求散列、
来源草稿散列，以及新计划、预检查和运行配置。

## 终端入口

真实执行必须另有针对完整计划的明确授权。获得授权后，才能在仓库根目录运行：

```bash
.venv/bin/python -m mobilityops \
  --mode audited-execution \
  --copilot-session-id stage14-shadow-20260918-v1 \
  --audit-id stage14-shadow-audit-20260918-v1 \
  --session-id <new-session-id> \
  --hgs-repo-path /home/runqiu/BRPWR-HGSADC-SBC \
  --gurobi-repo-path /home/runqiu/BRPWR-Gurobi \
  --runs-dir /home/runqiu/MobilityOps-Copilot/runs
```

入口先展示来源标识、两层请求指纹、调用数、等待预算、逐 case backend/时限和当前 backend
配置。只有当次终端显示的 `执行审计计划 <12位审阅散列>` 才会执行；任何其他输入均取消，
不会调用 solver。入口不需要 `GEMINI_API_KEY`，也不会产生新的 Gemini 调用。

阶段 14 当前保存的计划为 `baseline` 与 `capacity30` 各 1 次 HGS，内部时限各 5 秒、外部
timeout 各 10 秒，最大 2 次调用、总等待预算 20 秒、失败继续。该计划尚未真实执行。

## 产物

- `runs/audited-execution-sessions/<session-id>/`：终端 session、event、预检查、请求和审阅。
- `runs/audited-execution-requests/<experiment-id>/`：确认后的请求、来源草稿、状态和谱系。
- `runs/experiments/<experiment-id>/`：现有实验批次及逐次原始运行证据。
- `runs/experiment-reports/<report-id>/`：重新独立验证后生成的 JSON/Markdown 报告。

所有目录拒绝覆盖。中断或失败只保留状态和已有证据，不自动恢复、重试、补跑或续费。

## 离线验收

阶段 15 新增 5 项测试，完整 650 项测试通过。fake Gemini 只用于建立可重放的来源会话，fake
HGS 覆盖两个 case 的成功执行与独立报告；取消、错误确认、audit 篡改和 CLI 路由均有测试。
本阶段开发验收没有调用真实 Gemini、HGS/Gurobi 或许可证环境，没有修改 solver core。

阶段 15 仅完成离线接管能力。阶段 14 的真实计划仍需要新的明确授权，不能由本次开发、旧审计、
旧自然语言或旧取消确认推导出执行权限。
