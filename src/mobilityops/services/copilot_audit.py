"""Read-only replay and evidence manifest for saved Copilot sessions."""

from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

from mobilityops.config import Settings
from mobilityops.domain.copilot import CopilotDecision, CopilotModelInput
from mobilityops.domain.copilot_audit import (
    CopilotAuditExecution,
    CopilotAuditFile,
    CopilotAuditModelUsage,
    CopilotAuditReport,
)
from mobilityops.domain.experiment import ExperimentReport
from mobilityops.domain.decision_explanation import (
    DecisionExplanation,
    NextExperimentProposal,
)
from mobilityops.domain.experiment_intake import (
    ExperimentExtraction,
    ExperimentLanguageInput,
    ExperimentPlanDraft,
    ExperimentPlanExecutionRequest,
)
from mobilityops.domain.next_experiment import NextExperimentExecutionRequest
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.copilot import CopilotDecisionService
from mobilityops.services.decision_explanation import (
    DecisionExplanationService,
    propose_next_plan,
    report_fingerprint,
)
from mobilityops.services.experiment_report import ExperimentReportService
from mobilityops.services.experiment_service import checked_root, load_batch, write_json
from mobilityops.services.gemini_intake import GeminiBudgetConfig, USER_DATA_MARKER
from mobilityops.services.gurobi_inputs import read_local, strict_json
from mobilityops.services.scenario_intake import fingerprint


MAX_JSON_BYTES = 2_000_000
MAX_FILES = 20_000
WARNINGS = (
    "审计只重新验证本地证据的一致性；文件散列不能证明全部证据未被协调修改。",
    "审计不会重新调用 Gemini、solver 或许可证环境，也不重新判定模型当时的主观推理质量。",
    "模型输出仍是不可信提议；只有已保存的本地契约、确认和独立验证结果具有对应含义。",
)
SESSION_STATUSES = {
    "awaiting_llm_consent", "awaiting_input", "routing",
    "awaiting_execution_confirmation", "execution_confirmed",
    "cancelled", "completed", "interrupted", "failed",
}
SESSION_PHASES = {
    "llm_consent", "conversation", "routing", "tool", "executing",
}


class _SavedDecisionInterpreter:
    def __init__(self, decision: CopilotDecision) -> None:
        self.decision = decision

    def decide(self, request: CopilotModelInput) -> CopilotDecision:
        return self.decision


