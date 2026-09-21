"""Stage 16 closes the audit-bound execution with deterministic replay."""

import json
from unittest.mock import Mock

import pytest

from mobilityops import cli
from mobilityops.services.audited_execution_audit import (
    AuditedExecutionAuditService,
)
from mobilityops.services.audited_execution_terminal_session import (
    AuditedExecutionTerminalSession,
)
from mobilityops.services.solver_service import SolverService
from test_audited_execution import Script, make_source


def completed_execution(settings, scenario, *, suffix):
    source = make_source(settings, scenario, suffix=f"source-{suffix}")
    session = AuditedExecutionTerminalSession(
        SolverService(settings),
        io=Script(["<confirm>"]),
        session_id=suffix,
        source_session_id=source.session_id,
        source_audit_id=source.audit_id,
    )
    assert session.run() == 0
    return session


def test_post_execution_audit_replays_child_and_writes_manifest(settings, scenario):
    session = completed_execution(settings, scenario, suffix="complete")
    service = AuditedExecutionAuditService(settings)
    report = service.write_report(session.session_id, audit_id="closed-loop")

    assert report.audit_status == "verified_complete"
    assert report.batch_status == "completed"
    assert report.calls_invoked == report.verified_candidates == 2
    assert {item.scope for item in report.files} == {
        "session", "source_audit", "request", "experiment", "report", "run"
    }
    root = settings.runs_dir / "audited-execution-audits/closed-loop"
    assert (root / "report.json").is_file()
    assert "实际调用 2 次" in (root / "report.md").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="refusing overwrite"):
        service.write_report(session.session_id, audit_id="closed-loop")


def test_post_execution_audit_rejects_lineage_tamper(settings, scenario):
    session = completed_execution(settings, scenario, suffix="lineage")
    path = (
        settings.runs_dir
        / "audited-execution-requests/audited-lineage/lineage.json"
    )
    value = json.loads(path.read_bytes())
    value["child_report_sha256"] = "0" * 64
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="lineage is invalid"):
        AuditedExecutionAuditService(settings).analyze(session.session_id)


def test_post_execution_audit_rejects_run_evidence_tamper(settings, scenario):
    session = completed_execution(settings, scenario, suffix="run")
    path = settings.runs_dir / "audited-run-c001-r0001/solution.json"
    value = json.loads(path.read_bytes())
    value["objective_value"] += 1
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="Saved child report differs"):
        AuditedExecutionAuditService(settings).analyze(session.session_id)


def test_cli_routes_post_execution_audit_without_tty_or_solver(
    settings, monkeypatch, capsys
):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    factory = Mock()
    factory.return_value.write_report.return_value = Mock(
        audit_status="verified_complete",
        calls_invoked=2,
        verified_candidates=2,
        files=(),
    )
    monkeypatch.setattr(cli, "AuditedExecutionAuditService", factory)
    assert cli.main([
        "--mode", "audited-execution-audit",
        "--audited-execution-session-id", "source-session",
        "--audit-id", "output-audit",
        "--runs-dir", str(settings.runs_dir),
    ]) == 0
    factory.return_value.write_report.assert_called_once_with(
        "source-session", audit_id="output-audit"
    )
    assert "执行审计完成：verified_complete" in capsys.readouterr().out
