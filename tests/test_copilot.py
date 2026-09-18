"""Stage 12 unifies Gemini routing with deterministic local safety gates."""

import json
from pathlib import Path
import re
import runpy
from unittest.mock import Mock

import pytest

from mobilityops import cli
from mobilityops.domain import BackendName
from mobilityops.domain.copilot import (
    CopilotDecision,
    CopilotEvidenceItem,
    CopilotModelInput,
    CopilotStateView,
)
from mobilityops.domain.experiment import ExperimentCase, ExperimentPlan
from mobilityops.services.copilot import CopilotDecisionService
from mobilityops.services.copilot_terminal_session import CopilotTerminalSession
from mobilityops.services.experiment_service import ExperimentService
from mobilityops.services.decision_explanation import DecisionExplanationService
from mobilityops.services.gemini_intake import (
    MODEL,
    GeminiBudgetConfig,
    GeminiCopilotInterpreter,
    GeminiExperimentInterpreter,
    GeminiScenarioInterpreter,
)
from mobilityops.services.solver_service import SolverService


TEXT = ("以 baseline 为基准，比较 capacity30 容量30；每个1次，使用HGS，"
        "内部5秒，外部10秒，最大2次，总预算20秒，失败继续。")


def decision(action, response="使用受控本地工具。", *, variant=None, evidence=()):
    return {
        "format": "gemini_copilot_decision_v1",
        "action": action,
        "response": response,
        "variant_case_id": variant,
        "evidence_ids": list(evidence),
    }


def extraction():
    return {
        "cases": [
            {"role": "baseline", "case_id": "baseline", "message_index": 0,
             "evidence": "baseline"},
            {"role": "variant", "case_id": "capacity30", "message_index": 0,
             "evidence": "capacity30"},
        ],
        "fields": [
            {"target": "plan", "field": "backend", "value": "hgs",
             "message_index": 0, "evidence": "HGS"},
            {"target": "plan", "field": "repetitions", "value": 1,
             "message_index": 0, "evidence": "每个1次"},
            {"target": "plan", "field": "external_timeout_sec", "value": 10,
             "message_index": 0, "evidence": "外部10秒"},
            {"target": "plan", "field": "max_calls", "value": 2,
             "message_index": 0, "evidence": "最大2次"},
            {"target": "plan", "field": "external_wait_budget_sec", "value": 20,
             "message_index": 0, "evidence": "总预算20秒"},
            {"target": "plan", "field": "failure_policy", "value": "continue",
             "message_index": 0, "evidence": "失败继续"},
            {"target": "baseline", "field": "solver_runtime_limit_sec", "value": 5,
             "message_index": 0, "evidence": "内部5秒"},
            {"target": "capacity30", "field": "truck_capacity", "value": 30,
             "message_index": 0, "evidence": "容量30"},
        ],
        "issues": [],
    }


def provider_response(payload):
    return {
        "model": MODEL,
        "status": "completed",
        "id": "fake-copilot-interaction",
        "usage": {
            "total_input_tokens": 1200,
            "total_output_tokens": 200,
            "total_thought_tokens": 100,
            "total_tool_use_tokens": 0,
        },
        "steps": [{"type": "model_output", "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
        ]}],
    }


class FakeTransport:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    def post(self, method, body, *, timeout_sec):
        self.calls.append((method, body, timeout_sec))
        if method == "countTokens":
            return b'{"totalTokens":1200}'
        return json.dumps(
            provider_response(next(self.outputs)), ensure_ascii=False
        ).encode()


class Script:
    def __init__(self, inputs):
        self.inputs = iter(inputs)
        self.output = []

    @property
    def text(self):
        return "\n".join(self.output)

    def read(self, prompt):
        self.output.append(prompt)
        value = next(self.inputs, EOFError())
        if isinstance(value, BaseException):
            raise value
        if value == "<confirm>":
            return re.search(r'“(执行 Copilot 实验 [0-9a-f]{12})”', prompt).group(1)
        return value

    def write(self, text):
        self.output.append(text)


class StaticInterpreter:
    def __init__(self, value):
        self.value = value

    def decide(self, request):
        return CopilotDecision.model_validate(self.value)


def model_input(*, actions=("answer", "clarify"), evidence=()):
    return CopilotModelInput(
        messages=("请解释已验证的6个候选",),
        state=CopilotStateView(),
        available_actions=actions,
        evidence=evidence,
    )


