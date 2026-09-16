"""A bounded terminal conversation over the existing intake and experiment services."""

from collections.abc import Callable
from datetime import datetime, timezone
import json
from typing import Protocol
import unicodedata

from pydantic import ValidationError

from mobilityops.config import Settings
from mobilityops.domain import BackendName, ScenarioSpec
from mobilityops.domain.intake import LanguageInput, ScenarioDraft
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.experiment_service import ExperimentService, checked_root, write_json
from mobilityops.services.gemini_intake import GeminiBudgetConfig, GeminiCallError, GeminiScenarioInterpreter
from mobilityops.services.gurobi_inputs import read_local, strict_json
from mobilityops.services.scenario_intake import (
    LABELS, ScenarioIntakeService, ScenarioInterpreter, fingerprint, validate_language_input,
)
from mobilityops.services.solver_service import SolverService


class TerminalIO(Protocol):
    def read(self, prompt: str) -> str: ...
    def write(self, text: str) -> None: ...


def terminal_text(text: str) -> str:
    """Render untrusted text without terminal escapes, control codes or bidi overrides."""
    return "".join(f"\\u{ord(char):04x}" if unicodedata.category(char) in {"Cc", "Cf"}
                   and char not in "\n\t" else char for char in text)


HELP = """直接输入需求或补充说明，每次提取消耗一次已确认的 LLM 额度。
/review 查看当前候选及来源；/run 审阅单次执行请求并确认。
/withdraw 编号 明确撤回说明：撤回列出的未支持要求，消耗一次提取。
/help 查看帮助；/quit 退出并保存。自然语言中的“执行”只参与解释。
每会话最多执行一次；失败不重试，退出后不自动恢复。"""


