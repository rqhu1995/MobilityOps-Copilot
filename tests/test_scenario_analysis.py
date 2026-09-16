import hashlib
import json
from unittest.mock import Mock

import pytest

from mobilityops.domain import BackendName, ObjectiveProfile, ScenarioSpec
from mobilityops.domain.analysis import ScenarioAnalysis, SelectedSolution, SelectionDecision
from mobilityops.services.scenario_analysis import ScenarioAnalysisService, _compare
from mobilityops.services.solver_service import BackendSelectionError, SolverService
from mobilityops.solvers.gurobi.backend import GurobiBackend
from mobilityops.solvers.gurobi.contracts import GurobiOptions
from mobilityops.solvers.hgs import HgsBackend, HgsPreflightError
from test_gurobi import case, rewrite_fake  # Reuse the SDK-free original-output fixture.


def digest_tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def test_selection_is_side_effect_free_and_does_not_claim_runtime_readiness(settings, scenario):
    gurobi = GurobiBackend(settings, options=GurobiOptions(python_executable=settings.runs_dir / "absent"))
    gurobi.solve = Mock()
    hgs = HgsBackend(settings)
    hgs.solve = Mock()
    service = SolverService(settings, hgs_backend=hgs, gurobi_backend=gurobi)
    before = digest_tree(settings.runs_dir.parent)
    for profile, expected in [(ObjectiveProfile.EUDF_DEFAULT, BackendName.HGS),
                              (ObjectiveProfile.GUROBI_LINEAR_DEFAULT, BackendName.GUROBI)]:
        s = scenario.model_copy(update={"objective_profile": profile})
        decision = service.select(s)
        assert decision.selected_backend is expected and decision.scenario == s
        assert all(a.runtime_readiness == "not_checked" for a in decision.assessments)
        assert decision == SelectionDecision.model_validate_json(decision.model_dump_json())
    hgs.solve.assert_not_called()
    gurobi.solve.assert_not_called()
    assert before == digest_tree(settings.runs_dir.parent)


@pytest.mark.parametrize("update,requested", [
    ({"seed": 2}, None), ({}, BackendName.GUROBI),
    ({"objective_profile": ObjectiveProfile.GUROBI_LINEAR_DEFAULT}, None),
])
def test_no_selection_no_execution(settings, scenario, update, requested):
    service = SolverService(settings)
    s = scenario.model_copy(update=update)
    decision = service.select(s, backend=requested)
    assert decision.selected_backend is None
    with pytest.raises(BackendSelectionError) as error:
        service.solve_selected(s, backend=requested, run_id="refused")
    assert error.value.decision == decision
    assert not settings.runs_dir.exists()
    unregistered = decision.assessments[1]
    assert unregistered.compatible is None and not unregistered.registered


def test_registered_gurobi_rejection_reasons(case):
    settings, scenario, _, backend = case
    service = SolverService(settings, gurobi_backend=backend)
    for update, key in [({"seed": 1}, "seed"), ({"broken_bike_proportion": 0.0}, "broken_bike_proportion"),
                        ({"operation_time_budget_sec": 2401.0}, "time_grid")]:
        decision = service.select(scenario.model_copy(update=update))
        assert decision.selected_backend is None
        assert any(key in reason for reason in decision.assessments[1].reasons)
    assert not settings.runs_dir.exists()


def test_ambiguous_selection_requires_explicit_choice(settings, scenario):
    gurobi = GurobiBackend(settings, options=GurobiOptions(python_executable=settings.runs_dir / "absent"))
    gurobi.check_constraints = Mock(return_value=())
    service = SolverService(settings, gurobi_backend=gurobi)
    assert service.select(scenario).selected_backend is None
    assert "Multiple" in service.select(scenario).reason
    assert service.select(scenario, backend=BackendName.HGS).selected_backend is BackendName.HGS


def test_selection_does_not_mask_preflight_failures(settings, scenario):
    (settings.hgs_repo_path / "build/main").unlink()
    service = SolverService(settings)
    assert service.select(scenario).selected_backend is BackendName.HGS
    with pytest.raises(HgsPreflightError):
        service.solve_selected(scenario, run_id="missing-executable")
    assert not settings.runs_dir.exists()