class CopilotAuditService:
    def __init__(self, settings: Settings) -> None:
        self.settings = Settings.model_validate(settings.model_dump())

    def analyze(self, session_id: str) -> CopilotAuditReport:
        validate_run_id(session_id)
        session_root = checked_root(self.settings, "copilot-sessions") / session_id
        if not session_root.is_dir() or session_root.is_symlink():
            raise ValueError("Copilot session must be an existing non-symlink directory")
        session = self._object(session_root, session_root / "session.json")
        state = self._object(session_root, session_root / "state.json")
        if (session.get("format") != "copilot_terminal_session_v1"
                or session.get("session_id") != session_id):
            raise ValueError("Copilot session identity is invalid")
        if (session.get("output_experiment_id") != f"copilot-{session_id}"
                or session.get("budget_id") != f"copilot-{session_id}"):
            raise ValueError("Copilot session output or budget identity is invalid")
        if (state.get("format") != "copilot_terminal_session_v1"
                or state.get("session_id") != session_id
                or state.get("mode") != "copilot"):
            raise ValueError("Copilot state identity is invalid")
        session_status = state.get("status")
        if (session_status not in SESSION_STATUSES
                or state.get("phase") not in SESSION_PHASES):
            raise ValueError("Copilot state has an invalid status or phase")
        self._verify_events(session_root, state)

        model_usage, saved_copilot_turns, llm_root = self._verify_llm(
            session, state
        )
        decisions = self._verify_turns(
            session_root, state, saved_copilot_turns, session_status
        )
        referenced_experiments = self._verify_tool_artifacts(session_root)
        execution, evidence_roots = self._verify_execution(
            session_id, session_root, session, state
        )
        self._verify_presented_evidence(session_root, execution)
        roots: list[tuple[str, Path]] = [("session", session_root)]
        if llm_root is not None:
            roots.append(("llm", llm_root))
        roots.extend(evidence_roots)
        existing_paths = {path.resolve() for _, path in roots if path.exists()}
        for experiment_id in sorted(referenced_experiments):
            for scoped in self._experiment_evidence_roots(experiment_id):
                if scoped[1].resolve() not in existing_paths:
                    roots.append(scoped)
                    existing_paths.add(scoped[1].resolve())
        files = self._manifest(roots)
        manifest_sha256 = fingerprint([item.model_dump(mode="json") for item in files])
        if session_status == "completed":
            audit_status = "verified_complete" if execution.executed else "verified_no_execution"
        elif session_status == "cancelled" and not execution.executed:
            audit_status = "verified_no_execution"
        else:
            audit_status = "verified_incomplete"
        return CopilotAuditReport(
            session_id=session_id,
            session_status=session_status,
            audit_status=audit_status,
            manifest_sha256=manifest_sha256,
            model_usage=model_usage,
            decisions=tuple(item.action for item in decisions),
            execution=execution,
            files=files,
            warnings=WARNINGS,
        )

    def write_report(self, session_id: str, *, audit_id: str) -> CopilotAuditReport:
        validate_run_id(audit_id)
        report = self.analyze(session_id)
        root = checked_root(self.settings, "copilot-audits")
        folder = root / audit_id
        if folder.exists() or folder.is_symlink():
            raise ValueError("Copilot audit already exists; refusing overwrite")
        markdown = render_markdown(report)
        root.mkdir(parents=True, exist_ok=True)
        folder.mkdir(mode=0o700)
        write_json(folder / "report.json", report.model_dump(mode="json"))
        (folder / "report.md").write_text(markdown, encoding="utf-8")
        return report

    def _verify_events(self, root: Path, state: dict) -> None:
        data = self._read(root, root / "events.jsonl", max_bytes=MAX_JSON_BYTES)
        lines = data.splitlines()
        if not lines:
            raise ValueError("Copilot event log is empty")
        events = []
        for line in lines:
            value = strict_json(line)
            if (not isinstance(value, dict) or not isinstance(value.get("at"), str)
                    or value.get("format") != "copilot_terminal_session_v1"
                    or value.get("session_id") != state.get("session_id")
                    or value.get("mode") != "copilot"
                    or value.get("status") not in SESSION_STATUSES
                    or value.get("phase") not in SESSION_PHASES):
                raise ValueError("Copilot event log contains an invalid event")
            events.append(value)
        final = {key: value for key, value in events[-1].items() if key != "at"}
        if final != state:
            raise ValueError("Copilot final event differs from state.json")
        turns = [event.get("turn") for event in events]
        if any(type(turn) is not int or turn < 0 for turn in turns):
            raise ValueError("Copilot event log contains an invalid turn")
        if turns != sorted(turns):
            raise ValueError("Copilot event turns are not monotonic")

    def _verify_llm(
        self, session: dict, state: dict
    ) -> tuple[
        CopilotAuditModelUsage,
        list[tuple[CopilotModelInput, CopilotDecision]],
        Path | None,
    ]:
        budget_id = session.get("budget_id")
        budget = session.get("budget")
        if budget_id != f"copilot-{session.get('session_id')}":
            raise ValueError("Copilot budget ID differs from its session")
        config = GeminiBudgetConfig.model_validate(budget)
        root = checked_root(self.settings, "llm") / str(budget_id)
        if not root.exists():
            if not (state.get("status") == "cancelled"
                    and state.get("phase") == "llm_consent"
                    and state.get("turn") == 0):
                raise ValueError("Copilot LLM budget evidence is missing")
            return CopilotAuditModelUsage(
                calls_reserved=0, calls_observed=0, calls_succeeded=0,
                calls_failed=0, reserved_hkd="0", estimated_usage_hkd="0",
            ), [], None
        if not root.is_dir() or root.is_symlink():
            raise ValueError("Copilot LLM budget must be a non-symlink directory")
        saved_config = GeminiBudgetConfig.model_validate(
            self._object(root, root / "config.json")
        )
        if saved_config != config:
            raise ValueError("Copilot session budget differs from the saved LLM budget")
        ledger = self._object(root, root / "ledger.json")
        calls = ledger.get("calls_reserved")
        if type(calls) is not int or not 0 <= calls <= config.max_calls:
            raise ValueError("Copilot LLM ledger has an invalid call count")
        try:
            reserved = Decimal(str(ledger.get("reserved_hkd")))
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("Copilot LLM ledger has an invalid reservation") from exc
        if (not reserved.is_finite() or reserved < 0
                or reserved != config.reservation_hkd * calls):
            raise ValueError("Copilot LLM ledger reservation differs from its call count")
        call_dirs = sorted(root.glob("call-*"))
        if any(not path.is_dir() or path.is_symlink() for path in call_dirs):
            raise ValueError("Copilot LLM call evidence must use ordinary directories")
        if [path.name for path in call_dirs] != [f"call-{index:03d}" for index in range(1, calls + 1)]:
            raise ValueError("Copilot LLM call directories differ from the budget ledger")
        succeeded = failed = 0
        estimated = Decimal("0")
        copilot_turns = []
        for folder in call_dirs:
            call_state = self._object(root, folder / "state.json")
            status = call_state.get("status")
            if status == "succeeded":
                succeeded += 1
            elif status == "failed":
                failed += 1
            elif status not in {"reserved", "generating"}:
                raise ValueError("Copilot LLM call has an invalid status")
            if "estimated_usage_hkd" in call_state:
                try:
                    call_cost = Decimal(str(call_state["estimated_usage_hkd"]))
                except (InvalidOperation, TypeError) as exc:
                    raise ValueError("Copilot LLM call has invalid usage cost") from exc
                if not call_cost.is_finite() or call_cost < 0:
                    raise ValueError("Copilot LLM call has invalid usage cost")
                estimated += call_cost
            artifacts = [
                path for path in (
                    folder / "copilot-decision.json",
                    folder / "experiment-extraction.json",
                ) if path.exists() or path.is_symlink()
            ]
            if status == "succeeded" and len(artifacts) != 1:
                raise ValueError("Successful Copilot LLM call lacks one final artifact")
            if status != "succeeded" and artifacts:
                raise ValueError("Incomplete Copilot LLM call has a final artifact")
            if artifacts:
                request_payload = self._object(root, folder / "request.json")
                prompt = request_payload.get("input")
                if not isinstance(prompt, str) or prompt.count(USER_DATA_MARKER) != 1:
                    raise ValueError("Copilot LLM request lacks one bounded user payload")
                user_payload = strict_json(
                    prompt.partition(USER_DATA_MARKER)[2].encode("utf-8")
                )
                model_output = self._model_output(root, folder)
                if artifacts[0].name == "copilot-decision.json":
                    provider_input = CopilotModelInput.model_validate(user_payload)
                    raw_decision = CopilotDecision.model_validate(model_output)
                    saved_decision = CopilotDecision.model_validate(
                        self._object(root, artifacts[0])
                    )
                    if raw_decision != saved_decision:
                        raise ValueError(
                            "Copilot decision artifact differs from raw provider response"
                        )
                    copilot_turns.append((provider_input, saved_decision))
                else:
                    ExperimentLanguageInput.model_validate(user_payload)
                    raw_extraction = ExperimentExtraction.model_validate(model_output)
                    saved_extraction = ExperimentExtraction.model_validate(
                        self._object(root, artifacts[0])
                    )
                    if raw_extraction != saved_extraction:
                        raise ValueError(
                            "Experiment extraction differs from raw provider response"
                        )
        if state.get("status") == "completed" and (
                succeeded != calls or failed != 0):
            raise ValueError("Completed Copilot session has non-successful model calls")
        return CopilotAuditModelUsage(
            calls_reserved=calls,
            calls_observed=len(call_dirs),
            calls_succeeded=succeeded,
            calls_failed=failed,
            reserved_hkd=str(reserved),
            estimated_usage_hkd=str(estimated),
        ), copilot_turns, root

    def _verify_turns(
        self,
        root: Path,
        state: dict,
        provider_turns: list[tuple[CopilotModelInput, CopilotDecision]],
        session_status: str,
    ) -> list[CopilotDecision]:
        inputs = self._indexed(root, "model-input")
        saved = self._indexed(root, "decision")
        turn = state.get("turn")
        if type(turn) is not int or turn < 0 or list(inputs) != list(range(1, turn + 1)):
            raise ValueError("Copilot model inputs differ from the saved turn count")
        if any(index not in inputs for index in saved):
            raise ValueError("Copilot decision lacks its model input")
        turns = []
        for index, path in saved.items():
            request = CopilotModelInput.model_validate(self._object(root, inputs[index]))
            decision = CopilotDecision.model_validate(self._object(root, path))
            replayed = CopilotDecisionService(
                _SavedDecisionInterpreter(decision)
            ).decide(request)
            if replayed != decision:
                raise ValueError("Copilot decision replay differs from saved output")
            turns.append((request, decision))
        if turns != provider_turns[:len(turns)]:
            raise ValueError("Copilot turns differ from provider requests or artifacts")
        if session_status == "completed" and len(turns) != len(provider_turns):
            raise ValueError("Completed Copilot session has an unconsumed provider decision")
        return [decision for _, decision in turns]

    def _model_output(self, root: Path, folder: Path) -> dict:
        response = self._object(root, folder / "response.json")
        steps = response.get("steps")
        if response.get("status") != "completed" or not isinstance(steps, list):
            raise ValueError("Successful Copilot LLM call lacks a complete raw response")
        outputs = [
            step for step in steps
            if isinstance(step, dict) and step.get("type") == "model_output"
        ]
        if len(outputs) != 1 or any(
            not isinstance(step, dict)
            or step.get("type") not in {"thought", "model_output"}
            for step in steps
        ):
            raise ValueError("Copilot raw response has unexpected provider steps")
        content = outputs[0].get("content")
        if (not isinstance(content, list) or not content
                or any(not isinstance(part, dict)
                       or part.get("type") != "text"
                       or not isinstance(part.get("text"), str)
                       for part in content)):
            raise ValueError("Copilot raw response lacks one text model output")
        value = strict_json(
            "".join(part["text"] for part in content).encode("utf-8")
        )
        if not isinstance(value, dict):
            raise ValueError("Copilot raw model output must be a JSON object")
        return value

    def _verify_tool_artifacts(self, root: Path) -> set[str]:
        experiments = set()
        for path in sorted(root.glob("turn-*-plan-draft.json")):
            draft = ExperimentPlanDraft.model_validate(self._object(root, path))
            expected = fingerprint(
                draft.model_dump(mode="json", exclude={"draft_sha256"})
            )
            if draft.draft_sha256 != expected:
                raise ValueError("Copilot plan draft fingerprint is invalid")
        for path in sorted(root.glob("turn-*-explanation.json")):
            explanation = DecisionExplanation.model_validate(self._object(root, path))
            report = ExperimentReportService(self.settings).analyze(
                explanation.experiment_id
            )
            candidates = [
                DecisionExplanationService(self.settings).explain(
                    explanation.experiment_id
                ),
                *(
                    DecisionExplanationService(self.settings).explain(
                        explanation.experiment_id,
                        variant_case_id=case.case_id,
                    )
                    for case in report.batch.plan.variants
                ),
            ]
            if explanation not in candidates:
                raise ValueError("Copilot explanation differs from fresh evidence replay")
            experiments.add(explanation.experiment_id)
        for path in sorted(root.glob("turn-*-next-plan.json")):
            proposal = NextExperimentProposal.model_validate(self._object(root, path))
            report = ExperimentReportService(self.settings).analyze(
                proposal.experiment_id
            )
            candidates = [
                propose_next_plan(report, variant_case_id=case.case_id)
                for case in report.batch.plan.variants
            ]
            if proposal not in candidates:
                raise ValueError("Copilot next plan differs from fresh evidence replay")
            experiments.add(proposal.experiment_id)
        return experiments

    def _experiment_evidence_roots(
        self, experiment_id: str
    ) -> list[tuple[str, Path]]:
        validate_run_id(experiment_id)
        report = ExperimentReportService(self.settings).analyze(experiment_id)
        experiment = checked_root(self.settings, "experiments") / experiment_id
        roots: list[tuple[str, Path]] = [("experiment", experiment)]
        saved_report = checked_root(self.settings, "experiment-reports") / experiment_id
        if saved_report.exists() or saved_report.is_symlink():
            roots.append(("report", saved_report))
        for attempt in report.batch.attempts:
            run = self.settings.runs_dir / attempt.run_id
            if run.exists() or run.is_symlink():
                roots.append(("run", run))
        return roots

    def _verify_execution(
        self, session_id: str, root: Path, session: dict, state: dict
    ) -> tuple[CopilotAuditExecution, list[tuple[str, Path]]]:
        lineage_path = root / "lineage.json"
        completed = state.get("execution_completed") is True
        if not lineage_path.exists() and not lineage_path.is_symlink():
            if completed:
                raise ValueError("Copilot state claims execution without lineage evidence")
            return CopilotAuditExecution(executed=False), []
        lineage = self._object(root, lineage_path)
        if not completed:
            raise ValueError("Copilot lineage exists but state does not claim execution")
        if (lineage.get("format") != "copilot_experiment_lineage_v1"
                or lineage.get("session_id") != session_id):
            raise ValueError("Copilot lineage identity is invalid")
        recorded_lineage = lineage.get("lineage_sha256")
        expected_lineage = fingerprint({
            key: value for key, value in lineage.items() if key != "lineage_sha256"
        })
        if recorded_lineage != expected_lineage:
            raise ValueError("Copilot lineage fingerprint is invalid")
        child = lineage.get("child_experiment_id")
        source = lineage.get("source")
        if child != session.get("output_experiment_id") or source not in {"draft", "proposal"}:
            raise ValueError("Copilot lineage child or source is invalid")
        validate_run_id(child)
        reviews = self._execution_reviews(root)
        review_sha = lineage.get("review_sha256")
        if review_sha not in reviews:
            raise ValueError("Copilot lineage does not identify a saved execution review")
        request = reviews[review_sha]
        if request.request_sha256 != lineage.get("request_sha256"):
            raise ValueError("Copilot lineage request differs from its execution review")
        if request.experiment_id != child or request.report_id != child:
            raise ValueError("Copilot execution request uses an unexpected child ID")
        if source == "draft":
            if not isinstance(request, ExperimentPlanExecutionRequest):
                raise ValueError("Copilot draft lineage uses the wrong request type")
            drafts = [ExperimentPlanDraft.model_validate(self._object(root, path))
                      for path in sorted(root.glob("turn-*-plan-draft.json"))]
            if not any(item.draft_sha256 == request.draft_sha256 and item.plan == request.plan
                       for item in drafts):
                raise ValueError("Copilot execution request does not match a saved plan draft")
            request_root = checked_root(self.settings, "language-experiment-requests") / child
        else:
            if not isinstance(request, NextExperimentExecutionRequest):
                raise ValueError("Copilot proposal lineage uses the wrong request type")
            source_report = ExperimentReportService(self.settings).analyze(
                request.source_experiment_id
            )
            expected = propose_next_plan(
                source_report, variant_case_id=request.plan.variants[0].case_id
            )
            if request.proposal != expected:
                raise ValueError("Copilot proposal request differs from current source evidence")
            request_root = checked_root(self.settings, "next-experiment-requests") / child
        self._verify_request_folder(request_root, request)
        fresh = ExperimentReportService(self.settings).analyze(child)
        report_sha = report_fingerprint(fresh)
        if report_sha != lineage.get("child_report_sha256"):
            raise ValueError("Copilot child report differs from its lineage")
        report_root = checked_root(self.settings, "experiment-reports") / child
        saved_report = ExperimentReport.model_validate(
            self._object(report_root, report_root / "report.json")
        )
        if saved_report != fresh:
            raise ValueError("Copilot saved report differs from fresh evidence replay")
        batch, _ = load_batch(self.settings, child)
        experiment_root = checked_root(self.settings, "experiments") / child
        evidence_roots: list[tuple[str, Path]] = [
            ("experiment", experiment_root), ("report", report_root),
            ("request", request_root),
        ]
        for attempt in batch.attempts:
            run = self.settings.runs_dir / attempt.run_id
            if run.exists() or run.is_symlink():
                evidence_roots.append(("run", run))
        return CopilotAuditExecution(
            executed=True,
            source=source,
            parent_experiment_id=lineage.get("parent_experiment_id"),
            child_experiment_id=child,
            request_sha256=request.request_sha256,
            review_sha256=review_sha,
            lineage_sha256=recorded_lineage,
            report_sha256=report_sha,
            batch_status=fresh.batch.status,
            calls_invoked=fresh.batch.calls_invoked,
            verified_candidates=sum(item.verified_candidates for item in fresh.summaries),
        ), evidence_roots

    def _execution_reviews(
        self, root: Path
    ) -> dict[str, ExperimentPlanExecutionRequest | NextExperimentExecutionRequest]:
        reviews = {}
        for path in sorted(root.glob("turn-*-execution-review.json")):
            value = self._object(root, path)
            recorded = value.get("review_sha256")
            base = {key: item for key, item in value.items() if key != "review_sha256"}
            if value.get("format") != "copilot_execution_review_v1" or recorded != fingerprint(base):
                raise ValueError("Copilot execution review fingerprint is invalid")
            request_payload = value.get("request")
            if not isinstance(request_payload, dict):
                raise ValueError("Copilot execution review lacks a request")
            format_name = request_payload.get("format")
            model = (ExperimentPlanExecutionRequest if format_name == "language_experiment_execution_request_v1"
                     else NextExperimentExecutionRequest if format_name == "next_experiment_execution_request_v1"
                     else None)
            if model is None:
                raise ValueError("Copilot execution review has an unknown request type")
            request = model.model_validate(request_payload)
            expected = fingerprint(request.model_dump(mode="json", exclude={"request_sha256"}))
            if request.request_sha256 != expected:
                raise ValueError("Copilot execution request fingerprint is invalid")
            reviews[recorded] = request
        return reviews

    def _verify_request_folder(
        self,
        root: Path,
        request: ExperimentPlanExecutionRequest | NextExperimentExecutionRequest,
    ) -> None:
        if not root.is_dir() or root.is_symlink():
            raise ValueError("Copilot confirmed request evidence is missing")
        saved = type(request).model_validate(self._object(root, root / "request.json"))
        if saved != request:
            raise ValueError("Copilot confirmed request differs from its review")
        state = self._object(root, root / "state.json")
        if state.get("status") != "reported" or state.get("request_sha256") != request.request_sha256:
            raise ValueError("Copilot confirmed request did not reach the reported state")

    def _verify_presented_evidence(
        self, root: Path, execution: CopilotAuditExecution
    ) -> None:
        """Bind every model-visible evidence payload to a replayed local artifact."""
        known: set[str] = set()

        def remember(kind: str, payload: dict) -> None:
            known.add(fingerprint({"kind": kind, "payload": payload}))

        for path in sorted(root.glob("turn-*-plan-draft.json")):
            payload = ExperimentPlanDraft.model_validate(
                self._object(root, path)
            ).model_dump(mode="json")
            remember("plan_draft", payload)
        for path in sorted(root.glob("turn-*-explanation.json")):
            payload = DecisionExplanation.model_validate(
                self._object(root, path)
            ).model_dump(mode="json")
            remember("decision_explanation", payload)
        for path in sorted(root.glob("turn-*-next-plan.json")):
            payload = NextExperimentProposal.model_validate(
                self._object(root, path)
            ).model_dump(mode="json")
            remember("next_experiment_proposal", payload)
        for path in sorted(root.glob("turn-*-execution-review.json")):
            saved = self._object(root, path)
            payload = {
                key: value for key, value in saved.items()
                if key != "review_sha256"
            }
            remember("execution_review", payload)

        if execution.executed:
            assert execution.child_experiment_id is not None
            report = ExperimentReportService(self.settings).analyze(
                execution.child_experiment_id
            )
            remember("execution_report", {
                "experiment_id": report.experiment_id,
                "source_report_sha256": report_fingerprint(report),
                "batch_status": report.batch.status,
                "calls_invoked": report.batch.calls_invoked,
                "summaries": [
                    item.model_dump(mode="json") for item in report.summaries
                ],
                "comparisons": [
                    item.model_dump(mode="json") for item in report.comparisons
                ],
                "warnings": list(report.warnings),
            })
            explanation = DecisionExplanationService(self.settings).explain(
                execution.child_experiment_id
            )
            remember("decision_explanation", explanation.model_dump(mode="json"))

        evidence_by_id: dict[str, str] = {}
        for path in self._indexed(root, "model-input").values():
            request = CopilotModelInput.model_validate(self._object(root, path))
            numbers = [int(item.evidence_id.removeprefix("evidence-"))
                       for item in request.evidence]
            if numbers != sorted(numbers) or len(numbers) != len(set(numbers)):
                raise ValueError("Copilot model input has unordered or repeated evidence IDs")
            for item in request.evidence:
                payload_key = fingerprint({
                    "kind": item.kind,
                    "payload": item.payload,
                })
                if payload_key not in known:
                    raise ValueError(
                        "Copilot model input evidence differs from local tool artifacts"
                    )
                previous = evidence_by_id.setdefault(item.evidence_id, payload_key)
                if previous != payload_key:
                    raise ValueError("Copilot evidence ID was rebound to another payload")

    def _indexed(self, root: Path, suffix: str) -> dict[int, Path]:
        result = {}
        pattern = re.compile(rf"turn-(\d{{3}})-{re.escape(suffix)}\.json")
        for path in root.glob(f"turn-*-{suffix}.json"):
            match = pattern.fullmatch(path.name)
            if match is None:
                raise ValueError("Copilot turn artifact has an invalid name")
            index = int(match.group(1))
            if index in result:
                raise ValueError("Copilot turn artifact index is repeated")
            result[index] = path
        return dict(sorted(result.items()))

    def _object(self, root: Path, path: Path) -> dict:
        value = strict_json(self._read(root, path, max_bytes=MAX_JSON_BYTES))
        if not isinstance(value, dict):
            raise ValueError(f"Copilot evidence must be a JSON object: {path.name}")
        return value

    @staticmethod
    def _read(root: Path, path: Path, *, max_bytes: int) -> bytes:
        data = read_local(root, path)
        if len(data) > max_bytes:
            raise ValueError(f"Copilot evidence exceeds size limit: {path.name}")
        return data

    def _manifest(self, roots: list[tuple[str, Path]]) -> tuple[CopilotAuditFile, ...]:
        files = []
        seen = set()
        runs = self.settings.runs_dir.resolve()
        for scope, root in roots:
            if not root.is_dir() or root.is_symlink() or not root.resolve().is_relative_to(runs):
                raise ValueError("Copilot audit evidence root is invalid")
            for path in sorted(root.rglob("*")):
                if path.is_symlink():
                    raise ValueError("Copilot audit evidence cannot use symlinks")
                if not path.is_file():
                    continue
                resolved = path.resolve()
                if not resolved.is_relative_to(root.resolve()):
                    raise ValueError("Copilot audit evidence escapes its root")
                relative = str(path.relative_to(self.settings.runs_dir))
                if relative in seen:
                    continue
                seen.add(relative)
                digest = hashlib.sha256()
                size = 0
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                files.append(CopilotAuditFile(
                    scope=scope, path=relative, sha256=digest.hexdigest(), size_bytes=size
                ))
                if len(files) > MAX_FILES:
                    raise ValueError("Copilot audit evidence contains too many files")
        return tuple(sorted(files, key=lambda item: item.path))


