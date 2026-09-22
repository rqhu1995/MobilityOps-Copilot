"""Deterministic audit contracts for a completed dossier campaign."""

from typing import Literal

from pydantic import Field

from mobilityops.domain.analysis import AnalysisModel


class DossierCampaignAuditFile(AnalysisModel):
    scope: Literal[
        "session",
        "source_dossier",
        "source_audit",
        "request",
        "experiment",
        "report",
        "run",
    ]
    path: str
    sha256: str
    size_bytes: int = Field(ge=0)


class DossierCampaignAuditReport(AnalysisModel):
    format: Literal["dossier_campaign_audit_report_v1"] = (
        "dossier_campaign_audit_report_v1"
    )
    audit_status: Literal["verified_complete"] = "verified_complete"
    session_id: str
    source_dossier_id: str
    source_dossier_sha256: str
    source_execution_audit_id: str
    source_execution_audit_manifest_sha256: str
    experiment_id: str
    request_sha256: str
    child_request_sha256: str
    review_sha256: str
    lineage_sha256: str
    report_sha256: str
    batch_status: str
    calls_invoked: int = Field(ge=0)
    verified_candidates: int = Field(ge=0)
    manifest_sha256: str
    files: tuple[DossierCampaignAuditFile, ...]
    warnings: tuple[str, ...]
