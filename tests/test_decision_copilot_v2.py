"""Stage 20 produces a two-pass Gemini brief over replicated evidence."""

import json
import subprocess
from unittest.mock import Mock

import pytest

from mobilityops import cli
from mobilityops.domain.decision_copilot_v2 import (
    DecisionBrief,
    DecisionBriefReview,
)
from mobilityops.services.decision_copilot_v2 import DecisionCopilotV2Service
from mobilityops.services.dossier_campaign_audit import DossierCampaignAuditService
from mobilityops.services.gemini_intake import (
    GeminiBudgetConfig,
    GeminiDecisionCopilotV2Interpreter,
    GeminiScenarioInterpreter,
)
from mobilityops.services.replicated_decision_dossier import (
    ReplicatedDecisionDossierService,
)
from test_copilot import FakeTransport
from test_dossier_campaign_audit import completed_campaign


def replicated_dossier(settings, scenario, *, suffix):
    session, _ = completed_campaign(settings, scenario, suffix=f"v2-{suffix}")
    audit_id = f"v2-audit-{suffix}"
    dossier_id = f"v2-dossier-{suffix}"
    DossierCampaignAuditService(settings).write_report(
        session.session_id, audit_id=audit_id
    )
    dossier = ReplicatedDecisionDossierService(settings).write(
        audit_id, dossier_id=dossier_id, variant_case_id="capacity30"
    )
    return dossier_id, dossier


def brief(*, outcome="adopt_variant", summary="建议采用变体；仍需人工批准。"):
    return DecisionBrief(
        outcome=outcome,
        executive_summary=summary,
        evidence_ids=("evidence-001", "evidence-002"),
        risks=("不能证明因果关系。",),
        cannot_conclude=("不能证明全局最优。",),
    )


def review(*, verdict="approved", unsupported=()):
    return DecisionBriefReview(
        verdict=verdict,
        rationale="证据支持该简报；仍需人工批准。",
        evidence_ids=("evidence-001", "evidence-002"),
        unsupported_claims=unsupported,
    )


class StaticInterpreter:
    def __init__(self, draft_value=None, review_value=None):
        self.draft_value = draft_value or brief()
        self.review_value = review_value or review()
        self.calls = []

    def draft(self, request):
        self.calls.append(("draft", request))
        return self.draft_value

    def review(self, request):
        self.calls.append(("review", request))
        return self.review_value


def test_prepare_replays_dossier_and_builds_bounded_evidence_without_process(
    settings, scenario, monkeypatch
):
    dossier_id, dossier = replicated_dossier(settings, scenario, suffix="prepare")
    monkeypatch.setattr(
        subprocess, "run", Mock(side_effect=AssertionError("prepare is read-only"))
    )
    request = DecisionCopilotV2Service(settings).prepare(
        dossier_id,
        session_id="decision-v2-prepare",
        budget_hkd=1,
        max_calls=4,
    )

    assert request.source.dossier_sha256 == dossier.dossier_sha256
    assert request.source.deterministic_recommendation == "human_decision_review"
    assert request.source.evidence[0].kind == "lineage"
    assert len(request.source.evidence) > 8
    assert len(request.request_sha256) == 64
    assert not (settings.runs_dir / "decision-copilot-v2-sessions").exists()


def test_two_pass_brief_is_grounded_reviewed_and_never_executes_solver(
    settings, scenario
):
    dossier_id, _ = replicated_dossier(settings, scenario, suffix="execute")
    service = DecisionCopilotV2Service(settings)
    request = service.prepare(
        dossier_id, session_id="decision-v2-ok", budget_hkd=1, max_calls=4
    )
    interpreter = StaticInterpreter()
    report = service.execute(
        request,
        confirmed_request_sha256=request.request_sha256,
        interpreter=interpreter,
    )

    assert report.status == "approved"
    assert report.calls_invoked == 2
    assert report.brief.outcome == "adopt_variant"
    assert report.brief.auto_execute is False
    assert report.review.auto_execute is False
    assert [item[0] for item in interpreter.calls] == ["draft", "review"]
    root = settings.runs_dir / "decision-copilot-v2-sessions/decision-v2-ok"
    assert json.loads((root / "state.json").read_bytes())["calls_invoked"] == 2
    assert "真实 solver 调用：0" in (root / "report.md").read_text(encoding="utf-8")


