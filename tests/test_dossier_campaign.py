"""Stage 18 safely takes over a dossier-bound replication campaign."""

import json
from pathlib import Path
import re
import subprocess
from unittest.mock import Mock

import pytest

from mobilityops import cli
from mobilityops.services.decision_dossier import DecisionDossierService
from mobilityops.services.dossier_campaign import DossierCampaignService
from mobilityops.services.dossier_campaign_terminal_session import (
    DossierCampaignTerminalSession,
)
from mobilityops.services.experiment_report import ExperimentReportService
from mobilityops.services.solver_service import SolverService
from test_decision_dossier import completed_audit


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
            return re.search(
                r'“(执行 Dossier 重复实验 [0-9a-f]{12})”', prompt
            ).group(1)
        return action

    def write(self, text):
        self.output.append(text)


def make_dossier(settings, scenario, *, suffix):
    audit_id = completed_audit(settings, scenario, suffix=f"campaign-{suffix}")
    dossier_id = f"dossier-{suffix}"
    dossier = DecisionDossierService(settings).write(
        audit_id, dossier_id=dossier_id, variant_case_id="capacity30"
    )
    return dossier_id, dossier


def read_state(session):
    return json.loads((session.folder / "state.json").read_bytes())


def file_bytes(root: Path):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    }


def test_prepare_replays_dossier_and_binds_audit_plan_precheck_configuration(
    settings, scenario, monkeypatch
):
    dossier_id, dossier = make_dossier(settings, scenario, suffix="prepare")
    monkeypatch.setattr(
        subprocess, "run", Mock(side_effect=AssertionError("prepare must not execute"))
    )

    request = DossierCampaignService(SolverService(settings)).prepare_execution(
        dossier_id, experiment_id="campaign-review", report_id="campaign-review"
    )

    assert request.source_dossier_sha256 == dossier.dossier_sha256
    assert request.source_execution_audit_id == dossier.source_execution_audit_id
    assert request.dossier == dossier
    assert request.execution_request.proposal == dossier.next_experiment
    assert request.execution_request.plan.planned_calls == 6
    assert request.execution_request.precheck.planned_external_wait_sec == 60
    assert set(request.execution_request.execution_configurations) == {"hgs"}
    assert len(request.request_sha256) == 64
    assert not (settings.runs_dir / "experiments/campaign-review").exists()


def test_terminal_requires_exact_new_confirmation(settings, scenario):
    dossier_id, _ = make_dossier(settings, scenario, suffix="cancel")
    io = Script(["执行"])
    session = DossierCampaignTerminalSession(
        SolverService(settings),
        io=io,
        session_id="campaign-cancel",
        dossier_id=dossier_id,
    )

    assert session.run() == 0
    assert read_state(session)["status"] == "cancelled"
    assert "不构成确认" in io.text
    assert not (settings.runs_dir / f"experiments/{session.experiment_id}").exists()


def test_fake_hgs_executes_six_and_writes_dossier_lineage_after_confirmation(
    settings, scenario
):
    dossier_id, dossier = make_dossier(settings, scenario, suffix="execute")
    llm_before = file_bytes(settings.runs_dir / "llm")
    io = Script(["<confirm>"])
    session = DossierCampaignTerminalSession(
        SolverService(settings),
        io=io,
        session_id="campaign-execute",
        dossier_id=dossier_id,
    )

    assert session.run() == 0
    state = read_state(session)
    assert state["status"] == "reported"
    assert state["calls_invoked"] == state["verified_candidates"] == 6
    request_root = (
        settings.runs_dir / "dossier-campaign-requests" / session.experiment_id
    )
    lineage = json.loads((request_root / "lineage.json").read_bytes())
    assert lineage["source_dossier_sha256"] == dossier.dossier_sha256
    assert lineage["child_experiment_id"] == session.experiment_id
    saved = json.loads(
        (settings.runs_dir / "experiment-reports" / session.report_id / "report.json")
        .read_bytes()
    )
    replay = ExperimentReportService(settings).analyze(session.experiment_id)
    assert replay.model_dump(mode="json") == saved
    assert file_bytes(settings.runs_dir / "llm") == llm_before


def test_dossier_or_bound_candidate_tampering_is_rejected(settings, scenario):
    dossier_id, _ = make_dossier(settings, scenario, suffix="tamper")
    root = settings.runs_dir / "decision-dossiers" / dossier_id
    candidate = json.loads((root / "next-plan.json").read_bytes())
    candidate["auto_execute"] = True
    (root / "next-plan.json").write_text(json.dumps(candidate), encoding="utf-8")

    with pytest.raises(ValueError, match="next-plan differs"):
        DossierCampaignService(SolverService(settings)).prepare_execution(
            dossier_id, experiment_id="campaign-tamper", report_id="campaign-tamper"
        )
    assert not (settings.runs_dir / "experiments/campaign-tamper").exists()


def test_source_audit_change_invalidates_confirmation(settings, scenario):
    dossier_id, dossier = make_dossier(settings, scenario, suffix="audit-change")
    audit_path = (
        settings.runs_dir / "audited-execution-audits"
        / dossier.source_execution_audit_id / "report.json"
    )

    def change_and_confirm(prompt):
        payload = json.loads(audit_path.read_bytes())
        payload["manifest_sha256"] = "0" * 64
        audit_path.write_text(json.dumps(payload), encoding="utf-8")
        return re.search(
            r'“(执行 Dossier 重复实验 [0-9a-f]{12})”', prompt
        ).group(1)

    session = DossierCampaignTerminalSession(
        SolverService(settings),
        io=Script([change_and_confirm]),
        session_id="campaign-audit-change",
        dossier_id=dossier_id,
    )
    assert session.run() == 2
    assert read_state(session)["status"] == "review_changed"
    assert not (settings.runs_dir / f"experiments/{session.experiment_id}").exists()


def test_wrong_outer_confirmation_cannot_execute(settings, scenario):
    dossier_id, _ = make_dossier(settings, scenario, suffix="wrong-confirm")
    service = DossierCampaignService(SolverService(settings))
    request = service.prepare_execution(
        dossier_id,
        experiment_id="campaign-wrong-confirm",
        report_id="campaign-wrong-confirm",
    )

    with pytest.raises(ValueError, match="Confirmation does not match"):
        service.execute(request, confirmed_request_sha256="0" * 64)
    assert not (settings.runs_dir / "experiments/campaign-wrong-confirm").exists()


def test_cli_routes_dossier_campaign_without_llm_budget_validation(
    settings, monkeypatch
):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    factory = Mock()
    factory.return_value.run.return_value = 0
    monkeypatch.setattr(cli, "DossierCampaignTerminalSession", factory)

    assert cli.main([
        "--mode", "dossier-campaign",
        "--source-dossier-id", "source-dossier",
        "--budget-hkd", "0",
        "--session-id", "cli-campaign",
        "--hgs-repo-path", str(settings.hgs_repo_path),
        "--gurobi-repo-path", str(settings.gurobi_repo_path),
        "--runs-dir", str(settings.runs_dir),
    ]) == 0
    assert factory.call_args.kwargs["dossier_id"] == "source-dossier"
    assert factory.call_args.kwargs["session_id"] == "cli-campaign"
