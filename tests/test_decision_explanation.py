"""Stage 10 explains replayed evidence and never executes a proposed follow-up."""

import hashlib
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from mobilityops import cli
from mobilityops.domain import BackendName
from mobilityops.domain.decision_explanation import DecisionExplanation, NextExperimentProposal
from mobilityops.domain.experiment import ExperimentCase, ExperimentPlan
from mobilityops.services.decision_explanation import DecisionExplanationService
from mobilityops.services.decision_terminal_session import DecisionTerminalSession
from mobilityops.services.experiment_service import ExperimentService
from mobilityops.services.solver_service import SolverService
from mobilityops.solvers.gurobi.backend import GurobiBackend
from test_gurobi import case


def make_plan(scenario, *, repetitions=2, variant=None, backend=BackendName.HGS):
    baseline = ExperimentCase(
        case_id="base", scenario=scenario, backend=backend, repetitions=repetitions,
        external_timeout_sec=scenario.solver_runtime_limit_sec + 5,
    )
    variant = variant or baseline.model_copy(update={
        "case_id": "capacity", "scenario": scenario.model_copy(update={"truck_capacity": 30}),
    })
    return ExperimentPlan(
        baseline=baseline, variants=(variant,), max_calls=repetitions * 2,
        external_wait_budget_sec=(baseline.external_timeout_sec + variant.external_timeout_sec) * repetitions,
        failure_policy="continue",
    )


def digest(root: Path):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


class Script:
    def __init__(self, inputs):
        self.inputs = iter(inputs)
        self.output = []

    def read(self, prompt):
        self.output.append(prompt)
        return next(self.inputs)

    def write(self, value):
        self.output.append(value)

    @property
    def text(self):
        return "\n".join(self.output)


def test_explanation_is_deterministic_evidence_bound_and_read_only(settings, scenario):
    ExperimentService(SolverService(settings)).execute(make_plan(scenario), experiment_id="decision")
    before = digest(settings.runs_dir)
    service = DecisionExplanationService(settings)
    first = service.explain("decision")
    second = service.explain("decision")
    assert first == second == DecisionExplanation.model_validate_json(first.model_dump_json())
    assert digest(settings.runs_dir) == before
    assert [claim.category for claim in first.verified_facts] == ["verified_fact"] * 3
    assert any(claim.code == "median_difference" for claim in first.descriptive_observations)
    assert first.cannot_conclude[0].code == "no_causal_or_optimality_claim"
    assert all(claim.evidence for claim in (*first.verified_facts, *first.descriptive_observations))


def test_compare_filters_one_variant_and_rejects_unknown(settings, scenario):
    first = make_plan(scenario).variants[0]
    second = first.model_copy(update={
        "case_id": "trucks", "scenario": scenario.model_copy(update={"number_of_trucks": 2}),
    })
    plan = make_plan(scenario).model_copy(update={"variants": (first, second), "max_calls": 6,
                                                  "external_wait_budget_sec": 60})
    ExperimentService(SolverService(settings)).execute(plan, experiment_id="filter")
    explanation = DecisionExplanationService(settings).explain("filter", variant_case_id="trucks")
    assert {claim.case_id for claim in explanation.verified_facts} == {None, "base", "trucks"}
    assert all(claim.case_id != "capacity" for claim in explanation.descriptive_observations)
    with pytest.raises(ValueError, match="Unknown variant"):
        DecisionExplanationService(settings).explain("filter", variant_case_id="missing")


def test_next_plan_is_bounded_candidate_and_never_executes(settings, scenario, monkeypatch):
    ExperimentService(SolverService(settings)).execute(make_plan(scenario), experiment_id="next")
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=AssertionError("must remain offline")))
    proposal = DecisionExplanationService(settings).next_plan("next")
    assert proposal == NextExperimentProposal.model_validate_json(proposal.model_dump_json())
    assert proposal.plan is not None and proposal.plan.planned_calls == proposal.plan.max_calls == 6
    assert proposal.plan.external_wait_budget_sec == 60
    assert proposal.auto_execute is False and proposal.requires_new_review_and_confirmation is True
    assert not (settings.runs_dir / "next-c001-r0003").exists()


