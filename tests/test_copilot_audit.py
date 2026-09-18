"""Stage 13 replays Copilot evidence without model or solver calls."""

import hashlib
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from mobilityops import cli
from mobilityops.services.copilot_audit import CopilotAuditService
from test_copilot import TEXT, decision, extraction, make_session


def tree_digest(root: Path):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


def completed_session(settings, scenario, *, session_id="audit-complete"):
    outputs = [
        decision("draft_experiment"), extraction(),
        decision("prepare_execution_review"),
        decision("explain_experiment"),
    ]
    inputs = [
        "同意", TEXT, "请审阅并执行", "<confirm>",
        "解释刚才的结果", "/quit",
    ]
    session, _, _ = make_session(
        settings, scenario, outputs, inputs, session_id=session_id
    )
    assert session.run() == 0
    return session


def test_complete_audit_replays_execution_and_is_read_only(settings, scenario):
    session = completed_session(settings, scenario)
    before = tree_digest(settings.runs_dir)
    service = CopilotAuditService(settings)
    first = service.analyze(session.session_id)
    second = service.analyze(session.session_id)
    assert first == second
    assert tree_digest(settings.runs_dir) == before
    assert first.audit_status == "verified_complete"
    assert first.model_usage.calls_reserved == first.model_usage.calls_observed == 4
    assert first.model_usage.calls_succeeded == 4
    assert first.decisions == (
        "draft_experiment", "prepare_execution_review", "explain_experiment",
    )
    assert first.execution.executed is True
    assert first.execution.source == "draft"
    assert first.execution.calls_invoked == first.execution.verified_candidates == 2
    assert first.execution.batch_status == "completed"
    assert len(first.files) > 20 and len(first.manifest_sha256) == 64


def test_written_audit_is_deterministic_and_refuses_overwrite(settings, scenario):
    session = completed_session(settings, scenario, session_id="audit-write")
    service = CopilotAuditService(settings)
    first = service.write_report(session.session_id, audit_id="audit-one")
    second = service.write_report(session.session_id, audit_id="audit-two")
    assert first == second
    for name in ("report.json", "report.md"):
        assert (settings.runs_dir / "copilot-audits/audit-one" / name).read_bytes() == (
            settings.runs_dir / "copilot-audits/audit-two" / name
        ).read_bytes()
    with pytest.raises(ValueError, match="overwrite"):
        service.write_report(session.session_id, audit_id="audit-one")


def test_cancelled_before_consent_has_zero_call_verified_audit(settings, scenario):
    session, _, _ = make_session(
        settings, scenario, [], ["不同意"], session_id="audit-cancelled"
    )
    assert session.run() == 0
    report = CopilotAuditService(settings).analyze(session.session_id)
    assert report.audit_status == "verified_no_execution"
    assert report.session_status == "cancelled"
    assert report.model_usage.calls_observed == 0
    assert not report.execution.executed and report.decisions == ()


def test_tampered_turn_decision_fails_local_action_replay(settings, scenario):
    session = completed_session(settings, scenario, session_id="audit-decision")
    path = session.folder / "turn-001-decision.json"
    payload = json.loads(path.read_bytes())
    payload["action"] = "answer"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        CopilotAuditService(settings).analyze(session.session_id)


def test_tampered_model_input_evidence_is_not_treated_as_local_fact(settings, scenario):
    session = completed_session(settings, scenario, session_id="audit-model-evidence")
    path = session.folder / "turn-002-model-input.json"
    payload = json.loads(path.read_bytes())
    payload["evidence"][0]["payload"]["status"] = "ready-but-invented"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="provider requests|local tool artifacts"):
        CopilotAuditService(settings).analyze(session.session_id)


def test_tampered_run_breaks_lineage_report_replay(settings, scenario):
    session = completed_session(settings, scenario, session_id="audit-run")
    run = settings.runs_dir / f"{session.output_experiment_id}-c002-r0001/solver-result.txt"
    run.write_text("tampered")
    with pytest.raises(ValueError):
        CopilotAuditService(settings).analyze(session.session_id)


def test_state_event_mismatch_and_symlink_are_rejected(settings, scenario, tmp_path):
    session = completed_session(settings, scenario, session_id="audit-structure")
    state = json.loads((session.folder / "state.json").read_bytes())
    state["selected_variant_case_id"] = "invented"
    (session.folder / "state.json").write_text(json.dumps(state))
    with pytest.raises(ValueError, match="final event"):
        CopilotAuditService(settings).analyze(session.session_id)

    session = completed_session(settings, scenario, session_id="audit-invalid-state")
    state_path = session.folder / "state.json"
    state = json.loads(state_path.read_bytes())
    state["status"] = "invented"
    state_path.write_text(json.dumps(state))
    events_path = session.folder / "events.jsonl"
    events = events_path.read_text().splitlines()
    final = json.loads(events[-1])
    final["status"] = "invented"
    events[-1] = json.dumps(final)
    events_path.write_text("\n".join(events) + "\n")
    with pytest.raises(ValueError, match="invalid status"):
        CopilotAuditService(settings).analyze(session.session_id)

    session = completed_session(settings, scenario, session_id="audit-symlink")
    path = session.folder / "turn-001-decision.json"
    copy = tmp_path / "decision.json"
    path.rename(copy)
    path.symlink_to(copy)
    with pytest.raises(ValueError, match="symlink|escapes"):
        CopilotAuditService(settings).analyze(session.session_id)


def test_ledger_mismatch_is_rejected(settings, scenario):
    session = completed_session(settings, scenario, session_id="audit-ledger")
    ledger = session.budget_folder / "ledger.json"
    payload = json.loads(ledger.read_bytes())
    payload["calls_reserved"] -= 1
    ledger.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="reservation|directories"):
        CopilotAuditService(settings).analyze(session.session_id)

    session = completed_session(settings, scenario, session_id="audit-call-artifact")
    artifact = session.budget_folder / "call-002/experiment-extraction.json"
    artifact.unlink()
    with pytest.raises(ValueError, match="final artifact"):
        CopilotAuditService(settings).analyze(session.session_id)


def test_cli_audit_is_noninteractive_and_uses_no_solver_environment(
    settings, scenario, monkeypatch, capsys
):
    session = completed_session(settings, scenario, session_id="audit-cli-source")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    for name in ("HGS_REPO_PATH", "GUROBI_REPO_PATH", "MOBILITYOPS_RUNS_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(settings.runs_dir.parent)
    run = Mock(side_effect=AssertionError("audit must not start subprocesses"))
    monkeypatch.setattr(subprocess, "run", run)
    assert cli.main([
        "--mode", "audit", "--copilot-session-id", session.session_id,
        "--audit-id", "audit-cli",
    ]) == 0
    output = capsys.readouterr().out
    assert "审计完成：verified_complete" in output
    assert (settings.runs_dir / "copilot-audits/audit-cli/report.json").is_file()
    run.assert_not_called()
