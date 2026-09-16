"""Gurobi terminal integration uses existing raw fixtures and a fake Python child."""

import json
import re
from unittest.mock import Mock

import pytest

from mobilityops import cli
from mobilityops.domain import BackendName, ObjectiveProfile
from mobilityops.services.experiment_report import ExperimentReportService
from mobilityops.services.gemini_intake import GeminiBudgetConfig, GeminiScenarioInterpreter
from mobilityops.services.scenario_intake import fingerprint
from mobilityops.services.solver_service import SolverService
from mobilityops.services.terminal_session import TerminalSession
from mobilityops.solvers.gurobi.backend import GurobiBackend
from mobilityops.solvers.gurobi.contracts import GurobiOptions
from mobilityops.solvers.hgs.backend import HgsBackend
from test_gurobi import case, rewrite_fake
from test_terminal import FakeTransport, Script, assert_no_solver, extraction, proposal, state


def setup(case, inputs, *, outputs=None, backend=BackendName.GUROBI, context=None):
    settings, scenario, _, gurobi = case
    io = Script(inputs)
    transport = FakeTransport(outputs if outputs is not None else [extraction()])
    session = TerminalSession(
        SolverService(settings, gurobi_backend=gurobi), io=io, session_id="gurobi-terminal",
        budget=GeminiBudgetConfig(budget_hkd=1, max_calls=3),
        context=context if context is not None else scenario, backend=backend,
        interpreter_factory=lambda budget_id: GeminiScenarioInterpreter(settings, budget_id=budget_id, transport=transport),
    )
    return session, io, transport


def test_configuration_snapshot_does_not_probe_or_start_processes(case, monkeypatch):
    settings, _, options, gurobi = case
    service = SolverService(settings, gurobi_backend=gurobi)
    process = Mock(side_effect=AssertionError("Configuration is read-only and does not probe licenses"))
    monkeypatch.setattr("subprocess.run", process)
    gurobi.preflight = Mock(side_effect=AssertionError("No preflight"))
    first = service.execution_configuration(BackendName.GUROBI)
    assert first["options"] == options.model_dump(mode="json")
    first["options"]["threads"] = 42
    assert service.execution_configuration(BackendName.GUROBI)["options"]["threads"] == 1
    assert service.registered_backends == (BackendName.HGS, BackendName.GUROBI)
    assert not settings.runs_dir.exists()
    process.assert_not_called()
    gurobi.preflight.assert_not_called()


def test_gurobi_terminal_confirmed_execution_reuses_independent_verification(case):
    settings, _, options, _ = case
    session, io, transport = setup(case, ["同意", "沿用基准", "/run", "<confirm>"])
    assert session.run() == 0
    saved = state(session)
    assert saved["status"] == "reported" and saved["phase"] == "reporting"
    assert saved["outcome"] == "succeeded" and saved["verified_candidates"] == 1
    assert saved["confirmed_review_sha256"] == saved["review_sha256"]
    review = json.loads((session.folder / "review-001-execution.json").read_bytes())
    digest = review.pop("review_sha256")
    assert digest == fingerprint(review)
    assert digest != review["request"]["request_sha256"]
    run = settings.runs_dir / "terminal-gurobi-terminal-c001-r0001"
    assert json.loads((run / "options.json").read_bytes()) == review["execution_configuration"]["options"]
    assert (run / "native.sol").is_file()
    assert json.loads((run / "validation.json").read_bytes())["status"] == "passed"
    report = ExperimentReportService(settings).analyze("terminal-gurobi-terminal")
    assert report.batch.calls_invoked == 1 and report.reviews[0].status == "verified_candidate"
    assert report.reviews[0].observation.result.backend is BackendName.GUROBI
    assert transport.calls == ["countTokens", "interactions"]
    assert "Gurobi × 1 次" in io.text and "HGS × 1 次" not in io.text
    assert str(options.python_executable) in io.text
    assert "时间段长度 600 秒；线程数 1" in io.text
    assert "外部 timeout 10 秒" in io.text and "gurobi_linear_default 时间离散周期模型" in io.text
    assert "best bound=8.03318；optimality gap=0.000%" in io.text
    assert "hgs：不兼容" in io.text and "gurobi：兼容" in io.text


