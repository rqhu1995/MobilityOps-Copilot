"""Deterministic audit models for one saved Copilot session."""

from typing import Literal

from pydantic import Field

from mobilityops.domain.analysis import AnalysisModel


class CopilotAuditFile(AnalysisModel):
    scope: Literal["session", "llm", "experiment", "report", "run", "request"]
    path: str
    sha256: str
    size_bytes: int = Field(ge=0)


class CopilotAuditModelUsage(AnalysisModel):
    calls_reserved: int = Field(ge=0)
    calls_observed: int = Field(ge=0)
    calls_succeeded: int = Field(ge=0)
    calls_failed: int = Field(ge=0)
    reserved_hkd: str
    estimated_usage_hkd: str


class CopilotAuditExecution(AnalysisModel):
    executed: bool
    source: Literal["draft", "proposal"] | None = None
    parent_experiment_id: str | None = None
    child_experiment_id: str | None = None
    request_sha256: str | None = None
    review_sha256: str | None = None
    lineage_sha256: str | None = None
    report_sha256: str | None = None
    batch_status: str | None = None
    calls_invoked: int = Field(default=0, ge=0)
    verified_candidates: int = Field(default=0, ge=0)


class CopilotAuditReport(AnalysisModel):
    format: Literal["copilot_audit_report_v1"] = "copilot_audit_report_v1"
    session_id: str
    session_status: str
    audit_status: Literal[
        "verified_complete", "verified_no_execution", "verified_incomplete"
    ]
    manifest_sha256: str
    model_usage: CopilotAuditModelUsage
    decisions: tuple[str, ...]
    execution: CopilotAuditExecution
    files: tuple[CopilotAuditFile, ...]
    warnings: tuple[str, ...]
