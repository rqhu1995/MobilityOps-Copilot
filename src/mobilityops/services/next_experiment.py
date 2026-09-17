"""Offline takeover of stage-10 proposals into the confirmed execution chain."""

import hashlib
import hmac
from pathlib import Path

from pydantic import ValidationError

from mobilityops.domain.decision_explanation import NextExperimentProposal
from mobilityops.domain.experiment import ExperimentPrecheck
from mobilityops.domain.next_experiment import (
    NextExperimentExecution,
    NextExperimentExecutionRequest,
)
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.decision_explanation import propose_next_plan, report_fingerprint
from mobilityops.services.experiment_report import ExperimentReportService
from mobilityops.services.experiment_service import (
    ExperimentService,
    checked_root,
    experiment_path,
    write_json,
)
from mobilityops.services.gurobi_inputs import strict_json
from mobilityops.services.scenario_intake import fingerprint
from mobilityops.services.solver_service import SolverService


MAX_PROPOSAL_BYTES = 2_000_000


class NextExperimentPrecheckError(ValueError):
    def __init__(self, precheck: ExperimentPrecheck) -> None:
        self.precheck = precheck
        super().__init__("Every proposed case must pass precheck before confirmation")


class NextExperimentTakeoverService:
    """Rebuild provenance and review state before accepting a confirmation."""

    def __init__(self, solver_service: SolverService) -> None:
        self.solver_service = solver_service
        self.settings = solver_service.settings
        self.reports = ExperimentReportService(self.settings)

    @staticmethod
    def _read_proposal(path: Path) -> tuple[NextExperimentProposal, str, str]:
        path = path.expanduser()
        if path.is_symlink() or not path.is_file():
            raise ValueError("Proposal must be an existing regular, non-symlink file")
        data = path.read_bytes()
        if len(data) > MAX_PROPOSAL_BYTES:
            raise ValueError("Proposal file is too large")
        payload = strict_json(data)
        if not isinstance(payload, dict):
            raise ValueError("Proposal JSON must be an object")
        if payload.get("auto_execute") is not False:
            raise ValueError("Proposal auto_execute must be false")
        if payload.get("requires_new_review_and_confirmation") is not True:
            raise ValueError("Proposal must require a new review and confirmation")
        if payload.get("blockers"):
            raise ValueError("Blocked proposals cannot enter execution review")
        try:
            proposal = NextExperimentProposal.model_validate(payload)
        except ValidationError as exc:
            raise ValueError("Proposal does not satisfy next_experiment_proposal_v1") from exc
        if proposal.plan is None:
            raise ValueError("Proposal has no complete ExperimentPlan")
        if len(proposal.plan.variants) != 1:
            raise ValueError("A stage-10 proposal must contain exactly one selected variant")
        canonical_sha256 = fingerprint(proposal.model_dump(mode="json"))
        return proposal, canonical_sha256, hashlib.sha256(data).hexdigest()

    def _verify_proposal(self, proposal_path: Path) -> tuple[NextExperimentProposal, str, str]:
        proposal, proposal_sha256, file_sha256 = self._read_proposal(proposal_path)
        validate_run_id(proposal.experiment_id)
        report = self.reports.analyze(proposal.experiment_id)
        current_report_sha256 = report_fingerprint(report)
        if not hmac.compare_digest(proposal.source_report_sha256, current_report_sha256):
            raise ValueError(
                "Proposal source report is stale or its experiment evidence changed"
            )
        assert proposal.plan is not None
        variant_case_id = proposal.plan.variants[0].case_id
        expected = propose_next_plan(report, variant_case_id=variant_case_id)
        if expected != proposal:
            raise ValueError(
                "Proposal differs from the current deterministic candidate; regenerate it"
            )
        return proposal, proposal_sha256, file_sha256

    def prepare_execution(
        self,
        proposal_path: Path,
        *,
        experiment_id: str,
        report_id: str,
    ) -> NextExperimentExecutionRequest:
        proposal, proposal_sha256, file_sha256 = self._verify_proposal(proposal_path)
        validate_run_id(experiment_id)
        validate_run_id(report_id)
        experiment_path(self.settings, experiment_id)
        report_dir = checked_root(self.settings, "experiment-reports") / report_id
        if report_dir.exists() or report_dir.is_symlink():
            raise ValueError("Report ID already exists; refusing overwrite")
        plan = proposal.plan
        assert plan is not None
        precheck = ExperimentService(self.solver_service).precheck(
            plan, experiment_id=experiment_id
        )
        if not precheck.allowed:
            raise NextExperimentPrecheckError(precheck)
        if precheck.budget_may_skip_runs:
            raise ValueError("Proposal must budget every planned call before review")
        configurations = {}
        for case in precheck.cases:
            selected = case.decision.selected_backend
            if selected is None:
                raise ValueError("Every proposed case must have a selected backend")
            configurations[selected.value] = self.solver_service.execution_configuration(
                selected
            )
        request = NextExperimentExecutionRequest(
            request_sha256="",
            proposal_sha256=proposal_sha256,
            proposal_file_sha256=file_sha256,
            proposal=proposal,
            source_experiment_id=proposal.experiment_id,
            source_report_sha256=proposal.source_report_sha256,
            experiment_id=experiment_id,
            report_id=report_id,
            plan=plan,
            precheck=precheck,
            execution_configurations=configurations,
        )
        return request.model_copy(
            update={
                "request_sha256": fingerprint(
                    request.model_dump(mode="json", exclude={"request_sha256"})
                )
            }
        )

    def execute(
        self,
        proposal_path: Path,
        request: NextExperimentExecutionRequest,
        *,
        confirmed_request_sha256: str,
    ) -> NextExperimentExecution:
        request = NextExperimentExecutionRequest.model_validate(request.model_dump())
        expected = self.prepare_execution(
            proposal_path,
            experiment_id=request.experiment_id,
            report_id=request.report_id,
        )
        if request != expected or not hmac.compare_digest(
            confirmed_request_sha256, expected.request_sha256
        ):
            raise ValueError(
                "Execution confirmation does not match this proposal, evidence, precheck and configuration"
            )
        proposal, proposal_sha256, file_sha256 = self._verify_proposal(proposal_path)
        if (proposal_sha256 != request.proposal_sha256
                or file_sha256 != request.proposal_file_sha256):
            raise ValueError("Proposal changed after execution request verification")
        root = checked_root(self.settings, "next-experiment-requests")
        root.mkdir(parents=True, exist_ok=True)
        folder = root / request.experiment_id
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ValueError("Next-experiment request already exists; refusing repeat execution") from exc
        write_json(folder / "proposal.json", proposal.model_dump(mode="json"))
        write_json(folder / "request.json", request.model_dump(mode="json"))
        write_json(
            folder / "state.json",
            {"status": "confirmed", "request_sha256": request.request_sha256},
        )
        try:
            batch = ExperimentService(self.solver_service).execute(
                request.plan, experiment_id=request.experiment_id
            )
            report = ExperimentReportService(self.settings).write_report(
                request.experiment_id, report_id=request.report_id
            )
            write_json(
                folder / "state.json",
                {
                    "status": "reported",
                    "batch_status": batch.status,
                    "report_id": request.report_id,
                    "request_sha256": request.request_sha256,
                },
            )
        except BaseException as exc:
            write_json(
                folder / "state.json",
                {
                    "status": (
                        "interrupted"
                        if isinstance(exc, (KeyboardInterrupt, SystemExit))
                        else "failed"
                    ),
                    "error_type": type(exc).__name__,
                    "request_sha256": request.request_sha256,
                },
            )
            raise
        return NextExperimentExecution(
            request=request, proposal=proposal, batch=batch, report=report
        )
