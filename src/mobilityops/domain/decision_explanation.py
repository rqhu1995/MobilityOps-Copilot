"""Evidence-bound decision summaries and non-executing follow-up proposals."""

from typing import Literal

from pydantic import Field

from mobilityops.domain.analysis import AnalysisModel
from mobilityops.domain.experiment import ExperimentPlan


class EvidenceClaim(AnalysisModel):
    category: Literal["verified_fact", "descriptive_observation", "cannot_conclude"]
    code: str
    text: str
    evidence: tuple[str, ...] = ()
    case_id: str | None = None


class DecisionExplanation(AnalysisModel):
    format: Literal["decision_explanation_v1"] = "decision_explanation_v1"
    experiment_id: str
    source_report_sha256: str
    verified_facts: tuple[EvidenceClaim, ...]
    descriptive_observations: tuple[EvidenceClaim, ...]
    cannot_conclude: tuple[EvidenceClaim, ...]


class NextExperimentProposal(AnalysisModel):
    format: Literal["next_experiment_proposal_v1"] = "next_experiment_proposal_v1"
    experiment_id: str
    source_report_sha256: str
    plan: ExperimentPlan | None
    rationale: tuple[str, ...]
    blockers: tuple[str, ...] = ()
    auto_execute: Literal[False] = False
    requires_new_review_and_confirmation: Literal[True] = True