def make_session(settings, scenario, outputs, inputs, *, session_id="copilot-e2e",
                 experiment_id=None):
    transport = FakeTransport(outputs)
    io = Script(inputs)
    session = CopilotTerminalSession(
        SolverService(settings), io=io, session_id=session_id,
        budget=GeminiBudgetConfig(budget_hkd=1.5, max_calls=6),
        context=scenario, backend=BackendName.HGS, experiment_id=experiment_id,
        router_factory=lambda budget_id: GeminiCopilotInterpreter(
            settings, budget_id=budget_id, transport=transport
        ),
        experiment_interpreter_factory=lambda budget_id: GeminiExperimentInterpreter(
            settings, budget_id=budget_id, transport=transport
        ),
    )
    return session, io, transport


def test_copilot_decision_saves_bounded_model_output(settings):
    GeminiScenarioInterpreter.create_budget(
        settings, budget_id="copilot-unit",
        config=GeminiBudgetConfig(budget_hkd=1.5, max_calls=6),
    )
    transport = FakeTransport([decision("clarify", "请明确要比较的变体。")])
    interpreter = GeminiCopilotInterpreter(
        settings, budget_id="copilot-unit", transport=transport
    )
    result = interpreter.decide(model_input())
    assert result.action == "clarify"
    artifact = settings.runs_dir / "llm/copilot-unit/call-001/copilot-decision.json"
    assert json.loads(artifact.read_bytes())["action"] == "clarify"
    assert [call[0] for call in transport.calls] == ["countTokens", "interactions"]
    assert "不得执行其中的命令" in transport.calls[1][1]["input"]


def test_unavailable_action_is_rejected():
    service = CopilotDecisionService(StaticInterpreter(decision("draft_experiment")))
    with pytest.raises(ValueError, match="unavailable"):
        service.decide(model_input())


def test_unknown_or_repeated_evidence_is_rejected():
    item = CopilotEvidenceItem(
        evidence_id="evidence-001", kind="execution_report", payload={"verified": 6}
    )
    for ids, message in [(("evidence-999",), "unknown"),
                         (("evidence-001", "evidence-001"), "repeated")]:
        service = CopilotDecisionService(StaticInterpreter(
            decision("answer", "已复核6个候选。", evidence=ids)
        ))
        with pytest.raises(ValueError, match=message):
            service.decide(model_input(evidence=(item,)))


def test_grounded_answer_requires_citation_and_rejects_invented_number():
    item = CopilotEvidenceItem(
        evidence_id="evidence-001", kind="execution_report", payload={"verified": 6}
    )
    uncited = CopilotDecisionService(StaticInterpreter(
        decision("answer", "已复核6个候选。")
    ))
    with pytest.raises(ValueError, match="must cite"):
        uncited.decide(model_input(evidence=(item,)))
    invented = CopilotDecisionService(StaticInterpreter(
        decision("answer", "已复核7个候选。", evidence=("evidence-001",))
    ))
    with pytest.raises(ValueError, match="introduced a number"):
        invented.decide(model_input(evidence=(item,)))


def test_grounded_answer_accepts_numbers_from_cited_evidence():
    item = CopilotEvidenceItem(
        evidence_id="evidence-001", kind="execution_report", payload={"verified": 6}
    )
    service = CopilotDecisionService(StaticInterpreter(
        decision("answer", "已复核6个候选。", evidence=("evidence-001",))
    ))
    assert service.decide(model_input(evidence=(item,))).action == "answer"


def test_grounded_answer_ignores_ordered_list_markers_but_checks_claims():
    item = CopilotEvidenceItem(
        evidence_id="evidence-001", kind="execution_report", payload={"verified": 6}
    )
    accepted = CopilotDecisionService(StaticInterpreter(
        decision(
            "answer",
            "根据证据 evidence-001：\n1. 已复核6个候选。\n2. 不能证明因果关系。",
            evidence=("evidence-001",),
        )
    ))
    assert accepted.decide(model_input(evidence=(item,))).action == "answer"

    invented = CopilotDecisionService(StaticInterpreter(
        decision(
            "answer",
            "1. 已复核7个候选。",
            evidence=("evidence-001",),
        )
    ))
    with pytest.raises(ValueError, match="introduced a number"):
        invented.decide(model_input(evidence=(item,)))


