"""Execution contracts bound to an audited decision dossier."""

from typing import Literal

from mobilityops.domain.analysis import AnalysisModel
from mobilityops.domain.decision_dossier import AuditedDecisionDossier
from mobilityops.domain.next_experiment import (
    NextExperimentExecution,
    NextExperimentExecutionRequest,
)


class DossierCampaignRequest(AnalysisModel):
    format: Literal["dossier_campaign_request_v1"] = "dossier_campaign_request_v1"
    request_sha256: str
    source_dossier_id: str
    source_dossier_sha256: str
    source_execution_audit_id: str
    source_execution_audit_manifest_sha256: str
    dossier: AuditedDecisionDossier
    execution_request: NextExperimentExecutionRequest


class DossierCampaignExecution(AnalysisModel):
    format: Literal["dossier_campaign_execution_v1"] = (
        "dossier_campaign_execution_v1"
    )
    request: DossierCampaignRequest
    execution: NextExperimentExecution
    lineage_sha256: str
