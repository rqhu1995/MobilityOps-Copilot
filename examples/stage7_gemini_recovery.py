"""One explicit recovery attempt in the original six-call budget; no solver calls."""

import hashlib
import json
import os
from pathlib import Path
import time

from mobilityops.config import Settings
from mobilityops.domain import BackendName, ScenarioSpec
from mobilityops.services.experiment_service import write_json
from mobilityops.services.gemini_intake import GeminiCallError, GeminiScenarioInterpreter, GeminiTransport
from mobilityops.services.scenario_intake import ScenarioIntakeService
from mobilityops.services.solver_service import SolverService


BUDGET_ID = "stage7-gemini-20260916"


def run_recovery(settings: Settings, context: ScenarioSpec, *, transport: GeminiTransport | None = None) -> dict:
    interpreter = GeminiScenarioInterpreter(settings, budget_id=BUDGET_ID, transport=transport)
    budget = interpreter.folder
    ledger = json.loads((budget / "ledger.json").read_bytes())
    if ledger["calls_reserved"] != 5 or interpreter.config.max_calls != 6:
        raise ValueError("Recovery requires the original budget with exactly one attempt remaining")
    # The exclusive receipt directory also prevents rerunning an interrupted recovery.
    output = budget / "recovery-001"
    output.mkdir(mode=0o700)
    evidence = [budget / "acceptance.json", budget / "config.json", budget / "pricing.json"]
    evidence.extend(path for folder in sorted(budget.glob("call-00[1-5]"))
                    for path in sorted(folder.iterdir()) if path.is_file())
    hashes = {str(path.relative_to(budget)): hashlib.sha256(path.read_bytes()).hexdigest() for path in evidence}
    write_json(output / "original-evidence-sha256.json", hashes)
    intake = ScenarioIntakeService(SolverService(settings), interpreter=interpreter)
    started = time.monotonic()
    result = {"model": interpreter.config.model, "case": "capacity_recovery", "passed": False,
              "original_acceptance_passed": False, "full_six_case_acceptance_passed": False,
              "solver_calls": 0, "evidence": "../call-006/state.json"}
    try:
        draft = intake.propose("以给定基准为基础，把车辆容量提高到30辆自行车，其余参数保持不变。",
                               context=context, backend=BackendName.HGS)
        write_json(output / "draft.json", draft.model_dump(mode="json"))
        expected = context.model_copy(update={"truck_capacity": 30})
        checks = {"ready": draft.status == "ready", "only_capacity_changed": draft.candidate == expected,
                  "backend_preserved": draft.requested_backend == BackendName.HGS}
        result.update(status=draft.status, checks=checks, passed=all(checks.values()))
        if result["passed"]:
            request = intake.prepare_execution(draft, experiment_id=BUDGET_ID + "-recovery",
                                               report_id=BUDGET_ID + "-recovery", external_timeout_sec=10)
            write_json(output / "reviewed-execution-request.json", request.model_dump(mode="json"))
    except Exception as exc:
        result.update(passed=False, error_type=type(exc).__name__)
        if isinstance(exc, GeminiCallError):
            result["diagnostic"] = exc.diagnostic
    result["elapsed_sec"] = time.monotonic() - started
    result["original_evidence_unchanged"] = all(
        hashlib.sha256((budget / path).read_bytes()).hexdigest() == digest for path, digest in hashes.items())
    result["passed"] = result["passed"] and result["original_evidence_unchanged"]
    result["ledger"] = json.loads((budget / "ledger.json").read_bytes())
    write_json(output / "acceptance.json", result)
    return result


def main() -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        print("当前 Python 进程缺少 GEMINI_API_KEY；请在已有变量的终端先运行 export GEMINI_API_KEY。")
        return 2
    project = Path(__file__).resolve().parents[1]
    settings = Settings(hgs_repo_path=Path("/home/runqiu/BRPWR-HGSADC-SBC"),
                        gurobi_repo_path=Path("/home/runqiu/BRPWR-Gurobi"), runs_dir=project / "runs")
    context = ScenarioSpec.model_validate_json((project / "examples/scenario_6_1.json").read_bytes())
    print("仅使用原预算剩余1次；累计最多预留1.31328 HKD；solver调用0次。", flush=True)
    try:
        result = run_recovery(settings, context)
    except Exception as exc:
        print(f"恢复前置检查未通过：{type(exc).__name__}；未重开预算。")
        return 2
    print(json.dumps({key: result[key] for key in ("passed", "error_type", "diagnostic", "ledger")
                      if key in result}, ensure_ascii=False), flush=True)
    print(f"恢复记录：{settings.runs_dir / 'llm' / BUDGET_ID / 'recovery-001/acceptance.json'}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