@pytest.mark.parametrize("status,count,outcome,exit_code", [(9, 1, "timeout_with_candidate", 0),
                                                         (9, 0, "timeout_no_candidate", 1),
                                                         (3, 0, "infeasible", 1)])
def test_gurobi_terminal_separates_candidate_timeouts_from_no_candidate(case, status, count, outcome, exit_code):
    _, _, options, _ = case
    rewrite_fake(options, f"payload.update(status={status},solution_count={count})\n" +
                 ("payload.update(variables={},objective_value=None,optimality_gap=None,best_bound=None)\n(root/'native.sol').unlink()\n" if count == 0 else "") +
                 "(root/'worker-result.json').write_text(json.dumps(payload))\n")
    session, io, _ = setup(case, ["同意", "沿用基准", "/run", "<confirm>"])
    assert session.run() == exit_code
    assert state(session)["outcome"] == outcome
    assert state(session)["verified_candidates"] == count
    assert "Markdown 报告：" in io.text and outcome in io.text
    assert ("objective_value：n=1" in io.text) == bool(count)
    assert ("本次 Gurobi 周期模型：best bound=" in io.text) == bool(count)


@pytest.mark.parametrize("failure", ["license", "validation"])
def test_gurobi_terminal_failed_results_keep_evidence_without_candidate_metrics(case, failure):
    settings, _, options, _ = case
    suffix = ("(root/'worker-result.json').write_text('{\"worker_error\":\"GurobiError\",\"phase\":\"license\",\"code\":10009}')\n"
              if failure == "license" else "payload['objective_value']+=1\n(root/'worker-result.json').write_text(json.dumps(payload))\n")
    rewrite_fake(options, suffix)
    session, io, _ = setup(case, ["同意", "沿用基准", "/run", "<confirm>"])
    assert session.run() == 1
    assert state(session)["outcome"] == ("failed" if failure == "license" else "validation_failed")
    assert state(session)["verified_candidates"] == 0
    run = settings.runs_dir / "terminal-gurobi-terminal-c001-r0001"
    assert (run / "worker-result.json").is_file() and (run / "stdout.log").is_file()
    assert "objective_value：" not in io.text and "best bound=" not in io.text


@pytest.mark.parametrize("update,reason", [({"objective_profile": ObjectiveProfile.EUDF_DEFAULT}, "objective_profile"),
                                         ({"seed": 42}, "seed"), ({"broken_bike_proportion": 0.3}, "broken_bike_proportion"),
                                         ({"operation_time_budget_sec": 2401.0}, "time_grid")])
def test_gurobi_terminal_rejects_incompatible_requirements_without_rewriting(case, update, reason):
    settings, scenario, _, backend = case
    backend.solve = Mock(side_effect=AssertionError("No solver for incompatible candidates"))
    scenario = scenario.model_copy(update=update)
    session, io, _ = setup(case, ["同意", "沿用基准", "/run", "/quit"], context=scenario)
    assert session.run() == 0
    assert session.draft.candidate == scenario and session.draft.status == "incompatible"
    assert reason in io.text and "无法执行" in io.text
    backend.solve.assert_not_called()
    assert_no_solver(settings)


@pytest.mark.parametrize("failure", ["missing_python", "missing_data", "source_changed"])
def test_gurobi_precheck_rejects_without_launching_license_environment(case, failure, monkeypatch):
    settings, _, options, _ = case
    if failure == "missing_python":
        options.python_executable.unlink()
    elif failure == "missing_data":
        (settings.gurobi_repo_path / "resources/datasets/1_1/station_info_1.txt").unlink()
    else:
        (settings.gurobi_repo_path / "model/objective.py").write_text("changed source\n")
    process = Mock(side_effect=AssertionError("No child before successful precheck and confirmation"))
    monkeypatch.setattr("subprocess.run", process)
    session, io, _ = setup(case, ["同意", "沿用基准", "/run", "/quit"])
    assert session.run() == 0
    assert "预检查未通过" in io.text
    process.assert_not_called()
    assert_no_solver(settings)


