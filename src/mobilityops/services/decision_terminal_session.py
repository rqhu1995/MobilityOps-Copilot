"""Read-only terminal commands for evidence-bound experiment decisions."""

from datetime import datetime, timezone
from typing import Protocol

from mobilityops.config import Settings
from mobilityops.domain.decision_explanation import DecisionExplanation
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.decision_explanation import DecisionExplanationService
from mobilityops.services.experiment_service import checked_root, write_json
from mobilityops.services.terminal_session import terminal_text


HELP = """/explain 重新复核全部原始证据并解释整个实验。
/compare <variant-case-id> baseline 只解释该变体相对基准的证据。
/next-plan 生成并保存一个有界候选计划；不会调用 LLM 或 solver。
/help 查看帮助；/quit 退出并保存。每条分析命令都会重新复核原始证据。"""


class ReviewIO(Protocol):
    def read(self, prompt: str) -> str: ...
    def write(self, text: str) -> None: ...


class DecisionTerminalSession:
    def __init__(self, settings: Settings, *, io: ReviewIO, session_id: str,
                 experiment_id: str) -> None:
        validate_run_id(session_id)
        validate_run_id(experiment_id)
        self.settings = Settings.model_validate(settings.model_dump())
        self.io = io
        self.session_id = session_id
        self.experiment_id = experiment_id
        self.folder = checked_root(self.settings, "decision-sessions") / session_id
        self.service = DecisionExplanationService(self.settings)
        self.command_number = 0

    def say(self, value: str) -> None:
        self.io.write(terminal_text(value))

    def run(self) -> int:
        if self.folder.exists() or self.folder.is_symlink():
            raise ValueError("Decision session already exists; choose a new session ID")
        self.folder.parent.mkdir(parents=True, exist_ok=True)
        self.folder.mkdir(mode=0o700)
        write_json(self.folder / "session.json", {
            "format": "decision_terminal_session_v1", "session_id": self.session_id,
            "experiment_id": self.experiment_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "read_only_source": True, "auto_execute": False,
        })
        self.say(f"证据审阅模式：{self.experiment_id}。不会调用 LLM、solver 或许可证环境。")
        self.say(HELP)
        try:
            while True:
                command = self.io.read("审阅命令 > ").strip()
                if command == "/quit":
                    self._checkpoint("completed")
                    return 0
                if command == "/help":
                    self.say(HELP)
                elif command == "/explain":
                    try:
                        explanation = self.service.explain(self.experiment_id)
                    except (ValueError, OSError) as exc:
                        self._reject_evidence(exc)
                        continue
                    self._show(explanation, command="explain")
                elif command.startswith("/compare "):
                    parts = command.split()
                    if len(parts) != 3 or parts[2] != "baseline":
                        self.say("格式：/compare <variant-case-id> baseline")
                        continue
                    try:
                        explanation = self.service.explain(
                            self.experiment_id, variant_case_id=parts[1]
                        )
                    except (ValueError, OSError) as exc:
                        self._reject_evidence(exc)
                        continue
                    self._show(explanation, command=f"compare-{parts[1]}")
                elif command == "/next-plan":
                    try:
                        proposal = self.service.next_plan(self.experiment_id)
                    except (ValueError, OSError) as exc:
                        self._reject_evidence(exc)
                        continue
                    self.command_number += 1
                    path = self.folder / f"{self.command_number:03d}-next-plan.json"
                    write_json(path, proposal.model_dump(mode="json"))
                    if proposal.plan is None:
                        self.say("未生成候选计划：" + "；".join(proposal.blockers))
                    else:
                        self.say(f"候选计划已保存：{path}")
                        self.say(f"计划 {proposal.plan.planned_calls} 次；等待预算 "
                                 f"{proposal.plan.external_wait_budget_sec:g} 秒。"
                                 "它尚未预检查、确认或执行。")
                    self._checkpoint("proposal_created", artifact=str(path))
                elif command:
                    self.say("未知命令。输入 /help 查看可用命令。")
        except (EOFError, KeyboardInterrupt):
            self._checkpoint("interrupted")
            self.say("审阅已中断；没有启动 LLM 或 solver。")
            return 130

    def _show(self, explanation: DecisionExplanation, *, command: str) -> None:
        self.command_number += 1
        path = self.folder / f"{self.command_number:03d}-{command}.json"
        write_json(path, explanation.model_dump(mode="json"))
        self.say("已复核的事实：")
        for claim in explanation.verified_facts:
            self.say(f"- {claim.text}")
        self.say("本批描述性观察：")
        for claim in explanation.descriptive_observations:
            self.say(f"- {claim.text}")
        self.say("不能据此得出的结论：")
        for claim in explanation.cannot_conclude:
            self.say(f"- {claim.text}")
        self.say(f"结构化证据说明：{path}")
        self._checkpoint("explained", artifact=str(path),
                         source_report_sha256=explanation.source_report_sha256)

    def _checkpoint(self, status: str, **details: object) -> None:
        write_json(self.folder / "state.json", {
            "format": "decision_terminal_state_v1", "session_id": self.session_id,
            "experiment_id": self.experiment_id, "status": status,
            "command_number": self.command_number, **details,
        })

    def _reject_evidence(self, exc: ValueError | OSError) -> None:
        self.say(f"证据复核失败：{type(exc).__name__}: {exc}")
        self.say("没有生成解释、比较或候选计划，也没有启动 LLM 或 solver。")
        self._checkpoint("evidence_rejected", error_type=type(exc).__name__, reason=str(exc))