@pytest.mark.parametrize("update", [{"number_of_trucks": 0}, {"solver_runtime_limit_sec": float("inf")}, {"seed": -1}])
def test_selection_revalidates_model_copy(settings, scenario, update):
    with pytest.raises(ValueError):
        SolverService(settings).select(scenario.model_copy(update=update))
    assert not settings.runs_dir.exists()


@pytest.fixture
def hgs_pair(settings, scenario):
    service = SolverService(settings)
    for run_id, s in [("baseline", scenario), ("variant", scenario.model_copy(update={"truck_capacity": 30}))]:
        selected = service.solve_selected(s, run_id=run_id)
        assert selected.result.status == "succeeded"
        assert SelectedSolution.model_validate_json(selected.model_dump_json()) == selected
    return settings, scenario, ScenarioAnalysisService(settings)


def test_hgs_resource_analysis_replays_and_preserves_evidence(hgs_pair):
    settings, _, service = hgs_pair
    before = digest_tree(settings.runs_dir)
    analysis = service.compare(baseline_run_id="baseline", variant_run_ids=("variant",))
    comparison = analysis.comparisons[0]
    assert comparison.kind == "scenario_observations"
    assert [c.field for c in comparison.scenario_changes] == ["truck_capacity"]
    assert not comparison.search_changes
    assert len(comparison.deltas) == 3 and all(d.difference == 0 for d in comparison.deltas)
    assert all(not d.exceeds_reported_rounding for d in comparison.deltas)
    assert analysis == ScenarioAnalysis.model_validate_json(analysis.model_dump_json())
    assert analysis == service.compare(baseline_run_id="baseline", variant_run_ids=("variant",))
    assert before == digest_tree(settings.runs_dir)


@pytest.mark.parametrize("file", ["solution.json", "scenario.json", "solver-result.txt", "command.json",
                                 "process.json", "Instances/6_1/BCRF_1.txt", "Instances/6_1/time_matrix_6.txt"])
def test_corrupt_evidence_fails_closed(hgs_pair, file):
    settings, _, service = hgs_pair
    path = settings.runs_dir / "variant" / file
    path.write_bytes(b"corrupt evidence")
    before = digest_tree(settings.runs_dir)
    with pytest.raises(ValueError):
        service.compare(baseline_run_id="baseline", variant_run_ids=("variant",))
    assert before == digest_tree(settings.runs_dir)


@pytest.mark.parametrize("file", ["solution.json", "scenario.json", "solver-result.txt", "process.json"])
def test_symlink_evidence_rejected(hgs_pair, file):
    settings, _, service = hgs_pair
    path = settings.runs_dir / "variant" / file
    copy = path.with_suffix(".copy")
    path.rename(copy)
    path.symlink_to(copy)
    with pytest.raises(ValueError, match="symlink"):
        service.compare(baseline_run_id="baseline", variant_run_ids=("variant",))


@pytest.mark.parametrize("ids", [(), ("baseline",), ("variant", "variant"), ("../escape",)])
def test_bad_comparison_ids(hgs_pair, ids):
    _, _, service = hgs_pair
    with pytest.raises(ValueError):
        service.compare(baseline_run_id="baseline", variant_run_ids=ids)


def test_forged_pass_flag_recomputed(hgs_pair):
    settings, _, service = hgs_pair
    path = settings.runs_dir / "variant/solution.json"
    value = json.loads(path.read_text())
    value["solver_metadata"]["validation"] = {"status": "passed", "semantics": "invented"}
    path.write_text(json.dumps(value))
    analysis = service.compare(baseline_run_id="baseline", variant_run_ids=("variant",))
    assert analysis.observations[1].model_signature["semantics"] == "hgs_atomic_arrival_events"


def test_different_intact_input_snapshots_are_not_comparable(hgs_pair):
    settings, _, service = hgs_pair
    folder = settings.runs_dir / "variant"
    path = folder / "Instances/6_1/time_matrix_6.txt"
    path.write_bytes(path.read_bytes().replace(b"\t", b"  "))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("process.json", "solution.json"):
        target = folder / name
        data = json.loads(target.read_text())
        metadata = data if name == "process.json" else data["solver_metadata"]
        metadata["input_sha256"][path.name] = digest
        target.write_text(json.dumps(data))
    report = service.compare(baseline_run_id="baseline", variant_run_ids=("variant",))
    assert all(o.verified_candidate for o in report.observations)
    assert report.comparisons[0].kind == "not_comparable"
    assert any("checked_input_sha256" in r for r in report.comparisons[0].reasons)


