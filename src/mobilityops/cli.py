"""Standard-library terminal entry point; help and argument checks never call a model."""

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import sys
from uuid import uuid4

from pydantic import ValidationError

from mobilityops.config import Settings
from mobilityops.domain import BackendName, ScenarioSpec
from mobilityops.services.gemini_intake import GeminiBudgetConfig
from mobilityops.services.decision_terminal_session import DecisionTerminalSession
from mobilityops.services.experiment_terminal_session import ExperimentTerminalSession
from mobilityops.services.gurobi_inputs import strict_json
from mobilityops.services.solver_service import SolverService
from mobilityops.services.terminal_session import TerminalSession, terminal_text
from mobilityops.solvers.gurobi.backend import GurobiBackend
from mobilityops.solvers.gurobi.contracts import GurobiOptions


class Console:
    def read(self, prompt: str) -> str:
        return input(prompt)

    def write(self, text: str) -> None:
        print(text, flush=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="MobilityOps 终端交互：需求 → 澄清 → 审阅 → 明确确认 → 报告。")
    result.add_argument("--baseline", type=Path, help="显式基准 ScenarioSpec JSON；省略则逐项澄清")
    result.add_argument("--mode", choices=("scenario", "experiment", "analysis"), default="scenario",
                        help="scenario=单场景（默认）；experiment=有限重复实验计划；analysis=离线证据审阅")
    result.add_argument("--experiment-id", help="analysis 模式要重新复核的实验 ID")
    result.add_argument("--backend", choices=[b.value for b in BackendName], help="指定 backend；Gurobi 需显式配置 --gurobi-python")
    result.add_argument("--budget-hkd", type=float, default=1, help="本会话 LLM 费用上限，0 < x ≤ 10（默认 1 HKD）")
    result.add_argument("--max-calls", type=int, default=3, help="本会话最多提取次数，1–6（默认 3）")
    result.add_argument("--session-id", help="独立会话 ID，不可覆盖或恢复；省略则自动生成")
    result.add_argument("--hgs-repo-path", type=Path, help="HGS 仓库绝对路径；默认 HGS_REPO_PATH")
    result.add_argument("--gurobi-repo-path", type=Path, help="Gurobi 仓库绝对路径；默认 GUROBI_REPO_PATH")
    result.add_argument("--gurobi-python", type=Path, help="注册 Gurobi：已安装 gurobipy 的 Python 绝对路径")
    result.add_argument("--gurobi-time-interval-sec", type=int, help="Gurobi 时间段长度（正整数秒，默认 600）")
    result.add_argument("--gurobi-threads", type=int, help="Gurobi 线程数（1–64，默认 1）")
    result.add_argument("--runs-dir", type=Path, help="产物绝对路径；默认 MOBILITYOPS_RUNS_DIR")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser()
    args = arguments.parse_args(argv)
    if not sys.stdin.isatty():
        arguments.error("此入口需要交互终端；不接受管道中的预填确认。Python 自动化请使用现有 service API。")
    try:
        environment = {key: os.environ[key] for key in
                       ("HGS_REPO_PATH", "GUROBI_REPO_PATH", "MOBILITYOPS_RUNS_DIR") if key in os.environ}
        for key, value in (("HGS_REPO_PATH", args.hgs_repo_path), ("GUROBI_REPO_PATH", args.gurobi_repo_path),
                           ("MOBILITYOPS_RUNS_DIR", args.runs_dir)):
            if value is not None:
                environment[key] = str(value)
        settings = Settings.from_env(environment)
        if args.mode == "analysis":
            if not args.experiment_id:
                raise ValueError("analysis 模式需要 --experiment-id")
            if args.baseline or args.backend or args.gurobi_python or args.gurobi_time_interval_sec is not None or args.gurobi_threads is not None:
                raise ValueError("analysis 模式只读取既有实验；不要提供 baseline、backend 或 solver 配置")
            session_id = args.session_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
            return DecisionTerminalSession(
                settings, io=Console(), session_id=session_id,
                experiment_id=args.experiment_id,
            ).run()
        if args.experiment_id:
            raise ValueError("--experiment-id 只用于 analysis 模式")
        budget = GeminiBudgetConfig(budget_hkd=args.budget_hkd, max_calls=args.max_calls)
        if budget.reservation_hkd > Decimal(str(budget.budget_hkd)):
            raise ValueError(f"预算至少需要 {budget.reservation_hkd} HKD 才能预留一次提取")
        context = ScenarioSpec.model_validate(strict_json(args.baseline.read_bytes())) if args.baseline else None
        if args.gurobi_python is None and (
                args.backend == BackendName.GUROBI or args.gurobi_time_interval_sec is not None
                or args.gurobi_threads is not None):
            raise ValueError("请用 --gurobi-python 显式指定已有 Gurobi Python 环境的绝对路径")
        gurobi = None
        if args.gurobi_python is not None:
            options = GurobiOptions(
                python_executable=args.gurobi_python.expanduser(),
                time_interval_sec=args.gurobi_time_interval_sec if args.gurobi_time_interval_sec is not None else 600,
                threads=args.gurobi_threads if args.gurobi_threads is not None else 1,
            )
            gurobi = GurobiBackend(settings, options=options)
        session_id = args.session_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
        session_type = ExperimentTerminalSession if args.mode == "experiment" else TerminalSession
        return session_type(SolverService(settings, gurobi_backend=gurobi), io=Console(), session_id=session_id,
                            budget=budget, context=context,
                            backend=BackendName(args.backend) if args.backend else None).run()
    except ValidationError as exc:
        print("配置校验失败：" + terminal_text(str(exc.errors(include_input=False, include_url=False))), file=sys.stderr)
    except (ValueError, OSError) as exc:
        print(f"配置或文件错误：{terminal_text(str(exc))}", file=sys.stderr)
    return 2
