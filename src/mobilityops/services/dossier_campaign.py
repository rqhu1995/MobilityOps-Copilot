"""Revalidate a decision dossier before a bounded replication campaign."""

import hmac
from pathlib import Path

from mobilityops.domain.decision_dossier import AuditedDecisionDossier
from mobilityops.domain.dossier_campaign import (
    DossierCampaignExecution,
    DossierCampaignRequest,
)
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.decision_dossier import DecisionDossierService
from mobilityops.services.decision_explanation import report_fingerprint
from mobilityops.services.experiment_service import checked_root, write_json
from mobilityops.services.gurobi_inputs import read_local, strict_json
from mobilityops.services.next_experiment import NextExperimentTakeoverService
from mobilityops.services.scenario_intake import fingerprint
from mobilityops.services.solver_service import SolverService


class DossierCampaignService:
    def __init__(self, solver_service: SolverService) -> None:
        self.solver = solver_service
        self.settings = solver_service.settings
        self.dossiers = DecisionDossierService(self.settings)
        self.takeover = NextExperimentTakeoverService(solver_service)

    def prepare_execution(
        self,
        dossier_id: str,
        *,
        experiment_id: str,
        report_id: str,
    ) -> DossierCampaignRequest:
        dossier, proposal_path = self._verify_dossier(dossier_id)
        validate_run_id(experiment_id)
        validate_run_id(report_id)
        execution_request = self.takeover.prepare_execution(
            proposal_path,
            experiment_id=experiment_id,
            report_id=report_id,
        )
        if execution_request.proposal != dossier.next_experiment:
            raise ValueError("Dossier proposal differs from the takeover request")
        request = DossierCampaignRequest(
            request_sha256="",
            source_dossier_id=dossier_id,
            source_dossier_sha256=dossier.dossier_sha256,
            source_execution_audit_id=dossier.source_execution_audit_id,
            source_execution_audit_manifest_sha256=(
                dossier.source_execution_audit_manifest_sha256
            ),
            dossier=dossier,
            execution_request=execution_request,
        )
        return request.model_copy(update={
            "request_sha256": fingerprint(
                request.model_dump(mode="json", exclude={"request_sha256"})
            )
        })

    def execute(
        self,
        request: DossierCampaignRequest,
        *,
        confirmed_request_sha256: str,
    ) -> DossierCampaignExecution:
        request = DossierCampaignRequest.model_validate(request.model_dump())
        child = request.execution_request
        expected = self.prepare_execution(
            request.source_dossier_id,
            experiment_id=child.experiment_id,
            report_id=child.report_id,
        )
        if request != expected or not hmac.compare_digest(
            confirmed_request_sha256, expected.request_sha256
        ):
            raise ValueError(
                "Confirmation does not match the dossier, audit, proposal, "
                "precheck and configuration"
            )
        dossier, proposal_path = self._verify_dossier(request.source_dossier_id)
        root = checked_root(self.settings, "dossier-campaign-requests")
        root.mkdir(parents=True, exist_ok=True)
        folder = root / child.experiment_id
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ValueError(
                "Dossier campaign request already exists; refusing repeat execution"
            ) from exc
        write_json(folder / "dossier.json", dossier.model_dump(mode="json"))
        write_json(folder / "request.json", request.model_dump(mode="json"))
        write_json(folder / "state.json", {
            "status": "confirmed", "request_sha256": request.request_sha256,
        })
        try:
            execution = self.takeover.execute(
                proposal_path,
                child,
                confirmed_request_sha256=child.request_sha256,
            )
            lineage = {
                "format": "dossier_campaign_lineage_v1",
                "source_dossier_id": request.source_dossier_id,
                "source_dossier_sha256": request.source_dossier_sha256,
                "source_execution_audit_id": request.source_execution_audit_id,
                "source_execution_audit_manifest_sha256": (
                    request.source_execution_audit_manifest_sha256
                ),
                "request_sha256": request.request_sha256,
                "child_request_sha256": child.request_sha256,
                "child_experiment_id": execution.batch.experiment_id,
                "child_report_sha256": report_fingerprint(execution.report),
            }
            lineage_sha256 = fingerprint(lineage)
            write_json(folder / "lineage.json", {
                **lineage, "lineage_sha256": lineage_sha256,
            })
            write_json(folder / "state.json", {
                "status": "reported",
                "request_sha256": request.request_sha256,
                "batch_status": execution.batch.status,
                "report_id": child.report_id,
                "lineage_sha256": lineage_sha256,
            })
        except BaseException as exc:
            write_json(folder / "state.json", {
                "status": (
                    "interrupted"
                    if isinstance(exc, (KeyboardInterrupt, SystemExit))
                    else "failed"
                ),
                "request_sha256": request.request_sha256,
                "error_type": type(exc).__name__,
            })
            raise
        return DossierCampaignExecution(
            request=request,
            execution=execution,
            lineage_sha256=lineage_sha256,
        )

    def _verify_dossier(
        self, dossier_id: str
    ) -> tuple[AuditedDecisionDossier, Path]:
        validate_run_id(dossier_id)
        root = checked_root(self.settings, "decision-dossiers") / dossier_id
        if not root.is_dir() or root.is_symlink():
            raise ValueError("Decision dossier must be an existing ordinary directory")
        saved = AuditedDecisionDossier.model_validate(
            strict_json(read_local(root, root / "dossier.json"))
        )
        if saved.dossier_id != dossier_id:
            raise ValueError("Decision dossier identity is invalid")
        expected_sha = fingerprint(
            saved.model_dump(mode="json", exclude={"dossier_sha256"})
        )
        if not hmac.compare_digest(saved.dossier_sha256, expected_sha):
            raise ValueError("Decision dossier fingerprint is invalid")
        fresh = self.dossiers.build(
            saved.source_execution_audit_id,
            dossier_id=dossier_id,
            variant_case_id=saved.selected_variant_case_id,
        )
        if saved != fresh:
            raise ValueError("Saved decision dossier differs from current evidence replay")
        if (
            saved.recommendation != "collect_replicates"
            or any(item.status == "block" for item in saved.gates)
            or saved.next_experiment.plan is None
            or saved.next_experiment.auto_execute is not False
        ):
            raise ValueError(
                "Decision dossier is not eligible for a replication campaign"
            )
        proposal_path = root / "next-plan.json"
        proposal = strict_json(read_local(root, proposal_path))
        if proposal != saved.next_experiment.model_dump(mode="json"):
            raise ValueError("Saved dossier next-plan differs from its bound candidate")
        return saved, proposal_path