def render_markdown(report: CopilotAuditReport) -> str:
    usage = report.model_usage
    execution = report.execution
    lines = [
        f"# Copilot 会话审计：{report.session_id}", "",
        f"审计状态：**{report.audit_status}**；会话状态：`{report.session_status}`。", "",
        "## 模型调用", "",
        f"预留/观测/成功/失败：{usage.calls_reserved}/{usage.calls_observed}/"
        f"{usage.calls_succeeded}/{usage.calls_failed}。",
        f"预留 {usage.reserved_hkd} HKD；已返回 usage 的估算合计 {usage.estimated_usage_hkd} HKD。",
        f"本地接受的动作：{', '.join(report.decisions) or '无'}。", "",
        "## 执行与独立复核", "",
    ]
    if execution.executed:
        lines += [
            f"来源：`{execution.source}`；子实验：`{execution.child_experiment_id}`；"
            f"父实验：`{execution.parent_experiment_id or '无'}`。",
            f"实际调用 {execution.calls_invoked} 次；独立复核候选 {execution.verified_candidates} 个；"
            f"批次状态 `{execution.batch_status}`。",
            f"请求指纹 `{execution.request_sha256}`；审阅指纹 `{execution.review_sha256}`；"
            f"报告指纹 `{execution.report_sha256}`。", "",
        ]
    else:
        lines += ["没有保存的确认后 solver 执行谱系。", ""]
    lines += [
        "## 证据清单", "",
        f"共 {len(report.files)} 个文件；清单指纹 `{report.manifest_sha256}`。", "",
        "| 范围 | 路径 | 字节 | SHA-256 |",
        "| --- | --- | ---: | --- |",
    ]
    lines.extend(
        f"| {item.scope} | `{item.path}` | {item.size_bytes} | `{item.sha256}` |"
        for item in report.files
    )
    lines += ["", "## 限制", ""]
    lines.extend(f"- {warning}" for warning in report.warnings)
    return "\n".join(lines) + "\n"
