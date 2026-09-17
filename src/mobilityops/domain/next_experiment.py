"""Evidence-bound takeover requests for stage-10 experiment proposals."""

from typing import Literal

from pydantic import JsonValue, model_validator

from mobilityops.domain.analysis import AnalysisModel
from mobilityops.domain.decision_explanation import NextExperimentProposal
from mobilityops.domain.experiment import (
    ExperimentBatch,
    ExperimentPlan,
    ExperimentPrecheck,
    ExperimentReport,
)


class NextExperimentExecutionRequest(AnalysisModel):
    """A complete review snapshot; creating it never authorizes execution."""

    format: Literal["next_experiment_execution_request_v1"] = (
        "next_experiment_execution_request_v1"
    )
    request_sha256: str
    proposal_sha256: str
    proposal_file_sha256: str
    proposal: NextExperimentProposal
    source_experiment_id: str
    source_report_sha256: str
    experiment_id: str
    report_id: str
    plan: ExperimentPlan
    precheck: ExperimentPrecheck
    execution_configurations: dict[str, dict[str, JsonValue]]

    @model_validator(mode="after")
    def proposal_matches_request(self) -> "NextExperimentExecutionRequest":
        if self.proposal.plan != self.plan:
            raise ValueError("Request plan must equal the imported proposal plan")
        if self.proposal.experiment_id != self.source_experiment_id:
            raise ValueError("Request source experiment must equal the proposal source")
        if self.proposal.source_report_sha256 != self.source_report_sha256:
            raise ValueError("Request source report must equal the proposal source report")
        return self


class NextExperimentExecution(AnalysisModel):
    request: NextExperimentExecutionRequest
    proposal: NextExperimentProposal
    batch: ExperimentBatch
    report: ExperimentReport
