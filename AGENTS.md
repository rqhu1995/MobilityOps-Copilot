# MobilityOps-Copilot 协作说明

## 项目目标

本仓库负责交通运输优化工作流中的 orchestration、输入验证、solver adapter、确定性结果验证与后续 Agent workflow。长期数据流为：自然语言需求 → `ScenarioSpec` → 能力检查 → solver 选择与执行 → 标准化 `SolutionResult` → 解释与情景分析。

## 当前阶段边界

当前只建立确定性的 `ScenarioSpec`、`SolutionResult`、backend capability、约束记录和 solver `Protocol`。本阶段不调用 HGS，不实现任何 solver adapter、CLI、LLM/Agent SDK、RAG、多 Agent、API、UI、数据库、消息队列或云部署。

Python 代码使用 `src` layout、类型标注、Pydantic v2 和 pytest。避免为了未来需求引入复杂框架。

## 三个仓库的关系

- 本仓库：`/home/runqiu/MobilityOps-Copilot`，只负责 orchestration、adapter 和 validation。
- HGS solver：`/home/runqiu/BRPWR-HGSADC-SBC`，独立 C++/CMake 仓库，通过 `HGS_REPO_PATH` 引用。
- Gurobi solver：`/home/runqiu/BRPWR-Gurobi`，独立 Python/Gurobi 仓库，通过 `GUROBI_REPO_PATH` 引用。

不要复制两个 solver 的完整源码到本仓库。未经用户明确授权，不得修改 solver core；如果确实必须修改，应先停止并说明原因、最小修改范围及不修改的影响。

## 开发与测试

从仓库根目录运行：

```bash
.venv/bin/python -m pytest
.venv/bin/python -c "import mobilityops; print(mobilityops.__version__)"
git diff --check
```

外部进程必须使用参数列表而不是 `shell=True`，并显式设置 timeout、working directory、stdout/stderr 捕获和错误状态。保留 solver 原始输出，再生成标准化结果。

## 安全与版本控制

- 不读取、打印或提交 WLS 密钥、Token、`.env` 内容或其他敏感信息。
- `.env` 只能保留在本地；仓库只提交无敏感值的 `.env.example`。
- 不覆盖已有修改，不执行破坏性 Git 命令，不自动 commit 或 push。
