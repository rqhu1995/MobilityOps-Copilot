"""Natural-language plan proposals never bypass deterministic experiment contracts."""

from pathlib import Path

import pytest

from mobilityops.domain import BackendName
from mobilityops.domain.experiment_intake import (
    ExperimentExtraction, ProposedExperimentCase, ProposedExperimentField,
)
from mobilityops.services.experiment_intake import (
    ExperimentPlanIntakeService, ExperimentPlanPrecheckError,
)
from mobilityops.services.solver_service import SolverService
from mobilityops.solvers.gurobi.backend import GurobiBackend
from mobilityops.solvers.gurobi.contracts import GurobiOptions
from mobilityops.solvers.hgs.backend import HgsBackend


TEXT = ("以 baseline 为基准，比较 capacity30 容量30 和 two_trucks 车辆2；"
        "每个3次，使用HGS，外部10秒，最大9次，总预算90秒，失败继续；执行。")


def field(target, name, value, evidence, index=0):
    return ProposedExperimentField(target=target, field=name, value=value,
                                   message_index=index, evidence=evidence)


def complete_extraction(*, backend="hgs", max_calls=9, wait=90, repetitions=3,
                        baseline_id="baseline", variant_ids=("capacity30", "two_trucks")):
    cases = [ProposedExperimentCase(role="baseline", case_id=baseline_id,
                                    message_index=0, evidence="baseline")]
    evidence = {"capacity30": "capacity30", "two_trucks": "two_trucks"}
    cases += [ProposedExperimentCase(role="variant", case_id=case_id,
                                     message_index=0, evidence=evidence.get(case_id, case_id))
              for case_id in variant_ids]
    fields = [
        field("plan", "backend", backend, "HGS"),
        field("plan", "repetitions", repetitions, "每个3次"),
        field("plan", "external_timeout_sec", 10, "外部10秒"),
        field("plan", "max_calls", max_calls, "最大9次"),
        field("plan", "external_wait_budget_sec", wait, "总预算90秒"),
        field("plan", "failure_policy", "continue", "失败继续"),
    ]
    if "capacity30" in variant_ids:
        fields.append(field("capacity30", "truck_capacity", 30, "容量30"))
    if "two_trucks" in variant_ids:
        fields.append(field("two_trucks", "number_of_trucks", 2, "车辆2"))
    return ExperimentExtraction(cases=tuple(cases), fields=tuple(fields), issues=())


class StaticInterpreter:
    def __init__(self, extraction):
        self.extraction = extraction

    def extract(self, request):
        return self.extraction


def service(settings, extraction, *, solver=None):
    solver = solver or SolverService(settings)
    return ExperimentPlanIntakeService(solver, interpreter=StaticInterpreter(extraction))


def test_complete_three_case_plan_is_rebuilt_from_context(settings, scenario):
    draft = service(settings, complete_extraction()).propose(TEXT, context=scenario)
    assert draft.status == "ready"
    assert draft.plan.planned_calls == draft.plan.max_calls == 9
    assert draft.plan.external_wait_budget_sec == 90
    assert [(case.case_id, case.scenario.truck_capacity, case.scenario.number_of_trucks)
            for case in draft.plan.cases] == [
                ("baseline", 25, 1), ("capacity30", 30, 1), ("two_trucks", 25, 2),
            ]
    assert all(decision.selected_backend is BackendName.HGS for decision in draft.decisions.values())
    assert not settings.runs_dir.exists()


def test_missing_repetitions_blocks_plan(settings, scenario):
    extraction = complete_extraction()
    extraction = extraction.model_copy(update={
        "fields": tuple(item for item in extraction.fields if item.field != "repetitions")
    })
    draft = service(settings, extraction).propose(TEXT, context=scenario)
    assert draft.status == "needs_clarification"
    assert any(question.field == "baseline.repetitions" for question in draft.clarifications)


@pytest.mark.parametrize(("max_calls", "wait", "field_name"), [
    (8, 90, "max_calls"), (9, 89, "external_wait_budget_sec"),
])
def test_conflicting_solver_budget_blocks_plan(settings, scenario, max_calls, wait, field_name):
    draft = service(settings, complete_extraction(max_calls=max_calls, wait=wait)).propose(TEXT, context=scenario)
    assert draft.status == "needs_clarification"
    assert any(question.kind == "conflict" and question.field == field_name
               for question in draft.clarifications)


@pytest.mark.parametrize(("baseline_id", "variant_ids"), [
    ("baseline", ("capacity30", "capacity30")),
    ("../baseline", ("capacity30", "two_trucks")),
])
def test_duplicate_or_unsafe_case_id_blocks_plan(settings, scenario, baseline_id, variant_ids):
    extraction = complete_extraction(baseline_id=baseline_id, variant_ids=variant_ids)
    draft = service(settings, extraction).propose(TEXT.replace("baseline", baseline_id), context=scenario)
    assert draft.status == "needs_clarification"
    assert any(question.field == "case_id" for question in draft.clarifications)