def test_deterministic_gate_and_number_grounding_fail_closed(settings, scenario):
    dossier_id, _ = replicated_dossier(settings, scenario, suffix="validation")
    service = DecisionCopilotV2Service(settings)
    bad_outcome = service.prepare(
        dossier_id, session_id="decision-v2-bad-outcome", budget_hkd=1, max_calls=4
    )
    with pytest.raises(ValueError, match="exceeds deterministic gates"):
        service.execute(
            bad_outcome,
            confirmed_request_sha256=bad_outcome.request_sha256,
            interpreter=StaticInterpreter(draft_value=brief(outcome="review_blockers")),
        )

    invented = service.prepare(
        dossier_id, session_id="decision-v2-invented", budget_hkd=1, max_calls=4
    )
    with pytest.raises(ValueError, match="number absent from evidence"):
        service.execute(
            invented,
            confirmed_request_sha256=invented.request_sha256,
            interpreter=StaticInterpreter(
                draft_value=brief(summary="建议采用变体，预期改善999%。")
            ),
        )


def test_wrong_confirmation_stops_before_model_call(settings, scenario):
    dossier_id, _ = replicated_dossier(settings, scenario, suffix="wrong-confirm")
    service = DecisionCopilotV2Service(settings)
    request = service.prepare(
        dossier_id, session_id="decision-v2-wrong", budget_hkd=1, max_calls=4
    )
    interpreter = StaticInterpreter()
    with pytest.raises(ValueError, match="confirmation or evidence has changed"):
        service.execute(
            request,
            confirmed_request_sha256="0" * 64,
            interpreter=interpreter,
        )
    assert interpreter.calls == []
    assert not (
        settings.runs_dir / "decision-copilot-v2-sessions/decision-v2-wrong"
    ).exists()


def test_fake_gemini_uses_exactly_two_calls_and_forbids_tools(settings, scenario):
    dossier_id, _ = replicated_dossier(settings, scenario, suffix="gemini")
    service = DecisionCopilotV2Service(settings)
    request = service.prepare(
        dossier_id, session_id="decision-v2-gemini", budget_hkd=1, max_calls=4
    )
    transport = FakeTransport([
        brief().model_dump(mode="json"),
        review().model_dump(mode="json"),
    ])
    GeminiScenarioInterpreter.create_budget(
        settings,
        budget_id="decision-v2-fake-budget",
        config=GeminiBudgetConfig(budget_hkd=1, max_calls=4),
    )
    report = service.execute(
        request,
        confirmed_request_sha256=request.request_sha256,
        interpreter=GeminiDecisionCopilotV2Interpreter(
            settings,
            budget_id="decision-v2-fake-budget",
            transport=transport,
        ),
    )

    assert report.status == "approved"
    assert [method for method, _, _ in transport.calls] == [
        "countTokens", "interactions", "countTokens", "interactions"
    ]
    assert "没有 shell、Python、网络、solver" in transport.calls[1][1]["input"]
    assert "不能授权或执行 solver" in transport.calls[3][1]["input"]
    ledger = json.loads(
        (settings.runs_dir / "llm/decision-v2-fake-budget/ledger.json").read_bytes()
    )
    assert ledger["calls_reserved"] == 2


def test_cli_cancel_creates_no_budget_or_model_call(
    settings, scenario, monkeypatch, capsys
):
    dossier_id, _ = replicated_dossier(settings, scenario, suffix="cli-cancel")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "取消")

    assert cli.main([
        "--mode", "decision-copilot-v2",
        "--replicated-dossier-id", dossier_id,
        "--session-id", "decision-v2-cli-cancel",
        "--budget-hkd", "1",
        "--max-calls", "4",
        "--runs-dir", str(settings.runs_dir),
    ]) == 0
    assert "没有创建模型预算" in capsys.readouterr().out
    assert not (
        settings.runs_dir / "llm/decision-v2-decision-v2-cli-cancel"
    ).exists()
