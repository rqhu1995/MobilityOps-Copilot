"""Take over an audited Copilot review without trusting its saved pass markers."""

import hmac
from pathlib import Path

from pydantic import ValidationError

from mobilityops.domain.audited_execution import (
    AuditedExecution,
    AuditedExecutionRequest,
)
from mobilityops.domain.copilot_audit import CopilotAuditReport
from mobilityops.domain.experiment_intake import (
    ExperimentExtraction,
    ExperimentLanguageInput,
    ExperimentPlanDraft,
    ExperimentPlanExecutionRequest,
)
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.copilot_audit import CopilotAuditService
from mobilityops.services.decision_explanation import report_fingerprint
from mobilityops.services.experiment_intake import (
    ExperimentInterpreter,
    ExperimentPlanIntakeService,
)
from mobilityops.services.experiment_service import checked_root, write_json
from mobilityops.services.gurobi_inputs import read_local, strict_json
from mobilityops.services.scenario_intake import fingerprint
from mobilityops.services.solver_service import SolverService


class _NoModelInterpreter(ExperimentInterpreter):
    def extract(self, request: ExperimentLanguageInput) -> ExperimentExtraction:
        raise RuntimeError("Audited execution never calls a language model")


class AuditedExecutionService:
    def __init__(self, solver_service: SolverService) -> None:
        self.solver = solver_service
        self.settings = solver_service.settings
        self.intake = ExperimentPlanIntakeService(
            solver_service, interpreter=_NoModelInterpreter()
        )

    def prepare_execution(
        self,
        *,
        source_session_id: str,
        source_audit_id: str,
        experiment_id: str,
        report_id: str,
    ) -> AuditedExecutionRequest:
        validate_run_id(experiment_id)
        validate_run_id(report_id)
        audit, draft, source_request, review_sha256 = self._verify_source(
            source_session_id, source_audit_id
        )
        execution_request = self.intake.prepare_execution(
            draft, experiment_id=experiment_id, report_id=report_id
        )
        request = AuditedExecutionRequest(
            request_sha256="",
            source_session_id=source_session_id,
            source_audit_id=source_audit_id,
            source_audit_manifest_sha256=audit.manifest_sha256,
            source_review_sha256=review_sha256,
            source_execution_request_sha256=source_request.request_sha256,
            source_draft_sha256=draft.draft_sha256,
            execution_request=execution_request,
        )
        return request.model_copy(update={
            "request_sha256": fingerprint(
                request.model_dump(mode="json", exclude={"request_sha256"})
            )
        })

    def execute(
        self,
        request: AuditedExecutionRequest,
        *,
        confirmed_request_sha256: str,
    ) -> AuditedExecution:
        request = AuditedExecutionRequest.model_validate(request.model_dump())
        expected = self.prepare_execution(
            source_session_id=request.source_session_id,
            source_audit_id=request.source_audit_id,
            experiment_id=request.execution_request.experiment_id,
            report_id=request.execution_request.report_id,
        )
        if request != expected or not hmac.compare_digest(
            confirmed_request_sha256, expected.request_sha256
        ):
            raise ValueError(
                "Confirmation does not match the audit, review, plan, precheck and configuration"
            )
        _, draft, _, _ = self._verify_source(
            request.source_session_id, request.source_audit_id
        )
        root = checked_root(self.settings, "audited-execution-requests")
        root.mkdir(parents=True, exist_ok=True)
        folder = root / request.execution_request.experiment_id
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ValueError(
                "Audited execution request already exists; refusing repeat execution"
            ) from exc
        write_json(folder / "request.json", request.model_dump(mode="json"))
        write_json(folder / "draft.json", draft.model_dump(mode="json"))
        write_json(folder / "state.json", {
            "status": "confirmed", "request_sha256": request.request_sha256,
        })
        try:
            execution = self.intake.execute(
                draft,
                request.execution_request,
                confirmed_request_sha256=request.execution_request.request_sha256,
            )
            lineage = {
                "format": "audited_execution_lineage_v1",
                "source_session_id": request.source_session_id,
                "source_audit_id": request.source_audit_id,
                "source_audit_manifest_sha256": request.source_audit_manifest_sha256,
                "source_review_sha256": request.source_review_sha256,
                "source_execution_request_sha256": (
                    request.source_execution_request_sha256
                ),
                "request_sha256": request.request_sha256,
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
                "report_id": request.execution_request.report_id,
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
        return AuditedExecution(
            request=request,
            execution=execution,
            lineage_sha256=lineage_sha256,
        )

    def _verify_source(
        self, source_session_id: str, source_audit_id: str
    ) -> tuple[
        CopilotAuditReport,
        ExperimentPlanDraft,
        ExperimentPlanExecutionRequest,
        str,
    ]:
        validate_run_id(source_session_id)
        validate_run_id(source_audit_id)
        audit = CopilotAuditService(self.settings).analyze(source_session_id)
        if (audit.audit_status != "verified_no_execution"
                or audit.session_status != "completed"
                or audit.execution.executed):
            raise ValueError(
                "Source Copilot session must be completed and verified without execution"
            )
        audit_root = checked_root(self.settings, "copilot-audits") / source_audit_id
        if not audit_root.is_dir() or audit_root.is_symlink():
            raise ValueError("Saved source audit must be an existing ordinary directory")
        try:
            saved_audit = CopilotAuditReport.model_validate(
                strict_json(read_local(audit_root, audit_root / "report.json"))
            )
        except (OSError, ValidationError) as exc:
            raise ValueError("Saved source audit report is invalid") from exc
        if saved_audit != audit or saved_audit.session_id != source_session_id:
            raise ValueError("Saved source audit differs from current deterministic replay")
        session_root = (
            checked_root(self.settings, "copilot-sessions") / source_session_id
        )
        reviews = sorted(session_root.glob("turn-*-execution-review.json"))
        if len(reviews) != 1:
            raise ValueError("Source session must contain exactly one execution review")
        review = strict_json(read_local(session_root, reviews[0]))
        if not isinstance(review, dict):
            raise ValueError("Source execution review must be a JSON object")
        review_sha256 = review.get("review_sha256")
        review_body = {key: value for key, value in review.items()
                       if key != "review_sha256"}
        if (review.get("format") != "copilot_execution_review_v1"
                or review.get("source") != "draft"
                or not isinstance(review_sha256, str)
                or not hmac.compare_digest(review_sha256, fingerprint(review_body))):
            raise ValueError("Source execution review identity or fingerprint is invalid")
        try:
            source_request = ExperimentPlanExecutionRequest.model_validate(
                review.get("request")
            )
        except ValidationError as exc:
            raise ValueError("Source execution request is invalid") from exc
        drafts = []
        for path in sorted(session_root.glob("turn-*-plan-draft.json")):
            try:
                draft = ExperimentPlanDraft.model_validate(
                    strict_json(read_local(session_root, path))
                )
            except ValidationError as exc:
                raise ValueError("Source plan draft is invalid") from exc
            if draft.draft_sha256 == source_request.draft_sha256:
                drafts.append(draft)
        if len(drafts) != 1:
            raise ValueError("Source review must bind exactly one saved plan draft")
        draft = drafts[0]
        fresh_source = self.intake.prepare_execution(
            draft,
            experiment_id=source_request.experiment_id,
            report_id=source_request.report_id,
        )
        if fresh_source != source_request:
            raise ValueError(
                "Source review, plan, precheck or backend configuration has changed"
            )
        return audit, draft, source_request, review_sha256
