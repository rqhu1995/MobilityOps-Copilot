"""Natural-language experiment plans rebuilt and authorized by local contracts."""

import hmac
import math
from typing import Protocol

from pydantic import ValidationError

from mobilityops.domain import BackendName, ScenarioSpec
from mobilityops.domain.experiment import ExperimentCase, ExperimentPlan, ExperimentPrecheck
from mobilityops.domain.experiment_intake import (
    ExperimentExtraction, ExperimentLanguageInput, ExperimentPlanDraft,
    ExperimentPlanExecution, ExperimentPlanExecutionRequest,
)
from mobilityops.domain.intake import Clarification
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.experiment_report import ExperimentReportService
from mobilityops.services.experiment_service import ExperimentService, checked_root, experiment_path, write_json
from mobilityops.services.scenario_intake import LABELS, fingerprint
from mobilityops.services.solver_service import SolverService


PLAN_LABELS = {
    "backend": "backend", "repetitions": "每场景重复次数",
    "external_timeout_sec": "外部 timeout（秒）", "max_calls": "最大 solver 调用数",
    "external_wait_budget_sec": "总外部等待预算（秒）", "failure_policy": "失败策略",
}
PLAN_FIELDS = frozenset(PLAN_LABELS)
CASE_CONTROL_FIELDS = frozenset({"backend", "repetitions", "external_timeout_sec"})


class ExperimentInterpreter(Protocol):
    def extract(self, request: ExperimentLanguageInput) -> ExperimentExtraction:
        """Return untrusted plan proposals with exact indexed quotes."""
        ...


class ExperimentPlanPrecheckError(ValueError):
    def __init__(self, precheck: ExperimentPrecheck) -> None:
        self.precheck = precheck
        super().__init__("Every experiment case must pass precheck before an execution request is created")


def validate_experiment_language_input(request: ExperimentLanguageInput) -> ExperimentLanguageInput:
    request = ExperimentLanguageInput.model_validate(request.model_dump())
    if any(not message.strip() or len(message) > 4000 for message in request.messages):
        raise ValueError("Each user message must contain 1..4000 characters")
    if sum(map(len, request.messages)) > 20000:
        raise ValueError("The conversation must contain at most 20000 characters")
    return request


