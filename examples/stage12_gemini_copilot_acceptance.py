"""Six bounded Gemini routing checks with no solver access or execution."""

import json
import os
from pathlib import Path
import time

from mobilityops.config import Settings
from mobilityops.domain.copilot import (
    CopilotEvidenceItem,
    CopilotModelInput,
    CopilotStateView,
)
from mobilityops.services.copilot import CopilotDecisionService
from mobilityops.services.gemini_intake import (
    GeminiBudgetConfig,
    GeminiCallError,
    GeminiCopilotInterpreter,
    GeminiScenarioInterpreter,
)


BUDGET_ID = "stage12-gemini-copilot-20260917-v1"


def acceptance_cases() -> tuple[tuple[str, CopilotModelInput, str], ...]:
    current = CopilotStateView(current_experiment_id="saved-experiment")
    ready = CopilotStateView(
        draft_status="ready",
        draft_sha256="a" * 64,
        has_complete_plan=True,
        plan_summary={
            "case_ids": ["baseline", "capacity30"],
            "planned_calls": 6,
            "max_calls": 6,
            "external_wait_budget_sec": 60,
            "backends": ["hgs", "hgs"],
        },
    )
    evidence = CopilotEvidenceItem(
        evidence_id="evidence-001",
        kind="execution_report",
        payload={
            "experiment_id": "saved-experiment",
            "verified_candidates": 6,
            "cannot_conclude": ["不能证明因果效应或全局最优性"],
        },
    )
    return (
        ("draft", CopilotModelInput(
            messages=("请起草一个实验：比较 baseline 和 capacity30，每个3次。",),
            state=CopilotStateView(),
            available_actions=("draft_experiment", "clarify"),
        ), "draft_experiment"),
        ("missing_experiment", CopilotModelInput(
            messages=("请解释当前实验结果。",),
            state=CopilotStateView(),
            available_actions=("draft_experiment", "answer", "clarify"),
        ), "clarify"),
        ("explain", CopilotModelInput(
            messages=("请从已保存证据解释当前实验。",),
            state=current,
            available_actions=("explain_experiment", "clarify"),
        ), "explain_experiment"),
        ("next_plan", CopilotModelInput(
            messages=("为 capacity30 生成下一轮候选计划，不要执行。",),
            state=current,
            available_actions=("propose_next_experiment", "clarify"),
        ), "propose_next_experiment"),
        ("prepare_review", CopilotModelInput(
            messages=("对当前完整计划进行预检查并生成执行审阅，不要确认执行。",),
            state=ready,
            available_actions=("prepare_execution_review", "clarify"),
        ), "prepare_execution_review"),
        ("grounded_answer", CopilotModelInput(
            messages=("根据给定证据，有多少候选通过复核？能证明因果吗？",),
            state=current,
            available_actions=("answer", "clarify"),
            evidence=(evidence,),
        ), "answer"),
    )


def run_cases(service: CopilotDecisionService, output: Path) -> list[dict]:
    outcomes: list[dict] = []
    cases = acceptance_cases()
    for index, (name, request, expected) in enumerate(cases, 1):
        started = time.monotonic()
        try:
            decision = service.decide(request)
            passed = decision.action == expected
            if name == "next_plan":
                passed = passed and decision.variant_case_id == "capacity30"
            if name == "grounded_answer":
                passed = passed and "evidence-001" in decision.evidence_ids
            write = {
                "case": name,
                "expected_action": expected,
                "decision": decision.model_dump(mode="json"),
                "passed": passed,
                "elapsed_sec": time.monotonic() - started,
            }
            (output / f"{index:02d}-{name}.json").write_text(
                json.dumps(write, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            outcomes.append(write)
        except Exception as exc:
            outcome = {
                "case": name,
                "expected_action": expected,
                "passed": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "elapsed_sec": time.monotonic() - started,
            }
            if isinstance(exc, GeminiCallError):
                outcome["diagnostic"] = exc.diagnostic
            outcomes.append(outcome)
            outcomes.extend(
                {"case": next_name, "expected_action": next_expected,
                 "passed": False, "status": "skipped_after_failure"}
                for next_name, _, next_expected in cases[index:]
            )
            print(f"{index}/6 {name}: failed; remaining cases skipped", flush=True)
            break
        print(f"{index}/6 {name}: {'passed' if outcomes[-1]['passed'] else 'failed'}", flush=True)
        if not outcomes[-1]["passed"]:
            outcomes.extend(
                {"case": next_name, "expected_action": next_expected,
                 "passed": False, "status": "skipped_after_failure"}
                for next_name, _, next_expected in cases[index:]
            )
            break
    return outcomes


def main() -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY 未导出；未创建预算或发起请求。")
        return 2
    project = Path(__file__).resolve().parents[1]
    settings = Settings(
        hgs_repo_path=Path("/home/runqiu/BRPWR-HGSADC-SBC"),
        gurobi_repo_path=Path("/home/runqiu/BRPWR-Gurobi"),
        runs_dir=project / "runs",
    )
    config = GeminiBudgetConfig(budget_hkd=1.5, max_calls=6)
    output = settings.runs_dir / "llm" / BUDGET_ID
    if output.exists() or output.is_symlink():
        print(f"预算目录已存在，拒绝重开预算或重复验收：{output}")
        return 2
    output = GeminiScenarioInterpreter.create_budget(
        settings, budget_id=BUDGET_ID, config=config
    )
    plan = {
        "format": "stage12_gemini_copilot_acceptance_v1",
        "model": config.model,
        "max_interactions": config.max_calls,
        "budget_hkd": config.budget_hkd,
        "max_reserved_hkd": str(config.reservation_hkd * config.max_calls),
        "solver_calls": 0,
        "license_probes": 0,
        "failure_policy": "stop_after_first_failed_case",
        "cases": [
            {"case": name, "expected_action": expected,
             "request": request.model_dump(mode="json")}
            for name, request, expected in acceptance_cases()
        ],
    }
    (output / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    service = CopilotDecisionService(
        GeminiCopilotInterpreter(settings, budget_id=BUDGET_ID)
    )
    outcomes = run_cases(service, output)
    states = [
        json.loads(path.read_bytes())
        for path in sorted(output.glob("call-*/state.json"))
    ]
    ledger = json.loads((output / "ledger.json").read_bytes())
    summary = {
        "format": "stage12_gemini_copilot_acceptance_result_v1",
        "model": config.model,
        "budget_hkd": config.budget_hkd,
        "calls_reserved": ledger["calls_reserved"],
        "reserved_hkd": ledger["reserved_hkd"],
        "usage_cost_estimates_hkd": [
            state.get("estimated_usage_hkd") for state in states
        ],
        "solver_calls": 0,
        "license_probes": 0,
        "passed": len(outcomes) == 6 and all(item["passed"] for item in outcomes),
        "cases": outcomes,
    }
    (output / "acceptance.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"验收记录：{output / 'acceptance.json'}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