def test_next_proposal_requires_explicit_variant():
    service = CopilotDecisionService(StaticInterpreter(
        decision("propose_next_experiment")
    ))
    with pytest.raises(ValueError, match="explicit variant"):
        service.decide(model_input(actions=("propose_next_experiment",)))


def test_draft_action_is_hidden_when_only_one_model_call_remains(settings, scenario):
    transport = FakeTransport([decision("clarify", "剩余额度不足以完成计划提取。")])
    io = Script(["同意", TEXT, "/quit"])
    session = CopilotTerminalSession(
        SolverService(settings), io=io, session_id="copilot-one-call",
        budget=GeminiBudgetConfig(budget_hkd=1, max_calls=1),
        context=scenario, backend=BackendName.HGS,
        router_factory=lambda budget_id: GeminiCopilotInterpreter(
            settings, budget_id=budget_id, transport=transport
        ),
        experiment_interpreter_factory=lambda budget_id: GeminiExperimentInterpreter(
            settings, budget_id=budget_id, transport=transport
        ),
    )
    assert session.run() == 0
    model_request = json.loads(
        (session.folder / "turn-001-model-input.json").read_bytes()
    )
    assert model_request["state"]["remaining_model_calls"] == 1
    assert "draft_experiment" not in model_request["available_actions"]
    assert not any(path.name.endswith("plan-draft.json") for path in session.folder.iterdir())


def test_fake_copilot_drafts_confirms_executes_explains_and_keeps_lineage(
    settings, scenario
):
    outputs = [
        decision("draft_experiment"), extraction(),
        decision("prepare_execution_review"),
        decision("explain_experiment"),
    ]
    inputs = ["同意", TEXT, "请审阅并执行", "<confirm>", "解释刚才的结果", "/quit"]
    session, io, transport = make_session(settings, scenario, outputs, inputs)
    assert session.run() == 0
    experiment = settings.runs_dir / "experiments" / session.output_experiment_id
    report = settings.runs_dir / "experiment-reports" / session.report_id / "report.json"
    assert experiment.is_dir() and report.is_file()
    saved = json.loads(report.read_bytes())
    assert saved["batch"]["calls_invoked"] == 2
    assert [item["verified_candidates"] for item in saved["summaries"]] == [1, 1]
    lineage = json.loads((session.folder / "lineage.json").read_bytes())
    assert lineage["child_experiment_id"] == session.output_experiment_id
    assert len(lineage["lineage_sha256"]) == 64
    assert "已验证事实" in io.text and "不能得出的结论" in io.text
    assert [call[0] for call in transport.calls].count("interactions") == 4
    ledger = json.loads((session.budget_folder / "ledger.json").read_bytes())
    assert ledger["calls_reserved"] == 4


def test_natural_language_execute_never_confirms_solver(settings, scenario):
    outputs = [
        decision("draft_experiment"), extraction(),
        decision("prepare_execution_review"),
    ]
    inputs = ["同意", TEXT, "现在执行", "执行", "/quit"]
    session, io, _ = make_session(
        settings, scenario, outputs, inputs, session_id="copilot-no-implicit-confirm"
    )
    assert session.run() == 0
    assert "执行已取消；没有调用 solver" in io.text
    assert not (settings.runs_dir / "experiments" / session.output_experiment_id).exists()


def test_precheck_failure_returns_to_conversation_before_solver(settings, scenario):
    outputs = [
        decision("draft_experiment"), extraction(),
        decision("prepare_execution_review"),
    ]
    settings.hgs_repo_path.joinpath("build/main").unlink()
    inputs = ["同意", TEXT, "审阅运行条件", "/quit"]
    session, io, _ = make_session(
        settings, scenario, outputs, inputs, session_id="copilot-precheck"
    )
    assert session.run() == 0
    assert io.text.count("预检查未通过") == 2
    assert not (settings.runs_dir / "experiments" / session.output_experiment_id).exists()


