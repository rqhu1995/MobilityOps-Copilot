"""Deterministic post-execution audit for one audit-bound terminal session."""

from typing import Literal

from pydantic import Field

from mobilityops.domain.analysis import AnalysisModel


class AuditedExecutionAuditFile(AnalysisModel):
    scope: Literal[
        "session", "source_audit", "request", "experiment", "report", "run"
    ]
    path: str
    sha256: str
    size_bytes: int = Field(ge=0)


class AuditedExecutionAuditReport(AnalysisModel):
    format: Literal["audited_execution_audit_report_v1"] = (
        "audited_execution_audit_report_v1"
    )
    session_id: str
    audit_status: Literal["verified_complete"] = "verified_complete"
    source_session_id: str
    source_audit_id: str
    experiment_id: str
    request_sha256: str
    review_sha256: str
    lineage_sha256: str
    report_sha256: str
    batch_status: str
    calls_invoked: int = Field(ge=0)
    verified_candidates: int = Field(ge=0)
    manifest_sha256: str
    files: tuple[AuditedExecutionAuditFile, ...]
    warnings: tuple[str, ...]
