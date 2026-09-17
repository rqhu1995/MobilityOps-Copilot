"""Interactive review and one-shot execution for finite experiment plans."""

from collections.abc import Callable
from datetime import datetime, timezone
import json

from pydantic import ValidationError

from mobilityops.config import Settings
from mobilityops.domain import BackendName, ScenarioSpec
from mobilityops.domain.experiment_intake import ExperimentLanguageInput, ExperimentPlanDraft
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.experiment_intake import (
    ExperimentInterpreter, ExperimentPlanIntakeService, ExperimentPlanPrecheckError,
    validate_experiment_language_input,
)
from mobilityops.services.experiment_service import checked_root, write_json
from mobilityops.services.gemini_intake import (
    GeminiBudgetConfig, GeminiCallError, GeminiExperimentInterpreter,
)
from mobilityops.services.gurobi_inputs import read_local, strict_json
from mobilityops.services.scenario_intake import LABELS, fingerprint
from mobilityops.services.solver_service import SolverService
from mobilityops.services.terminal_session import TerminalIO, terminal_text


HELP = """直接输入完整实验需求或补充说明，每次提取消耗一次已确认的 LLM 额度。
/review 查看当前完整计划、来源、预算与能力选择；/run 执行全计划预检查并审阅确认。
/withdraw 编号 明确撤回说明：撤回列出的未支持要求，消耗一次提取。
/help 查看帮助；/quit 退出并保存。自然语言中的“执行”只参与解释。
每个计划最多执行一次；串行运行，不重试、不补跑，退出后不自动恢复。"""


