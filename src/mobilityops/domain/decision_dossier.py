"""Audited evidence package for a bounded human decision review."""

from typing import Literal

from mobilityops.domain.analysis import AnalysisModel
from mobilityops.domain.decision_explanation import (
    DecisionExplanation,
    NextExperimentProposal,
)


class DecisionGate(AnalysisModel):
    gate: Literal[
        "evidence_integrity",
        "execution_completion",
        "comparison_validity",
        "evidence_strength",
        "next_action",
    ]
    status: Literal["pass", "caution", "block"]
    rationale: str
    evidence: tuple[str, ...]


class AuditedDecisionDossier(AnalysisModel):
    format: Literal["audited_decision_dossier_v1"] = "audited_decision_dossier_v1"
    dossier_sha256: str
    dossier_id: str
    source_execution_audit_id: str
    source_execution_audit_manifest_sha256: str
    source_session_id: str
    experiment_id: str
    source_report_sha256: str
    selected_variant_case_id: str
    recommendation: Literal[
        "review_blockers", "collect_replicates", "human_decision_review"
    ]
    gates: tuple[DecisionGate, ...]
    explanation: DecisionExplanation
    next_experiment: NextExperimentProposal