class ExperimentPlanIntakeService:
    def __init__(self, solver_service: SolverService, *, interpreter: ExperimentInterpreter) -> None:
        self.solver_service = solver_service
        self.interpreter = interpreter

    def propose(self, text: str, *, context: ScenarioSpec | None = None,
                backend: BackendName | None = None) -> ExperimentPlanDraft:
        request = validate_experiment_language_input(
            ExperimentLanguageInput(messages=(text,), context=context, context_backend=backend)
        )
        return self.evaluate(request, self.interpreter.extract(request))

    def revise(self, draft: ExperimentPlanDraft, text: str,
               *, withdraw_issues: tuple[int, ...] = ()) -> ExperimentPlanDraft:
        self._check_draft(draft)
        if (len(set(withdraw_issues)) != len(withdraw_issues)
                or any(type(index) is not int or not 0 <= index < len(draft.extraction.issues)
                       for index in withdraw_issues)):
            raise ValueError("Invalid issue withdrawal indices")
        request = validate_experiment_language_input(
            draft.input.model_copy(update={"messages": (*draft.input.messages, text)})
        )
        fresh = ExperimentExtraction.model_validate(self.interpreter.extract(request).model_dump())
        cases = list(fresh.cases)
        fresh_case_keys = {(case.role, case.case_id) for case in fresh.cases}
        fresh_has_baseline = any(case.role == "baseline" for case in fresh.cases)
        cases.extend(case for case in draft.extraction.cases
                     if not (case.role == "baseline" and fresh_has_baseline)
                     and (case.role, case.case_id) not in fresh_case_keys)
        fresh_field_keys = {(field.target, field.field) for field in fresh.fields}
        fields = (*fresh.fields, *(field for field in draft.extraction.fields
                                   if (field.target, field.field) not in fresh_field_keys))
        fresh_issue_keys = {(issue.target, issue.field) for issue in fresh.issues}
        issues = list(fresh.issues)
        for index, issue in enumerate(draft.extraction.issues):
            if (index not in withdraw_issues and (issue.target, issue.field) not in fresh_issue_keys
                    and issue not in issues):
                issues.append(issue)
        return self.evaluate(request, ExperimentExtraction(cases=tuple(cases), fields=fields,
                                                           issues=tuple(issues)))

    def evaluate(self, request: ExperimentLanguageInput,
                 extraction: ExperimentExtraction) -> ExperimentPlanDraft:
        request = validate_experiment_language_input(request)
        extraction = ExperimentExtraction.model_validate(extraction.model_dump())
        questions: list[Clarification] = []
        sources: dict[str, str] = {}

        def provenance(item: object) -> None:
            message_index = getattr(item, "message_index")
            evidence = getattr(item, "evidence")
            field = getattr(item, "field", None)
            if message_index >= len(request.messages) or evidence not in request.messages[message_index]:
                questions.append(Clarification(
                    kind="provenance", field=str(field) if field else None,
                    question="模型输出的引用不在对应原文中，请核对该实验要求。",
                ))

        for item in (*extraction.cases, *extraction.fields, *extraction.issues):
            provenance(item)

        baselines = [case for case in extraction.cases if case.role == "baseline"]
        variants = [case for case in extraction.cases if case.role == "variant"]
        if len(baselines) != 1:
            questions.append(Clarification(kind="missing" if not baselines else "conflict",
                                           field="baseline",
                                           question="请明确一个且仅一个基准 case ID。"))
        declared = baselines[:1] + variants
        case_ids = [case.case_id for case in declared]
        if len(set(case_ids)) != len(case_ids):
            questions.append(Clarification(kind="conflict", field="case_id",
                                           question="case ID 必须安全且互不重复。"))
        for case in declared:
            try:
                validate_run_id(case.case_id)
                if len(case.case_id) > 40:
                    raise ValueError
            except ValueError:
                questions.append(Clarification(kind="invalid", field="case_id",
                                               question=f"case ID {case.case_id!r} 不安全；请使用不超过 40 字符的安全标识。"))

        assignments: dict[tuple[str, str], list] = {}
        declared_ids = set(case_ids)
        for proposal in extraction.fields:
            if proposal.target != "plan" and proposal.target not in declared_ids:
                questions.append(Clarification(kind="invalid", field=proposal.field,
                                               question=f"字段目标 {proposal.target!r} 不是已声明的实验场景。"))
                continue
            if proposal.target == "plan" and proposal.field not in PLAN_FIELDS:
                questions.append(Clarification(kind="invalid", field=proposal.field,
                                               question=f"{proposal.field} 不能作为全计划字段。"))
                continue
            if (proposal.target != "plan"
                    and proposal.field not in CASE_CONTROL_FIELDS | set(ScenarioSpec.model_fields)):
                questions.append(Clarification(kind="invalid", field=proposal.field,
                                               question=f"{proposal.field} 不能作为单个场景字段。"))
                continue
            assignments.setdefault((proposal.target, proposal.field), []).append(proposal)

        resolved: dict[tuple[str, str], object] = {}
        for key, proposals in assignments.items():
            latest = max(proposal.message_index for proposal in proposals)
            current = [proposal for proposal in proposals if proposal.message_index == latest]
            if len({fingerprint(proposal.value) for proposal in current}) != 1:
                questions.append(Clarification(kind="ambiguous", field=f"{key[0]}.{key[1]}",
                                               question=f"{key[0]} 的 {key[1]} 有多个冲突值，请给出一个明确值。"))
                continue
            resolved[key] = current[0].value
            sources[f"{key[0]}.{key[1]}"] = "user_text"

        for issue in extraction.issues:
            questions.append(Clarification(kind="unsupported", field=issue.field,
                                           question=issue.question))

        global_values = {field: resolved[("plan", field)] for field in PLAN_FIELDS
                         if ("plan", field) in resolved}
        if request.context_backend is not None and "backend" not in global_values:
            global_values["backend"] = request.context_backend.value
            sources["plan.backend"] = "context"
        for field in ("max_calls", "external_wait_budget_sec", "failure_policy"):
            if field not in global_values:
                questions.append(Clarification(kind="missing", field=field,
                                               question=f"请提供{PLAN_LABELS[field]}。"))

        plan: ExperimentPlan | None = None
        decisions = {}
        built_cases: list[ExperimentCase] = []
        baseline_values = request.context.model_dump(mode="json") if request.context else {}
        for field in baseline_values:
            key = baselines[0].case_id if len(baselines) == 1 else "baseline"
            sources[f"{key}.{field}"] = "context"
        baseline_id = baselines[0].case_id if len(baselines) == 1 else None

        if baseline_id is not None:
            for (target, field), value in resolved.items():
                if target == baseline_id and field not in CASE_CONTROL_FIELDS:
                    baseline_values[field] = value
                    sources[f"{baseline_id}.{field}"] = "user_text"

        scenario_by_case: dict[str, ScenarioSpec] = {}
        if baseline_id is not None:
            try:
                scenario_by_case[baseline_id] = ScenarioSpec.model_validate(baseline_values)
            except ValidationError as exc:
                self._scenario_questions(exc, baseline_id, questions)

        for case in variants:
            values = dict(baseline_values)
            for field in values:
                sources.setdefault(f"{case.case_id}.{field}", "inherited")
            for (target, field), value in resolved.items():
                if target == case.case_id and field not in CASE_CONTROL_FIELDS:
                    values[field] = value
                    sources[f"{case.case_id}.{field}"] = "user_text"
            try:
                scenario_by_case[case.case_id] = ScenarioSpec.model_validate(values)
            except ValidationError as exc:
                self._scenario_questions(exc, case.case_id, questions)

        if scenario_by_case and len(scenario_by_case) == len(declared):
            for case in declared:
                control = {}
                for field in CASE_CONTROL_FIELDS:
                    if (case.case_id, field) in resolved:
                        control[field] = resolved[(case.case_id, field)]
                    elif field in global_values:
                        control[field] = global_values[field]
                        sources.setdefault(f"{case.case_id}.{field}", sources.get(f"plan.{field}", "inherited"))
                    else:
                        questions.append(Clarification(kind="missing", field=f"{case.case_id}.{field}",
                                                       question=f"请提供 {case.case_id} 的 {PLAN_LABELS[field]}。"))
                if len(control) != len(CASE_CONTROL_FIELDS):
                    continue
                try:
                    built = ExperimentCase(case_id=case.case_id, scenario=scenario_by_case[case.case_id],
                                           backend=control["backend"], repetitions=control["repetitions"],
                                           external_timeout_sec=control["external_timeout_sec"])
                    built_cases.append(built)
                    decisions[case.case_id] = self.solver_service.select(built.scenario, backend=built.backend)
                except (ValidationError, ValueError) as exc:
                    questions.append(Clarification(kind="invalid", field=case.case_id,
                                                   question=f"{case.case_id} 不符合实验契约：{self._error_text(exc)}"))

        if len(built_cases) == len(declared) and built_cases and all(
                field in global_values for field in ("max_calls", "external_wait_budget_sec", "failure_policy")):
            try:
                plan = ExperimentPlan(
                    baseline=built_cases[0], variants=tuple(built_cases[1:]),
                    max_calls=global_values["max_calls"],
                    external_wait_budget_sec=global_values["external_wait_budget_sec"],
                    failure_policy=global_values["failure_policy"],
                )
                planned_wait = math.fsum(case.external_timeout_sec * case.repetitions for case in plan.cases)
                if plan.planned_calls > plan.max_calls:
                    questions.append(Clarification(kind="conflict", field="max_calls",
                                                   question=f"计划需要 {plan.planned_calls} 次调用，但上限只有 {plan.max_calls}。"))
                if planned_wait > plan.external_wait_budget_sec:
                    questions.append(Clarification(kind="conflict", field="external_wait_budget_sec",
                                                   question=f"计划需要 {planned_wait:g} 秒等待预算，但仅提供 {plan.external_wait_budget_sec:g} 秒。"))
            except (ValidationError, ValueError) as exc:
                questions.append(Clarification(kind="invalid", field="plan",
                                               question=f"实验计划不符合契约：{self._error_text(exc)}"))

        incompatible = plan is not None and any(
            decision.selected_backend is None for decision in decisions.values()
        )
        status = "needs_clarification" if questions else "incompatible" if incompatible or plan is None else "ready"
        values = {
            "plan": global_values,
            "cases": [case.model_dump(mode="json") for case in built_cases],
        }
        data = dict(input=request, extraction=extraction, values=values, field_sources=sources,
                    plan=plan, decisions=decisions, clarifications=tuple(questions), status=status)
        draft = ExperimentPlanDraft(draft_sha256="", **data)
        return draft.model_copy(update={
            "draft_sha256": fingerprint(draft.model_dump(mode="json", exclude={"draft_sha256"}))
        })

    @staticmethod
    def _scenario_questions(exc: ValidationError, case_id: str,
                            questions: list[Clarification]) -> None:
        for error in exc.errors(include_input=False, include_url=False):
            field = str(error["loc"][0])
            missing = error["type"] == "missing"
            questions.append(Clarification(
                kind="missing" if missing else "invalid", field=f"{case_id}.{field}",
                question=(f"请提供 {case_id} 的 {LABELS.get(field, field)}。" if missing
                          else f"{case_id} 的 {LABELS.get(field, field)}不符合字段约束：{error['msg']}"),
            ))

    @staticmethod
    def _error_text(exc: Exception) -> str:
        if isinstance(exc, ValidationError):
            return "；".join(error["msg"] for error in exc.errors(include_input=False, include_url=False))
        return str(exc)

    def _check_draft(self, draft: ExperimentPlanDraft) -> ExperimentPlanDraft:
        draft = ExperimentPlanDraft.model_validate(draft.model_dump())
        fresh = self.evaluate(draft.input, draft.extraction)
        if draft != fresh:
            raise ValueError("Plan draft or capability assessment changed; regenerate review before execution")
        return fresh

    def prepare_execution(self, draft: ExperimentPlanDraft, *, experiment_id: str,
                          report_id: str) -> ExperimentPlanExecutionRequest:
        draft = self._check_draft(draft)
        if draft.status != "ready" or draft.plan is None:
            raise ValueError("Resolve all plan clarifications and incompatibilities before execution review")
        experiment_path(self.solver_service.settings, experiment_id)
        validate_run_id(report_id)
        precheck = ExperimentService(self.solver_service).precheck(draft.plan, experiment_id=experiment_id)
        if not precheck.allowed:
            raise ExperimentPlanPrecheckError(precheck)
        if precheck.budget_may_skip_runs:
            raise ValueError("A language-authored plan must budget every planned call before review")
        configurations = {}
        for decision in draft.decisions.values():
            if decision.selected_backend is None:
                raise ValueError("Every case must have a selected backend")
            key = decision.selected_backend.value
            configurations[key] = self.solver_service.execution_configuration(decision.selected_backend)
        request = ExperimentPlanExecutionRequest(
            request_sha256="", draft_sha256=draft.draft_sha256,
            experiment_id=experiment_id, report_id=report_id, plan=draft.plan,
            precheck=precheck, execution_configurations=configurations,
        )
        return request.model_copy(update={
            "request_sha256": fingerprint(request.model_dump(mode="json", exclude={"request_sha256"}))
        })

    def execute(self, draft: ExperimentPlanDraft, request: ExperimentPlanExecutionRequest,
                *, confirmed_request_sha256: str) -> ExperimentPlanExecution:
        request = ExperimentPlanExecutionRequest.model_validate(request.model_dump())
        expected = self.prepare_execution(draft, experiment_id=request.experiment_id,
                                          report_id=request.report_id)
        if request != expected or not hmac.compare_digest(confirmed_request_sha256,
                                                          expected.request_sha256):
            raise ValueError("Execution confirmation does not match this plan, precheck and configuration")
        settings = self.solver_service.settings
        report_dir = checked_root(settings, "experiment-reports") / request.report_id
        if report_dir.exists() or report_dir.is_symlink():
            raise ValueError("Report ID already exists; refusing to start an unreportable execution")
        experiment_dir = experiment_path(settings, request.experiment_id)
        if experiment_dir.exists() or experiment_dir.is_symlink():
            raise ValueError("Experiment ID already exists; refusing to repeat execution")
        root = checked_root(settings, "language-experiment-requests")
        root.mkdir(parents=True, exist_ok=True)
        folder = root / request.experiment_id
        folder.mkdir(mode=0o700)
        write_json(folder / "draft.json", draft.model_dump(mode="json"))
        write_json(folder / "request.json", request.model_dump(mode="json"))
        write_json(folder / "state.json", {"status": "confirmed",
                                            "request_sha256": request.request_sha256})
        try:
            batch = ExperimentService(self.solver_service).execute(
                request.plan, experiment_id=request.experiment_id
            )
            report = ExperimentReportService(settings).write_report(
                request.experiment_id, report_id=request.report_id
            )
            write_json(folder / "state.json", {
                "status": "reported", "batch_status": batch.status,
                "report_id": request.report_id, "request_sha256": request.request_sha256,
            })
        except BaseException as exc:
            write_json(folder / "state.json", {
                "status": "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed",
                "error_type": type(exc).__name__, "request_sha256": request.request_sha256,
            })
            raise
        return ExperimentPlanExecution(request=request, batch=batch, report=report)
