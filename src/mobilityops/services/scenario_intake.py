"""Language proposals cannot authorize solving; validation owns the workflow."""

import hashlib
import hmac
import json
from typing import Protocol

from pydantic import ValidationError

from mobilityops.domain import BackendName, ScenarioSpec
from mobilityops.domain.experiment import ExperimentCase, ExperimentPlan
from mobilityops.domain.intake import (
    Clarification, ExecutionRequest, IntakeExecution, LanguageInput, ScenarioDraft, ScenarioExtraction,
)
from mobilityops.services.experiment_report import ExperimentReportService
from mobilityops.services.experiment_service import ExperimentService, checked_root, experiment_path, write_json
from mobilityops.services.solver_service import SolverService
from mobilityops.domain.solution import validate_run_id


LABELS = {
    "scenario_name": "场景名称", "instance_id": "实例编号", "number_of_stations": "站点数量",
    "number_of_trucks": "车辆数量", "number_of_repairers": "维修人员数量", "truck_capacity": "每辆车容量",
    "operation_time_budget_sec": "作业预算（秒）", "solver_runtime_limit_sec": "求解时限（秒）",
    "loading_time_sec": "单车装卸时间（秒）", "repair_time_sec": "单车维修时间（秒）",
    "broken_bike_proportion": "损坏车比例", "seed": "seed", "objective_profile": "目标标识",
    "solution_requirement": "求解策略", "backend": "指定 backend",
}


class ScenarioInterpreter(Protocol):
    def extract(self, request: LanguageInput) -> ScenarioExtraction:
        """Return untrusted proposals with exact quotes from indexed user messages."""
        ...


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_language_input(request: LanguageInput) -> LanguageInput:
    request = LanguageInput.model_validate(request.model_dump())
    if any(not message.strip() or len(message) > 4000 for message in request.messages):
        raise ValueError("Each user message must contain 1..4000 characters")
    if sum(map(len, request.messages)) > 20000:
        raise ValueError("The conversation must contain at most 20000 characters")
    return request


