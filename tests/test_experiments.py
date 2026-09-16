"""Batch behavior uses existing independent fixtures and SDK-free executables."""

import hashlib
import json
import subprocess
from unittest.mock import Mock

import pytest

from mobilityops.domain import BackendName
from mobilityops.domain.experiment import ExperimentCase, ExperimentPlan, ExperimentReport
from mobilityops.services.experiment_report import ExperimentReportService, group_observations, render_markdown
from mobilityops.services.experiment_service import ExperimentService, experiment_path
from mobilityops.services.scenario_analysis import ScenarioAnalysisService
from mobilityops.services.solver_service import SolverService
from mobilityops.solvers.hgs.errors import ArtifactError
from test_gurobi import case, rewrite_fake


def digest(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def make_plan(scenario, *, repetitions=2, variants=(), backend=BackendName.HGS, **updates):
    baseline = ExperimentCase(case_id="base", scenario=scenario, backend=backend,
                              repetitions=repetitions, external_timeout_sec=scenario.solver_runtime_limit_sec + 5)
    return ExperimentPlan(baseline=baseline, variants=variants, max_calls=10,
                          external_wait_budget_sec=100, failure_policy="continue", **updates)


@pytest.mark.parametrize("update", [
    {"repetitions": 0}, {"repetitions": True}, {"repetitions": 1001}, {"case_id": "../escape"},
    {"external_timeout_sec": 5}, {"external_timeout_sec": float("inf")}, {"external_timeout_sec": float("nan")},
])
def test_invalid_case(scenario, update):
    with pytest.raises(ValueError):
        ExperimentCase.model_validate({**make_plan(scenario).baseline.model_dump(), **update})


@pytest.mark.parametrize("update", [
    {"max_calls": 0}, {"max_calls": True}, {"external_wait_budget_sec": 0},
    {"external_wait_budget_sec": float("inf")}, {"failure_policy": "retry"},
])
def test_invalid_plan(scenario, update):
    with pytest.raises(ValueError):
        ExperimentPlan.model_validate({**make_plan(scenario).model_dump(), **update})


def test_duplicate_cases_and_revalidation(settings, scenario):
    plan = make_plan(scenario)
    with pytest.raises(ValueError, match="distinct"):
        ExperimentPlan.model_validate({**plan.model_dump(), "variants": [plan.baseline.model_dump()]})
    malformed = plan.model_copy(update={"max_calls": 0})
    with pytest.raises(ValueError):
        ExperimentService(SolverService(settings)).execute(malformed, experiment_id="bad")
    assert not settings.runs_dir.exists()


@pytest.mark.parametrize("update", [{"seed": 1}, {"instance_id": 999}])
def test_all_cases_prechecked_before_any_call(settings, scenario, update):
    plan = make_plan(scenario)
    variant = plan.baseline.model_copy(update={"case_id": "bad", "scenario": scenario.model_copy(update=update)})
    plan = plan.model_copy(update={"variants": (variant,)})
    solver = SolverService(settings)
    solver.solve_selected = Mock()
    service = ExperimentService(solver)
    before = digest(settings.runs_dir.parent)
    precheck = service.precheck(plan, experiment_id="rejected")
    assert len(precheck.cases) == 2 and not precheck.allowed
    assert before == digest(settings.runs_dir.parent)
    batch = service.execute(plan, experiment_id="rejected")
    assert batch.status == "precheck_rejected" and batch.calls_invoked == 0
    assert all(a.status == "skipped_precheck" for a in batch.attempts)
    solver.solve_selected.assert_not_called()
    report = ExperimentReportService(settings).analyze("rejected")
    assert not report.groups and not report.comparisons[0].median_differences


@pytest.mark.parametrize("change", ["timeout", "missing_executable", "malformed_input", "later_collision"])
def test_readiness_precheck_rejects_before_solver(settings, scenario, change):
    plan = make_plan(scenario)
    if change == "timeout":
        plan = plan.model_copy(update={"baseline": plan.baseline.model_copy(update={"external_timeout_sec": 11})})
    elif change == "missing_executable":
        (settings.hgs_repo_path / "build/main").unlink()
    elif change == "malformed_input":
        (settings.hgs_repo_path / "Instances/6_1/time_matrix_6.txt").write_text("bad matrix")
    else:
        (settings.runs_dir / "precheck-c001-r0002").mkdir(parents=True)
    solver = SolverService(settings)
    solver.solve_selected = Mock()
    batch = ExperimentService(solver).execute(plan, experiment_id="precheck")
    assert batch.status == "precheck_rejected"
    solver.solve_selected.assert_not_called()


def test_round_trip_replay_and_readable_report_are_deterministic(settings, scenario):
    plan = make_plan(scenario)
    variant = plan.baseline.model_copy(update={"case_id": "capacity", "scenario": scenario.model_copy(update={"truck_capacity": 30})})
    plan = plan.model_copy(update={"variants": (variant,)})
    batch = ExperimentService(SolverService(settings)).execute(plan, experiment_id="four")
    assert batch.calls_invoked == 4 and batch.reserved_external_wait_sec == 40
    assert batch.status == "completed" and all(a.status == "succeeded" for a in batch.attempts)
    before = digest(settings.runs_dir)
    reports = ExperimentReportService(settings)
    first, second = reports.analyze("four"), reports.analyze("four")
    assert first == second == ExperimentReport.model_validate_json(first.model_dump_json())
    assert digest(settings.runs_dir) == before
    assert [s.verified_candidates for s in first.summaries] == [2, 2]
    assert first.summaries[1].scenario_changes[0].field == "truck_capacity"
    assert len(first.groups) == 2 and all(m.n == 2 for g in first.groups for m in g.metrics)
    assert first.comparisons[0].median_differences == dict.fromkeys(("objective_value", "dissatisfaction", "emission"), 0)
    solver_tree = digest(settings.hgs_repo_path)
    written = reports.write_report("four", report_id="report-a")
    reports.write_report("four", report_id="report-b")
    for name in ("report.json", "report.md"):
        assert (settings.runs_dir / "experiment-reports/report-a" / name).read_bytes() == (settings.runs_dir / "experiment-reports/report-b" / name).read_bytes()
    assert written == first
    assert solver_tree == digest(settings.hgs_repo_path)
    markdown = render_markdown(first, settings)
    assert "中位数" in markdown and "统计显著性" in markdown and "不重试" in markdown
    assert "solver-result.txt" in markdown and "00001.json" in markdown
    assert first.reviews[0].observation.result.solver_metadata["validation"]["status"] == "passed"
    before = digest(settings.runs_dir)
    with pytest.raises(ValueError, match="overwrite"):
        reports.write_report("four", report_id="report-a")
    with pytest.raises(ValueError, match="overwrite"):
        ExperimentService(SolverService(settings)).execute(plan, experiment_id="four")
    assert before == digest(settings.runs_dir)


@pytest.mark.parametrize("caps,expected", [({"max_calls": 1}, 1), ({"external_wait_budget_sec": 19}, 1), ({"external_wait_budget_sec": 9}, 0)])
def test_budget_caps_do_not_refund_early_completions(settings, scenario, caps, expected):
    plan = make_plan(scenario, repetitions=3).model_copy(update=caps)
    batch = ExperimentService(SolverService(settings)).execute(plan, experiment_id="budget")
    assert batch.status == "budget_exhausted" and batch.calls_invoked == expected
    assert batch.reserved_external_wait_sec == 10 * expected
    assert [a.status for a in batch.attempts] == ["succeeded"] * expected + ["skipped_budget"] * (3 - expected)
    report = ExperimentReportService(settings).analyze("budget")
    assert report.summaries[0].planned == 3 and report.summaries[0].verified_candidates == expected


@pytest.mark.parametrize("policy,calls", [("continue", 3), ("stop", 1)])
def test_failure_policy_and_process_start_errors(settings, scenario, monkeypatch, policy, calls):
    plan = make_plan(scenario, repetitions=3).model_copy(update={"failure_policy": policy})
    run = Mock(side_effect=OSError("cannot start"))
    monkeypatch.setattr(subprocess, "run", run)
    batch = ExperimentService(SolverService(settings)).execute(plan, experiment_id="launch")
    assert batch.calls_invoked == run.call_count == calls
    assert batch.attempts[0].status == "start_failed"
    report = ExperimentReportService(settings).analyze("launch")
    assert not report.groups and report.summaries[0].verified_candidates == 0


@pytest.mark.parametrize("kind,expected", [("timeout", "timeout_no_candidate"), ("exit", "failed"), ("validation", "validation_failed")])
def test_hgs_failure_states(settings, scenario, monkeypatch, kind, expected):
    if kind == "timeout":
        monkeypatch.setattr(subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired(["fake"], 10, output=b"retained")))
    elif kind == "exit":
        monkeypatch.setattr(subprocess, "run", Mock(return_value=subprocess.CompletedProcess(["fake"], 2, b"out", b"err")))
    else:
        path = settings.hgs_repo_path / "build/main"
        path.write_bytes(path.read_bytes().replace(b"104.039", b"204.039"))
    batch = ExperimentService(SolverService(settings)).execute(make_plan(scenario, repetitions=1), experiment_id="failure")
    assert batch.attempts[0].status == expected
    report = ExperimentReportService(settings).analyze("failure")
    assert not report.groups and report.reviews[0].status == "no_candidate"


