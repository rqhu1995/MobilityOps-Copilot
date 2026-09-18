"""Stage 15 re-audits a saved Copilot review before explicit execution."""

import json
import re
from unittest.mock import Mock

import pytest

from mobilityops import cli
from mobilityops.services.audited_execution import AuditedExecutionService
from mobilityops.services.audited_execution_terminal_session import (
    AuditedExecutionTerminalSession,
)
from mobilityops.services.experiment_intake import ExperimentPlanPrecheckError
from mobilityops.services.solver_service import SolverService
from mobilityops.solvers.hgs.backend import HgsBackend
from test_copilot import decision, extraction
from test_copilot_pilot import run_pilot


def make_source(settings, scenario, *, suffix):
    return run_pilot(
        settings,
        scenario,
        [
            decision("draft_experiment"),
            extraction(),
            decision("prepare_execution_review"),
            decision(
                "answer",
                "计划2次 solver 调用，等待预算20秒；不能证明更优。",
                evidence=("evidence-001", "evidence-002"),
            ),
        ],
        suffix=suffix,
    )[0]


class Script:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.output = []

    def read(self, prompt):
        self.output.append(prompt)
        answer = next(self.answers, EOFError())
        if isinstance(answer, BaseException):
            raise answer
        if answer == "<confirm>":
            return re.search(r"“(执行审计计划 [0-9a-f]{12})”", prompt).group(1)
        return answer

    def write(self, value):
        self.output.append(value)

    @property
    def text(self):
        return "\n".join(self.output)


def test_audit_bound_service_executes_fake_plan_and_writes_lineage(
    settings, scenario
):
    source = make_source(settings, scenario, suffix="execute")
    service = AuditedExecutionService(SolverService(settings))
    request = service.prepare_execution(
        source_session_id=source.session_id,
        source_audit_id=source.audit_id,
        experiment_id="audited-service",
        report_id="audited-service",
    )

    assert request.execution_request.plan.planned_calls == 2
    assert request.execution_request.precheck.planned_external_wait_sec == 20
    assert request.source_audit_manifest_sha256 == source.audit_manifest_sha256
    receipt = service.execute(
        request, confirmed_request_sha256=request.request_sha256
    )

    assert receipt.execution.batch.status == "completed"
    assert receipt.execution.batch.calls_invoked == 2
    assert sum(
        item.verified_candidates for item in receipt.execution.report.summaries
    ) == 2
    root = settings.runs_dir / "audited-execution-requests/audited-service"
    lineage = json.loads((root / "lineage.json").read_bytes())
    assert lineage["source_session_id"] == source.session_id
    assert lineage["source_audit_id"] == source.audit_id
    assert lineage["lineage_sha256"] == receipt.lineage_sha256


def test_audit_bound_terminal_requires_exact_confirmation_and_can_cancel(
    settings, scenario
):
    source = make_source(settings, scenario, suffix="cancel")
    io = Script(["执行"])
    session = AuditedExecutionTerminalSession(
        SolverService(settings),
        io=io,
        session_id="cancel",
        source_session_id=source.session_id,
        source_audit_id=source.audit_id,
    )

    assert session.run() == 0
    state = json.loads((session.folder / "state.json").read_bytes())
    assert state["status"] == "cancelled"
    assert "计划 2 次 / 上限 2 次" in io.text
    assert "总等待预算 20 秒" in io.text
    assert not (settings.runs_dir / "experiments/audited-cancel").exists()


def test_audit_bound_terminal_confirmed_fake_execution_reports(
    settings, scenario
):
    source = make_source(settings, scenario, suffix="terminal")
    io = Script(["<confirm>"])
    session = AuditedExecutionTerminalSession(
        SolverService(settings),
        io=io,
        session_id="confirmed",
        source_session_id=source.session_id,
        source_audit_id=source.audit_id,
    )

    assert session.run() == 0
    state = json.loads((session.folder / "state.json").read_bytes())
    assert state["status"] == "reported"
    assert state["calls_invoked"] == state["verified_candidates"] == 2
    assert "谱系散列=" in io.text
    assert (settings.runs_dir / "experiment-reports/audited-confirmed/report.md").is_file()


def test_audit_bound_rejects_tamper_and_wrong_confirmation_without_solver(
    settings, scenario
):
    source = make_source(settings, scenario, suffix="reject")
    service = AuditedExecutionService(SolverService(settings))
    request = service.prepare_execution(
        source_session_id=source.session_id,
        source_audit_id=source.audit_id,
        experiment_id="audited-reject",
        report_id="audited-reject",
    )
    with pytest.raises(ValueError, match="Confirmation does not match"):
        service.execute(request, confirmed_request_sha256="0" * 64)
    assert not (settings.runs_dir / "experiments/audited-reject").exists()

    changed = AuditedExecutionService(SolverService(
        settings,
        hgs_backend=HgsBackend(settings, timeout_grace_sec=4),
    ))
    with pytest.raises(ExperimentPlanPrecheckError):
        changed.execute(
            request, confirmed_request_sha256=request.request_sha256
        )
    assert not (settings.runs_dir / "experiments/audited-reject").exists()

    audit_path = settings.runs_dir / f"copilot-audits/{source.audit_id}/report.json"
    saved = json.loads(audit_path.read_bytes())
    saved["manifest_sha256"] = "0" * 64
    audit_path.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError, match="differs from current deterministic replay"):
        service.prepare_execution(
            source_session_id=source.session_id,
            source_audit_id=source.audit_id,
            experiment_id="audited-tampered",
            report_id="audited-tampered",
        )
    assert not (settings.runs_dir / "experiments/audited-tampered").exists()


def test_cli_routes_audited_execution_without_llm_budget_validation(
    settings, monkeypatch
):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    factory = Mock()
    factory.return_value.run.return_value = 0
    monkeypatch.setattr(cli, "AuditedExecutionTerminalSession", factory)
    assert cli.main([
        "--mode", "audited-execution",
        "--copilot-session-id", "shadow-source",
        "--audit-id", "audit-source",
        "--budget-hkd", "0",
        "--session-id", "cli-audited",
        "--hgs-repo-path", str(settings.hgs_repo_path),
        "--gurobi-repo-path", str(settings.gurobi_repo_path),
        "--runs-dir", str(settings.runs_dir),
    ]) == 0
    assert factory.call_args.kwargs["session_id"] == "cli-audited"
    assert factory.call_args.kwargs["source_session_id"] == "shadow-source"
    assert factory.call_args.kwargs["source_audit_id"] == "audit-source"
    assert not settings.runs_dir.exists()
