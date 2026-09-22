"""Two-pass, evidence-bound Gemini decision brief with no execution capability."""

import hmac
import json
from pathlib import Path
import re
from typing import Protocol

from mobilityops.config import Settings
from mobilityops.domain.decision_copilot_v2 import (
    DecisionBrief,
    DecisionBriefReview,
    DecisionBriefReviewInput,
    DecisionCopilotEvidence,
    DecisionCopilotInput,
    DecisionCopilotReport,
    DecisionCopilotRequest,
)
from mobilityops.domain.replicated_decision_dossier import (
    ReplicatedDecisionDossier,
)
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.experiment_service import checked_root, write_json
from mobilityops.services.gurobi_inputs import read_local, strict_json
from mobilityops.services.replicated_decision_dossier import (
    ReplicatedDecisionDossierService,
)
from mobilityops.services.scenario_intake import fingerprint


class DecisionCopilotV2Interpreter(Protocol):
    def draft(self, request: DecisionCopilotInput) -> DecisionBrief: ...

    def review(self, request: DecisionBriefReviewInput) -> DecisionBriefReview: ...


class DecisionCopilotV2Service:
    def __init__(self, settings: Settings) -> None:
        self.settings = Settings.model_validate(settings.model_dump())

    def prepare(
        self,
        dossier_id: str,
        *,
        session_id: str,
        budget_hkd: float,
        max_calls: int,
    ) -> DecisionCopilotRequest:
        validate_run_id(dossier_id)
        validate_run_id(session_id)
        if max_calls != 4 or not 0 < budget_hkd <= 1:
            raise ValueError("Decision Copilot v2 requires 4 calls maximum and at most 1 HKD")
        dossier = self._verify_dossier(dossier_id)
        evidence = self._evidence(dossier)
        source = DecisionCopilotInput(
            dossier_id=dossier.dossier_id,
            dossier_sha256=dossier.dossier_sha256,
            deterministic_recommendation=dossier.recommendation,
            selected_variant_case_id=dossier.selected_variant_case_id,
            evidence=evidence,
        )
        request = DecisionCopilotRequest(
            request_sha256="",
            session_id=session_id,
            budget_hkd=budget_hkd,
            max_calls=4,
            source=source,
        )
        return request.model_copy(update={
            "request_sha256": fingerprint(
                request.model_dump(mode="json", exclude={"request_sha256"})
            )
        })

    def execute(
        self,
        request: DecisionCopilotRequest,
        *,
        confirmed_request_sha256: str,
        interpreter: DecisionCopilotV2Interpreter,
    ) -> DecisionCopilotReport:
        request = DecisionCopilotRequest.model_validate(request.model_dump())
        expected = self.prepare(
            request.source.dossier_id,
            session_id=request.session_id,
            budget_hkd=request.budget_hkd,
            max_calls=request.max_calls,
        )
        if request != expected or not hmac.compare_digest(
            confirmed_request_sha256, expected.request_sha256
        ):
            raise ValueError("Decision Copilot confirmation or evidence has changed")
        root = checked_root(self.settings, "decision-copilot-v2-sessions")
        folder = root / request.session_id
        if folder.exists() or folder.is_symlink():
            raise ValueError("Decision Copilot v2 session exists; refusing overwrite")
        root.mkdir(parents=True, exist_ok=True)
        folder.mkdir(mode=0o700)
        write_json(folder / "request.json", request.model_dump(mode="json"))
        write_json(folder / "state.json", {
            "status": "confirmed",
            "request_sha256": request.request_sha256,
            "calls_invoked": 0,
        })
        try:
            brief = DecisionBrief.model_validate(
                interpreter.draft(request.source).model_dump()
            )
            self._validate_brief(request.source, brief)
            write_json(folder / "brief.json", brief.model_dump(mode="json"))
            write_json(folder / "state.json", {
                "status": "drafted",
                "request_sha256": request.request_sha256,
                "calls_invoked": 1,
            })
            review_input = DecisionBriefReviewInput(
                source=request.source,
                draft=brief,
            )
            review = DecisionBriefReview.model_validate(
                interpreter.review(review_input).model_dump()
            )
            self._validate_review(request.source, brief, review)
            write_json(folder / "review.json", review.model_dump(mode="json"))
            brief_sha256 = fingerprint(brief.model_dump(mode="json"))
            review_sha256 = fingerprint(review.model_dump(mode="json"))
            report = DecisionCopilotReport(
                session_id=request.session_id,
                request_sha256=request.request_sha256,
                dossier_id=request.source.dossier_id,
                dossier_sha256=request.source.dossier_sha256,
                brief_sha256=brief_sha256,
                review_sha256=review_sha256,
                status=(
                    "approved" if review.verdict == "approved" else "review_rejected"
                ),
                brief=brief,
                review=review,
            )
            write_json(folder / "report.json", report.model_dump(mode="json"))
            (folder / "report.md").write_text(
                render_markdown(report), encoding="utf-8"
            )
            write_json(folder / "state.json", {
                "status": report.status,
                "request_sha256": request.request_sha256,
                "calls_invoked": 2,
                "brief_sha256": brief_sha256,
                "review_sha256": review_sha256,
            })
            return report
        except BaseException as exc:
            state_path = folder / "state.json"
            state = strict_json(read_local(folder, state_path))
            calls = state.get("calls_invoked", 0) if isinstance(state, dict) else 0
            write_json(folder / "state.json", {
                "status": "failed",
                "request_sha256": request.request_sha256,
                "calls_invoked": calls,
                "error_type": type(exc).__name__,
                "retry_allowed": False,
            })
            raise

    def _verify_dossier(self, dossier_id: str) -> ReplicatedDecisionDossier:
        root = checked_root(self.settings, "replicated-decision-dossiers") / dossier_id
        if not root.is_dir() or root.is_symlink():
            raise ValueError("Replicated dossier must be an ordinary directory")
        saved = ReplicatedDecisionDossier.model_validate(
            strict_json(read_local(root, root / "dossier.json"))
        )
        expected_sha = fingerprint(
            saved.model_dump(mode="json", exclude={"dossier_sha256"})
        )
        fresh = ReplicatedDecisionDossierService(self.settings).build(
            saved.source_campaign_audit_id,
            dossier_id=dossier_id,
            variant_case_id=saved.selected_variant_case_id,
        )
        if (
            saved.dossier_id != dossier_id
            or not hmac.compare_digest(saved.dossier_sha256, expected_sha)
            or saved != fresh
            or strict_json(read_local(root, root / "next-plan.json"))
            != saved.next_experiment.model_dump(mode="json")
        ):
            raise ValueError("Replicated dossier differs from deterministic replay")
        return saved

    @staticmethod
    def _evidence(
        dossier: ReplicatedDecisionDossier,
    ) -> tuple[DecisionCopilotEvidence, ...]:
        raw: list[tuple[str, dict]] = [
            ("lineage", {
                "dossier_id": dossier.dossier_id,
                "dossier_sha256": dossier.dossier_sha256,
                "campaign_audit_id": dossier.source_campaign_audit_id,
                "campaign_audit_manifest_sha256": (
                    dossier.source_campaign_audit_manifest_sha256
                ),
                "experiment_id": dossier.experiment_id,
                "report_sha256": dossier.source_report_sha256,
                "selected_variant_case_id": dossier.selected_variant_case_id,
                "recommendation": dossier.recommendation,
            }),
        ]
        raw.extend(("gate", item.model_dump(mode="json")) for item in dossier.gates)
        raw.extend(
            ("fact", item.model_dump(mode="json"))
            for item in dossier.explanation.verified_facts
        )
        raw.extend(
            ("observation", item.model_dump(mode="json"))
            for item in dossier.explanation.descriptive_observations
        )
        raw.extend(
            ("limitation", item.model_dump(mode="json"))
            for item in dossier.explanation.cannot_conclude
        )
        return tuple(
            DecisionCopilotEvidence(
                evidence_id=f"evidence-{index:03d}", kind=kind, payload=payload
            )
            for index, (kind, payload) in enumerate(raw, start=1)
        )

    def _validate_brief(
        self, source: DecisionCopilotInput, brief: DecisionBrief
    ) -> None:
        self._validate_evidence_claims(
            source,
            brief.evidence_ids,
            " ".join((brief.executive_summary, *brief.risks, *brief.cannot_conclude)),
        )
        allowed = {
            "review_blockers": {"review_blockers"},
            "collect_replicates": {"collect_more_evidence"},
            "human_decision_review": {
                "adopt_variant", "retain_baseline", "collect_more_evidence"
            },
        }[source.deterministic_recommendation]
        if brief.outcome not in allowed:
            raise ValueError("Decision brief outcome exceeds deterministic gates")

    def _validate_review(
        self,
        source: DecisionCopilotInput,
        brief: DecisionBrief,
        review: DecisionBriefReview,
    ) -> None:
        self._validate_evidence_claims(
            source,
            review.evidence_ids,
            " ".join((review.rationale, *review.unsupported_claims)),
        )
        if review.verdict == "approved" and review.unsupported_claims:
            raise ValueError("An approved review cannot list unsupported claims")
        if review.verdict == "rejected" and not review.unsupported_claims:
            raise ValueError("A rejected review must identify unsupported claims")
        if brief.auto_execute or review.auto_execute:
            raise ValueError("Decision Copilot v2 cannot authorize execution")

    @staticmethod
    def _validate_evidence_claims(
        source: DecisionCopilotInput,
        evidence_ids: tuple[str, ...],
        claims: str,
    ) -> None:
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("Decision Copilot repeated an evidence ID")
        evidence = {item.evidence_id: item for item in source.evidence}
        if any(item not in evidence for item in evidence_ids):
            raise ValueError("Decision Copilot cited unknown evidence")
        cited = " ".join(
            json.dumps(
                evidence[item].payload,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            for item in evidence_ids
        )
        cleaned = claims
        for evidence_id in evidence_ids:
            cleaned = cleaned.replace(evidence_id, "")
        cleaned = re.sub(r"(?m)^(\s*)\d+[.)、]\s+", r"\1", cleaned)
        numbers = re.findall(r"(?<![A-Za-z0-9_])-?\d+(?:\.\d+)?%?", cleaned)
        if any(number not in cited for number in numbers):
            raise ValueError("Decision Copilot introduced a number absent from evidence")


def render_markdown(report: DecisionCopilotReport) -> str:
    brief = report.brief
    review = report.review
    lines = [
        f"# Gemini Decision Copilot v2：{report.session_id}",
        "",
        f"状态：**{report.status}**；Dossier：`{report.dossier_id}` / "
        f"`{report.dossier_sha256}`。",
        f"模型：`{report.model}`；调用：{report.calls_invoked}；"
        "真实 solver 调用：0。",
        "",
        "## 决策简报",
        "",
        f"建议：`{brief.outcome}`。{brief.executive_summary}",
        f"证据：{', '.join(brief.evidence_ids)}。",
        "",
        "## 独立模型复核",
        "",
        f"结论：`{review.verdict}`。{review.rationale}",
        f"证据：{', '.join(review.evidence_ids)}。",
        "",
        "本报告要求人类批准，`auto_execute=false`；不能启动 solver。",
    ]
    return "\n".join(lines) + "\n"
