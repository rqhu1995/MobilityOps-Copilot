"""Run one real-model Copilot shadow session and audit it without solver execution."""

from collections.abc import Callable
import json
from pathlib import Path

from mobilityops.config import Settings
from mobilityops.domain import BackendName, ScenarioSpec, SolutionResult
from mobilityops.domain.copilot_pilot import CopilotPilotReport
from mobilityops.services.copilot import CopilotInterpreter
from mobilityops.services.copilot_audit import CopilotAuditService
from mobilityops.services.copilot_terminal_session import CopilotTerminalSession
from mobilityops.services.experiment_intake import ExperimentInterpreter
from mobilityops.services.experiment_service import checked_root, write_json
from mobilityops.services.gemini_intake import GeminiBudgetConfig
from mobilityops.services.gurobi_inputs import read_local, strict_json
from mobilityops.services.solver_service import SolverService
from mobilityops.solvers.hgs.backend import HgsBackend


EXPECTED_DECISIONS = (
    "draft_experiment",
    "prepare_execution_review",
    "answer",
)


class ExecutionBlockedHgsBackend(HgsBackend):
    """Allow deterministic HGS preflight but fail closed before process execution."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.solve_attempts = 0

    def solve(self, scenario: ScenarioSpec, *, run_id: str) -> SolutionResult:
        self.solve_attempts += 1
        raise RuntimeError("Stage 14 shadow pilot forbids solver execution")


class ShadowPilotIO:
    """Fixed local operator choices; never emits the exact execution confirmation."""

    def __init__(self, objective: str, evidence_question: str) -> None:
        self.objective = objective
        self.evidence_question = evidence_question
        self.output: list[str] = []
        self.copilot_prompts = 0
        self.confirmation_prompts = 0

    @property
    def text(self) -> str:
        return "\n".join(self.output)

    def write(self, text: str) -> None:
        self.output.append(text)

    def read(self, prompt: str) -> str:
        self.output.append(prompt)
        if "启用本会话 Copilot" in prompt:
            return "同意"
        if "执行 Copilot 实验" in prompt:
            self.confirmation_prompts += 1
            return "取消"
        if prompt.strip() != "Copilot >":
            raise ValueError("Shadow pilot received an unexpected terminal prompt")
        self.copilot_prompts += 1
        if self.copilot_prompts == 1:
            return self.objective
        if self.copilot_prompts == 2:
            self._require("Gemini 选择受控动作：draft_experiment")
            self._require("当前计划：ready")
            return "对当前完整计划进行预检查并生成执行审阅，不要执行 solver。"
        if self.copilot_prompts == 3:
            self._require("Gemini 选择受控动作：prepare_execution_review")
            self._require("全计划预检查通过")
            self._require("执行已取消；没有调用 solver。")
            return self.evidence_question
        if self.copilot_prompts == 4:
            self._require("Gemini 选择受控动作：answer")
            return "/quit"
        raise ValueError("Shadow pilot exceeded its fixed conversation")

    def _require(self, text: str) -> None:
        if text not in self.text:
            raise ValueError(f"Shadow pilot expected transcript marker: {text}")


class CopilotShadowPilotService:
    def __init__(
        self,
        settings: Settings,
        *,
        router_factory: Callable[[str], CopilotInterpreter] | None = None,
        experiment_interpreter_factory: (
            Callable[[str], ExperimentInterpreter] | None
        ) = None,
    ) -> None:
        self.settings = Settings.model_validate(settings.model_dump())
        self.router_factory = router_factory
        self.experiment_interpreter_factory = experiment_interpreter_factory

    def run(
        self,
        *,
        pilot_id: str,
        session_id: str,
        audit_id: str,
        budget: GeminiBudgetConfig,
        context: ScenarioSpec,
        objective: str,
        evidence_question: str,
    ) -> CopilotPilotReport:
        if budget.max_calls != 4 or budget.budget_hkd > 1:
            raise ValueError("Stage 14 requires exactly 4 calls and at most 1 HKD")
        paths = {
            "pilot": checked_root(self.settings, "copilot-pilots") / pilot_id,
            "session": checked_root(self.settings, "copilot-sessions") / session_id,
            "budget": checked_root(self.settings, "llm") / f"copilot-{session_id}",
            "audit": checked_root(self.settings, "copilot-audits") / audit_id,
        }
        if any(path.exists() or path.is_symlink() for path in paths.values()):
            raise ValueError("Stage 14 evidence ID already exists; refusing overwrite")
        pilot_root = paths["pilot"]
        pilot_root.parent.mkdir(parents=True, exist_ok=True)
        pilot_root.mkdir(mode=0o700)
        write_json(pilot_root / "plan.json", {
            "format": "copilot_shadow_pilot_plan_v1",
            "pilot_id": pilot_id,
            "session_id": session_id,
            "audit_id": audit_id,
            "model": budget.model,
            "budget": budget.model_dump(mode="json"),
            "maximum_reserved_hkd": str(budget.reservation_hkd * budget.max_calls),
            "objective": objective,
            "evidence_question": evidence_question,
            "expected_decisions": list(EXPECTED_DECISIONS),
            "solver_calls": 0,
            "license_probes": 0,
            "confirmation_policy": "always_refuse",
            "failure_policy": "stop_without_retry",
        })
        io = ShadowPilotIO(objective, evidence_question)
        backend = ExecutionBlockedHgsBackend(self.settings)
        session = CopilotTerminalSession(
            SolverService(self.settings, hgs_backend=backend),
            io=io,
            session_id=session_id,
            budget=budget,
            context=context,
            backend=BackendName.HGS,
            router_factory=self.router_factory,
            experiment_interpreter_factory=self.experiment_interpreter_factory,
        )
        code = session.run()
        write_json(pilot_root / "transcript.json", {
            "format": "copilot_shadow_pilot_transcript_v1",
            "lines": io.output,
        })
        if code != 0:
            self._failure(pilot_root, session, backend, "Copilot session failed")
        audit = CopilotAuditService(self.settings).write_report(
            session_id, audit_id=audit_id
        )
        review_path = session.folder / "turn-002-execution-review.json"
        review = strict_json(read_local(session.folder, review_path))
        request = review.get("request")
        if not isinstance(request, dict) or not isinstance(request.get("plan"), dict):
            self._failure(pilot_root, session, backend, "Execution review is incomplete")
        plan_payload = request["plan"]
        state = strict_json(read_local(session.folder, session.folder / "state.json"))
        expected = (
            state.get("status") == "completed"
            and state.get("execution_completed") is False
            and tuple(audit.decisions) == EXPECTED_DECISIONS
            and audit.audit_status == "verified_no_execution"
            and audit.model_usage.calls_reserved == 4
            and audit.model_usage.calls_succeeded == 4
            and not audit.execution.executed
            and audit.execution.calls_invoked == 0
            and backend.solve_attempts == 0
            and io.confirmation_prompts == 1
        )
        if not expected:
            self._failure(pilot_root, session, backend, "Shadow invariants failed")
        report = CopilotPilotReport(
            pilot_id=pilot_id,
            session_id=session_id,
            audit_id=audit_id,
            model=budget.model,
            budget_hkd=budget.budget_hkd,
            max_calls=budget.max_calls,
            calls_reserved=audit.model_usage.calls_reserved,
            calls_succeeded=audit.model_usage.calls_succeeded,
            reserved_hkd=audit.model_usage.reserved_hkd,
            estimated_usage_hkd=audit.model_usage.estimated_usage_hkd,
            decisions=audit.decisions,
            planned_solver_calls=plan_payload["max_calls"],
            planned_external_wait_sec=plan_payload["external_wait_budget_sec"],
            execution_review_sha256=review["review_sha256"],
            audit_manifest_sha256=audit.manifest_sha256,
            session_status="completed",
            audit_status="verified_no_execution",
        )
        write_json(pilot_root / "report.json", report.model_dump(mode="json"))
        (pilot_root / "report.md").write_text(
            self._markdown(report), encoding="utf-8"
        )
        return report

    @staticmethod
    def _failure(
        root: Path,
        session: CopilotTerminalSession,
        backend: ExecutionBlockedHgsBackend,
        reason: str,
    ) -> None:
        state_path = session.folder / "state.json"
        state = json.loads(state_path.read_bytes()) if state_path.is_file() else None
        write_json(root / "failure.json", {
            "format": "copilot_shadow_pilot_failure_v1",
            "reason": reason,
            "session_state": state,
            "solver_attempts": backend.solve_attempts,
            "retry_allowed": False,
        })
        raise RuntimeError(reason)

    @staticmethod
    def _markdown(report: CopilotPilotReport) -> str:
        return (
            "# Copilot 影子会话验收\n\n"
            f"- 会话：`{report.session_id}`\n"
            f"- 审计：`{report.audit_id}` / `{report.audit_status}`\n"
            f"- Gemini：`{report.calls_succeeded}/{report.max_calls}`；"
            f"usage 估算 `{report.estimated_usage_hkd}` HKD\n"
            f"- 计划 solver 调用：`{report.planned_solver_calls}`；实际：`0`\n"
            f"- 审计清单：`{report.audit_manifest_sha256}`\n"
        )
