"""Unified Gemini-guided conversation over deterministic MobilityOps services."""

from collections.abc import Callable
from datetime import datetime, timezone
import json
from pathlib import Path

from pydantic import ValidationError

from mobilityops.config import Settings
from mobilityops.domain import BackendName, ScenarioSpec
from mobilityops.domain.copilot import (
    CopilotAction,
    CopilotEvidenceItem,
    CopilotModelInput,
    CopilotStateView,
)
from mobilityops.domain.experiment_intake import (
    ExperimentPlanDraft,
    ExperimentPlanExecutionRequest,
)
from mobilityops.domain.next_experiment import NextExperimentExecutionRequest
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.copilot import CopilotDecisionService, CopilotInterpreter
from mobilityops.services.decision_explanation import (
    DecisionExplanationService,
    report_fingerprint,
)
from mobilityops.services.experiment_intake import (
    ExperimentInterpreter,
    ExperimentPlanIntakeService,
    ExperimentPlanPrecheckError,
)
from mobilityops.services.experiment_service import checked_root, write_json
from mobilityops.services.gemini_intake import (
    GeminiBudgetConfig,
    GeminiCallError,
    GeminiCopilotInterpreter,
    GeminiExperimentInterpreter,
    GeminiScenarioInterpreter,
)
from mobilityops.services.gurobi_inputs import read_local, strict_json
from mobilityops.services.next_experiment import (
    NextExperimentPrecheckError,
    NextExperimentTakeoverService,
)
from mobilityops.services.scenario_intake import LABELS, fingerprint
from mobilityops.services.solver_service import SolverService
from mobilityops.services.terminal_session import TerminalIO, terminal_text


HELP = """直接描述业务目标、要求解释当前证据、生成下一轮候选或审阅当前计划。
/state 查看当前确定性状态；/help 查看帮助；/quit 退出并保存。
每条自然语言最多触发一个受限路由决策；计划字段提取可能再消耗一次 Gemini 调用。
Gemini 不能调用 solver 或确认执行；只有本地一次性确认短语可以启动当前完整计划。"""


ExecutionRequest = ExperimentPlanExecutionRequest | NextExperimentExecutionRequest