def test_search_change_proposal_aligns_conditions(settings, scenario):
    base = make_plan(scenario, repetitions=1).baseline
    variant = base.model_copy(update={
        "case_id": "longer", "scenario": scenario.model_copy(update={"solver_runtime_limit_sec": 6.0}),
        "external_timeout_sec": 11,
    })
    ExperimentService(SolverService(settings)).execute(
        make_plan(scenario, repetitions=1, variant=variant), experiment_id="search-next"
    )
    proposal = DecisionExplanationService(settings).next_plan("search-next")
    assert proposal.plan is not None
    assert proposal.plan.variants[0].scenario.solver_runtime_limit_sec == scenario.solver_runtime_limit_sec
    assert proposal.plan.variants[0].external_timeout_sec == proposal.plan.baseline.external_timeout_sec
    assert "对齐" in proposal.rationale[0]


def test_no_candidate_blocks_metric_claims_and_next_plan(settings, scenario, monkeypatch):
    monkeypatch.setattr(subprocess, "run", Mock(
        return_value=subprocess.CompletedProcess(["fake"], 2, b"failed", b"error")
    ))
    ExperimentService(SolverService(settings)).execute(make_plan(scenario), experiment_id="none")
    service = DecisionExplanationService(settings)
    explanation = service.explain("none")
    assert not any(claim.code == "median_difference" for claim in explanation.descriptive_observations)
    assert any(claim.code == "not_comparable" for claim in explanation.cannot_conclude)
    proposal = service.next_plan("none")
    assert proposal.plan is None and proposal.blockers and proposal.auto_execute is False


def test_tampered_run_is_excluded_and_cannot_produce_difference(settings, scenario):
    batch = ExperimentService(SolverService(settings)).execute(
        make_plan(scenario, repetitions=1), experiment_id="tampered-decision"
    )
    path = settings.runs_dir / batch.attempts[1].run_id / "solver-result.txt"
    path.write_text("tampered")
    service = DecisionExplanationService(settings)
    explanation = service.explain("tampered-decision")
    assert not any(claim.code == "median_difference" for claim in explanation.descriptive_observations)
    assert any(claim.code == "not_comparable" for claim in explanation.cannot_conclude)
    assert service.next_plan("tampered-decision").plan is None


def test_cross_backend_comparison_does_not_create_unified_plan(settings, scenario, case):
    gurobi_settings, gurobi_scenario, options, _ = case
    configured = settings.model_copy(update={"gurobi_repo_path": gurobi_settings.gurobi_repo_path})
    variant = ExperimentCase(
        case_id="gurobi", scenario=gurobi_scenario, backend=BackendName.GUROBI,
        repetitions=1, external_timeout_sec=10,
    )
    plan = make_plan(scenario, repetitions=1, variant=variant).model_copy(
        update={"external_wait_budget_sec": 20}
    )
    ExperimentService(SolverService(
        configured, gurobi_backend=GurobiBackend(configured, options=options)
    )).execute(plan, experiment_id="cross-decision")
    service = DecisionExplanationService(configured)
    explanation = service.explain("cross-decision")
    assert any(claim.code == "not_comparable" for claim in explanation.cannot_conclude)
    assert service.next_plan("cross-decision").plan is None


def test_terminal_commands_write_explanations_and_candidate_only(settings, scenario, monkeypatch):
    ExperimentService(SolverService(settings)).execute(make_plan(scenario), experiment_id="terminal-review")
    run_count = len(list(settings.runs_dir.glob("terminal-review-c*-r*")))
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=AssertionError("must remain offline")))
    io = Script(["/explain", "/compare capacity baseline", "/next-plan", "/quit"])
    session = DecisionTerminalSession(
        settings, io=io, session_id="review-session", experiment_id="terminal-review"
    )
    assert session.run() == 0
    assert "已复核的事实" in io.text and "不能据此得出的结论" in io.text
    artifacts = sorted(session.folder.glob("*.json"))
    assert any(path.name.endswith("-next-plan.json") for path in artifacts)
    assert len(list(settings.runs_dir.glob("terminal-review-c*-r*"))) == run_count
    proposal_path = next(path for path in artifacts if path.name.endswith("-next-plan.json"))
    assert json.loads(proposal_path.read_bytes())["auto_execute"] is False


def test_cli_analysis_mode_uses_review_session_without_budget_or_solver(settings, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    factory = Mock()
    factory.return_value.run.return_value = 0
    monkeypatch.setattr(cli, "DecisionTerminalSession", factory)
    assert cli.main([
        "--mode", "analysis", "--experiment-id", "saved-experiment",
        "--session-id", "saved-review", "--budget-hkd", "0",
        "--hgs-repo-path", str(settings.hgs_repo_path),
        "--gurobi-repo-path", str(settings.gurobi_repo_path),
        "--runs-dir", str(settings.runs_dir),
    ]) == 0
    assert factory.call_args.kwargs["experiment_id"] == "saved-experiment"
    assert not settings.runs_dir.exists()