@pytest.mark.parametrize("answer", ["", "yes", "/quit", EOFError(), KeyboardInterrupt()])
def test_gurobi_cancelled_confirmation_does_not_start_child(case, answer, monkeypatch):
    process = Mock(side_effect=AssertionError("No child without confirmation"))
    monkeypatch.setattr("subprocess.run", process)
    session, _, _ = setup(case, ["同意", "沿用基准", "/run", answer, "/quit"])
    assert session.run() in {0, 130}
    process.assert_not_called()
    assert_no_solver(case[0])


@pytest.mark.parametrize("changed", ["threads", "time_interval_sec", "python_executable", "instance_directory"])
def test_changed_gurobi_configuration_invalidates_confirmation(case, changed):
    settings, _, options, gurobi = case

    def change_before_confirming(prompt):
        if changed == "instance_directory":
            gurobi.instance_directory = settings.runs_dir / "different-input"
        else:
            value = {"threads": 2, "time_interval_sec": 300,
                     "python_executable": settings.runs_dir / "different-python"}[changed]
            gurobi.options = options.model_copy(update={changed: value})
        return re.search(r'“(执行 [0-9a-f]{12})”', prompt).group(1)

    session, io, _ = setup(case, ["同意", "沿用基准", "/run", change_before_confirming, "/quit"])
    assert session.run() == 0
    assert "运行配置已变化，旧确认失效" in io.text
    assert "confirmed_review_sha256" not in state(session)
    assert_no_solver(settings)


def test_configuration_change_produces_new_confirmation_even_when_scenario_is_identical(case):
    settings, _, options, gurobi = case
    first_token = []

    def cancel_then_change_threads(prompt):
        first_token.append(re.search(r'“(执行 [0-9a-f]{12})”', prompt).group(1))
        gurobi.options = options.model_copy(update={"threads": 2})
        return "取消"

    session, io, _ = setup(case, ["同意", "沿用基准", "/run", cancel_then_change_threads,
                                "/run", lambda _: first_token[0], "/quit"])
    assert session.run() == 0
    reviews = [json.loads((session.folder / f"review-{index:03d}-execution.json").read_bytes()) for index in (1, 2)]
    assert reviews[0]["request"] == reviews[1]["request"]
    assert reviews[0]["review_sha256"] != reviews[1]["review_sha256"]
    assert "线程数 2" in io.text
    assert_no_solver(settings)


def test_external_timeout_comes_from_selected_adapter_configuration(case):
    _, _, options, gurobi = case
    gurobi.options = options.model_copy(update={"timeout_grace_sec": 9.0})
    session, io, _ = setup(case, ["同意", "沿用基准", "/run", "取消", "/quit"])
    assert session.run() == 0
    request = json.loads((session.folder / "review-001-request.json").read_bytes())
    assert request["plan"]["baseline"]["external_timeout_sec"] == 14
    assert "外部 timeout 14 秒（含 9 秒退出余量）" in io.text


def test_registered_gurobi_can_be_uniquely_selected_without_explicit_backend(case):
    session, io, _ = setup(case, ["同意", "沿用基准", "/run", "取消", "/quit"], backend=None)
    assert session.run() == 0
    assert session.draft.decision.requested_backend is None
    assert session.draft.decision.selected_backend is BackendName.GUROBI
    assert "执行预算：Gurobi × 1 次" in io.text


def test_registering_gurobi_does_not_change_explicit_hgs_execution(settings, scenario):
    gurobi = GurobiBackend(settings, options=GurobiOptions(python_executable=settings.runs_dir / "absent-python"))
    session, io, _ = setup((settings, scenario, gurobi.options, gurobi),
                           ["同意", "沿用基准", "/run", "<confirm>"], backend=BackendName.HGS)
    assert session.run() == 0
    assert "执行预算：HGS × 1 次" in io.text and "执行预算：Gurobi" not in io.text
    assert "本次 Gurobi 周期模型：best bound=" not in io.text
    assert state(session)["outcome"] == "succeeded"


