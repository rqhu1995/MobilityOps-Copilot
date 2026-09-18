"""Execution request bound to a verified no-execution Copilot audit."""

from typing import Literal

from mobilityops.domain.analysis import AnalysisModel
from mobilityops.domain.experiment_intake import (
    ExperimentPlanExecution,
    ExperimentPlanExecutionRequest,
)


class AuditedExecutionRequest(AnalysisModel):
    format: Literal["audited_execution_request_v1"] = "audited_execution_request_v1"
    request_sha256: str
    source_session_id: str
    source_audit_id: str
    source_audit_manifest_sha256: str
    source_review_sha256: str
    source_execution_request_sha256: str
    source_draft_sha256: str
    execution_request: ExperimentPlanExecutionRequest


class AuditedExecution(AnalysisModel):
    format: Literal["audited_execution_v1"] = "audited_execution_v1"
    request: AuditedExecutionRequest
    execution: ExperimentPlanExecution
    lineage_sha256: str
