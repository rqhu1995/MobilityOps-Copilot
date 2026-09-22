"""Decision dossier derived from one audited replication campaign."""

from typing import Literal

from mobilityops.domain.analysis import AnalysisModel
from mobilityops.domain.decision_dossier import DecisionGate
from mobilityops.domain.decision_explanation import (
    DecisionExplanation,
    NextExperimentProposal,
)


class ReplicatedDecisionDossier(AnalysisModel):
    format: Literal["replicated_decision_dossier_v1"] = (
        "replicated_decision_dossier_v1"
    )
    dossier_sha256: str
    dossier_id: str
    source_campaign_audit_id: str
    source_campaign_audit_manifest_sha256: str
    parent_dossier_id: str
    parent_dossier_sha256: str
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
