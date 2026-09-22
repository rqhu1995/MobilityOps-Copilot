"""Stage 19 closes and replays the dossier replication campaign."""

import json
import subprocess
from unittest.mock import Mock

import pytest

from mobilityops import cli
from mobilityops.services.dossier_campaign_audit import DossierCampaignAuditService
from mobilityops.services.dossier_campaign_terminal_session import (
    DossierCampaignTerminalSession,
)
from mobilityops.services.replicated_decision_dossier import (
    ReplicatedDecisionDossierService,
)
from mobilityops.services.solver_service import SolverService
from test_dossier_campaign import Script, make_dossier


def completed_campaign(settings, scenario, *, suffix):
    dossier_id, source = make_dossier(settings, scenario, suffix=suffix)
    session = DossierCampaignTerminalSession(
        SolverService(settings),
        io=Script(["<confirm>"]),
        session_id=f"replication-{suffix}",
        dossier_id=dossier_id,
    )
    assert session.run() == 0
    return session, source


def test_campaign_audit_replays_complete_six_run_lineage_without_process(
    settings, scenario, monkeypatch
):
    session, source = completed_campaign(settings, scenario, suffix="audit")
    monkeypatch.setattr(
        subprocess, "run", Mock(side_effect=AssertionError("audit is read-only"))
    )
    report = DossierCampaignAuditService(settings).analyze(session.session_id)

    assert report.audit_status == "verified_complete"
    assert report.calls_invoked == report.verified_candidates == 6
    assert report.source_dossier_sha256 == source.dossier_sha256
    assert report.experiment_id == session.experiment_id
    assert len(report.files) > 20
    assert {item.scope for item in report.files} >= {
        "session", "source_dossier", "source_audit", "request",
        "experiment", "report", "run",
    }


def test_campaign_audit_writes_manifest_and_refuses_overwrite(settings, scenario):
    session, _ = completed_campaign(settings, scenario, suffix="write-audit")
    service = DossierCampaignAuditService(settings)
    report = service.write_report(session.session_id, audit_id="campaign-audit-one")
    root = settings.runs_dir / "dossier-campaign-audits/campaign-audit-one"

    assert json.loads((root / "report.json").read_bytes())["manifest_sha256"] == (
        report.manifest_sha256
    )
    assert "verified_complete" in (root / "report.md").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="refusing overwrite"):
        service.write_report(session.session_id, audit_id="campaign-audit-one")


def test_replicated_dossier_reaches_human_review_at_n_three(settings, scenario):
    session, source = completed_campaign(settings, scenario, suffix="dossier")
    audit = DossierCampaignAuditService(settings).write_report(
        session.session_id, audit_id="campaign-audit-dossier"
    )
    dossier = ReplicatedDecisionDossierService(settings).write(
        "campaign-audit-dossier",
        dossier_id="replicated-decision-one",
        variant_case_id="capacity30",
    )

    assert dossier.recommendation == "human_decision_review"
    assert [item.status for item in dossier.gates] == [
        "pass", "pass", "pass", "pass", "pass"
    ]
    assert dossier.parent_dossier_sha256 == source.dossier_sha256
    assert dossier.source_campaign_audit_manifest_sha256 == audit.manifest_sha256
    assert "最小样本数为 3" in dossier.gates[3].rationale
    root = settings.runs_dir / "replicated-decision-dossiers/replicated-decision-one"
    assert (root / "dossier.json").is_file()
    assert json.loads((root / "next-plan.json").read_bytes())["auto_execute"] is False


def test_campaign_audit_rejects_tampered_lineage(settings, scenario):
    session, _ = completed_campaign(settings, scenario, suffix="tamper-lineage")
    path = (
        settings.runs_dir / "dossier-campaign-requests"
        / session.experiment_id / "lineage.json"
    )
    value = json.loads(path.read_bytes())
    value["child_report_sha256"] = "0" * 64
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="lineage is invalid"):
        DossierCampaignAuditService(settings).analyze(session.session_id)


def test_cli_campaign_decision_routes_audit_and_dossier_without_tty(
    settings, monkeypatch, capsys
):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    audit_factory = Mock()
    audit_factory.return_value.write_report.return_value = Mock(
        audit_status="verified_complete", calls_invoked=6, verified_candidates=6
    )
    dossier_factory = Mock()
    dossier_factory.return_value.write.return_value = Mock(
        recommendation="human_decision_review", dossier_sha256="a" * 64
    )
    monkeypatch.setattr(cli, "DossierCampaignAuditService", audit_factory)
    monkeypatch.setattr(cli, "ReplicatedDecisionDossierService", dossier_factory)

    assert cli.main([
        "--mode", "campaign-decision",
        "--dossier-campaign-session-id", "source-session",
        "--audit-id", "new-audit",
        "--dossier-id", "new-dossier",
        "--variant-case-id", "capacity30",
        "--runs-dir", str(settings.runs_dir),
    ]) == 0
    audit_factory.return_value.write_report.assert_called_once_with(
        "source-session", audit_id="new-audit"
    )
    dossier_factory.return_value.write.assert_called_once_with(
        "new-audit", dossier_id="new-dossier", variant_case_id="capacity30"
    )
    assert "human_decision_review" in capsys.readouterr().out
