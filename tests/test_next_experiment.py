"""Stage 11 takes over stage-10 proposals without LLM or implicit execution."""

import json
from pathlib import Path
import re
import subprocess
from unittest.mock import Mock

import pytest

from mobilityops import cli
from mobilityops.domain import BackendName
from mobilityops.domain.experiment import ExperimentCase, ExperimentPlan
from mobilityops.services.decision_explanation import DecisionExplanationService
from mobilityops.services.experiment_report import ExperimentReportService
from mobilityops.services.experiment_service import ExperimentService
from mobilityops.services.next_experiment import (
    NextExperimentPrecheckError,
    NextExperimentTakeoverService,
)
from mobilityops.services.next_experiment_terminal_session import (
    NextExperimentTerminalSession,
)
from mobilityops.services.solver_service import SolverService
from mobilityops.solvers.hgs.backend import HgsBackend


class Script:
    def __init__(self, inputs):
        self.inputs = iter(inputs)
        self.output = []

    @property
    def text(self):
        return "\n".join(self.output)

    def read(self, prompt):
        self.output.append(prompt)
        action = next(self.inputs, EOFError())
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action(prompt)
        if action == "<confirm>":
            return re.search(r'“(执行下一轮实验 [0-9a-f]{12})”', prompt).group(1)
        return action

    def write(self, text):
        self.output.append(text)


def make_source_proposal(settings, scenario, *, source_id="stage10-source"):
    baseline = ExperimentCase(
        case_id="baseline", scenario=scenario, backend=BackendName.HGS,
        repetitions=1, external_timeout_sec=10,
    )
    variant = ExperimentCase(
        case_id="capacity30",
        scenario=scenario.model_copy(update={"truck_capacity": 30}),
        backend=BackendName.HGS, repetitions=1, external_timeout_sec=10,
    )
    plan = ExperimentPlan(
        baseline=baseline, variants=(variant,), max_calls=2,
        external_wait_budget_sec=20, failure_policy="continue",
    )
    batch = ExperimentService(SolverService(settings)).execute(
        plan, experiment_id=source_id
    )
    proposal = DecisionExplanationService(settings).next_plan(
        source_id, variant_case_id="capacity30"
    )
    folder = settings.runs_dir / "decision-sessions" / f"{source_id}-review"
    folder.mkdir(parents=True)
    path = folder / "001-next-plan.json"
    path.write_text(proposal.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return proposal, path, batch


def read_state(session):
    return json.loads((session.folder / "state.json").read_bytes())


def test_prepare_binds_fresh_report_plan_precheck_and_configuration(
    settings, scenario, monkeypatch
):
    proposal, path, _ = make_source_proposal(settings, scenario)
    monkeypatch.setattr(
        subprocess, "run", Mock(side_effect=AssertionError("prepare must not execute"))
    )
    request = NextExperimentTakeoverService(SolverService(settings)).prepare_execution(
        path, experiment_id="next-review", report_id="next-review"
    )
    assert request.source_experiment_id == proposal.experiment_id
    assert request.source_report_sha256 == proposal.source_report_sha256
    assert request.proposal == proposal
    assert request.plan == proposal.plan
    assert request.plan.planned_calls == request.plan.max_calls == 6
    assert request.precheck.allowed is True
    assert request.precheck.planned_external_wait_sec == 60
    assert set(request.execution_configurations) == {"hgs"}
    assert len(request.request_sha256) == len(request.proposal_sha256) == 64
    assert not (settings.runs_dir / "experiments/next-review").exists()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("auto_execute", True, "auto_execute"),
        ("requires_new_review_and_confirmation", False, "require a new review"),
        ("blockers", ["do not run"], "Blocked proposals"),
        ("plan", None, "no complete ExperimentPlan"),
    ],
)
def test_rejects_execution_flags_blockers_and_missing_plan(
    settings, scenario, field, value, message
):
    _, path, _ = make_source_proposal(settings, scenario)
    payload = json.loads(path.read_bytes())
    payload[field] = value
    if field == "plan":
        payload["blockers"] = []
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        NextExperimentTakeoverService(SolverService(settings)).prepare_execution(
            path, experiment_id="rejected-next", report_id="rejected-next"
        )
    assert not (settings.runs_dir / "experiments/rejected-next").exists()


def test_rejects_tampered_plan_and_stale_source_evidence(settings, scenario):
    _, path, batch = make_source_proposal(settings, scenario)
    original = json.loads(path.read_bytes())
    changed = json.loads(path.read_bytes())
    changed["plan"]["max_calls"] = 7
    path.write_text(json.dumps(changed), encoding="utf-8")
    service = NextExperimentTakeoverService(SolverService(settings))
    with pytest.raises(ValueError, match="differs from the current deterministic candidate"):
        service.prepare_execution(path, experiment_id="tampered-next", report_id="tampered-next")

    path.write_text(json.dumps(original), encoding="utf-8")
    run_dir = settings.runs_dir / batch.attempts[1].run_id
    (run_dir / "solver-result.txt").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="source report is stale"):
        service.prepare_execution(path, experiment_id="stale-next", report_id="stale-next")


