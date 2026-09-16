"""Bounded manual acceptance example: six LLM attempts, zero solver invocations.

Run in a terminal that already exports GEMINI_API_KEY. Never paste a key here.
The fixed budget ID refuses accidental reruns; this is not a general-purpose CLI.
"""

from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import time

from mobilityops.config import Settings
from mobilityops.domain import BackendName, ScenarioSpec
from mobilityops.services.gemini_intake import GeminiBudgetConfig, GeminiCallError, GeminiScenarioInterpreter
from mobilityops.services.scenario_intake import ScenarioIntakeService
from mobilityops.services.solver_service import SolverService


BUDGET_ID = "stage7-gemini-interactions-final-20260916"
CASES = (
    ("capacity", "以给定基准为基础，把车辆容量提高到30辆自行车，其余参数保持不变。", "ready"),
    ("missing_runtime", "场景名称为晚间调度，实例6_1，1辆车、1名维修人员，车辆容量25，作业预算2小时，单车装卸时间60秒，单车维修时间300秒，目标标识eudf_default，求解策略fast_feasible，使用HGS。", "needs_clarification"),
    ("clarify_runtime", "求解时限5秒。", "ready"),
    ("time_windows", "在给定基准场景上增加每个站点必须在指定时间窗内访问的限制。", "needs_clarification"),
    ("seed", "基于给定基准，设置seed为42。", "incompatible"),
    ("two_trucks", "基于给定基准，把车辆数改为2辆；不要执行求解，只生成候选。", "ready"),
)


def run_cases(intake: ScenarioIntakeService, context: ScenarioSpec, output: Path) -> list[dict]:
    """Explicit fixed cases; no retry, no solver execution, no hidden defaults."""
    outcomes = []
    incomplete = None
    cases = CASES
    for index, (name, text, expected) in enumerate(cases, 1):
        started = time.monotonic()
        try:
            if name == "clarify_runtime":
                if incomplete is None:
                    raise ValueError("No preceding incomplete draft; follow-up skipped")
                draft = intake.revise(incomplete, text)
            else:
                draft = intake.propose(text, context=None if name == "missing_runtime" else context,
                                       backend=None if name == "missing_runtime" else BackendName.HGS)
            (output / f"{index:02d}-{name}-draft.json").write_text(draft.model_dump_json(indent=2) + "\n", encoding="utf-8")
            checks = {"expected_status": draft.status == expected}
            if name == "missing_runtime":
                incomplete = draft
                checks["missing_runtime_question"] = any(q.field == "solver_runtime_limit_sec" and q.kind == "missing" for q in draft.clarifications)
            elif name == "capacity":
                checks["capacity_30"] = draft.candidate is not None and draft.candidate.truck_capacity == 30
                checks["other_parameters_preserved"] = draft.candidate == context.model_copy(update={"truck_capacity": 30})
                if draft.status == "ready":
                    request = intake.prepare_execution(draft, experiment_id=BUDGET_ID + "-reviewed",
                                                       report_id=BUDGET_ID + "-reviewed", external_timeout_sec=10)
                    (output / "reviewed-execution-request.json").write_text(request.model_dump_json(indent=2) + "\n", encoding="utf-8")
            elif name == "clarify_runtime":
                checks["runtime_5"] = draft.candidate is not None and draft.candidate.solver_runtime_limit_sec == 5
                checks["operation_budget_7200"] = draft.candidate is not None and draft.candidate.operation_time_budget_sec == 7200
            elif name == "seed":
                checks["seed_preserved"] = draft.candidate is not None and draft.candidate.seed == 42
            elif name == "two_trucks":
                checks["two_trucks"] = draft.candidate is not None and draft.candidate.number_of_trucks == 2
                checks["other_parameters_preserved"] = draft.candidate == context.model_copy(update={"number_of_trucks": 2})
            outcomes.append({"case": name, "status": draft.status, "checks": checks, "passed": all(checks.values()),
                             "elapsed_sec": time.monotonic() - started, "draft_sha256": draft.draft_sha256})
        except Exception as exc:
            # Never echo provider response bodies, headers, key values or arbitrary exception messages.
            outcomes.append({"case": name, "passed": False, "error_type": type(exc).__name__,
                             "elapsed_sec": time.monotonic() - started})
            if isinstance(exc, GeminiCallError):
                outcomes[-1]["diagnostic"] = exc.diagnostic
            print(f"{index}/6 {name}: failed; remaining cases skipped", flush=True)
            outcomes.extend({"case": next_name, "passed": False, "status": "skipped_after_failure"}
                            for next_name, _, _ in cases[index:])
            break
        print(f"{index}/6 {name}: {'passed' if outcomes[-1]['passed'] else 'failed; inspect saved evidence'}", flush=True)
    return outcomes


