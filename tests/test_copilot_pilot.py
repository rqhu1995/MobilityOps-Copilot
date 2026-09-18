"""Stage 14 runs a fully audited shadow session without solver execution."""

import json

import pytest

from mobilityops.services.copilot_pilot import CopilotShadowPilotService
from mobilityops.services.gemini_intake import (
    GeminiBudgetConfig,
    GeminiCopilotInterpreter,
    GeminiExperimentInterpreter,
)
from test_copilot import FakeTransport, decision, extraction


OBJECTIVE = (
    "以 baseline 为基准，比较 capacity30 容量30；每个1次，使用HGS，"
    "内部5秒，外部10秒，最大2次，总预算20秒，失败继续。"
)
QUESTION = "根据证据，计划多少次 solver 调用和多少秒等待预算？能证明更优吗？"


def service(settings, outputs):
    transport = FakeTransport(outputs)
    return CopilotShadowPilotService(
        settings,
        router_factory=lambda budget_id: GeminiCopilotInterpreter(
            settings, budget_id=budget_id, transport=transport
        ),
        experiment_interpreter_factory=lambda budget_id: GeminiExperimentInterpreter(
            settings, budget_id=budget_id, transport=transport
        ),
    ), transport


def run_pilot(settings, scenario, outputs, *, suffix="ok"):
    runner, transport = service(settings, outputs)
    report = runner.run(
        pilot_id=f"pilot-{suffix}",
        session_id=f"shadow-{suffix}",
        audit_id=f"audit-{suffix}",
        budget=GeminiBudgetConfig(budget_hkd=1, max_calls=4),
        context=scenario,
        objective=OBJECTIVE,
        evidence_question=QUESTION,
    )
    return report, transport


def test_shadow_pilot_drafts_reviews_refuses_execution_answers_and_audits(
    settings, scenario
):
    outputs = [
        decision("draft_experiment"),
        extraction(),
        decision("prepare_execution_review"),
        decision(
            "answer",
            "计划2次 solver 调用，等待预算20秒；不能证明更优。",
            evidence=("evidence-001", "evidence-002"),
        ),
    ]
    report, transport = run_pilot(settings, scenario, outputs)
    assert report.audit_status == "verified_no_execution"
    assert report.decisions == (
        "draft_experiment", "prepare_execution_review", "answer"
    )
    assert report.calls_succeeded == report.calls_reserved == 4
    assert report.planned_solver_calls == 2
    assert report.planned_external_wait_sec == 20
    assert report.solver_calls == report.license_probes == 0
    assert [method for method, _, _ in transport.calls] == [
        "countTokens", "interactions",
        "countTokens", "interactions",
        "countTokens", "interactions",
        "countTokens", "interactions",
    ]
    pilot = settings.runs_dir / "copilot-pilots/pilot-ok"
    assert (pilot / "report.json").is_file()
    assert not list(settings.runs_dir.glob("copilot-shadow-ok-*-r*"))


def test_shadow_pilot_stops_after_unexpected_action_without_solver(
    settings, scenario
):
    runner, transport = service(settings, [decision("clarify", "请补充要求。")])
    with pytest.raises(RuntimeError, match="session failed"):
        runner.run(
            pilot_id="pilot-stop", session_id="shadow-stop", audit_id="audit-stop",
            budget=GeminiBudgetConfig(budget_hkd=1, max_calls=4),
            context=scenario, objective=OBJECTIVE, evidence_question=QUESTION,
        )
    failure = json.loads(
        (settings.runs_dir / "copilot-pilots/pilot-stop/failure.json").read_bytes()
    )
    assert failure["solver_attempts"] == 0
    assert failure["retry_allowed"] is False
    assert [method for method, _, _ in transport.calls] == [
        "countTokens", "interactions"
    ]


@pytest.mark.parametrize(
    "config",
    [GeminiBudgetConfig(budget_hkd=1, max_calls=5),
     GeminiBudgetConfig(budget_hkd=1.1, max_calls=4)],
)
def test_shadow_pilot_rejects_expanded_budget_before_creating_evidence(
    settings, scenario, config
):
    runner, _ = service(settings, [])
    with pytest.raises(ValueError, match="exactly 4 calls|at most 1 HKD"):
        runner.run(
            pilot_id="pilot-budget", session_id="shadow-budget",
            audit_id="audit-budget", budget=config, context=scenario,
            objective=OBJECTIVE, evidence_question=QUESTION,
        )
    assert not (settings.runs_dir / "copilot-pilots/pilot-budget").exists()


def test_shadow_pilot_refuses_existing_identity(settings, scenario):
    root = settings.runs_dir / "copilot-pilots/pilot-existing"
    root.mkdir(parents=True)
    runner, _ = service(settings, [])
    with pytest.raises(ValueError, match="refusing overwrite"):
        runner.run(
            pilot_id="pilot-existing", session_id="shadow-existing",
            audit_id="audit-existing",
            budget=GeminiBudgetConfig(budget_hkd=1, max_calls=4),
            context=scenario, objective=OBJECTIVE, evidence_question=QUESTION,
        )
