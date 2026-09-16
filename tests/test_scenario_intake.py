"""Language outputs remain untrusted; unit tests never call a hosted LLM."""

import json
from unittest.mock import Mock

import pytest

from mobilityops.domain import BackendName
from mobilityops.domain.intake import LanguageInput, ProposedField, ScenarioDraft, ScenarioExtraction, InterpretationIssue
from mobilityops.services.scenario_intake import ScenarioIntakeService
from mobilityops.services.solver_service import SolverService


def field(name, value, quote="容量30", index=0):
    return ProposedField(field=name, value=value, evidence=quote, message_index=index)


def service(settings, fields=(), issues=()):
    interpreter = Mock()
    interpreter.extract.return_value = ScenarioExtraction(fields=fields, issues=issues)
    return ScenarioIntakeService(SolverService(settings), interpreter=interpreter)


def test_candidate_uses_explicit_context_and_never_executes(settings, scenario):
    intake = service(settings, (field("truck_capacity", 30),))
    intake.solver_service.solve_selected = Mock()
    intake.solver_service.preflight = Mock(side_effect=AssertionError("No preflight during interpretation"))
    draft = intake.propose("容量30", context=scenario, backend=BackendName.HGS)
    assert draft.status == "ready" and draft.candidate.truck_capacity == 30
    assert draft.field_sources["truck_capacity"] == "user_text"
    assert draft.field_sources["solver_runtime_limit_sec"] == "context"
    assert draft.decision.selected_backend == BackendName.HGS
    assert ScenarioDraft.model_validate_json(draft.model_dump_json()) == draft
    intake.solver_service.solve_selected.assert_not_called()
    assert not settings.runs_dir.exists()


def test_missing_fields_become_specific_clarifications(settings):
    draft = service(settings, (field("truck_capacity", 30),)).propose("容量30")
    assert draft.status == "needs_clarification" and draft.candidate is None
    missing = {q.field for q in draft.clarifications}
    assert "solver_runtime_limit_sec" in missing and "objective_profile" in missing
    assert "truck_capacity" not in missing
    assert "seed" not in missing and "broken_bike_proportion" not in missing
    assert not settings.runs_dir.exists()


@pytest.mark.parametrize("name,value", [("truck_capacity", 0), ("number_of_trucks", True),
                                       ("loading_time_sec", 1.5), ("solver_runtime_limit_sec", -1),
                                       ("objective_profile", "auto"), ("backend", "unknown")])
def test_invalid_proposals_not_coerced_into_execution(settings, scenario, name, value):
    intake = service(settings, (field(name, value),))
    draft = intake.propose("容量30", context=scenario)
    assert draft.status == "needs_clarification"
    assert any(q.field == name for q in draft.clarifications)
    with pytest.raises(ValueError, match="Resolve"):
        intake.prepare_execution(draft, experiment_id="invalid", report_id="invalid", external_timeout_sec=10)


@pytest.mark.parametrize("name,value,backend", [("seed", 1, BackendName.HGS),
                                               ("objective_profile", "eudf_default", BackendName.GUROBI)])
def test_capability_rejection_survives_language_layer(settings, scenario, name, value, backend):
    intake = service(settings, (field(name, value),))
    draft = intake.propose("容量30", context=scenario, backend=backend)
    assert draft.status == "incompatible" and draft.decision.selected_backend is None
    with pytest.raises(ValueError):
        intake.prepare_execution(draft, experiment_id="incompatible", report_id="incompatible", external_timeout_sec=10)


@pytest.mark.parametrize("quote,index", [("invented evidence", 0), ("容量30", 8)])
def test_fabricated_provenance_blocks_execution(settings, scenario, quote, index):
    draft = service(settings, (field("truck_capacity", 30, quote, index),)).propose("容量30", context=scenario)
    assert draft.status == "needs_clarification"
    assert draft.clarifications[0].kind == "provenance"


def test_conflicting_values_ask_instead_of_last_value_wins(settings, scenario):
    intake = service(settings, (field("truck_capacity", 30), field("truck_capacity", 40)))
    draft = intake.propose("容量30或40", context=scenario)
    assert draft.candidate is None and draft.status == "needs_clarification"
    assert "truck_capacity" not in draft.values
    assert any(q.kind == "ambiguous" for q in draft.clarifications)
    intake.interpreter.extract.return_value = ScenarioExtraction(fields=(field("truck_capacity", 40, "选40", 1),), issues=())
    clarified = intake.revise(draft, "选40")
    assert clarified.status == "ready" and clarified.candidate.truck_capacity == 40


def test_unsupported_requirement_cannot_disappear_on_followup(settings, scenario):
    issue = InterpretationIssue(field=None, question="当前模型没有时间窗，请修改或明确撤回此要求。", evidence="每站时间窗", message_index=0)
    intake = service(settings, issues=(issue,))
    draft = intake.propose("每站时间窗", context=scenario)
    intake.interpreter.extract.return_value = ScenarioExtraction(fields=(field("truck_capacity", 30, "容量30", 1),), issues=())
    still_blocked = intake.revise(draft, "容量30")
    assert still_blocked.status == "needs_clarification" and still_blocked.extraction.issues == (issue,)
    intake.interpreter.extract.return_value = ScenarioExtraction(fields=(), issues=())
    retracted = intake.revise(still_blocked, "撤回时间窗要求", withdraw_issues=(0,))
    assert retracted.status == "ready" and retracted.candidate.truck_capacity == 30