class ExperimentTerminalSession:
    def __init__(self, solver: SolverService, *, io: TerminalIO, session_id: str,
                 budget: GeminiBudgetConfig, context: ScenarioSpec | None = None,
                 backend: BackendName | None = None,
                 interpreter_factory: Callable[[str], ExperimentInterpreter] | None = None) -> None:
        validate_run_id(session_id)
        if len(session_id) > 80:
            raise ValueError("Session ID must have at most 80 characters")
        self.solver = solver
        self.settings: Settings = solver.settings
        self.io = io
        self.session_id = session_id
        self.budget = budget
        self.context = context
        self.backend = backend
        self.budget_id = f"terminal-experiment-{session_id}"
        self.experiment_id = f"terminal-experiment-{session_id}"
        self.report_id = self.experiment_id
        self.folder = checked_root(self.settings, "terminal-sessions") / session_id
        self.budget_folder = checked_root(self.settings, "llm") / self.budget_id
        self.interpreter_factory = interpreter_factory or (
            lambda budget_id: GeminiExperimentInterpreter(self.settings, budget_id=budget_id)
        )
        self.draft: ExperimentPlanDraft | None = None
        self.intake: ExperimentPlanIntakeService | None = None
        self.turn = 0
        self.review_number = 0
        self.phase = "startup"
        self.state: dict = {"format": "experiment_terminal_session_v1",
                            "session_id": session_id, "mode": "experiment"}

    def say(self, text: str) -> None:
        self.io.write(terminal_text(text))

    def checkpoint(self, status: str, **details: object) -> None:
        self.state.update(status=status, phase=self.phase, turn=self.turn, **details)
        write_json(self.folder / "state.json", self.state)
        with (self.folder / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), **self.state},
                                    ensure_ascii=False, allow_nan=False) + "\n")

    def finish(self, status: str, code: int, **details: object) -> int:
        self.checkpoint(status, **details)
        self.say(f"会话状态：{status}；证据：{self.folder}")
        return code

    def run(self) -> int:
        if self.budget_folder.exists() or self.budget_folder.is_symlink():
            raise ValueError("LLM budget ID already exists; choose a new session ID")
        self.folder.parent.mkdir(parents=True, exist_ok=True)
        self.folder.mkdir(mode=0o700)
        try:
            write_json(self.folder / "session.json", {
                "session_id": self.session_id, "mode": "experiment",
                "budget_id": self.budget_id, "budget": self.budget.model_dump(mode="json"),
                "context": self.context.model_dump(mode="json") if self.context else None,
                "backend": self.backend.value if self.backend else None,
                "experiment_id": self.experiment_id, "report_id": self.report_id,
                "execution_configurations": {
                    backend.value: self.solver.execution_configuration(backend)
                    for backend in self.solver.registered_backends
                },
            })
            self.phase = "llm_consent"
            self.checkpoint("awaiting_llm_consent")
            self.say(f"步骤 1/4：确认本会话 LLM 预算。会话 {self.session_id}")
            self.say(f"Google {self.budget.model}；最多 {self.budget.max_calls} 次计划提取；"
                     f"费用上限 {self.budget.budget_hkd:g} HKD；每次预留 "
                     f"{self.budget.reservation_hkd} HKD。\n"
                     "LLM 预算只控制计划提取；solver 的调用数和等待秒数由计划另行约束。"
                     "失败保留预留，不自动重试或续费。需求、基准和澄清原文会发送给 Google。")
            if self.io.read('输入“同意”启用本会话计划提取；其他输入退出：').strip() != "同意":
                return self.finish("cancelled", 0)
            self.checkpoint("llm_authorized")
            GeminiExperimentInterpreter.create_budget(
                self.settings, budget_id=self.budget_id, config=self.budget
            )
            self.intake = ExperimentPlanIntakeService(
                self.solver, interpreter=self.interpreter_factory(self.budget_id)
            )
            self.say("步骤 2/4：输入有限实验需求。" +
                     ("已载入显式 ScenarioSpec 基准。" if self.context else
                      "未载入基准，完整基准字段会逐项澄清。"))
            self.say("已注册 backend：" + "、".join(
                backend.value for backend in self.solver.registered_backends
            ) + "。注册不代表运行环境或许可证已检查。")
            self.say(HELP)
            self.phase = "conversation"
            self.checkpoint("awaiting_input")
            while True:
                text = self.io.read("实验需求或命令 > ").strip()
                if text == "/quit":
                    return self.finish("cancelled", 0)
                if text == "/help":
                    self.say(HELP)
                elif text == "/review":
                    self.show_draft()
                elif text == "/run":
                    result = self.review_and_execute()
                    if result is not None:
                        return result
                elif text.startswith("/withdraw "):
                    self.withdraw(text)
                elif text.startswith("/"):
                    self.say("未知命令。输入 /help 查看可用命令。")
                elif text:
                    self.interpret(text)
                else:
                    self.say("请输入实验需求或 /quit。空输入不调用模型。")
        except (EOFError, KeyboardInterrupt) as exc:
            self.say("输入或执行已中断；保留当前证据，不自动继续、重试或补跑。")
            return self.finish("interrupted", 130 if isinstance(exc, KeyboardInterrupt) else 0)
        except Exception as exc:
            diagnostic = exc.diagnostic if isinstance(exc, GeminiCallError) else None
            self.say(f"{self.phase} 阶段停止：{type(exc).__name__}。证据已保留。")
            if isinstance(exc, GeminiCallError):
                self.say(str(exc))
            if diagnostic and diagnostic.get("safe_message"):
                self.say(str(diagnostic["safe_message"]))
            return self.finish("failed", 1, error_type=type(exc).__name__,
                               diagnostic=diagnostic,
                               reason=str(exc) if isinstance(exc, GeminiCallError) else None)

    def show_draft(self) -> None:
        draft = self.draft
        if draft is None:
            self.say("尚无当前实验计划，请先输入需求。")
            return
        self.say(f"计划版本 {self.turn}：{draft.status}")
        plan = draft.plan
        if plan is not None:
            required_wait = sum(case.external_timeout_sec * case.repetitions
                                for case in plan.cases)
            self.say(f"solver 预算：计划 {plan.planned_calls} 次 / 上限 {plan.max_calls} 次；"
                     f"需要 {required_wait:g} 秒 / 总等待预算 {plan.external_wait_budget_sec:g} 秒；"
                     f"失败策略 {plan.failure_policy}。")
            baseline = plan.baseline.scenario.model_dump(mode="json")
            for case in plan.cases:
                values = case.scenario.model_dump(mode="json")
                changes = [f"{LABELS.get(field, field)}：{baseline[field]} → {value}"
                           for field, value in values.items()
                           if field != "scenario_name" and baseline.get(field) != value]
                self.say(f"  {case.case_id}：backend={case.backend.value if case.backend else '唯一兼容'}；"
                         f"重复 {case.repetitions}；内部/外部 {case.scenario.solver_runtime_limit_sec:g}/"
                         f"{case.external_timeout_sec:g} 秒；相对基准："
                         f"{'；'.join(changes) if changes else '无'}")
                self.say("    ScenarioSpec=" + json.dumps(values, ensure_ascii=False, sort_keys=True))
                decision = draft.decisions.get(case.case_id)
                if decision:
                    self.say(f"    能力选择={decision.selected_backend.value if decision.selected_backend else '无'}；"
                             f"{decision.reason}")
        for question in draft.clarifications:
            self.say(f"待澄清 [{question.kind}] {question.field or '要求'}：{question.question}")
        for index, issue in enumerate(draft.extraction.issues, 1):
            self.say(f"可明确撤回的要求 {index}：{issue.evidence}；使用 /withdraw {index} 撤回说明")
        if draft.status == "ready":
            self.say("步骤 3/4：计划字段与能力检查已通过。输入 /run 进行全计划运行预检查并审阅确认。")

    def interpret(self, text: str, *, withdraw_issues: tuple[int, ...] = ()) -> None:
        assert self.intake is not None
        previous = self.draft
        try:
            validate_experiment_language_input(ExperimentLanguageInput(
                messages=(*(previous.input.messages if previous else ()), text),
                context=self.context, context_backend=self.backend,
            ))
        except ValueError:
            self.say("输入未提交：每条须为 1–4000 字符，最多 20 条、合计 20000 字符。")
            return
        self.turn += 1
        self.phase = "interpreting"
        self.draft = None
        write_json(self.folder / f"turn-{self.turn:03d}-input.json", {
            "text": text, "withdraw_issues": withdraw_issues,
            "previous_draft_sha256": previous.draft_sha256 if previous else None,
        })
        self.checkpoint("interpreting", draft_sha256=None, request_sha256=None,
                        review_sha256=None, diagnostic=None)
        self.say("正在提取并由本地契约重建完整计划；本步骤不会运行 solver 或许可证检查。")
        draft = (self.intake.revise(previous, text, withdraw_issues=withdraw_issues)
                 if previous else self.intake.propose(text, context=self.context,
                                                      backend=self.backend))
        write_json(self.folder / f"turn-{self.turn:03d}-draft.json",
                   draft.model_dump(mode="json"))
        self.draft = draft
        self.phase = "conversation"
        self.checkpoint(draft.status, draft_sha256=draft.draft_sha256)
        ledger = strict_json(read_local(self.budget_folder,
                                        self.budget_folder / "ledger.json"))
        self.say(f"LLM 预算已预留 {ledger['calls_reserved']}/{self.budget.max_calls} 次，"
                 f"{ledger['reserved_hkd']}/{self.budget.budget_hkd:g} HKD；"
                 "与 solver 次数/等待预算分开。")
        self.show_draft()

    def withdraw(self, command: str) -> None:
        parts = command.split(maxsplit=2)
        try:
            index = int(parts[1]) - 1
            if len(parts) != 3 or not parts[2].strip() or self.draft is None:
                raise ValueError
            if not 0 <= index < len(self.draft.extraction.issues):
                raise ValueError
        except (ValueError, IndexError):
            self.say("撤回未提交。格式：/withdraw 编号 明确撤回说明。")
            return
        issue = self.draft.extraction.issues[index]
        self.interpret(f"明确撤回此前要求「{issue.evidence}」。说明：{parts[2]}",
                       withdraw_issues=(index,))

    def review_and_execute(self) -> int | None:
        draft = self.draft
        assert self.intake is not None
        if draft is None or draft.status != "ready" or draft.plan is None:
            self.say("无法执行：请先解决计划缺失、冲突、未支持要求或 backend 不兼容。")
            return None
        self.phase = "review"
        self.show_draft()
        self.review_number += 1
        prefix = self.folder / f"review-{self.review_number:03d}"
        try:
            request = self.intake.prepare_execution(
                draft, experiment_id=self.experiment_id, report_id=self.report_id
            )
        except ExperimentPlanPrecheckError as exc:
            write_json(prefix.with_name(prefix.name + "-precheck.json"),
                       exc.precheck.model_dump(mode="json"))
            for case in exc.precheck.cases:
                for error in case.errors:
                    self.say(f"预检查未通过 [{case.case_id}]：{error}")
            self.checkpoint("precheck_rejected", request_sha256=None,
                            review_sha256=None)
            self.phase = "conversation"
            self.say("全部场景通过前不会启动第一个 solver；修复配置或计划后重新 /run。")
            return None
        except (ValidationError, ValueError) as exc:
            diagnostic = ("；".join(error["msg"] for error in
                                    exc.errors(include_input=False, include_url=False))
                          if isinstance(exc, ValidationError) else str(exc))
            self.say(f"执行请求未生成：{diagnostic}")
            self.checkpoint("request_rejected", diagnostic=diagnostic,
                            request_sha256=None, review_sha256=None)
            self.phase = "conversation"
            return None
        write_json(prefix.with_name(prefix.name + "-request.json"),
                   request.model_dump(mode="json"))
        review = {
            "format": "terminal_experiment_execution_review_v1",
            "request": request.model_dump(mode="json"),
            "llm_budget": self.budget.model_dump(mode="json"),
        }
        review_sha256 = fingerprint(review)
        write_json(prefix.with_name(prefix.name + "-execution.json"),
                   {**review, "review_sha256": review_sha256})
        write_json(prefix.with_name(prefix.name + "-precheck.json"),
                   request.precheck.model_dump(mode="json"))
        self.say(f"全计划预检查：{len(request.precheck.cases)} 个场景全部通过；"
                 "未启动 solver、模型调用或许可证环境。")
        self.say(f"solver 预算：{request.plan.planned_calls} 次调用；最大 "
                 f"{request.plan.max_calls} 次；总外部等待预算 "
                 f"{request.plan.external_wait_budget_sec:g} 秒；失败策略 "
                 f"{request.plan.failure_policy}；串行执行，不重试、不补跑。")
        for case in request.plan.cases:
            self.say(f"  {case.case_id}：{case.backend.value if case.backend else '唯一兼容 backend'} × "
                     f"{case.repetitions}；内部/外部 {case.scenario.solver_runtime_limit_sec:g}/"
                     f"{case.external_timeout_sec:g} 秒。")
        self.say("backend 运行配置=" + json.dumps(
            request.execution_configurations, ensure_ascii=False, sort_keys=True
        ))
        self.say(f"experiment ID={request.experiment_id}；report ID={request.report_id}\n"
                 f"请求指纹={request.request_sha256}\n审阅指纹={review_sha256}\n"
                 "LLM 预算已用于提取，不会因 solver 确认而增加；solver 预算如上。")
        self.checkpoint("awaiting_execution_confirmation",
                        request_sha256=request.request_sha256,
                        review_sha256=review_sha256)
        confirmation = f"执行实验 {review_sha256[:12]}"
        answer = self.io.read(f'输入“{confirmation}”执行当前完整计划；其他输入取消：').strip()
        if answer != confirmation:
            self.checkpoint("execution_cancelled", request_sha256=None, review_sha256=None)
            self.phase = "conversation"
            if answer == "/quit":
                return self.finish("cancelled", 0)
            self.say("本次执行已取消；计划修改后须重新预检查、审阅和确认。")
            return None
        try:
            fresh = self.intake.prepare_execution(
                draft, experiment_id=self.experiment_id, report_id=self.report_id
            )
        except (ValueError, OSError):
            fresh = None
        if fresh != request:
            self.checkpoint("review_changed", request_sha256=None, review_sha256=None)
            self.phase = "conversation"
            self.say("计划、预检查依据或运行配置已变化，旧确认失效；未调用 solver。请重新 /run。")
            return None
        self.phase = "executing"
        self.checkpoint("execution_confirmed",
                        confirmed_request_sha256=request.request_sha256,
                        confirmed_review_sha256=review_sha256)
        self.say("步骤 4/4：按 case/repetition 顺序串行执行，随后重新复核并生成报告。")
        receipt = self.intake.execute(
            draft, request, confirmed_request_sha256=request.request_sha256
        )
        report_dir = checked_root(self.settings, "experiment-reports") / request.report_id
        for attempt in receipt.batch.attempts:
            self.say(f"运行 {attempt.run_id}：{attempt.status}")
        verified = sum(summary.verified_candidates for summary in receipt.report.summaries)
        self.say(f"实际调用 {receipt.batch.calls_invoked}/{request.plan.max_calls} 次；"
                 f"预留等待 {receipt.batch.reserved_external_wait_sec:g}/"
                 f"{request.plan.external_wait_budget_sec:g} 秒；复核候选 {verified} 个。")
        for group in receipt.report.groups:
            metrics = {metric.metric: metric for metric in group.metrics}
            objective = metrics["objective_value"]
            self.say(f"{group.case_id}：objective n={objective.n}，最小/中位/最大 "
                     f"{objective.minimum:g}/{objective.median:g}/{objective.maximum:g}")
        for comparison in receipt.report.comparisons:
            self.say(f"{comparison.variant_case_id} 相对基准：{comparison.kind}；"
                     f"{'；'.join(comparison.reasons) if comparison.reasons else '仅描述中位数观测差'}")
        self.say(f"Markdown 报告：{report_dir / 'report.md'}\n"
                 f"JSON 报告：{report_dir / 'report.json'}")
        self.phase = "reporting"
        successful = (verified == request.plan.planned_calls
                      and receipt.batch.status == "completed")
        return self.finish(
            "reported", 0 if successful else 1,
            batch_status=receipt.batch.status, verified_candidates=verified,
            calls_invoked=receipt.batch.calls_invoked,
            report_markdown=str(report_dir / "report.md"),
            report_json=str(report_dir / "report.json"),
        )
