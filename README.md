# MobilityOps-Copilot

MobilityOps-Copilot 是交通运输优化项目的独立 orchestration 仓库。它将逐步负责场景输入验证、solver 适配、执行管理、原始结果解析、确定性验证，以及后续的解释和 Agent workflow。

## 三仓库关系

| 仓库 | 服务器路径 | 职责 |
| --- | --- | --- |
| MobilityOps-Copilot | `/home/runqiu/MobilityOps-Copilot` | Orchestration、adapter、validation 和标准化输出 |
| BRPWR-HGSADC-SBC | `/home/runqiu/BRPWR-HGSADC-SBC` | 独立的 C++/CMake HGS 启发式 solver |
| BRPWR-Gurobi | `/home/runqiu/BRPWR-Gurobi` | 独立的 Python/Gurobi 精确 MILP solver |

两个 solver 仓库保持独立。本项目通过环境变量引用它们，不复制其完整源码，也不默认修改其核心算法。

## 安装

要求 Python 3.11 或更新的稳定版本。当前服务器的系统 Python 3.12 缺少
`ensurepip/python3.12-venv`，因此使用已经验证过的 `gurobi1301` Python
3.12.13 作为基础解释器来创建项目自己的 `.venv`：

```bash
cd /home/runqiu/MobilityOps-Copilot
/home/runqiu/anaconda3/envs/gurobi1301/bin/python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

新依赖只安装到 `.venv`，不会安装到或升级 `gurobi1301`。在其他已经提供
`venv`/`ensurepip` 的 Python 3.11+ 环境中，也可以使用 `python3 -m venv
.venv`。

如需配置本机路径，可从示例开始：

```bash
cp .env.example .env
```

`.env` 被 Git 忽略。当前代码只读取进程环境变量，不自动加载 `.env`，以避免引入额外依赖；启动命令或后续 CLI 应显式提供这些变量。

## 当前能够做什么

- 以 `src` layout 安装和导入 `mobilityops` 包；
- 使用 Pydantic v2 表示并验证三个基础路径配置；
- 从进程环境变量读取 `HGS_REPO_PATH`、`GUROBI_REPO_PATH` 和 `MOBILITYOPS_RUNS_DIR`；
- 校验并 JSON round-trip `ScenarioSpec`；
- 序列化统一的 `SolutionResult`、车辆路线和维修路线；
- 描述 backend capability、约束处理记录和 solver `Protocol`；
- 校验真实存在的 `examples/scenario_6_1.json`。

HGS 参数与领域字段的逐项映射见
[docs/solver-contract.md](docs/solver-contract.md)。

## 当前不能做什么

当前版本尚未实现：

- HGS 或 Gurobi adapter；
- solver 编译、执行、结果解析与确定性验证；
- `mobilityops.cli`；
- LLM、Agent SDK、RAG、多 Agent、API、UI、数据库或云部署。

## 测试

```bash
.venv/bin/python -c "import mobilityops; print(mobilityops.__version__)"
.venv/bin/python -m pytest
git diff --check
```