class CopilotTerminalSession:
    def __init__(
        self,
        solver: SolverService,
        *,
        io: TerminalIO,
        session_id: str,
        budget: GeminiBudgetConfig,
        context: ScenarioSpec | None = None,
        backend: BackendName | None = None,
        experiment_id: str | None = None,
        router_factory: Callable[[str], CopilotInterpreter] | None = None,
        experiment_interpreter_factory: Callable[[str], ExperimentInterpreter] | None = None,
    ) -> None:
        validate_run_id(session_id)
        if len(session_id) > 80:
            raise ValueError("Session ID must have at most 80 characters")
        if experiment_id is not None:
            validate_run_id(experiment_id)
        self.solver = solver
        self.settings: Settings = solver.settings
        self.io = io
        self.session_id = session_id
        self.budget = budget
        self.context = context
        self.backend = backend
        self.current_experiment_id = experiment_id
        self.output_experiment_id = f"copilot-{session_id}"
        self.report_id = self.output_experiment_id
        self.budget_id = f"copilot-{session_id}"
        self.folder = checked_root(self.settings, "copilot-sessions") / session_id
        self.budget_folder = checked_root(self.settings, "llm") / self.budget_id
        self.router_factory = router_factory or (
            lambda budget_id: GeminiCopilotInterpreter(
                self.settings, budget_id=budget_id
            )
        )
        self.experiment_interpreter_factory = experiment_interpreter_factory or (
            lambda budget_id: GeminiExperimentInterpreter(
                self.settings, budget_id=budget_id
            )
        )
        self.messages: list[str] = []
        self.evidence: list[CopilotEvidenceItem] = []
        self.evidence_number = 0
        self.turn = 0
        self.phase = "startup"
        self.draft: ExperimentPlanDraft | None = None
        self.proposal_path: Path | None = None
        self.proposal_has_plan = False
        self.active_plan_source: str | None = None
        self.selected_variant_case_id: str | None = None
        self.execution_review_ready = False
        self.execution_completed = False
        self.intake: ExperimentPlanIntakeService | None = None
        self.decisions: CopilotDecisionService | None = None
        self.takeover = NextExperimentTakeoverService(solver)
        self.state: dict = {
            "format": "copilot_terminal_session_v1",
            "session_id": session_id,
            "mode": "copilot",
        }

    def say(self, value: str) -> None:
        self.io.write(terminal_text(value))

    def checkpoint(self, status: str, **details: object) -> None:
        self.state.update(
            status=status,
            phase=self.phase,
            turn=self.turn,
            current_experiment_id=self.current_experiment_id,
            selected_variant_case_id=self.selected_variant_case_id,
            active_plan_source=self.active_plan_source,
            execution_review_ready=self.execution_review_ready,
            execution_completed=self.execution_completed,
            evidence_ids=[item.evidence_id for item in self.evidence],
            **details,
        )
        write_json(self.folder / "state.json", self.state)
        with (self.folder / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {"at": datetime.now(timezone.utc).isoformat(), **self.state},
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            )

    def finish(self, status: str, code: int, **details: object) -> int:
        self.checkpoint(status, **details)
        self.say(f"会话状态：{status}；证据：{self.folder}")
        return code

    def run(self) -> int:
        if self.budget_folder.exists() or self.budget_folder.is_symlink():
            raise ValueError("Copilot LLM budget ID already exists; choose a new session ID")
        if self.folder.exists() or self.folder.is_symlink():
            raise ValueError("Copilot session already exists; choose a new session ID")
        self.folder.parent.mkdir(parents=True, exist_ok=True)
        self.folder.mkdir(mode=0o700)
        try:
            write_json(
                self.folder / "session.json",
                {
                    "format": "copilot_terminal_session_v1",
                    "session_id": self.session_id,
                    "budget_id": self.budget_id,
                    "budget": self.budget.model_dump(mode="json"),
                    "context": self.context.model_dump(mode="json") if self.context else None,
                    "backend": self.backend.value if self.backend else None,
                    "initial_experiment_id": self.current_experiment_id,
                    "output_experiment_id": self.output_experiment_id,
                    "execution_configurations": {
                        backend.value: self.solver.execution_configuration(backend)
                        for backend in self.solver.registered_backends
                    },
                },
            )
            self.phase = "llm_consent"
            self.checkpoint("awaiting_llm_consent")
            self.say(f"步骤 1/3：确认 Gemini Decision Copilot 预算。会话 {self.session_id}")
            self.say(
                f"Google {self.budget.model}；最多 {self.budget.max_calls} 次 interactions；"
                f"费用上限 {self.budget.budget_hkd:g} HKD；每次保守预留 "
                f"{self.budget.reservation_hkd} HKD。失败保留预留，不自动重试或续费。\n"
                "需求、当前状态和明确列出的证据会发送给 Google；凭据不会写入产物。"
                "Gemini 只能选择有限本地工具，不能调用 solver 或确认执行。"
            )
            if self.io.read('输入“同意”启用本会话 Copilot；其他输入退出：').strip() != "同意":
                return self.finish("cancelled", 0)
            GeminiScenarioInterpreter.create_budget(
                self.settings, budget_id=self.budget_id, config=self.budget
            )
            self.decisions = CopilotDecisionService(
                self.router_factory(self.budget_id)
            )
            self.intake = ExperimentPlanIntakeService(
                self.solver,
                interpreter=self.experiment_interpreter_factory(self.budget_id),
            )
            self.phase = "conversation"
            self.checkpoint("awaiting_input")
            self.say(
                "步骤 2/3：描述目标或询问现有证据。"
                + (f"已载入实验 {self.current_experiment_id}。" if self.current_experiment_id else "")
            )
            self.say(HELP)
            while True:
                text = self.io.read("Copilot > ").strip()
                if text == "/quit":
                    return self.finish("completed", 0)
                if text == "/help":
                    self.say(HELP)
                    continue
                if text == "/state":
                    self.show_state()
                    continue
                if text.startswith("/"):
                    self.say("未知命令。输入 /help 查看可用命令。")
                    continue
                if not text:
                    self.say("请输入业务目标、证据问题或 /quit。空输入不调用 Gemini。")
                    continue
                self.handle_message(text)
        except (EOFError, KeyboardInterrupt) as exc:
            self.say("Copilot 已中断；保留证据，不自动重试、恢复或执行。")
            return self.finish(
                "interrupted", 130 if isinstance(exc, KeyboardInterrupt) else 0
            )
        except Exception as exc:
            diagnostic = exc.diagnostic if isinstance(exc, GeminiCallError) else None
            self.say(f"{self.phase} 阶段停止：{type(exc).__name__}。证据已保留。")
            if isinstance(exc, GeminiCallError):
                self.say(str(exc))
            if diagnostic and diagnostic.get("safe_message"):
                self.say(str(diagnostic["safe_message"]))
            return self.finish(
                "failed",
                1,
                error_type=type(exc).__name__,
                diagnostic=diagnostic,
                reason=str(exc),
            )

    def handle_message(self, text: str) -> None:
        assert self.decisions is not None and self.intake is not None
        candidate_messages = (*self.messages, text)
        if (len(candidate_messages) > 20 or len(text) > 4000
                or sum(map(len, candidate_messages)) > 20000):
            self.say("输入未提交：每条 1–4000 字符，最多 20 条、合计 20000 字符。")
            return
        self.messages.append(text)
        self.turn += 1
        request = self._model_input()
        write_json(
            self.folder / f"turn-{self.turn:03d}-model-input.json",
            request.model_dump(mode="json"),
        )
        self.phase = "routing"
        self.checkpoint("routing")
        decision = self.decisions.decide(request)
        write_json(
            self.folder / f"turn-{self.turn:03d}-decision.json",
            decision.model_dump(mode="json"),
        )
        self.say(f"Gemini 选择受控动作：{decision.action}")
        self.phase = "tool"
        if decision.action == "draft_experiment":
            self._draft_experiment(text)
        elif decision.action == "explain_experiment":
            self._explain(decision.variant_case_id)
        elif decision.action == "propose_next_experiment":
            assert decision.variant_case_id is not None
            self._propose_next(decision.variant_case_id)
        elif decision.action == "prepare_execution_review":
            self._prepare_and_maybe_execute()
        elif decision.action == "answer":
            citations = "、".join(decision.evidence_ids) or "无"
            self.say(f"{decision.response}\n证据：{citations}")
        elif decision.action == "clarify":
            self.say(decision.response)
        self.phase = "conversation"
        self.checkpoint("awaiting_input")

    def _model_input(self) -> CopilotModelInput:
        return CopilotModelInput(
            messages=tuple(self.messages),
            state=self._state_view(),
            available_actions=self._available_actions(),
            evidence=tuple(self.evidence),
        )

    def _available_actions(self) -> tuple[CopilotAction, ...]:
        actions: list[CopilotAction] = []
        if self._remaining_model_calls() >= 2:
            actions.append("draft_experiment")
        if self.current_experiment_id is not None:
            actions.extend(["explain_experiment", "propose_next_experiment"])
        if (not self.execution_completed and (
                self.draft is not None and self.draft.status == "ready"
                or self.proposal_path is not None and self.proposal_has_plan)):
            actions.append("prepare_execution_review")
        actions.extend(["answer", "clarify"])
        return tuple(actions)

    def _state_view(self) -> CopilotStateView:
        plan = self.draft.plan if self.draft is not None else None
        if self.proposal_path is not None:
            payload = strict_json(read_local(self.folder, self.proposal_path))
            proposal_plan = payload.get("plan") if isinstance(payload, dict) else None
        else:
            proposal_plan = None
        plan_summary = None
        source = plan.model_dump(mode="json") if plan is not None else proposal_plan
        if isinstance(source, dict):
            cases = [source["baseline"], *source.get("variants", [])]
            plan_summary = {
                "case_ids": [case["case_id"] for case in cases],
                "planned_calls": sum(case["repetitions"] for case in cases),
                "max_calls": source["max_calls"],
                "external_wait_budget_sec": source["external_wait_budget_sec"],
                "backends": [case.get("backend") for case in cases],
            }
        return CopilotStateView(
            remaining_model_calls=self._remaining_model_calls(),
            draft_status=self.draft.status if self.draft else None,
            draft_sha256=self.draft.draft_sha256 if self.draft else None,
            has_complete_plan=source is not None,
            current_experiment_id=self.current_experiment_id,
            selected_variant_case_id=self.selected_variant_case_id,
            proposal_ready=self.proposal_path is not None and self.proposal_has_plan,
            execution_review_ready=self.execution_review_ready,
            execution_completed=self.execution_completed,
            plan_summary=plan_summary,
            clarifications=tuple(
                question.question for question in self.draft.clarifications
            ) if self.draft else (),
        )

    def _remaining_model_calls(self) -> int:
        ledger = strict_json(
            read_local(self.budget_folder, self.budget_folder / "ledger.json")
        )
        calls = ledger.get("calls_reserved")
        if type(calls) is not int or not 0 <= calls <= self.budget.max_calls:
            raise ValueError("Copilot budget ledger has an invalid call count")
        return self.budget.max_calls - calls

    def _draft_experiment(self, text: str) -> None:
        assert self.intake is not None
        self.say("由实验字段提取器解析本轮文本，再由本地契约重建完整计划。")
        draft = (
            self.intake.revise(self.draft, text)
            if self.draft is not None
            else self.intake.propose(text, context=self.context, backend=self.backend)
        )
        self.draft = draft
        self.proposal_path = None
        self.proposal_has_plan = False
        self.active_plan_source = "draft"
        self.execution_review_ready = False
        path = self.folder / f"turn-{self.turn:03d}-plan-draft.json"
        write_json(path, draft.model_dump(mode="json"))
        self._add_evidence("plan_draft", draft.model_dump(mode="json"))
        self.show_draft()

    def show_draft(self) -> None:
        assert self.draft is not None
        draft = self.draft
        self.say(f"当前计划：{draft.status}；版本={draft.draft_sha256}")
        if draft.plan is not None:
            plan = draft.plan
            self.say(
                f"计划 {plan.planned_calls}/{plan.max_calls} 次调用；等待预算 "
                f"{plan.external_wait_budget_sec:g} 秒；失败策略 {plan.failure_policy}。"
            )
            baseline = plan.baseline.scenario.model_dump(mode="json")
            for case in plan.cases:
                values = case.scenario.model_dump(mode="json")
                changes = [
                    f"{LABELS.get(field, field)}：{baseline[field]} → {value}"
                    for field, value in values.items()
                    if field != "scenario_name" and baseline.get(field) != value
                ]
                self.say(
                    f"  {case.case_id}：backend={case.backend.value if case.backend else '唯一兼容'}；"
                    f"重复 {case.repetitions}；内部/外部 "
                    f"{case.scenario.solver_runtime_limit_sec:g}/{case.external_timeout_sec:g} 秒；"
                    f"变化={'；'.join(changes) if changes else '无'}"
                )
        for question in draft.clarifications:
            self.say(f"待澄清 [{question.kind}]：{question.question}")

    def _explain(self, variant_case_id: str | None) -> None:
        if self.current_experiment_id is None:
            raise ValueError("No current experiment is available for explanation")
        explanation = DecisionExplanationService(self.settings).explain(
            self.current_experiment_id, variant_case_id=variant_case_id
        )
        self.selected_variant_case_id = variant_case_id
        path = self.folder / f"turn-{self.turn:03d}-explanation.json"
        write_json(path, explanation.model_dump(mode="json"))
        evidence = self._add_evidence(
            "decision_explanation", explanation.model_dump(mode="json")
        )
        self.say("已验证事实：")
        for claim in explanation.verified_facts:
            self.say(f"- {claim.text}")
        self.say("本批描述性观察：")
        for claim in explanation.descriptive_observations:
            self.say(f"- {claim.text}")
        self.say("不能得出的结论：")
        for claim in explanation.cannot_conclude:
            self.say(f"- {claim.text}")
        self.say(f"结构化证据：{evidence.evidence_id}；{path}")

    def _propose_next(self, variant_case_id: str) -> None:
        if self.current_experiment_id is None:
            raise ValueError("No current experiment is available for a next proposal")
        proposal = DecisionExplanationService(self.settings).next_plan(
            self.current_experiment_id, variant_case_id=variant_case_id
        )
        path = self.folder / f"turn-{self.turn:03d}-next-plan.json"
        write_json(path, proposal.model_dump(mode="json"))
        self._add_evidence(
            "next_experiment_proposal", proposal.model_dump(mode="json")
        )
        self.selected_variant_case_id = variant_case_id
        self.draft = None
        self.proposal_path = path
        self.proposal_has_plan = proposal.plan is not None
        self.active_plan_source = "proposal"
        self.execution_review_ready = False
        if proposal.plan is None:
            self.say("下一轮候选被阻止：" + "；".join(proposal.blockers))
        else:
            self.say(
                f"下一轮候选：{proposal.plan.planned_calls} 次调用；等待预算 "
                f"{proposal.plan.external_wait_budget_sec:g} 秒；尚未预检查或确认。"
            )

    def _prepare_and_maybe_execute(self) -> None:
        assert self.intake is not None
        try:
            if self.active_plan_source == "draft" and self.draft is not None:
                request: ExecutionRequest = self.intake.prepare_execution(
                    self.draft,
                    experiment_id=self.output_experiment_id,
                    report_id=self.report_id,
                )
                kind = "draft"
            elif self.active_plan_source == "proposal" and self.proposal_path is not None:
                request = self.takeover.prepare_execution(
                    self.proposal_path,
                    experiment_id=self.output_experiment_id,
                    report_id=self.report_id,
                )
                kind = "proposal"
            else:
                raise ValueError("No complete plan is available for execution review")
        except (ExperimentPlanPrecheckError, NextExperimentPrecheckError) as exc:
            for case in exc.precheck.cases:
                for error in case.errors:
                    self.say(f"预检查未通过 [{case.case_id}]：{error}")
            self.say("全部场景通过前不会调用 solver 或许可证环境。")
            self.execution_review_ready = False
            return
        except (ValidationError, ValueError, OSError) as exc:
            self.say(f"执行审阅被本地契约拒绝：{type(exc).__name__}: {exc}")
            self.say("没有调用 solver；修正计划或配置后可重新审阅。")
            self.execution_review_ready = False
            return
        review = {
            "format": "copilot_execution_review_v1",
            "source": kind,
            "request": request.model_dump(mode="json"),
        }
        review_sha256 = fingerprint(review)
        write_json(
            self.folder / f"turn-{self.turn:03d}-execution-review.json",
            {**review, "review_sha256": review_sha256},
        )
        self._add_evidence("execution_review", review)
        self.execution_review_ready = True
        self.say(
            f"全计划预检查通过：{len(request.precheck.cases)} 个场景；"
            f"计划 {request.plan.planned_calls}/{request.plan.max_calls} 次调用；"
            f"需要 {request.precheck.planned_external_wait_sec:g}/"
            f"{request.plan.external_wait_budget_sec:g} 秒等待预算。"
        )
        self.say(
            "backend 运行配置="
            + json.dumps(
                request.execution_configurations,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        self.say(
            f"请求指纹={request.request_sha256}\n审阅指纹={review_sha256}\n"
            "Gemini 的动作或文字不构成执行确认。"
        )
        self.checkpoint(
            "awaiting_execution_confirmation",
            request_sha256=request.request_sha256,
            review_sha256=review_sha256,
        )
        confirmation = f"执行 Copilot 实验 {review_sha256[:12]}"
        answer = self.io.read(
            f'输入“{confirmation}”执行当前完整计划；其他输入取消：'
        ).strip()
        if answer != confirmation:
            self.execution_review_ready = False
            self.say("执行已取消；没有调用 solver。")
            return
        fresh = self._fresh_request(kind)
        if fresh != request:
            self.execution_review_ready = False
            self.say("计划、证据、预检查或运行配置已变化，旧确认失效；未调用 solver。")
            return
        self.phase = "executing"
        self.checkpoint(
            "execution_confirmed",
            confirmed_request_sha256=request.request_sha256,
            confirmed_review_sha256=review_sha256,
        )
        if kind == "draft":
            assert self.draft is not None
            receipt = self.intake.execute(
                self.draft,
                request,
                confirmed_request_sha256=request.request_sha256,
            )
        else:
            assert self.proposal_path is not None
            receipt = self.takeover.execute(
                self.proposal_path,
                request,
                confirmed_request_sha256=request.request_sha256,
            )
        self.current_experiment_id = request.experiment_id
        self.execution_completed = True
        self.execution_review_ready = False
        report = receipt.report
        report_payload = {
            "experiment_id": report.experiment_id,
            "source_report_sha256": report_fingerprint(report),
            "batch_status": report.batch.status,
            "calls_invoked": report.batch.calls_invoked,
            "summaries": [item.model_dump(mode="json") for item in report.summaries],
            "comparisons": [item.model_dump(mode="json") for item in report.comparisons],
            "warnings": list(report.warnings),
        }
        self._add_evidence("execution_report", report_payload)
        explanation = DecisionExplanationService(self.settings).explain(
            request.experiment_id
        )
        self._add_evidence(
            "decision_explanation", explanation.model_dump(mode="json")
        )
        lineage = {
            "format": "copilot_experiment_lineage_v1",
            "session_id": self.session_id,
            "source": kind,
            "parent_experiment_id": (
                request.source_experiment_id
                if isinstance(request, NextExperimentExecutionRequest)
                else None
            ),
            "request_sha256": request.request_sha256,
            "review_sha256": review_sha256,
            "child_experiment_id": request.experiment_id,
            "child_report_sha256": report_fingerprint(report),
        }
        lineage["lineage_sha256"] = fingerprint(lineage)
        write_json(self.folder / "lineage.json", lineage)
        verified = sum(item.verified_candidates for item in report.summaries)
        self.say(
            f"执行完成：实际调用 {report.batch.calls_invoked} 次；"
            f"复核候选 {verified} 个；报告状态 {report.batch.status}。"
        )
        self.say("可以继续询问结果、要求下一轮候选，或 /quit。")

    def _fresh_request(self, kind: str) -> ExecutionRequest | None:
        try:
            if kind == "draft" and self.draft is not None and self.intake is not None:
                return self.intake.prepare_execution(
                    self.draft,
                    experiment_id=self.output_experiment_id,
                    report_id=self.report_id,
                )
            if kind == "proposal" and self.proposal_path is not None:
                return self.takeover.prepare_execution(
                    self.proposal_path,
                    experiment_id=self.output_experiment_id,
                    report_id=self.report_id,
                )
        except (ValidationError, ValueError, OSError):
            return None
        return None

    def _add_evidence(
        self, kind: str, payload: dict
    ) -> CopilotEvidenceItem:
        self.evidence_number += 1
        item = CopilotEvidenceItem(
            evidence_id=f"evidence-{self.evidence_number:03d}",
            kind=kind,
            payload=payload,
        )
        self.evidence.append(item)
        if len(self.evidence) > 20:
            self.evidence = self.evidence[-20:]
        return item

    def show_state(self) -> None:
        view = self._state_view()
        self.say(
            "Copilot 状态="
            + json.dumps(view.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        )
        ledger = strict_json(
            read_local(self.budget_folder, self.budget_folder / "ledger.json")
        )
        self.say(
            f"Gemini 已预留 {ledger['calls_reserved']}/{self.budget.max_calls} 次，"
            f"{ledger['reserved_hkd']}/{self.budget.budget_hkd:g} HKD。"
        )
