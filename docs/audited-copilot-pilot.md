# 阶段 14：可审计的真实 Copilot 影子会话

阶段 14 验证阶段 12 的统一会话与阶段 13 的离线审计能在同一条真实 Gemini 链路上闭环，
但仍停在 solver 执行之前。固定脚本使用 `gemini-3.8-flash`，最多 4 次 interactions、预算上限
1 HKD；HGS 只执行只读预检查，`solve()` 被专用 adapter 硬阻断，不注册 Gurobi，也不探测
许可证。

## 固定纵向路径

1. 路由真实业务目标到 `draft_experiment`。
2. 用第二次调用提取 baseline/capacity30 的完整有限计划。
3. 路由到 `prepare_execution_review`，执行本地全计划预检查并固定返回“取消”。
4. 根据计划草稿和执行审阅证据回答调用数、等待预算及不能得出的结论，随后 `/quit`。
5. 阶段 13 审计重新核对会话、4 次调用、本地工具证据和零执行状态。

固定入口：

```bash
.venv/bin/python examples/stage14_audited_copilot_pilot.py
```

预算、会话、审计和 pilot ID 均固定且拒绝覆盖。任何动作偏离、计划不完整、预检查失败、证据回答
被本地约束拒绝或审计不一致都会立即停止，不重试、不续费，也不会进入 solver。成功产物位于：

```text
runs/copilot-pilots/stage14-audited-shadow-20260918-v1/
runs/copilot-sessions/stage14-shadow-20260918-v1/
runs/llm/copilot-stage14-shadow-20260918-v1/
runs/copilot-audits/stage14-shadow-audit-20260918-v1/
```

成功只证明固定影子会话在保存证据下通过本地契约与审计，不证明 capacity30 更优，不授权执行
计划中的 2 次 HGS，也不构成生产部署或自动决策授权。

## 真实验收完成记录

2026-09-18 会话 `stage14-shadow-20260918-v1` 调用 `gemini-3.8-flash` 4 次，依次完成
`draft_experiment` 路由、计划字段提取、`prepare_execution_review` 路由和证据绑定的 `answer`。
4 次均成功；预算预留 0.87552 HKD，usage 估算合计 0.187116 HKD。完整计划包含 baseline 与
capacity30 各 1 次、最多 2 次 solver 调用、总等待预算 20 秒；两个场景的 HGS 只读预检查通过。

本地确认固定返回“取消”，会话终态为 `completed` 且 `execution_completed=false`。专用 adapter
记录的 `solve()` 尝试为 0，没有 solver run 目录。审计 `stage14-shadow-audit-20260918-v1`
返回 `verified_no_execution`，清单包含 39 个文件，清单指纹为
`54ae40e65444d8a3e114d26e3e98fb2638509d3cd12a410a12d136afacc79dd5`。真实 HGS/Gurobi 调用、
许可证探测和重试均为 0；两个 solver 仓库保持不变。
