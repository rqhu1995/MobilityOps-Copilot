"""One-shot terminal review for a dossier-bound replication campaign."""

from datetime import datetime, timezone
import json

from pydantic import ValidationError

from mobilityops.domain.solution import validate_run_id
from mobilityops.services.dossier_campaign import DossierCampaignService
from mobilityops.services.experiment_service import checked_root, write_json
from mobilityops.services.next_experiment import NextExperimentPrecheckError
from mobilityops.services.scenario_intake import fingerprint
from mobilityops.services.solver_service import SolverService
from mobilityops.services.terminal_session import TerminalIO, terminal_text


class DossierCampaignTerminalSession:
    def __init__(
        self,
        solver: SolverService,
        *,
        io: TerminalIO,
        session_id: str,
        dossier_id: str,
    ) -> None:
        validate_run_id(session_id)
        if len(session_id) > 80:
            raise ValueError("Session ID must have at most 80 characters")
        validate_run_id(dossier_id)
        self.solver = solver
        self.settings = solver.settings
        self.io = io
        self.session_id = session_id
        self.dossier_id = dossier_id
        self.experiment_id = f"campaign-{session_id}"
        self.report_id = self.experiment_id
        self.folder = (
            checked_root(self.settings, "dossier-campaign-sessions") / session_id
        )
        self.service = DossierCampaignService(solver)
        self.phase = "startup"
        self.state = {
            "format": "dossier_campaign_terminal_session_v1",
            "session_id": session_id,
            "mode": "dossier-campaign",
            "source_dossier_id": dossier_id,
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
            raise ValueError("Dossier campaign session already exists; choose a new ID")
        self.folder.parent.mkdir(parents=True, exist_ok=True)
        self.folder.mkdir(mode=0o700)
        write_json(
            self.folder / "session.json",
            {
                "format": "dossier_campaign_terminal_session_v1",
                "session_id": self.session_id,
                "mode": "dossier-campaign",
                "source_dossier_id": self.dossier_id,
                "experiment_id": self.experiment_id,
                "report_id": self.report_id,
            },
        )
        try:
            self.phase = "dossier_review"
            self.checkpoint("validating_dossier")
            self.say("步骤 1/3：重放执行审计并重新核对 Dossier、候选计划和运行条件。")
            try:
                request = self.service.prepare_execution(
                    self.dossier_id,
                    experiment_id=self.experiment_id,
                    report_id=self.report_id,
                )
            except NextExperimentPrecheckError as exc:
                write_json(
                    self.folder / "precheck.json", exc.precheck.model_dump(mode="json")
                )
                for case in exc.precheck.cases:
                    for error in case.errors:
                        self.say(f"预检查未通过 [{case.case_id}]：{error}")
                self.say("全部场景通过前不会启动第一个 solver；未探测许可证。")
                return self.finish("precheck_rejected", 2)
            except (ValidationError, ValueError, OSError) as exc:
                self.say(f"Dossier 拒绝：{exc}")
                return self.finish(
                    "dossier_rejected", 2,
                    error_type=type(exc).__name__, reason=str(exc),
                )

            child = request.execution_request
            write_json(self.folder / "request.json", request.model_dump(mode="json"))
            write_json(
                self.folder / "precheck.json", child.precheck.model_dump(mode="json")
            )
            review = {
                "format": "dossier_campaign_execution_review_v1",
                "request": request.model_dump(mode="json"),
            }
            review_sha256 = fingerprint(review)
            write_json(
                self.folder / "review.json",
                {**review, "review_sha256": review_sha256},
            )
            self.say(
                f"Dossier={request.source_dossier_id}；Dossier 散列="
                f"{request.source_dossier_sha256}；建议={request.dossier.recommendation}"
            )
            self.say(
                f"来源执行审计={request.source_execution_audit_id}；清单散列="
                f"{request.source_execution_audit_manifest_sha256}"
            )
            self.say(
                f"solver 预算：计划 {child.plan.planned_calls} 次 / 上限 "
                f"{child.plan.max_calls} 次；需要 "
                f"{child.precheck.planned_external_wait_sec:g} 秒 / 总等待预算 "
                f"{child.plan.external_wait_budget_sec:g} 秒；失败策略 "
                f"{child.plan.failure_policy}。"
            )
            for case in child.plan.cases:
                decision = next(
                    item.decision for item in child.precheck.cases
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
                    child.execution_configurations,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            self.say(
                f"子请求指纹={child.request_sha256}\n"
                f"Dossier 接管请求指纹={request.request_sha256}\n"
                f"审阅指纹={review_sha256}"
            )
            self.phase = "confirmation"
            self.checkpoint(
                "awaiting_execution_confirmation",
                request_sha256=request.request_sha256,
                review_sha256=review_sha256,
            )
            self.say(
                "步骤 2/3：Dossier、审计或候选中的“执行”文字不构成确认；"
                "这里只接受下方一次性确认短语。"
            )
            confirmation = f"执行 Dossier 重复实验 {review_sha256[:12]}"
            answer = self.io.read(
                f'输入“{confirmation}”执行当前完整计划；其他输入取消：'
            ).strip()
            if answer != confirmation:
                return self.finish(
                    "cancelled", 0, request_sha256=None, review_sha256=None
                )

            try:
                fresh = self.service.prepare_execution(
                    self.dossier_id,
                    experiment_id=self.experiment_id,
                    report_id=self.report_id,
                )
            except (ValidationError, ValueError, OSError):
                fresh = None
            if fresh != request:
                self.say(
                    "Dossier、来源审计、候选、预检查依据或运行配置已变化，"
                    "旧确认失效；未调用 solver。"
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
                request,
                confirmed_request_sha256=request.request_sha256,
            )
            execution = receipt.execution
            for attempt in execution.batch.attempts:
                self.say(f"运行 {attempt.run_id}：{attempt.status}")
            verified = sum(
                summary.verified_candidates for summary in execution.report.summaries
            )
            report_dir = (
                checked_root(self.settings, "experiment-reports") / child.report_id
            )
            lineage_path = (
                checked_root(self.settings, "dossier-campaign-requests")
                / child.experiment_id / "lineage.json"
            )
            self.say(
                f"实际调用 {execution.batch.calls_invoked}/{child.plan.max_calls} 次；"
                f"预留等待 {execution.batch.reserved_external_wait_sec:g}/"
                f"{child.plan.external_wait_budget_sec:g} 秒；复核候选 {verified} 个。"
            )
            self.say(
                f"Markdown 报告：{report_dir / 'report.md'}\n"
                f"JSON 报告：{report_dir / 'report.json'}\n"
                f"谱系证据：{lineage_path}"
            )
            successful = (
                verified == child.plan.planned_calls
                and execution.batch.status == "completed"
            )
            self.phase = "reporting"
            return self.finish(
                "reported",
                0 if successful else 1,
                batch_status=execution.batch.status,
                verified_candidates=verified,
                calls_invoked=execution.batch.calls_invoked,
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