def main() -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        print('GEMINI_API_KEY 未导出到当前 Python 进程。fish 中运行 set -gx GEMINI_API_KEY "$GEMINI_API_KEY"。')
        return 2
    project = Path(__file__).resolve().parents[1]
    settings = Settings(hgs_repo_path=Path("/home/runqiu/BRPWR-HGSADC-SBC"),
                        gurobi_repo_path=Path("/home/runqiu/BRPWR-Gurobi"), runs_dir=project / "runs")
    config = GeminiBudgetConfig(budget_hkd=1.31328, max_calls=6)
    if (settings.runs_dir / "llm" / BUDGET_ID).exists():
        print(f"预算目录已存在，拒绝重开预算或重复验收：{settings.runs_dir / 'llm' / BUDGET_ID}")
        return 2
    prior_folders = sorted((settings.runs_dir / "llm").glob("stage7-gemini*"))
    prior_reserved = sum((Decimal(json.loads((path / "ledger.json").read_bytes())["reserved_hkd"])
                          for path in prior_folders if (path / "ledger.json").is_file()), Decimal(0))
    manual_probe_reserve = Decimal("0.02")
    combined_max = prior_reserved + manual_probe_reserve + config.reservation_hkd * config.max_calls
    if combined_max > Decimal(10):
        print("累计保守预留将超过用户 10 HKD 上限，拒绝启动。")
        return 2
    previous_hashes = {str(path.relative_to(settings.runs_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
                       for folder in prior_folders for path in folder.rglob("*") if path.is_file()}
    output = GeminiScenarioInterpreter.create_budget(settings, budget_id=BUDGET_ID, config=config)
    plan = {"api": "interactions", "user_budget_hkd": 10, "prior_reserved_hkd": str(prior_reserved),
            "user_manual_probe_reserve_hkd": str(manual_probe_reserve),
            "batch_max_reservation_hkd": str(config.reservation_hkd * config.max_calls),
            "combined_max_reservation_hkd": str(combined_max), "max_generation_attempts": 6,
            "per_http_timeout_sec": config.network_timeout_sec, "solver_calls": 0,
            "failure_policy": "stop_on_first_exception", "previous_evidence_sha256": previous_hashes,
            "cases": [{"case": name, "request": text, "expected_status": expected} for name, text, expected in CASES]}
    (output / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    interpreter = GeminiScenarioInterpreter(settings, budget_id=BUDGET_ID)
    intake = ScenarioIntakeService(SolverService(settings), interpreter=interpreter)
    context = ScenarioSpec.model_validate_json((project / "examples/scenario_6_1.json").read_bytes())
    print(f"最多6次生成；累计最坏预留 {combined_max} HKD（含旧失败与用户手动验证预留）；solver调用0次。", flush=True)
    outcomes = run_cases(intake, context, output)
    states = [json.loads(path.read_bytes()) for path in sorted(output.glob("call-*/state.json"))]
    unchanged = all(hashlib.sha256((settings.runs_dir / path).read_bytes()).hexdigest() == digest
                    for path, digest in previous_hashes.items())
    summary = {"api": "interactions", "model": config.model, "budget_hkd": config.budget_hkd,
               "user_budget_hkd": 10, "combined_max_reservation_hkd": str(combined_max),
               "previous_evidence_unchanged": unchanged,
               "worst_case_reserved_hkd": str(config.reservation_hkd * len(states)),
               "generation_attempts": sum(s.get("generation_started", False) for s in states),
               "solver_calls": 0, "passed": unchanged and all(case["passed"] for case in outcomes), "cases": outcomes,
               "usage_cost_estimates_hkd": [s.get("estimated_usage_hkd") for s in states]}
    (output / "acceptance.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"验收记录：{output / 'acceptance.json'}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