def test_precheck_rejects_every_case_before_execution(settings, scenario):
    _, path, _ = make_source_proposal(settings, scenario)
    settings.hgs_repo_path.joinpath("build/main").unlink()
    with pytest.raises(NextExperimentPrecheckError) as caught:
        NextExperimentTakeoverService(SolverService(settings)).prepare_execution(
            path, experiment_id="precheck-next", report_id="precheck-next"
        )
    assert len(caught.value.precheck.cases) == 2
    assert all(case.errors for case in caught.value.precheck.cases)
    assert not (settings.runs_dir / "experiments/precheck-next").exists()


def test_terminal_does_not_treat_execute_text_as_confirmation(settings, scenario):
    _, path, _ = make_source_proposal(settings, scenario)
    io = Script(["执行"])
    session = NextExperimentTerminalSession(
        SolverService(settings), io=io, session_id="next-cancel", proposal_path=path
    )
    assert session.run() == 0
    assert read_state(session)["status"] == "cancelled"
    assert "不构成确认" in io.text
    assert not (settings.runs_dir / "experiments" / session.experiment_id).exists()


def test_proposal_file_change_invalidates_confirmation(settings, scenario):
    _, path, _ = make_source_proposal(settings, scenario)

    def rewrite_and_confirm(prompt):
        payload = json.loads(path.read_bytes())
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        return re.search(r'“(执行下一轮实验 [0-9a-f]{12})”', prompt).group(1)

    io = Script([rewrite_and_confirm])
    session = NextExperimentTerminalSession(
        SolverService(settings), io=io, session_id="next-file-change", proposal_path=path
    )
    assert session.run() == 2
    assert read_state(session)["status"] == "review_changed"
    assert "旧确认失效" in io.text
    assert not (settings.runs_dir / "experiments" / session.experiment_id).exists()


def test_backend_configuration_change_invalidates_confirmation(
    settings, scenario, tmp_path
):
    _, path, _ = make_source_proposal(settings, scenario)
    backend = HgsBackend(settings)
    solver = SolverService(settings, hgs_backend=backend)
    replacement = tmp_path / "replacement-hgs"
    replacement.write_bytes(backend.executable.read_bytes())
    replacement.chmod(0o700)

    def change_and_confirm(prompt):
        backend.executable = replacement
        return re.search(r'“(执行下一轮实验 [0-9a-f]{12})”', prompt).group(1)

    io = Script([change_and_confirm])
    session = NextExperimentTerminalSession(
        solver, io=io, session_id="next-config-change", proposal_path=path
    )
    assert session.run() == 2
    assert read_state(session)["status"] == "review_changed"
    assert not (settings.runs_dir / "experiments" / session.experiment_id).exists()


def test_fake_hgs_end_to_end_executes_six_only_after_exact_confirmation(
    settings, scenario
):
    _, path, _ = make_source_proposal(settings, scenario)
    io = Script(["<confirm>"])
    session = NextExperimentTerminalSession(
        SolverService(settings), io=io, session_id="next-e2e", proposal_path=path
    )
    assert session.run() == 0
    state = read_state(session)
    assert state["status"] == "reported" and state["calls_invoked"] == 6
    assert state["verified_candidates"] == 6
    report_path = settings.runs_dir / "experiment-reports" / session.report_id / "report.json"
    saved = json.loads(report_path.read_bytes())
    replay = ExperimentReportService(settings).analyze(session.experiment_id)
    assert replay.model_dump(mode="json") == saved
    assert not (settings.runs_dir / "llm").exists()
    assert "计划 6 次 / 上限 6 次" in io.text


def test_wrong_request_confirmation_cannot_execute(settings, scenario):
    _, path, _ = make_source_proposal(settings, scenario)
    service = NextExperimentTakeoverService(SolverService(settings))
    request = service.prepare_execution(
        path, experiment_id="wrong-confirm", report_id="wrong-confirm"
    )
    with pytest.raises(ValueError, match="confirmation does not match"):
        service.execute(path, request, confirmed_request_sha256="0" * 64)
    assert not (settings.runs_dir / "experiments/wrong-confirm").exists()


def test_cli_routes_next_experiment_without_llm_budget_validation(
    settings, monkeypatch, tmp_path
):
    proposal = tmp_path / "proposal.json"
    proposal.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    factory = Mock()
    factory.return_value.run.return_value = 0
    monkeypatch.setattr(cli, "NextExperimentTerminalSession", factory)
    assert cli.main([
        "--mode", "next-experiment", "--proposal", str(proposal),
        "--budget-hkd", "0", "--session-id", "cli-next",
        "--hgs-repo-path", str(settings.hgs_repo_path),
        "--gurobi-repo-path", str(settings.gurobi_repo_path),
        "--runs-dir", str(settings.runs_dir),
    ]) == 0
    assert factory.call_args.kwargs["proposal_path"] == proposal
    assert factory.call_args.kwargs["session_id"] == "cli-next"
    assert not settings.runs_dir.exists()