class TerminalSession:
    def __init__(self, solver: SolverService, *, io: TerminalIO, session_id: str,
                 budget: GeminiBudgetConfig, context: ScenarioSpec | None = None,
                 backend: BackendName | None = None,
                 interpreter_factory: Callable[[str], ScenarioInterpreter] | None = None) -> None:
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
        self.budget_id = f"terminal-{session_id}"
        self.folder = checked_root(self.settings, "terminal-sessions") / session_id
        self.budget_folder = checked_root(self.settings, "llm") / self.budget_id
        self.interpreter_factory = interpreter_factory or (
            lambda budget_id: GeminiScenarioInterpreter(self.settings, budget_id=budget_id)
        )
        self.draft: ScenarioDraft | None = None
        self.intake: ScenarioIntakeService | None = None
        self.turn = 0
        self.review_number = 0
        self.phase = "startup"
        self.state: dict = {"format": "terminal_session_v1", "session_id": session_id}

    def say(self, text: str) -> None:
        self.io.write(terminal_text(text))

    def checkpoint(self, status: str, **details: object) -> None:
        self.state.update(status=status, phase=self.phase, turn=self.turn, **details)
        write_json(self.folder / "state.json", self.state)
        with (self.folder / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                     **self.state}, ensure_ascii=False, allow_nan=False) + "\n")

    def finish(self, status: str, code: int, **details: object) -> int:
        self.checkpoint(status, **details)
        self.say(f"会话状态：{status}；证据：{self.folder}")
        return code

    def run(self) -> int:
        # Reserve once. Reusing an ID cannot reset a budget or replay an execution.
        if self.budget_folder.exists() or self.budget_folder.is_symlink():
            raise ValueError("LLM budget ID already exists; choose a new session ID")
        self.folder.parent.mkdir(parents=True, exist_ok=True)
        self.folder.mkdir(mode=0o700)
        try:
            write_json(self.folder / "session.json", {
                "session_id": self.session_id, "budget_id": self.budget_id,
                "budget": self.budget.model_dump(mode="json"),
                "context": self.context.model_dump(mode="json") if self.context else None,
                "backend": self.backend.value if self.backend else None,
                "solver_max_calls": 1,
                "execution_configurations": {backend.value: self.solver.execution_configuration(backend)
                                             for backend in self.solver.registered_backends},
            })
            self.phase = "llm_consent"
            self.checkpoint("awaiting_llm_consent")
            self.say(f"步骤 1/4：确认本会话 LLM 预算。会话 {self.session_id}")
            self.say(f"Google {self.budget.model}；最多 {self.budget.max_calls} 次提取；"
                     f"费用上限 {self.budget.budget_hkd:g} HKD。\n"
                     f"每次保守预留 {self.budget.reservation_hkd} HKD；失败保留预留。"
                     "预留和 usage 估算均不是实际账单。\n"
                     f"每次输入 ≤ {self.budget.max_input_tokens} token，输出含思考 ≤ "
                     f"{self.budget.max_output_tokens} token；每个 HTTP 请求超时 "
                     f"{self.budget.network_timeout_sec:g} 秒。每次提取先计数再生成，无自动重试。\n"
                     "需求、基准和澄清原文会发给 Google；本地保存会话与响应证据。"
                     "本预算仅属于当前会话，不重置或合并历史账本。")
            if self.io.read('输入“同意”启用本会话提取；其他输入退出：').strip() != "同意":
                return self.finish("cancelled", 0)
            self.checkpoint("llm_authorized")
            GeminiScenarioInterpreter.create_budget(self.settings, budget_id=self.budget_id, config=self.budget)
            self.intake = ScenarioIntakeService(self.solver, interpreter=self.interpreter_factory(self.budget_id))
            self.say("步骤 2/4：输入需求。" + ("已载入显式基准。" if self.context else "未指定基准，缺失字段会逐项澄清。"))
            self.say("已注册 backend：" + "、".join(backend.value for backend in self.solver.registered_backends)
                     + "。注册表示已配置；运行环境和许可证尚未检查。")
            if BackendName.GUROBI not in self.solver.registered_backends:
                self.say("如需 Gurobi，请启动时提供 --gurobi-python；不会自动更换目标或 backend。")
            self.say(HELP)
            self.phase = "conversation"
            self.checkpoint("awaiting_input")
            while True:
                text = self.io.read("需求或命令 > ").strip()
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
                    self.say("请输入需求或 /quit。空输入不调用模型。")
        except (EOFError, KeyboardInterrupt) as exc:
            self.say("输入已结束；保留当前证据，不自动继续或重试。")
            return self.finish("interrupted", 130 if isinstance(exc, KeyboardInterrupt) else 0)
        except Exception as exc:
            # Never echo arbitrary transport/exception bodies or environment values.
            diagnostic = exc.diagnostic if isinstance(exc, GeminiCallError) else None
            self.say(f"{self.phase} 阶段停止：{type(exc).__name__}。证据已保留。")
            if isinstance(exc, GeminiCallError):
                self.say(str(exc))
            if diagnostic and diagnostic.get("safe_message"):
                self.say(str(diagnostic["safe_message"]))
            self.say("请先检查 state.json 和 LLM 调用证据；失败不会恢复旧候选执行或自动重试。")
            if isinstance(exc, GeminiCallError) and "GEMINI_API_KEY is unavailable" in str(exc):
                self.say("如进程缺少凭据，fish 中用 set -gx GEMINI_API_KEY $GEMINI_API_KEY 导出已有变量。")
            return self.finish("failed", 1, error_type=type(exc).__name__, diagnostic=diagnostic,
                               reason=str(exc) if isinstance(exc, GeminiCallError) else None)

    def show_draft(self) -> None:
        draft = self.draft
        if draft is None:
            self.say("尚无当前候选，请先输入需求。")
            return
        self.say(f"候选版本 {self.turn}：{draft.status}")
        values = draft.candidate.model_dump(mode="json") if draft.candidate else draft.values
        source_labels = {"context": "基准", "user_text": "用户原文", "schema_default": "字段默认值"}
        for field, value in values.items():
            self.say(f"  {LABELS.get(field, field)} [{field}] = {json.dumps(value, ensure_ascii=False)}"
                     f" （来源：{source_labels.get(draft.field_sources.get(field, ''), '待确认')}）")
        if draft.input.context:
            before = draft.input.context.model_dump(mode="json")
            changes = [f"{LABELS[field]}：{before.get(field)} → {value}" for field, value in values.items()
                       if before.get(field) != value]
            self.say("相对基准的变化：" + ("；".join(changes) if changes else "无字段变化"))
        self.say(f"指定 backend：{draft.requested_backend.value if draft.requested_backend else '未指定'}")
        if draft.decision:
            selected = draft.decision.selected_backend
            self.say(f"能力选择：{selected.value if selected else '无可执行选择'}；运行环境尚需预检查。")
            if selected is None:
                self.say(draft.decision.reason)
            for assessment in draft.decision.assessments:
                status = "兼容" if assessment.eligible else "不兼容" if assessment.registered else "未注册"
                self.say(f"  {assessment.backend.value}：{status}"
                         + (f"；{'；'.join(assessment.reasons)}" if assessment.reasons else ""))
        for question in draft.clarifications:
            self.say(f"待澄清 [{question.kind}]：{question.question}")
        for index, issue in enumerate(draft.extraction.issues, 1):
            self.say(f"可明确撤回的要求 {index}：{issue.evidence}；使用 /withdraw {index} 撤回说明")
        if draft.status == "ready":
            self.say("步骤 3/4：候选已通过字段与能力检查。输入 /run 审阅时限和单次执行预算，或继续修改。")

    def interpret(self, text: str, *, withdraw_issues: tuple[int, ...] = ()) -> None:
        assert self.intake is not None
        previous = self.draft
        try:
            validate_language_input(LanguageInput(
                messages=(*(previous.input.messages if previous else ()), text),
                context=self.context, context_backend=self.backend,
            ))
        except ValueError:
            self.say("输入未提交：每条须为 1–4000 字符，最多 20 条、合计 20000 字符。请缩短输入。")
            return
        self.turn += 1
        self.phase = "interpreting"
        self.draft = None  # A failed edit must never leave an executable stale candidate.
        write_json(self.folder / f"turn-{self.turn:03d}-input.json", {
            "text": text, "withdraw_issues": withdraw_issues,
            "previous_draft_sha256": previous.draft_sha256 if previous else None,
        })
        self.checkpoint("interpreting", draft_sha256=None, request_sha256=None,
                        review_sha256=None, diagnostic=None)
        self.say("正在提取并校验；一次提取包含计数与生成两个有界 HTTP 请求。")
        draft = (self.intake.revise(previous, text, withdraw_issues=withdraw_issues) if previous
                 else self.intake.propose(text, context=self.context, backend=self.backend))
        write_json(self.folder / f"turn-{self.turn:03d}-draft.json", draft.model_dump(mode="json"))
        self.draft = draft
        self.phase = "conversation"
        self.checkpoint(draft.status, draft_sha256=draft.draft_sha256)
        ledger = strict_json(read_local(self.budget_folder, self.budget_folder / "ledger.json"))
        self.say(f"本会话已预留 {ledger['calls_reserved']}/{self.budget.max_calls} 次，"
                 f"{ledger['reserved_hkd']}/{self.budget.budget_hkd:g} HKD。")
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
            self.say("撤回未提交。格式：/withdraw 编号 明确撤回说明；编号以当前候选显示为准。")
            return
        issue = self.draft.extraction.issues[index]
        text = f"明确撤回此前要求「{issue.evidence}」。说明：{parts[2]}"
        self.interpret(text, withdraw_issues=(index,))

    def review_and_execute(self) -> int | None:
        draft = self.draft
        assert self.intake is not None
        if draft is None or draft.status != "ready" or draft.candidate is None:
            self.say("无法执行：请先解决缺失字段、未支持要求或 backend 不兼容。")
            return None
        self.phase = "review"
        self.show_draft()
        assert draft.decision is not None and draft.decision.selected_backend is not None
        selected_backend = draft.decision.selected_backend
        configuration = self.solver.execution_configuration(selected_backend)
        options = configuration["options"]
        assert isinstance(options, dict)
        grace = float(options["timeout_grace_sec"])
        timeout = draft.candidate.solver_runtime_limit_sec + grace
        try:
            request = self.intake.prepare_execution(draft, experiment_id=f"terminal-{self.session_id}",
                                                   report_id=f"terminal-{self.session_id}",
                                                   external_timeout_sec=timeout)
        except ValueError as exc:
            diagnostic = ("；".join(error["msg"] for error in exc.errors(include_input=False, include_url=False))
                          if isinstance(exc, ValidationError) else str(exc))
            self.say(f"执行请求未生成：{diagnostic}。请修改对应字段后再 /run。")
            self.checkpoint("request_rejected", request_sha256=None, review_sha256=None, diagnostic=diagnostic)
            self.phase = "conversation"
            return None
        self.review_number += 1
        prefix = self.folder / f"review-{self.review_number:03d}"
        write_json(prefix.with_name(prefix.name + "-request.json"), request.model_dump(mode="json"))
        review = {"format": "terminal_execution_review_v1", "request": request.model_dump(mode="json"),
                  "execution_configuration": configuration}
        review_sha256 = fingerprint(review)
        write_json(prefix.with_name(prefix.name + "-execution.json"), {**review, "review_sha256": review_sha256})
        precheck = ExperimentService(self.solver).precheck(request.plan, experiment_id=request.experiment_id)
        write_json(prefix.with_name(prefix.name + "-precheck.json"), precheck.model_dump(mode="json"))
        if not precheck.allowed:
            for case in precheck.cases:
                for error in case.errors:
                    self.say(f"预检查未通过：{error}")
            self.checkpoint("precheck_rejected", request_sha256=request.request_sha256, review_sha256=review_sha256)
            self.phase = "conversation"
            self.say("修复上述配置或输入后可再次 /run；本次未调用 solver。")
            return None
        backend_label = "Gurobi" if selected_backend is BackendName.GUROBI else "HGS"
        self.say(f"执行预算：{backend_label} × 1 次，内部 {draft.candidate.solver_runtime_limit_sec:g} 秒；"
                 f"外部 timeout {timeout:g} 秒（含 {grace:g} 秒退出余量），不重试。\n"
                 "等待预算不含输入快照、独立复核和报告生成时间。")
        if selected_backend is BackendName.GUROBI:
            self.say(f"Gurobi 配置：Python={options['python_executable']}；时间段长度 "
                     f"{options['time_interval_sec']} 秒；线程数 {options['threads']}。\n"
                     "模型边界：gurobi_linear_default 时间离散周期模型、线性不满意度；"
                     "与 HGS 的库存时序及目标口径不同，目标值不作跨模型优劣比较。\n"
                     "best bound / gap 只属于本次 Gurobi 模型；限时求解不保证最优。"
                     "预检查不启动许可证环境，可用性由确认后的受控子进程检查。")
        else:
            self.say(f"HGS executable：{options['executable']}\n"
                     "模型边界：HGS 到站时原子更新库存；不保证最优，单次结果不作因果结论。")
        self.say(f"运行路径：{json.dumps(configuration['settings'], ensure_ascii=False)}\n"
                 f"完整执行审阅：{prefix.with_name(prefix.name + '-execution.json')}\n"
                 f"请求指纹：{request.request_sha256}\n执行审阅指纹（含运行配置）：{review_sha256}")
        self.checkpoint("awaiting_execution_confirmation", request_sha256=request.request_sha256,
                        review_sha256=review_sha256)
        confirmation = f"执行 {review_sha256[:12]}"
        answer = self.io.read(f'输入“{confirmation}”执行当前请求；其他输入取消：').strip()
        if answer != confirmation:
            self.checkpoint("execution_cancelled", request_sha256=None, review_sha256=None)
            self.phase = "conversation"
            if answer == "/quit":
                return self.finish("cancelled", 0)
            self.say("本次执行已取消。修改需求请在下一行输入；再次 /run 会重新审阅。")
            return None
        if self.solver.execution_configuration(selected_backend) != configuration:
            self.checkpoint("configuration_changed", request_sha256=None, review_sha256=None)
            self.phase = "conversation"
            self.say("运行配置已变化，旧确认失效；未调用 solver。请重新 /run 审阅。")
            return None
        self.phase = "executing"
        self.checkpoint("execution_confirmed", confirmed_request_sha256=request.request_sha256,
                        confirmed_review_sha256=review_sha256)
        self.say("步骤 4/4：执行一次求解，随后独立复核并生成报告。")
        receipt = self.intake.execute(draft, request, confirmed_request_sha256=request.request_sha256)
        report_dir = checked_root(self.settings, "experiment-reports") / request.report_id
        for attempt in receipt.batch.attempts:
            self.say(f"运行 {attempt.run_id}：{attempt.status}")
        verified = sum(summary.verified_candidates for summary in receipt.report.summaries)
        self.say(f"实际调用 {receipt.batch.calls_invoked} 次；通过复核的候选 {verified} 个。")
        for group in receipt.report.groups:
            for metric in group.metrics:
                self.say(f"{metric.metric}：n={metric.n}，最小/中位/最大 "
                         f"{metric.minimum:g} / {metric.median:g} / {metric.maximum:g}")
        for review_result in receipt.report.reviews:
            observation = review_result.observation
            if review_result.status == "verified_candidate" and observation and observation.result.backend is BackendName.GUROBI:
                result = observation.result
                bound = f"{result.best_bound:g}" if result.best_bound is not None else "未提供"
                gap = f"{result.optimality_gap:.3%}" if result.optimality_gap is not None else "未提供"
                self.say(f"本次 Gurobi 周期模型：best bound={bound}；optimality gap={gap}。")
        self.say(f"Markdown 报告：{report_dir / 'report.md'}\nJSON 报告：{report_dir / 'report.json'}")
        # A generated report is not evidence that a candidate passed independent review.
        successful = verified == 1 and receipt.batch.attempts[0].status in {"succeeded", "timeout_with_candidate"}
        self.phase = "reporting"
        return self.finish("reported", 0 if successful else 1,
                           batch_status=receipt.batch.status, verified_candidates=verified,
                           outcome=receipt.batch.attempts[0].status,
                           report_markdown=str(report_dir / "report.md"), report_json=str(report_dir / "report.json"))
