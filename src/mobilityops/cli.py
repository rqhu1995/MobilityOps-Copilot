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
from mobilityops.services.audited_execution_audit import (
    AuditedExecutionAuditService,
)
from mobilityops.services.audited_execution_terminal_session import (
    AuditedExecutionTerminalSession,
)
from mobilityops.services.copilot_audit import CopilotAuditService
from mobilityops.services.copilot_terminal_session import CopilotTerminalSession
from mobilityops.services.gemini_intake import GeminiBudgetConfig
from mobilityops.services.decision_terminal_session import DecisionTerminalSession
from mobilityops.services.decision_dossier import DecisionDossierService
from mobilityops.services.experiment_terminal_session import ExperimentTerminalSession
from mobilityops.services.gurobi_inputs import strict_json
from mobilityops.services.next_experiment_terminal_session import NextExperimentTerminalSession
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
    result.add_argument(
        "--mode",
        choices=(
            "scenario", "experiment", "analysis", "next-experiment", "copilot",
            "audit", "audited-execution", "audited-execution-audit", "dossier",
        ),
        default="scenario",
        help=("scenario=单场景（默认）；experiment=有限重复实验计划；"
              "analysis=离线证据审阅；next-experiment=接管阶段 10 候选；"
              "copilot=统一 Gemini 决策会话；audit=离线审计 Copilot 会话；"
              "audited-execution=重新审计后接管已审阅计划；"
              "audited-execution-audit=离线复核接管后的完整执行；"
              "dossier=生成审计绑定的决策证据包"),
    )
    result.add_argument("--experiment-id", help="analysis 要复核或 copilot 初始载入的实验 ID")
    result.add_argument("--proposal", type=Path, help="next-experiment 模式的阶段 10 候选 JSON")
    result.add_argument("--copilot-session-id", help="audit/audited-execution 的来源 Copilot 会话 ID")
    result.add_argument("--audit-id", help="audit 输出 ID，或 audited-execution 的来源审计 ID")
    result.add_argument(
        "--audited-execution-session-id",
        help="audited-execution-audit 要复核的阶段 15 会话 ID",
    )
    result.add_argument("--execution-audit-id", help="dossier 的来源执行审计 ID")
    result.add_argument("--dossier-id", help="dossier 的新输出 ID")
    result.add_argument("--variant-case-id", help="dossier 明确选择的变体 case ID")
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
    if args.mode not in {"audit", "audited-execution-audit", "dossier"} and not sys.stdin.isatty():
        arguments.error("此入口需要交互终端；不接受管道中的预填确认。Python 自动化请使用现有 service API。")
    try:
        environment = {key: os.environ[key] for key in
                       ("HGS_REPO_PATH", "GUROBI_REPO_PATH", "MOBILITYOPS_RUNS_DIR") if key in os.environ}
        for key, value in (("HGS_REPO_PATH", args.hgs_repo_path), ("GUROBI_REPO_PATH", args.gurobi_repo_path),
                           ("MOBILITYOPS_RUNS_DIR", args.runs_dir)):
            if value is not None:
                environment[key] = str(value)
        if args.mode == "audit":
            if not args.copilot_session_id:
                raise ValueError("audit 模式需要 --copilot-session-id")
            if args.session_id is not None or any((
                args.baseline, args.experiment_id, args.proposal, args.backend,
                args.audited_execution_session_id,
                args.execution_audit_id, args.dossier_id, args.variant_case_id,
                args.hgs_repo_path, args.gurobi_repo_path, args.gurobi_python,
                args.gurobi_time_interval_sec, args.gurobi_threads,
            )):
                raise ValueError("audit 模式只读取 Copilot 证据；不要提供会话、计划或 solver 参数")
            runs_dir = Path(environment["MOBILITYOPS_RUNS_DIR"]).expanduser() if environment.get(
                "MOBILITYOPS_RUNS_DIR"
            ) else Path.cwd() / "runs"
            settings = Settings(
                hgs_repo_path=runs_dir / ".audit-only/hgs-unused",
                gurobi_repo_path=runs_dir / ".audit-only/gurobi-unused",
                runs_dir=runs_dir,
            )
            audit_id = args.audit_id or args.copilot_session_id
            report = CopilotAuditService(settings).write_report(
                args.copilot_session_id, audit_id=audit_id
            )
            folder = settings.runs_dir / "copilot-audits" / audit_id
            print(
                f"审计完成：{report.audit_status}；模型调用 "
                f"{report.model_usage.calls_observed}；solver 调用 "
                f"{report.execution.calls_invoked}；证据文件 {len(report.files)}。\n"
                f"Markdown：{folder / 'report.md'}\nJSON：{folder / 'report.json'}",
                flush=True,
            )
            return 0
        if args.mode == "audited-execution-audit":
            if not args.audited_execution_session_id or not args.audit_id:
                raise ValueError(
                    "audited-execution-audit 模式需要 "
                    "--audited-execution-session-id 和 --audit-id"
                )
            if args.session_id is not None or any((
                args.baseline, args.experiment_id, args.proposal, args.backend,
                args.copilot_session_id, args.hgs_repo_path,
                args.gurobi_repo_path, args.gurobi_python,
                args.execution_audit_id, args.dossier_id, args.variant_case_id,
                args.gurobi_time_interval_sec, args.gurobi_threads,
            )):
                raise ValueError(
                    "audited-execution-audit 只读取本地证据；不要提供计划、"
                    "Copilot 或 solver 参数"
                )
            runs_dir = (
                Path(environment["MOBILITYOPS_RUNS_DIR"]).expanduser()
                if environment.get("MOBILITYOPS_RUNS_DIR")
                else Path.cwd() / "runs"
            )
            settings = Settings(
                hgs_repo_path=runs_dir / ".audit-only/hgs-unused",
                gurobi_repo_path=runs_dir / ".audit-only/gurobi-unused",
                runs_dir=runs_dir,
            )
            report = AuditedExecutionAuditService(settings).write_report(
                args.audited_execution_session_id, audit_id=args.audit_id
            )
            folder = settings.runs_dir / "audited-execution-audits" / args.audit_id
            print(
                f"执行审计完成：{report.audit_status}；solver 调用 "
                f"{report.calls_invoked}；复核候选 {report.verified_candidates}；"
                f"证据文件 {len(report.files)}。\n"
                f"Markdown：{folder / 'report.md'}\nJSON：{folder / 'report.json'}",
                flush=True,
            )
            return 0
        if args.mode == "dossier":
            if not args.execution_audit_id or not args.dossier_id:
                raise ValueError(
                    "dossier 模式需要 --execution-audit-id 和 --dossier-id"
                )
            if args.session_id is not None or any((
                args.baseline, args.experiment_id, args.proposal, args.backend,
                args.copilot_session_id, args.audit_id,
                args.audited_execution_session_id, args.hgs_repo_path,
                args.gurobi_repo_path, args.gurobi_python,
                args.gurobi_time_interval_sec, args.gurobi_threads,
            )):
                raise ValueError(
                    "dossier 只读取已审计证据；不要提供会话、计划或 solver 参数"
                )
            runs_dir = (
                Path(environment["MOBILITYOPS_RUNS_DIR"]).expanduser()
                if environment.get("MOBILITYOPS_RUNS_DIR")
                else Path.cwd() / "runs"
            )
            settings = Settings(
                hgs_repo_path=runs_dir / ".dossier-only/hgs-unused",
                gurobi_repo_path=runs_dir / ".dossier-only/gurobi-unused",
                runs_dir=runs_dir,
            )
            dossier = DecisionDossierService(settings).write(
                args.execution_audit_id,
                dossier_id=args.dossier_id,
                variant_case_id=args.variant_case_id,
            )
            folder = settings.runs_dir / "decision-dossiers" / args.dossier_id
            print(
                f"决策证据包完成：{dossier.recommendation}；变体 "
                f"{dossier.selected_variant_case_id}；指纹 "
                f"{dossier.dossier_sha256}。\n"
                f"Markdown：{folder / 'dossier.md'}\n"
                f"JSON：{folder / 'dossier.json'}\n"
                f"下一轮候选：{folder / 'next-plan.json'}",
                flush=True,
            )
            return 0
        if args.execution_audit_id or args.dossier_id or args.variant_case_id:
            raise ValueError(
                "--execution-audit-id、--dossier-id 和 --variant-case-id "
                "只用于 dossier 模式"
            )
        if args.mode == "audited-execution":
            if not args.copilot_session_id or not args.audit_id:
                raise ValueError(
                    "audited-execution 模式需要 --copilot-session-id 和 --audit-id"
                )
            if args.baseline or args.experiment_id or args.proposal or args.backend:
                raise ValueError(
                    "audited-execution 从已审计计划读取场景；不要提供 baseline、"
                    "experiment-id、proposal 或 backend"
                )
            settings = Settings.from_env(environment)
            if args.gurobi_python is None and (
                args.gurobi_time_interval_sec is not None
                or args.gurobi_threads is not None
            ):
                raise ValueError(
                    "请用 --gurobi-python 显式注册来源计划需要的 Gurobi adapter"
                )
            gurobi = None
            if args.gurobi_python is not None:
                gurobi = GurobiBackend(
                    settings,
                    options=GurobiOptions(
                        python_executable=args.gurobi_python.expanduser(),
                        time_interval_sec=(
                            args.gurobi_time_interval_sec
                            if args.gurobi_time_interval_sec is not None
                            else 600
                        ),
                        threads=(
                            args.gurobi_threads
                            if args.gurobi_threads is not None
                            else 1
                        ),
                    ),
                )
            session_id = args.session_id or (
                f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
            )
            return AuditedExecutionTerminalSession(
                SolverService(settings, gurobi_backend=gurobi),
                io=Console(),
                session_id=session_id,
                source_session_id=args.copilot_session_id,
                source_audit_id=args.audit_id,
            ).run()
        if args.copilot_session_id or args.audit_id:
            raise ValueError(
                "--copilot-session-id 和 --audit-id 只用于 audit 或 "
                "audited-execution 模式"
            )
        if args.audited_execution_session_id:
            raise ValueError(
                "--audited-execution-session-id 只用于 audited-execution-audit 模式"
            )
        if args.mode == "analysis":
            if not args.experiment_id:
                raise ValueError("analysis 模式需要 --experiment-id")
            if (args.baseline or args.proposal or args.backend or args.gurobi_python
                    or args.gurobi_time_interval_sec is not None or args.gurobi_threads is not None):
                raise ValueError("analysis 模式只读取既有实验；不要提供 proposal、baseline、backend 或 solver 配置")
            runs_dir = Path(environment["MOBILITYOPS_RUNS_DIR"]).expanduser() if environment.get(
                "MOBILITYOPS_RUNS_DIR"
            ) else Path.cwd() / "runs"
            settings = Settings(
                hgs_repo_path=runs_dir / ".analysis-only/hgs-unused",
                gurobi_repo_path=runs_dir / ".analysis-only/gurobi-unused",
                runs_dir=runs_dir,
            )
            session_id = args.session_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
            return DecisionTerminalSession(
                settings, io=Console(), session_id=session_id,
                experiment_id=args.experiment_id,
            ).run()
        if args.mode == "next-experiment":
            if args.proposal is None:
                raise ValueError("next-experiment 模式需要 --proposal")
            if args.experiment_id or args.baseline or args.backend:
                raise ValueError(
                    "next-experiment 模式从候选读取计划；不要提供 experiment-id、baseline 或 backend"
                )
            settings = Settings.from_env(environment)
            if args.gurobi_python is None and (
                    args.gurobi_time_interval_sec is not None or args.gurobi_threads is not None):
                raise ValueError("请用 --gurobi-python 显式注册候选需要的 Gurobi adapter")
            gurobi = None
            if args.gurobi_python is not None:
                gurobi = GurobiBackend(settings, options=GurobiOptions(
                    python_executable=args.gurobi_python.expanduser(),
                    time_interval_sec=(args.gurobi_time_interval_sec
                                       if args.gurobi_time_interval_sec is not None else 600),
                    threads=args.gurobi_threads if args.gurobi_threads is not None else 1,
                ))
            session_id = args.session_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
            return NextExperimentTerminalSession(
                SolverService(settings, gurobi_backend=gurobi), io=Console(),
                session_id=session_id, proposal_path=args.proposal,
            ).run()
        if args.proposal:
            raise ValueError("--proposal 只用于 next-experiment")
        if args.experiment_id and args.mode != "copilot":
            raise ValueError("--experiment-id 只用于 analysis 或 copilot")
        settings = Settings.from_env(environment)
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
        common = dict(
            io=Console(), session_id=session_id, budget=budget, context=context,
            backend=BackendName(args.backend) if args.backend else None,
        )
        solver = SolverService(settings, gurobi_backend=gurobi)
        if args.mode == "copilot":
            return CopilotTerminalSession(
                solver, experiment_id=args.experiment_id, **common
            ).run()
        session_type = ExperimentTerminalSession if args.mode == "experiment" else TerminalSession
        return session_type(solver, **common).run()
    except ValidationError as exc:
        print("配置校验失败：" + terminal_text(str(exc.errors(include_input=False, include_url=False))), file=sys.stderr)
    except (ValueError, OSError) as exc:
        print(f"配置或文件错误：{terminal_text(str(exc))}", file=sys.stderr)
    return 2