def test_hgs_seed_is_preserved_and_rejected_by_capability(settings, scenario):
    extraction = complete_extraction()
    extraction = extraction.model_copy(update={
        "fields": (*extraction.fields, field("baseline", "seed", 42, "执行"))
    })
    draft = service(settings, extraction).propose(TEXT, context=scenario)
    assert draft.plan.baseline.scenario.seed == 42
    assert draft.status == "incompatible"
    assert draft.decisions["baseline"].selected_backend is None


def test_gurobi_target_mismatch_is_not_rewritten(settings, scenario):
    gurobi = GurobiBackend(settings, options=GurobiOptions(
        python_executable=Path("/absolute/fake-python")
    ))
    solver = SolverService(settings, gurobi_backend=gurobi)
    extraction = complete_extraction(backend="gurobi")
    draft = service(settings, extraction, solver=solver).propose(TEXT, context=scenario)
    assert draft.status == "incompatible"
    assert draft.plan.baseline.scenario.objective_profile == "eudf_default"
    assert all(decision.selected_backend is None for decision in draft.decisions.values())


def test_mixed_backends_remain_explicit_and_separately_selected(settings, scenario):
    gurobi = GurobiBackend(settings, options=GurobiOptions(
        python_executable=Path("/absolute/fake-python")
    ))
    solver = SolverService(settings, gurobi_backend=gurobi)
    extraction = complete_extraction(variant_ids=("capacity30",))
    fields = tuple(item for item in extraction.fields
                   if not (item.target == "plan" and item.field == "backend"))
    extraction = extraction.model_copy(update={"fields": (
        *fields,
        field("baseline", "backend", "hgs", "HGS"),
        field("capacity30", "backend", "gurobi", "执行"),
        field("capacity30", "objective_profile", "gurobi_linear_default", "执行"),
    )})
    draft = service(settings, extraction, solver=solver).propose(TEXT, context=scenario)
    assert draft.status == "ready"
    assert [case.backend for case in draft.plan.cases] == [BackendName.HGS, BackendName.GUROBI]
    assert [draft.decisions[case.case_id].selected_backend for case in draft.plan.cases] == [
        BackendName.HGS, BackendName.GUROBI,
    ]


def test_forged_quote_blocks_plan_even_when_values_are_valid(settings, scenario):
    extraction = complete_extraction()
    first = extraction.fields[0].model_copy(update={"evidence": "not in user text"})
    extraction = extraction.model_copy(update={"fields": (first, *extraction.fields[1:])})
    draft = service(settings, extraction).propose(TEXT, context=scenario)
    assert draft.status == "needs_clarification"
    assert any(question.kind == "provenance" for question in draft.clarifications)


def test_precheck_failure_blocks_request_before_any_solver_call(settings, scenario):
    settings.hgs_repo_path.joinpath("build/main").unlink()
    intake = service(settings, complete_extraction())
    draft = intake.propose(TEXT, context=scenario)
    with pytest.raises(ExperimentPlanPrecheckError) as error:
        intake.prepare_execution(draft, experiment_id="precheck-fails", report_id="precheck-fails")
    assert not error.value.precheck.allowed
    assert not settings.runs_dir.exists()


def test_natural_language_execute_word_never_starts_solver(settings, scenario):
    draft = service(settings, complete_extraction()).propose(TEXT, context=scenario)
    assert draft.status == "ready"
    assert not settings.runs_dir.exists()


def test_plan_edit_invalidates_old_request(settings, scenario):
    intake = service(settings, complete_extraction())
    draft = intake.propose(TEXT, context=scenario)
    request = intake.prepare_execution(draft, experiment_id="stale-plan", report_id="stale-plan")
    changed = draft.model_copy(update={"draft_sha256": "0" * 64})
    with pytest.raises(ValueError, match="changed"):
        intake.execute(changed, request, confirmed_request_sha256=request.request_sha256)
    assert not (settings.runs_dir / "experiments/stale-plan").exists()


def test_runtime_configuration_change_invalidates_confirmation(settings, scenario, tmp_path):
    backend = HgsBackend(settings)
    solver = SolverService(settings, hgs_backend=backend)
    intake = service(settings, complete_extraction(), solver=solver)
    draft = intake.propose(TEXT, context=scenario)
    request = intake.prepare_execution(draft, experiment_id="stale-config", report_id="stale-config")
    replacement = tmp_path / "replacement-hgs"
    replacement.write_bytes(backend.executable.read_bytes())
    replacement.chmod(0o700)
    backend.executable = replacement
    with pytest.raises(ValueError, match="does not match"):
        intake.execute(draft, request, confirmed_request_sha256=request.request_sha256)
    assert not (settings.runs_dir / "experiments/stale-config").exists()