def test_followup_preserves_prior_explicit_values(settings, scenario):
    intake = service(settings, (field("truck_capacity", 30),))
    draft = intake.propose("容量30", context=scenario)
    intake.interpreter.extract.return_value = ScenarioExtraction(fields=(), issues=())
    revised = intake.revise(draft, "看看候选")
    assert revised.candidate.truck_capacity == 30
    assert revised.draft_sha256 != draft.draft_sha256
    assert revised.input.messages == ("容量30", "看看候选")


def test_malformed_schema_cannot_add_execution_actions():
    with pytest.raises(ValueError):
        ScenarioExtraction.model_validate({"fields": [], "issues": [], "execute": True})
    with pytest.raises(ValueError):
        field("shell_command", "rm -rf /anything")


@pytest.mark.parametrize("text", ["", " ", "x" * 4001])
def test_empty_or_oversized_input_rejected_before_llm(settings, text):
    intake = service(settings)
    with pytest.raises(ValueError):
        intake.propose(text)
    intake.interpreter.extract.assert_not_called()


def test_prepare_is_side_effect_free_and_binds_single_call(settings, scenario):
    intake = service(settings, (field("truck_capacity", 30),))
    draft = intake.propose("容量30", context=scenario)
    request = intake.prepare_execution(draft, experiment_id="ready", report_id="report", external_timeout_sec=10)
    assert request.plan.max_calls == request.plan.planned_calls == 1
    assert request.plan.external_wait_budget_sec == 10
    assert request.plan.baseline.scenario == draft.candidate
    assert request.draft_sha256 == draft.draft_sha256
    assert not settings.runs_dir.exists()


@pytest.mark.parametrize("modification", ["confirmation", "candidate", "budget", "draft_revision"])
def test_execution_rejects_unconfirmed_or_changed_requests(settings, scenario, modification):
    intake = service(settings, (field("truck_capacity", 30),))
    draft = intake.propose("容量30", context=scenario)
    request = intake.prepare_execution(draft, experiment_id="gated", report_id="report", external_timeout_sec=10)
    confirmation = request.request_sha256
    if modification == "confirmation":
        confirmation = "yes execute"
    elif modification == "candidate":
        draft = draft.model_copy(update={"candidate": scenario})
    elif modification == "budget":
        request = request.model_copy(update={"plan": request.plan.model_copy(update={"max_calls": 2})})
    else:
        intake.interpreter.extract.return_value = ScenarioExtraction(fields=(field("truck_capacity", 40, "容量40", 1),), issues=())
        draft = intake.revise(draft, "容量40")
    with pytest.raises(ValueError):
        intake.execute(draft, request, confirmed_request_sha256=confirmation)
    assert not settings.runs_dir.exists()


def test_explicit_execution_reuses_verified_batch_and_reports(settings, scenario):
    intake = service(settings, (field("truck_capacity", 30),))
    draft = intake.propose("容量30", context=scenario)
    request = intake.prepare_execution(draft, experiment_id="approved", report_id="approved-report", external_timeout_sec=10)
    receipt = intake.execute(draft, request, confirmed_request_sha256=request.request_sha256)
    assert receipt.batch.calls_invoked == 1 and receipt.batch.attempts[0].status == "succeeded"
    assert receipt.report.summaries[0].verified_candidates == 1
    assert (settings.runs_dir / "experiment-reports/approved-report/report.md").is_file()
    state = json.loads((settings.runs_dir / "language-requests/approved/state.json").read_bytes())
    assert state["status"] == "reported"
    with pytest.raises(ValueError, match="already exists"):
        intake.execute(draft, request, confirmed_request_sha256=request.request_sha256)


def test_report_collision_refuses_solver_execution(settings, scenario):
    intake = service(settings, (field("truck_capacity", 30),))
    draft = intake.propose("容量30", context=scenario)
    request = intake.prepare_execution(draft, experiment_id="unused", report_id="existing", external_timeout_sec=10)
    (settings.runs_dir / "experiment-reports/existing").mkdir(parents=True)
    intake.solver_service.solve_selected = Mock()
    with pytest.raises(ValueError, match="Report ID"):
        intake.execute(draft, request, confirmed_request_sha256=request.request_sha256)
    intake.solver_service.solve_selected.assert_not_called()


def test_whole_transcript_size_is_bounded(settings, scenario):
    intake = service(settings)
    request = LanguageInput(messages=("x" * 4000,) * 6, context=scenario)
    with pytest.raises(ValueError, match="20000"):
        intake.evaluate(request, ScenarioExtraction(fields=(), issues=()))