def test_run_directory_symlink_rejected(hgs_pair):
    settings, _, service = hgs_pair
    path = settings.runs_dir / "variant"
    target = settings.runs_dir / "moved"
    path.rename(target)
    path.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        service.compare(baseline_run_id="baseline", variant_run_ids=("variant",))


@pytest.mark.parametrize("change", [None, "0" * 64])
def test_missing_or_disagreeing_implementation_provenance_rejected(hgs_pair, change):
    settings, _, service = hgs_pair
    path = settings.runs_dir / "variant/solution.json"
    data = json.loads(path.read_text())
    data["solver_metadata"]["executable_sha256"] = change
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="provenance"):
        service.compare(baseline_run_id="baseline", variant_run_ids=("variant",))


@pytest.mark.parametrize("key,value", [("backend", "gurobi"), ("objective_profile", "gurobi_linear_default"),
                                     ("time_interval_sec", 300), ("checked_input_sha256", {}),
                                     ("implementation_sha256", "another-build"), ("semantics", "other")])
def test_signature_differences_block_all_metric_deltas(hgs_pair, key, value):
    _, _, service = hgs_pair
    analysis = service.compare(baseline_run_id="baseline", variant_run_ids=("variant",))
    base, variant = analysis.observations
    variant = variant.model_copy(update={"model_signature": {**variant.model_signature, key: value}})
    comparison = _compare(base, variant)
    assert comparison.kind == "not_comparable" and not comparison.deltas
    assert any(key in reason for reason in comparison.reasons)


def test_search_changes_do_not_claim_a_changed_transport_scenario(settings, scenario):
    service = SolverService(settings)
    service.solve_selected(scenario, run_id="baseline")
    variant = scenario.model_copy(update={"scenario_name": "longer-search", "solver_runtime_limit_sec": 10.0})
    service.solve_selected(variant, run_id="variant")
    comparison = ScenarioAnalysisService(settings).compare(baseline_run_id="baseline", variant_run_ids=("variant",)).comparisons[0]
    assert comparison.kind == "same_scenario_observations"
    assert not comparison.scenario_changes
    assert [c.field for c in comparison.search_changes] == ["solver_runtime_limit_sec"]


@pytest.mark.parametrize("status,count", [(2, 1), (9, 1), (9, 0), (3, 0), (11, 0)])
def test_gurobi_analysis_verifies_candidates_and_does_not_promote_failures(case, status, count):
    settings, scenario, options, backend = case
    service = SolverService(settings, gurobi_backend=backend)
    assert service.solve_selected(scenario, run_id="baseline").result.feasible is True
    rewrite_fake(options, f"payload.update(status={status},solution_count={count})\n" +
                 ("payload.update(variables={},objective_value=None,optimality_gap=None,best_bound=None)\n(root/'native.sol').unlink()\n" if count == 0 else "") +
                 "(root/'worker-result.json').write_text(json.dumps(payload))\n")
    service.solve_selected(scenario, run_id="variant")
    path = settings.runs_dir / "variant/solution.json"
    saved = json.loads(path.read_text())
    saved['solver_metadata']['validation'] = {"status": "passed"}
    path.write_text(json.dumps(saved))
    before = digest_tree(settings.runs_dir)
    analysis = ScenarioAnalysisService(settings).compare(baseline_run_id="baseline", variant_run_ids=("variant",))
    assert analysis.observations[1].verified_candidate is bool(count)
    assert bool(analysis.comparisons[0].deltas) is bool(count)
    assert all(d.metric not in {"best_bound", "optimality_gap", "runtime_sec"} for d in analysis.comparisons[0].deltas)
    assert digest_tree(settings.runs_dir) == before


def test_gurobi_native_corruption_rejected_in_analysis(case):
    settings, scenario, _, backend = case
    for run_id in ("baseline", "variant"):
        backend.solve(scenario, run_id=run_id)
    (settings.runs_dir / "variant/native.sol").write_text("corrupt")
    with pytest.raises(ValueError):
        ScenarioAnalysisService(settings).compare(baseline_run_id="baseline", variant_run_ids=("variant",))