def test_partial_checkpoint_after_interrupt_is_readable(settings, scenario):
    solver = SolverService(settings)
    real = solver.solve_selected
    calls = 0
    def interrupt(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt()
        return real(*args, **kwargs)
    solver.solve_selected = interrupt
    with pytest.raises(KeyboardInterrupt):
        ExperimentService(solver).execute(make_plan(scenario, repetitions=3), experiment_id="partial")
    report = ExperimentReportService(settings).analyze("partial")
    assert report.batch.status == "interrupted" and report.batch.calls_invoked == 2
    assert [a.status for a in report.batch.attempts] == ["succeeded", "interrupted", "pending"]
    assert report.summaries[0].verified_candidates == 1
    assert report.groups[0].metrics[0].n == 1


def test_artifact_failure_aborts_without_further_calls(settings, scenario):
    solver = SolverService(settings)
    solver.solve_selected = Mock(side_effect=ArtifactError(settings.runs_dir / "io", "failed"))
    with pytest.raises(ArtifactError):
        ExperimentService(solver).execute(make_plan(scenario), experiment_id="io")
    solver.solve_selected.assert_called_once()
    report = ExperimentReportService(settings).analyze("io")
    assert report.batch.status == "stopped" and not report.groups


@pytest.mark.parametrize("name", ["scenario.json", "solution.json", "solver-result.txt", "process.json", "command.json", "Instances/6_1/time_matrix_6.txt"])
def test_corrupt_evidence_excluded_without_losing_other_samples(settings, scenario, name):
    batch = ExperimentService(SolverService(settings)).execute(make_plan(scenario), experiment_id="corrupt")
    (settings.runs_dir / batch.attempts[1].run_id / name).write_text("broken evidence")
    before = digest(settings.runs_dir)
    report = ExperimentReportService(settings).analyze("corrupt")
    assert [r.status for r in report.reviews] == ["verified_candidate", "excluded"]
    assert report.summaries[0].invoked == 2 and report.summaries[0].verified_candidates == 1
    assert report.groups[0].metrics[0].n == 1
    assert before == digest(settings.runs_dir)


@pytest.mark.parametrize("name", ["plan.json", "precheck.json", "attempts/00001.json"])
def test_checkpoint_tampering_fails_closed(settings, scenario, name):
    ExperimentService(SolverService(settings)).execute(make_plan(scenario), experiment_id="checkpoint")
    path = experiment_path(settings, "checkpoint") / name
    path.write_text("{}")
    with pytest.raises(ValueError):
        ExperimentReportService(settings).analyze("checkpoint")


def test_fake_pass_flag_does_not_replace_fresh_verification(settings, scenario):
    batch = ExperimentService(SolverService(settings)).execute(make_plan(scenario), experiment_id="flags")
    for attempt in batch.attempts:
        path = settings.runs_dir / attempt.run_id / "solution.json"
        data = json.loads(path.read_bytes())
        data["solver_metadata"]["validation"] = {"status": "passed", "semantics": "invented"}
        path.write_text(json.dumps(data))
    report = ExperimentReportService(settings).analyze("flags")
    assert report.summaries[0].verified_candidates == 2
    assert report.groups[0].model_signature["semantics"] == "hgs_atomic_arrival_events"


@pytest.mark.parametrize("key", ["model_signature", "search_context"])
def test_signature_drift_splits_groups(settings, scenario, key):
    SolverService(settings).solve_selected(scenario, run_id="sample")
    observation = ScenarioAnalysisService(settings).observe("sample")
    second = observation.model_copy(update={key: {**getattr(observation, key), "drift": "changed"}})
    groups = group_observations([("base", observation), ("base", second)])
    assert len(groups) == 2 and all(g.metrics[0].n == 1 for g in groups)


@pytest.mark.parametrize("values,expected", [([9, 1, 5], (1, 5, 9)), ([2, 8], (2, 5, 8))])
def test_descriptive_statistics_denominators_and_medians(settings, scenario, values, expected):
    SolverService(settings).solve_selected(scenario, run_id="sample")
    observation = ScenarioAnalysisService(settings).observe("sample")
    samples = [("base", observation.model_copy(update={"result": observation.result.model_copy(update={"objective_value": value})})) for value in values]
    samples.append(("base", observation.model_copy(update={"verified_candidate": False})))
    metric = group_observations(samples)[0].metrics[0]
    assert (metric.minimum, metric.median, metric.maximum) == expected
    assert metric.n == len(values)


def test_search_changes_reported_without_scenario_difference(settings, scenario):
    plan = make_plan(scenario, repetitions=1)
    variant = plan.baseline.model_copy(update={"case_id": "longer", "scenario": scenario.model_copy(update={"solver_runtime_limit_sec": 6.0}), "external_timeout_sec": 11})
    ExperimentService(SolverService(settings)).execute(plan.model_copy(update={"variants": (variant,)}), experiment_id="search")
    report = ExperimentReportService(settings).analyze("search")
    assert report.comparisons[0].kind == "search_conditions_changed"
    assert not report.comparisons[0].median_differences
    assert not report.summaries[1].scenario_changes
    assert report.summaries[1].search_changes


@pytest.mark.parametrize("status,count,expected", [(2, 1, "succeeded"), (9, 1, "timeout_with_candidate"), (9, 0, "timeout_no_candidate"), (3, 0, "infeasible"), (11, 0, "failed")])
def test_gurobi_batch_states_and_verification_without_sdk(case, status, count, expected):
    settings, scenario, options, backend = case
    rewrite_fake(options, f"payload.update(status={status},solution_count={count})\n" +
                 ("payload.update(variables={},objective_value=None,optimality_gap=None,best_bound=None)\n(root/'native.sol').unlink()\n" if count == 0 else "") +
                 "(root/'worker-result.json').write_text(json.dumps(payload))\n")
    plan = make_plan(scenario, backend=BackendName.GUROBI)
    batch = ExperimentService(SolverService(settings, gurobi_backend=backend)).execute(plan, experiment_id="gurobi")
    assert [a.status for a in batch.attempts] == [expected] * 2
    report = ExperimentReportService(settings).analyze("gurobi")
    assert report.summaries[0].verified_candidates == count * 2
    assert bool(report.groups) == bool(count)
    if count:
        assert report.groups[0].metrics[0].n == 2


def test_gurobi_preflight_does_not_start_process_or_license(case, monkeypatch):
    settings, scenario, _, backend = case
    run = Mock(side_effect=AssertionError("No subprocess permitted"))
    monkeypatch.setattr(subprocess, "run", run)
    before = digest(settings.runs_dir.parent)
    check = ExperimentService(SolverService(settings, gurobi_backend=backend)).precheck(make_plan(scenario, backend=BackendName.GUROBI), experiment_id="dry")
    assert check.allowed and before == digest(settings.runs_dir.parent)
    assert check.cases[0].license_readiness == "not_checked"
    run.assert_not_called()


def test_gurobi_native_corruption_excluded(case):
    settings, scenario, _, backend = case
    batch = ExperimentService(SolverService(settings, gurobi_backend=backend)).execute(make_plan(scenario, backend=BackendName.GUROBI), experiment_id="native")
    (settings.runs_dir / batch.attempts[0].run_id / "native.sol").write_text("corrupt")
    report = ExperimentReportService(settings).analyze("native")
    assert report.summaries[0].verified_candidates == 1


def test_cross_backend_results_stay_separate(settings, scenario, case):
    from mobilityops.solvers.gurobi.backend import GurobiBackend
    gurobi_settings, gurobi_scenario, options, _ = case
    configured = settings.model_copy(update={"gurobi_repo_path": gurobi_settings.gurobi_repo_path})
    backend = GurobiBackend(configured, options=options)
    base = make_plan(scenario, repetitions=1)
    variant = ExperimentCase(case_id="gurobi", scenario=gurobi_scenario, backend=BackendName.GUROBI,
                             repetitions=1, external_timeout_sec=10)
    ExperimentService(SolverService(configured, gurobi_backend=backend)).execute(base.model_copy(update={"variants": (variant,)}), experiment_id="cross")
    report = ExperimentReportService(configured).analyze("cross")
    assert len(report.groups) == 2 and all(s.verified_candidates == 1 for s in report.summaries)
    assert report.comparisons[0].kind == "not_comparable" and not report.comparisons[0].median_differences
    assert any("backend" in reason for reason in report.comparisons[0].reasons)


def test_multiple_intact_input_signatures_cannot_be_pooled(settings, scenario):
    plan = make_plan(scenario)
    variant = plan.baseline.model_copy(update={"case_id": "capacity", "scenario": scenario.model_copy(update={"truck_capacity": 30})})
    batch = ExperimentService(SolverService(settings)).execute(plan.model_copy(update={"variants": (variant,)}), experiment_id="drift")
    run = settings.runs_dir / batch.attempts[1].run_id
    path = run / "Instances/6_1/time_matrix_6.txt"
    path.write_bytes(path.read_bytes().replace(b"\t", b"  "))
    changed_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("process.json", "solution.json"):
        target = run / name
        data = json.loads(target.read_bytes())
        metadata = data if name == "process.json" else data["solver_metadata"]
        metadata["input_sha256"][path.name] = changed_hash
        target.write_text(json.dumps(data))
    report = ExperimentReportService(settings).analyze("drift")
    assert report.summaries[0].verified_candidates == 2
    assert len(report.groups) == 3
    assert report.comparisons[0].kind == "not_comparable" and not report.comparisons[0].median_differences


def test_serial_mixed_outcomes_preserve_correct_denominators(settings, scenario, monkeypatch):
    original = subprocess.run
    outcomes = iter(["success", "timeout", "exit", "success"])
    def mixed(*args, **kwargs):
        outcome = next(outcomes)
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        if outcome == "exit":
            return subprocess.CompletedProcess(args[0], 2, b"failure", b"")
        return original(*args, **kwargs)
    monkeypatch.setattr(subprocess, "run", mixed)
    batch = ExperimentService(SolverService(settings)).execute(make_plan(scenario, repetitions=4), experiment_id="mixed")
    assert [a.status for a in batch.attempts] == ["succeeded", "timeout_no_candidate", "failed", "succeeded"]
    report = ExperimentReportService(settings).analyze("mixed")
    summary = report.summaries[0]
    assert (summary.planned, summary.invoked, summary.verified_candidates) == (4, 4, 2)
    assert summary.execution_status_counts == {"succeeded": 2, "timeout_no_candidate": 1, "failed": 1}
    assert all(m.n == 2 for m in report.groups[0].metrics)


def test_selection_rejection_after_precheck_retains_decision(settings, scenario):
    solver = SolverService(settings)
    original = solver.select
    count = 0
    def select(*args, **kwargs):
        nonlocal count
        count += 1
        decision = original(*args, **kwargs)
        if count >= 3:
            return decision.model_copy(update={"selected_backend": None, "reason": "Backend became unavailable"})
        return decision
    solver.select = select
    solver.solve_selected = Mock()
    batch = ExperimentService(solver).execute(make_plan(scenario), experiment_id="changed")
    assert batch.calls_invoked == 0
    assert all(a.status == "constraint_rejected" and a.decision.selected_backend is None for a in batch.attempts)
    solver.solve_selected.assert_not_called()
    assert not ExperimentReportService(settings).analyze("changed").groups


def test_evidence_symlink_is_excluded(settings, scenario):
    batch = ExperimentService(SolverService(settings)).execute(make_plan(scenario, repetitions=1), experiment_id="link")
    path = settings.runs_dir / batch.attempts[0].run_id / "solver-result.txt"
    copy = path.with_suffix(".copy")
    path.rename(copy)
    path.symlink_to(copy)
    report = ExperimentReportService(settings).analyze("link")
    assert report.reviews[0].status == "excluded" and not report.groups


def test_checkpoint_write_failure_prevents_solver_call(settings, scenario, monkeypatch):
    from mobilityops.services import experiment_service
    original = experiment_service.write_json
    def fail(path, value):
        if path.parent.name == "attempts":
            raise OSError("checkpoint unavailable")
        original(path, value)
    monkeypatch.setattr(experiment_service, "write_json", fail)
    solver = SolverService(settings)
    solver.solve_selected = Mock()
    with pytest.raises(OSError, match="checkpoint"):
        ExperimentService(solver).execute(make_plan(scenario), experiment_id="write")
    solver.solve_selected.assert_not_called()


def test_selection_rejected_inside_solve_retains_actual_rejection(settings, scenario):
    from mobilityops.services.solver_service import BackendSelectionError
    solver = SolverService(settings)
    rejected = solver.select(scenario, backend=BackendName.HGS).model_copy(update={"selected_backend": None, "reason": "Rejected at execution boundary"})
    solver.solve_selected = Mock(side_effect=BackendSelectionError(rejected))
    batch = ExperimentService(solver).execute(make_plan(scenario, repetitions=1), experiment_id="late")
    assert batch.attempts[0].decision == rejected
    assert batch.attempts[0].status == "constraint_rejected" and batch.calls_invoked == 1
    assert not ExperimentReportService(settings).analyze("late").groups


def test_contradictory_outcome_rejected_even_when_checkpoints_match(settings, scenario):
    ExperimentService(SolverService(settings)).execute(make_plan(scenario, repetitions=1), experiment_id="contradict")
    root = experiment_path(settings, "contradict")
    for name in ("batch.json", "attempts/00001.json"):
        path = root / name
        value = json.loads(path.read_bytes())
        attempt = value["attempts"][0] if name == "batch.json" else value
        attempt["status"] = "timeout_with_candidate"
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="contradicts"):
        ExperimentReportService(settings).analyze("contradict")


@pytest.mark.parametrize("category", ["experiments", "experiment-reports"])
def test_symlink_output_root_rejected(settings, scenario, tmp_path, category):
    settings.runs_dir.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (settings.runs_dir / category).symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        if category == "experiments":
            ExperimentService(SolverService(settings)).execute(make_plan(scenario), experiment_id="unsafe")
        else:
            ExperimentReportService(settings).write_report("unused", report_id="unsafe")
    assert not list(target.iterdir())
