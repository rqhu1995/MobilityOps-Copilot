"""One-shot terminal confirmation for an audit-bound Copilot plan."""

from datetime import datetime, timezone
import json

from pydantic import ValidationError

from mobilityops.domain.solution import validate_run_id
from mobilityops.services.audited_execution import AuditedExecutionService
from mobilityops.services.experiment_intake import ExperimentPlanPrecheckError
from mobilityops.services.experiment_service import checked_root, write_json
from mobilityops.services.scenario_intake import fingerprint
from mobilityops.services.solver_service import SolverService
from mobilityops.services.terminal_session import TerminalIO, terminal_text


class AuditedExecutionTerminalSession:
    def __init__(
        self,
        solver: SolverService,
        *,
        io: TerminalIO,
        session_id: str,
        source_session_id: str,
        source_audit_id: str,
    ) -> None:
        for value in (session_id, source_session_id, source_audit_id):
            validate_run_id(value)
            if len(value) > 80:
                raise ValueError("Session and audit IDs must have at most 80 characters")
        self.solver = solver
        self.settings = solver.settings
        self.io = io
        self.session_id = session_id
        self.source_session_id = source_session_id
        self.source_audit_id = source_audit_id
        self.experiment_id = f"audited-{session_id}"
        self.report_id = self.experiment_id
        self.folder = (
            checked_root(self.settings, "audited-execution-sessions") / session_id
        )
        self.service = AuditedExecutionService(solver)
        self.phase = "startup"
        self.state: dict[str, object] = {
            "format": "audited_execution_terminal_session_v1",
            "session_id": session_id,
            "mode": "audited-execution",
            "source_session_id": source_session_id,
            "source_audit_id": source_audit_id,
            "experiment_id": self.experiment_id,
            "report_id": self.report_id,
        }

    def say(self, value: str) -> None:
        self.io.write(terminal_text(value))

    def checkpoint(self, status: str, **details: object) -> None:
        self.state.update(status=status, phase=self.phase, **details)
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
        if self.folder.exists() or self.folder.is_symlink():
            raise ValueError(
                "Audited-execution session already exists; choose a new session ID"
            )
        self.folder.parent.mkdir(parents=True, exist_ok=True)
        self.folder.mkdir(mode=0o700)
        write_json(
            self.folder / "session.json",
            {
                "format": "audited_execution_terminal_session_v1",
                "session_id": self.session_id,
                "mode": "audited-execution",
                "source_session_id": self.source_session_id,
                "source_audit_id": self.source_audit_id,
                "experiment_id": self.experiment_id,
                "report_id": self.report_id,
            },
        )
        try:
            self.phase = "audit_review"
            self.checkpoint("verifying_source")
            self.say("步骤 1/3：重新审计来源会话、执行审阅、完整计划和当前运行条件。")
            try:
                request = self.service.prepare_execution(
                    source_session_id=self.source_session_id,
                    source_audit_id=self.source_audit_id,
                    experiment_id=self.experiment_id,
                    report_id=self.report_id,
                )
            except ExperimentPlanPrecheckError as exc:
                write_json(
                    self.folder / "precheck.json", exc.precheck.model_dump(mode="json")
                )
                for case in exc.precheck.cases:
                    for error in case.errors:
                        self.say(f"预检查未通过 [{case.case_id}]：{error}")
                self.say("全部场景通过前不会启动第一个 solver；未探测许可证。")
                return self.finish("precheck_rejected", 2)
            except (ValidationError, ValueError, OSError) as exc:
                self.say(f"审计接管拒绝：{exc}")
                return self.finish(
                    "source_rejected",
                    2,
                    error_type=type(exc).__name__,
                    reason=str(exc),
                )

            write_json(self.folder / "request.json", request.model_dump(mode="json"))
            write_json(
                self.folder / "precheck.json",
                request.execution_request.precheck.model_dump(mode="json"),
            )
            review = {
                "format": "audited_execution_review_v1",
                "request": request.model_dump(mode="json"),
            }
            review_sha256 = fingerprint(review)
            write_json(
                self.folder / "review.json",
                {**review, "review_sha256": review_sha256},
            )
            plan = request.execution_request.plan
            precheck = request.execution_request.precheck
            self.say(
                f"来源会话={request.source_session_id}；来源审计={request.source_audit_id}；"
                f"审计清单散列={request.source_audit_manifest_sha256}"
            )
            self.say(
                f"solver 预算：计划 {plan.planned_calls} 次 / 上限 {plan.max_calls} 次；"
                f"需要 {precheck.planned_external_wait_sec:g} 秒 / 总等待预算 "
                f"{plan.external_wait_budget_sec:g} 秒；失败策略 {plan.failure_policy}。"
            )
            for case in plan.cases:
                decision = next(
                    item.decision
                    for item in precheck.cases
                    if item.case_id == case.case_id
                )
                self.say(
                    f"  {case.case_id}：backend={decision.selected_backend.value} × "
                    f"{case.repetitions}；内部/外部 "
                    f"{case.scenario.solver_runtime_limit_sec:g}/"
                    f"{case.external_timeout_sec:g} 秒。"
                )
            self.say(
                "backend 运行配置="
                + json.dumps(
                    request.execution_request.execution_configurations,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            self.say(
                f"来源审阅指纹={request.source_review_sha256}\n"
                f"来源请求指纹={request.source_execution_request_sha256}\n"
                f"新请求指纹={request.request_sha256}\n审阅指纹={review_sha256}"
            )
            self.phase = "confirmation"
            self.checkpoint(
                "awaiting_execution_confirmation",
                request_sha256=request.request_sha256,
                review_sha256=review_sha256,
            )
            self.say(
                "步骤 2/3：Gemini、来源文件或自然语言中的“执行”均不构成确认；"
                "这里只接受下方一次性终端确认短语。"
            )
            confirmation = f"执行审计计划 {review_sha256[:12]}"
            answer = self.io.read(
                f'输入“{confirmation}”执行当前完整计划；其他输入取消：'
            ).strip()
            if answer != confirmation:
                return self.finish(
                    "cancelled", 0, request_sha256=None, review_sha256=None
                )

            try:
                fresh = self.service.prepare_execution(
                    source_session_id=self.source_session_id,
                    source_audit_id=self.source_audit_id,
                    experiment_id=self.experiment_id,
                    report_id=self.report_id,
                )
            except (ValidationError, ValueError, OSError):
                fresh = None
            if fresh != request:
                self.say(
                    "来源审计、审阅、计划、预检查依据或运行配置已变化，旧确认失效；"
                    "未调用 solver。"
                )
                return self.finish(
                    "review_changed", 2, request_sha256=None, review_sha256=None
                )

            self.phase = "executing"
            self.checkpoint(
                "execution_confirmed",
                confirmed_request_sha256=request.request_sha256,
                confirmed_review_sha256=review_sha256,
            )
            self.say("步骤 3/3：按 case/repetition 顺序串行执行并重新复核报告。")
            receipt = self.service.execute(
                request, confirmed_request_sha256=request.request_sha256
            )
            report_dir = (
                checked_root(self.settings, "experiment-reports")
                / request.execution_request.report_id
            )
            for attempt in receipt.execution.batch.attempts:
                self.say(f"运行 {attempt.run_id}：{attempt.status}")
            verified = sum(
                summary.verified_candidates
                for summary in receipt.execution.report.summaries
            )
            self.say(
                f"实际调用 {receipt.execution.batch.calls_invoked}/{plan.max_calls} 次；"
                f"预留等待 {receipt.execution.batch.reserved_external_wait_sec:g}/"
                f"{plan.external_wait_budget_sec:g} 秒；复核候选 {verified} 个。"
            )
            self.say(
                f"谱系散列={receipt.lineage_sha256}\n"
                f"Markdown 报告：{report_dir / 'report.md'}\n"
                f"JSON 报告：{report_dir / 'report.json'}"
            )
            successful = (
                verified == plan.planned_calls
                and receipt.execution.batch.status == "completed"
            )
            self.phase = "reporting"
            return self.finish(
                "reported",
                0 if successful else 1,
                batch_status=receipt.execution.batch.status,
                verified_candidates=verified,
                calls_invoked=receipt.execution.batch.calls_invoked,
                lineage_sha256=receipt.lineage_sha256,
                report_markdown=str(report_dir / "report.md"),
                report_json=str(report_dir / "report.json"),
            )
        except (EOFError, KeyboardInterrupt) as exc:
            self.say("输入或执行已中断；保留当前证据，不自动继续、重试或补跑。")
            return self.finish(
                "interrupted", 130 if isinstance(exc, KeyboardInterrupt) else 0
            )
        except Exception as exc:
            self.say(f"{self.phase} 阶段停止：{type(exc).__name__}。证据已保留。")
            return self.finish(
                "failed", 1, error_type=type(exc).__name__, reason=str(exc)
            )