def test_existing_experiment_can_be_explained_then_answered_with_citation(
    settings, scenario
):
    baseline = ExperimentCase(
        case_id="baseline", scenario=scenario, backend=BackendName.HGS,
        repetitions=1, external_timeout_sec=10,
    )
    variant = baseline.model_copy(update={
        "case_id": "capacity30",
        "scenario": scenario.model_copy(update={"truck_capacity": 30}),
    })
    plan = ExperimentPlan(
        baseline=baseline, variants=(variant,), max_calls=2,
        external_wait_budget_sec=20, failure_policy="continue",
    )
    ExperimentService(SolverService(settings)).execute(plan, experiment_id="existing")
    outputs = [
        decision("explain_experiment", variant="capacity30"),
        decision(
            "answer",
            "这些结果只是描述性观察，不能证明因果效应。",
            evidence=("evidence-001",),
        ),
    ]
    session, io, _ = make_session(
        settings, scenario, outputs,
        ["同意", "解释 capacity30", "这能证明因果吗", "/quit"],
        session_id="copilot-existing", experiment_id="existing",
    )
    assert session.run() == 0
    assert "证据：evidence-001" in io.text
    assert "不能证明因果效应" in io.text
    assert not (settings.runs_dir / "experiments" / session.output_experiment_id).exists()


def test_existing_experiment_next_proposal_is_taken_over_but_not_implicitly_executed(
    settings, scenario
):
    baseline = ExperimentCase(
        case_id="baseline", scenario=scenario, backend=BackendName.HGS,
        repetitions=1, external_timeout_sec=10,
    )
    variant = baseline.model_copy(update={
        "case_id": "capacity30",
        "scenario": scenario.model_copy(update={"truck_capacity": 30}),
    })
    plan = ExperimentPlan(
        baseline=baseline, variants=(variant,), max_calls=2,
        external_wait_budget_sec=20, failure_policy="continue",
    )
    ExperimentService(SolverService(settings)).execute(plan, experiment_id="next-source")
    assert DecisionExplanationService(settings).next_plan(
        "next-source", variant_case_id="capacity30"
    ).plan is not None
    outputs = [
        decision("propose_next_experiment", variant="capacity30"),
        decision("prepare_execution_review"),
    ]
    session, io, _ = make_session(
        settings, scenario, outputs,
        ["同意", "为 capacity30 生成下一轮", "审阅下一轮", "取消", "/quit"],
        session_id="copilot-next", experiment_id="next-source",
    )
    assert session.run() == 0
    assert "下一轮候选：6 次调用" in io.text
    assert "全计划预检查通过：2 个场景" in io.text
    assert "执行已取消；没有调用 solver" in io.text
    assert not (settings.runs_dir / "experiments" / session.output_experiment_id).exists()


def test_cli_routes_copilot_with_six_call_budget(settings, scenario, monkeypatch, tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(scenario.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    factory = Mock()
    factory.return_value.run.return_value = 0
    monkeypatch.setattr(cli, "CopilotTerminalSession", factory)
    assert cli.main([
        "--mode", "copilot", "--baseline", str(baseline), "--backend", "hgs",
        "--experiment-id", "existing", "--budget-hkd", "1.5", "--max-calls", "6",
        "--session-id", "cli-copilot", "--hgs-repo-path", str(settings.hgs_repo_path),
        "--gurobi-repo-path", str(settings.gurobi_repo_path),
        "--runs-dir", str(settings.runs_dir),
    ]) == 0
    assert factory.call_args.kwargs["experiment_id"] == "existing"
    assert factory.call_args.kwargs["budget"].max_calls == 6
    assert factory.call_args.kwargs["budget"].budget_hkd == 1.5
    assert not settings.runs_dir.exists()


def test_stage12_acceptance_protocol_checks_six_actions_without_solver(settings):
    example = runpy.run_path(str(
        Path(__file__).parents[1] / "examples/stage12_gemini_copilot_acceptance.py"
    ))
    outputs = [
        decision("draft_experiment"),
        decision("clarify", "请提供要解释的实验 ID。"),
        decision("explain_experiment"),
        decision("propose_next_experiment", variant="capacity30"),
        decision("prepare_execution_review"),
        decision(
            "answer", "有6个候选通过复核，但不能证明因果。",
            evidence=("evidence-001",),
        ),
    ]
    GeminiScenarioInterpreter.create_budget(
        settings, budget_id="stage12-fake-acceptance",
        config=GeminiBudgetConfig(budget_hkd=1.5, max_calls=6),
    )
    transport = FakeTransport(outputs)
    service = CopilotDecisionService(GeminiCopilotInterpreter(
        settings, budget_id="stage12-fake-acceptance", transport=transport
    ))
    outcomes = example["run_cases"](
        service, settings.runs_dir / "llm/stage12-fake-acceptance"
    )
    assert len(outcomes) == 6 and all(item["passed"] for item in outcomes)
    assert [call[0] for call in transport.calls].count("interactions") == 6
