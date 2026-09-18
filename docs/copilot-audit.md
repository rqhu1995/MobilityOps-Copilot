# Copilot 会话离线审计

阶段 13 为阶段 12 保存的 Copilot 会话增加确定性、只读的审计入口。它从本地原始证据重新
验证会话状态、Gemini 调用账本、模型动作、本地工具产物、执行确认、父子实验谱系、原始
solver 结果和报告，并生成带逐文件 SHA-256 的 JSON/Markdown 证据包。

该入口不会调用 Gemini、HGS、Gurobi 或许可证环境，不要求 solver 仓库路径，也不会执行
新的实验。审计结果说明保存证据在当前本地契约下能否相互印证；它不证明全部文件从未被
协调修改，也不重新评价模型当时的主观推理质量。

## 启动

从仓库根目录审计一个已保存的阶段 12 会话：

```bash
.venv/bin/python -m mobilityops \
  --mode audit \
  --copilot-session-id <copilot-session-id> \
  --audit-id <audit-id>
```

`--audit-id` 可省略，此时使用 Copilot 会话 ID。默认从 `./runs` 读取；从其他目录运行时可
用 `--runs-dir /absolute/path/to/runs`。这是非交互入口，不要求 TTY、`GEMINI_API_KEY`、
`HGS_REPO_PATH` 或 `GUROBI_REPO_PATH`。

审计 ID 必须是安全的 run ID。已有 `runs/copilot-audits/<audit-id>` 时拒绝覆盖；需要再次
保存时应使用新的审计 ID。成功退出码为 0，证据或配置错误为 2。

## 重新验证的契约

1. **会话和事件**：`session.json`、`state.json` 与 `events.jsonl` 的会话 ID、最终状态和
   turn 顺序必须一致；证据目录和文件不得使用符号链接逃逸。
2. **模型调用**：会话预算、LLM 配置、账本预留次数、逐次 call 目录、状态和费用合计必须
   一致。每个保存的 `gemini_copilot_decision_v1` 都重新经过动作白名单、evidence ID 和
   有来源数字校验，并与对应 turn 及 provider 产物核对。
3. **本地工具证据**：计划草稿、解释和下一轮候选从原始实验证据重新计算；模型输入中的
   每个 evidence payload 必须逐项匹配保存或重放得到的本地工具产物，同一 evidence ID
   不能在后续 turn 绑定到其他内容。
4. **确认与谱系**：若会话执行过实验，审计重新验证 execution review、完整请求、请求目录
   终态、一次性确认所绑定的散列，以及 draft/proposal 到子实验的 lineage。
5. **solver 与报告证据**：从子实验的原始 run 输入和结果重新执行现有独立验证及报告分析，
   要求保存报告和 lineage 报告散列一致；随后为会话、LLM、请求、实验、报告和 run 文件生成
   去重后的 SHA-256 清单。

任一必需证据缺失、损坏、陈旧、被替换或与其他层不一致时，整个审计失败，不输出“部分
通过”的新报告。取消且从未启用模型的会话可以得到零调用的 `verified_no_execution`。

## 审计状态和产物

审计成功时有三种状态：

| 状态 | 含义 |
| --- | --- |
| `verified_complete` | 保存会话已完成，并且确认后的执行及报告谱系通过重新验证 |
| `verified_no_execution` | 会话已完成或在启用模型前取消，且没有保存的 solver 执行谱系 |
| `verified_incomplete` | 未完成会话的现有证据内部一致；它不代表流程已经完成 |

输出位于：

```text
runs/copilot-audits/<audit-id>/
├── report.json
└── report.md
```

`report.json` 使用 `copilot_audit_report_v1`，记录会话状态、模型调用统计、已接受动作、执行
谱系、独立复核候选数、逐文件散列及整个清单的规范指纹。`report.md` 是同一结果的可读版本。
审计输出本身不加入被审计清单，因而对相同源证据使用不同 audit ID 会产生相同报告内容。

## 安全边界与离线验收

审计只读取 `runs/` 下由现有服务定义的证据目录。它不启动子进程、不访问网络、不探测
许可证、不加载 solver 仓库，也不把审计成功解释为重新执行、最优性证明或部署授权。
文件清单能检测审计后发生的变化，但仅有本地散列不能证明攻击者没有同时改写文件和关联
散列；需要外部不可变存储或签名时，应在后续阶段另行设计信任根。

阶段 13 新增 9 项离线测试，覆盖完整 fake Gemini/fake HGS 会话、零调用取消、确定性双份
报告、CLI 无 TTY/无 solver 环境、模型动作篡改、模型可见证据篡改、账本不一致、run 损坏、
状态与事件不一致及符号链接拒绝。完整 640 项测试通过；开发验收期间真实 Gemini、HGS、
Gurobi 调用和许可证探测均为 0，两个 solver 仓库未修改。