class ScenarioIntakeService:
    def __init__(self, solver_service: SolverService, *, interpreter: ScenarioInterpreter) -> None:
        self.solver_service = solver_service
        self.interpreter = interpreter

    def propose(self, text: str, *, context: ScenarioSpec | None = None,
                backend: BackendName | None = None) -> ScenarioDraft:
        """One bounded interpretation call; no solver, preflight or license probe."""
        request = validate_language_input(LanguageInput(messages=(text,), context=context, context_backend=backend))
        return self.evaluate(request, self.interpreter.extract(request))

    def revise(self, draft: ScenarioDraft, text: str, *, withdraw_issues: tuple[int, ...] = ()) -> ScenarioDraft:
        """Reinterpret the full transcript; preserve fields and unresolved requirements.

        A caller must explicitly withdraw generic unsupported requirements by index.
        Field issues are resolved only by a new assignment quoting the new message.
        """
        self._check_draft(draft)
        if len(set(withdraw_issues)) != len(withdraw_issues) or any(type(i) is not int or not 0 <= i < len(draft.extraction.issues) for i in withdraw_issues):
            raise ValueError("Invalid issue withdrawal indices")
        request = validate_language_input(draft.input.model_copy(update={"messages": (*draft.input.messages, text)}))
        extraction = ScenarioExtraction.model_validate(self.interpreter.extract(request).model_dump())
        retained = tuple(f for f in draft.extraction.fields if f not in extraction.fields)
        fresh_fields = {f.field for f in extraction.fields if f.message_index == len(request.messages) - 1}
        issues = list(extraction.issues)
        for index, issue in enumerate(draft.extraction.issues):
            if index not in withdraw_issues and (issue.field is None or issue.field not in fresh_fields) and issue not in issues:
                issues.append(issue)
        combined = ScenarioExtraction(fields=(*extraction.fields, *retained), issues=tuple(issues))
        return self.evaluate(request, combined)

    def evaluate(self, request: LanguageInput, extraction: ScenarioExtraction) -> ScenarioDraft:
        """Recompute a draft entirely from proposals and existing domain constraints."""
        request = validate_language_input(request)
        extraction = ScenarioExtraction.model_validate(extraction.model_dump())
        values = request.context.model_dump(mode="json") if request.context else {}
        sources = {key: "context" for key in values}
        backend = request.context_backend
        if backend is not None:
            sources["backend"] = "context"
        questions = []
        assignments = {}
        for proposal in (*extraction.fields, *extraction.issues):
            if proposal.message_index >= len(request.messages) or proposal.evidence not in request.messages[proposal.message_index]:
                questions.append(Clarification(kind="provenance", field=proposal.field,
                                                question="模型输出的引用不在对应原文中，请核对该要求。"))
        for proposal in extraction.fields:
            assignments.setdefault(proposal.field, []).append(proposal)
        for field, proposals in assignments.items():
            latest = max(p.message_index for p in proposals)
            current = [p for p in proposals if p.message_index == latest]
            if len({fingerprint(p.value) for p in current}) != 1:
                values.pop(field, None)
                sources.pop(field, None)
                questions.append(Clarification(kind="ambiguous", field=field,
                                                question=f"{LABELS[field]}有多个冲突值，请给出一个明确值。"))
                continue
            value = current[0].value
            if field == "backend":
                try:
                    backend = BackendName(value) if value is not None else None
                    sources[field] = "user_text"
                except (ValueError, TypeError):
                    questions.append(Clarification(kind="invalid", field=field, question="backend 仅支持 hgs、gurobi 或不指定。"))
            else:
                values[field] = value
                sources[field] = "user_text"
        questions.extend(Clarification(kind="unsupported", field=issue.field, question=issue.question) for issue in extraction.issues)
        candidate, decision = None, None
        try:
            candidate = ScenarioSpec.model_validate(values)
        except ValidationError as exc:
            for error in exc.errors(include_input=False, include_url=False):
                field = str(error["loc"][0])
                if any(q.field == field for q in questions):
                    continue
                missing = error["type"] == "missing"
                questions.append(Clarification(kind="missing" if missing else "invalid", field=field,
                                                question=f"请提供{LABELS.get(field, field)}。" if missing else f"{LABELS.get(field, field)}不符合字段约束：{error['msg']}"))
        if candidate is not None:
            for field in ScenarioSpec.model_fields:
                if field not in sources:
                    sources[field] = "schema_default"
            decision = self.solver_service.select(candidate, backend=backend)
        status = "needs_clarification" if questions else "incompatible" if decision is None or decision.selected_backend is None else "ready"
        data = dict(input=request, extraction=extraction, values=values, requested_backend=backend,
                    field_sources=sources, candidate=candidate, decision=decision,
                    clarifications=tuple(questions), status=status)
        draft = ScenarioDraft(draft_sha256="", **data)
        return draft.model_copy(update={"draft_sha256": fingerprint(draft.model_dump(mode="json", exclude={"draft_sha256"}))})

    def _check_draft(self, draft: ScenarioDraft) -> ScenarioDraft:
        draft = ScenarioDraft.model_validate(draft.model_dump())
        fresh = self.evaluate(draft.input, draft.extraction)
        if draft != fresh:
            raise ValueError("Draft content or capability assessment changed; regenerate the review before execution")
        return fresh

    def prepare_execution(self, draft: ScenarioDraft, *, experiment_id: str, report_id: str,
                          external_timeout_sec: float) -> ExecutionRequest:
        """Build a reviewable single-call plan without executing or probing readiness."""
        draft = self._check_draft(draft)
        if draft.status != "ready" or draft.candidate is None or draft.decision is None:
            raise ValueError("Resolve clarification and backend incompatibility before preparing execution")
        experiment_path(self.solver_service.settings, experiment_id)
        validate_run_id(report_id)
        case = ExperimentCase(case_id="candidate", scenario=draft.candidate,
                              backend=draft.decision.selected_backend, repetitions=1,
                              external_timeout_sec=external_timeout_sec)
        plan = ExperimentPlan(baseline=case, max_calls=1, external_wait_budget_sec=external_timeout_sec,
                              failure_policy="stop")
        request = ExecutionRequest(request_sha256="", draft_sha256=draft.draft_sha256,
                                   experiment_id=experiment_id, report_id=report_id, plan=plan)
        return request.model_copy(update={"request_sha256": fingerprint(request.model_dump(mode="json", exclude={"request_sha256"}))})

    def execute(self, draft: ScenarioDraft, request: ExecutionRequest, *, confirmed_request_sha256: str) -> IntakeExecution:
        """An explicit caller action confirms exactly one reviewed plan and its budget."""
        request = ExecutionRequest.model_validate(request.model_dump())
        expected = self.prepare_execution(draft, experiment_id=request.experiment_id, report_id=request.report_id,
                                          external_timeout_sec=request.plan.baseline.external_timeout_sec)
        if request != expected or not hmac.compare_digest(confirmed_request_sha256, expected.request_sha256):
            raise ValueError("Execution confirmation does not match this exact draft and request")
        settings = self.solver_service.settings
        report_dir = checked_root(settings, "experiment-reports") / request.report_id
        if report_dir.exists() or report_dir.is_symlink():
            raise ValueError("Report ID already exists; refusing to start an unreportable execution")
        experiment_dir = experiment_path(settings, request.experiment_id)
        if experiment_dir.exists() or experiment_dir.is_symlink():
            raise ValueError("Experiment ID already exists; refusing to repeat execution")
        root = checked_root(settings, "language-requests")
        root.mkdir(parents=True, exist_ok=True)
        folder = root / request.experiment_id
        folder.mkdir(mode=0o700)
        write_json(folder / "draft.json", draft.model_dump(mode="json"))
        write_json(folder / "request.json", request.model_dump(mode="json"))
        write_json(folder / "state.json", {"status": "confirmed", "request_sha256": request.request_sha256})
        try:
            batch = ExperimentService(self.solver_service).execute(request.plan, experiment_id=request.experiment_id)
            report = ExperimentReportService(settings).write_report(request.experiment_id, report_id=request.report_id)
            write_json(folder / "state.json", {"status": "reported", "batch_status": batch.status,
                                               "report_id": request.report_id, "request_sha256": request.request_sha256})
        except BaseException as exc:
            write_json(folder / "state.json", {"status": "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed",
                                               "error_type": type(exc).__name__, "request_sha256": request.request_sha256})
            raise
        return IntakeExecution(request=request, batch=batch, report=report)
