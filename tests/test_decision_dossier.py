"""Stage 17 turns a closed execution audit into a non-executing decision package."""

import hashlib
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from mobilityops import cli
from mobilityops.domain.decision_dossier import AuditedDecisionDossier
from mobilityops.services.audited_execution_audit import (
    AuditedExecutionAuditService,
)
from mobilityops.services.decision_dossier import DecisionDossierService
from test_audited_execution_audit import completed_execution


def digest(root: Path):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


def completed_audit(settings, scenario, *, suffix):
    session = completed_execution(settings, scenario, suffix=suffix)
    audit_id = f"audit-{suffix}"
    AuditedExecutionAuditService(settings).write_report(
        session.session_id, audit_id=audit_id
    )
    return audit_id


def test_dossier_replays_audit_explains_and_proposes_bounded_replicates(
    settings, scenario, monkeypatch
):
    audit_id = completed_audit(settings, scenario, suffix="dossier")
    monkeypatch.setattr(
        subprocess, "run", Mock(side_effect=AssertionError("dossier is read-only"))
    )
    before = digest(settings.runs_dir)
    service = DecisionDossierService(settings)
    dossier = service.build(audit_id, dossier_id="decision-one")

    assert digest(settings.runs_dir) == before
    assert dossier == AuditedDecisionDossier.model_validate_json(
        dossier.model_dump_json()
    )
    assert dossier.recommendation == "collect_replicates"
    assert dossier.selected_variant_case_id == "capacity30"
    assert [item.status for item in dossier.gates] == [
        "pass", "pass", "pass", "caution", "pass"
    ]
    assert dossier.next_experiment.plan is not None
    assert dossier.next_experiment.plan.planned_calls == 6
    assert dossier.next_experiment.plan.external_wait_budget_sec == 60
    assert dossier.next_experiment.auto_execute is False


def test_dossier_write_is_deterministic_and_refuses_overwrite(settings, scenario):
    audit_id = completed_audit(settings, scenario, suffix="write")
    service = DecisionDossierService(settings)
    dossier = service.write(audit_id, dossier_id="decision-write")
    root = settings.runs_dir / "decision-dossiers/decision-write"

    assert json.loads((root / "dossier.json").read_bytes())["dossier_sha256"] == (
        dossier.dossier_sha256
    )
    assert json.loads((root / "next-plan.json").read_bytes())["auto_execute"] is False
    assert "collect_replicates" in (root / "dossier.md").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="refusing overwrite"):
        service.write(audit_id, dossier_id="decision-write")


def test_dossier_rejects_tampered_execution_audit(settings, scenario):
    audit_id = completed_audit(settings, scenario, suffix="tamper")
    path = settings.runs_dir / f"audited-execution-audits/{audit_id}/report.json"
    value = json.loads(path.read_bytes())
    value["manifest_sha256"] = "0" * 64
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="differs from deterministic replay"):
        DecisionDossierService(settings).build(audit_id, dossier_id="rejected")
    assert not (settings.runs_dir / "decision-dossiers/rejected").exists()


def test_dossier_rejects_unknown_variant(settings, scenario):
    audit_id = completed_audit(settings, scenario, suffix="variant")
    with pytest.raises(ValueError, match="Unknown variant"):
        DecisionDossierService(settings).build(
            audit_id, dossier_id="unknown", variant_case_id="missing"
        )


def test_cli_routes_dossier_without_tty_or_solver(settings, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    factory = Mock()
    factory.return_value.write.return_value = Mock(
        recommendation="collect_replicates",
        selected_variant_case_id="capacity30",
        dossier_sha256="a" * 64,
    )
    monkeypatch.setattr(cli, "DecisionDossierService", factory)

    assert cli.main([
        "--mode", "dossier",
        "--execution-audit-id", "source-audit",
        "--dossier-id", "output-dossier",
        "--variant-case-id", "capacity30",
        "--runs-dir", str(settings.runs_dir),
    ]) == 0
    factory.return_value.write.assert_called_once_with(
        "source-audit",
        dossier_id="output-dossier",
        variant_case_id="capacity30",
    )
    assert "决策证据包完成：collect_replicates" in capsys.readouterr().out