def cli_args(settings):
    return ["--hgs-repo-path", str(settings.hgs_repo_path), "--gurobi-repo-path", str(settings.gurobi_repo_path),
            "--runs-dir", str(settings.runs_dir)]


@pytest.mark.parametrize("extra", [["--backend", "gurobi"], ["--gurobi-time-interval-sec", "600"],
                                   ["--gurobi-threads", "1"], ["--gurobi-python", "relative"],
                                   ["--gurobi-python", "/fake/python", "--gurobi-threads", "0"],
                                   ["--gurobi-python", "/fake/python", "--gurobi-threads", "65"],
                                   ["--gurobi-python", "/fake/python", "--gurobi-time-interval-sec", "0"]])
def test_cli_rejects_incomplete_or_invalid_gurobi_config_before_consent(settings, monkeypatch, extra):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert cli.main([*cli_args(settings), *extra]) == 2
    assert not settings.runs_dir.exists()


def test_cli_explicit_options_register_existing_adapter_without_probing(settings, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    process = Mock(side_effect=AssertionError("No license probe on CLI startup"))
    monkeypatch.setattr("subprocess.run", process)
    factory = Mock()
    factory.return_value.run.return_value = 0
    monkeypatch.setattr(cli, "TerminalSession", factory)
    python = tmp_path / "python with spaces"
    assert cli.main([*cli_args(settings), "--gurobi-python", str(python), "--backend", "gurobi",
                     "--gurobi-time-interval-sec", "300", "--gurobi-threads", "2"]) == 0
    service = factory.call_args.args[0]
    configuration = service.execution_configuration(BackendName.GUROBI)
    assert configuration["options"] == {"python_executable": str(python), "time_interval_sec": 300,
                                         "threads": 2, "timeout_grace_sec": 5.0}
    assert factory.call_args.kwargs["backend"] is BackendName.GUROBI
    process.assert_not_called()
    assert not settings.runs_dir.exists()


def test_cli_gurobi_full_conversation_to_report(case, monkeypatch, tmp_path):
    settings, scenario, options, _ = case
    baseline = tmp_path / "scenario.json"
    baseline.write_text(scenario.model_dump_json())
    io = Script(["同意", "沿用基准", "/run", "<confirm>"])
    transport = FakeTransport([extraction()])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(cli, "Console", lambda: io)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-terminal-key")
    monkeypatch.setattr("mobilityops.services.gemini_intake.GeminiHttpTransport.post", transport.post)
    assert cli.main([*cli_args(settings), "--baseline", str(baseline), "--backend", "gurobi",
                     "--gurobi-python", str(options.python_executable), "--session-id", "cli-gurobi"]) == 0
    report_dir = settings.runs_dir / "experiment-reports/terminal-cli-gurobi"
    assert (report_dir / "report.md").is_file()
    report = json.loads((report_dir / "report.json").read_bytes())
    assert report["summaries"][0]["verified_candidates"] == 1
    assert "Gurobi × 1 次" in io.text
    for path in settings.runs_dir.rglob("*"):
        if path.is_file():
            assert b"fake-terminal-key" not in path.read_bytes()


def test_hgs_review_configuration_is_detached_and_tracks_custom_executable(settings):
    backend = HgsBackend(settings, executable=settings.hgs_repo_path / "alternative", timeout_grace_sec=7)
    service = SolverService(settings, hgs_backend=backend)
    configuration = service.execution_configuration(BackendName.HGS)
    assert configuration["options"] == {"executable": str(backend.executable), "timeout_grace_sec": 7}
    configuration["settings"]["runs_dir"] = "/different"
    assert service.settings == settings and not settings.runs_dir.exists()
    with pytest.raises(ValueError, match="not registered"):
        service.execution_configuration(BackendName.GUROBI)
