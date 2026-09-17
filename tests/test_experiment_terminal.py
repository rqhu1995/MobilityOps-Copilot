"""Stage 9 terminal mode uses fake HTTP and fake solvers only."""

import json
from pathlib import Path
import re
import sys
from unittest.mock import Mock

import pytest

from mobilityops import cli
from mobilityops.domain import BackendName
from mobilityops.services.experiment_intake import ExperimentPlanIntakeService
from mobilityops.services.experiment_report import ExperimentReportService
from mobilityops.services.experiment_terminal_session import ExperimentTerminalSession
from mobilityops.services.gemini_intake import (
    GeminiBudgetConfig, GeminiExperimentInterpreter, MODEL,
)
from mobilityops.services.solver_service import SolverService
from mobilityops.solvers.hgs.backend import HgsBackend


TEXT = ("以 baseline 为基准，比较 capacity30 容量30 和 two_trucks 车辆2；"
        "每个3次，使用HGS，内部5秒，外部10秒，最大9次，总预算90秒，失败继续；执行。")


def extraction(*, capacity=30, capacity_index=0, include_repetitions=True):
    fields = [
        {"target": "plan", "field": "backend", "value": "hgs", "message_index": 0, "evidence": "HGS"},
        {"target": "plan", "field": "external_timeout_sec", "value": 10, "message_index": 0, "evidence": "外部10秒"},
        {"target": "plan", "field": "max_calls", "value": 9, "message_index": 0, "evidence": "最大9次"},
        {"target": "plan", "field": "external_wait_budget_sec", "value": 90, "message_index": 0, "evidence": "总预算90秒"},
        {"target": "plan", "field": "failure_policy", "value": "continue", "message_index": 0, "evidence": "失败继续"},
        {"target": "capacity30", "field": "truck_capacity", "value": capacity,
         "message_index": capacity_index, "evidence": f"容量{capacity}"},
        {"target": "two_trucks", "field": "number_of_trucks", "value": 2, "message_index": 0, "evidence": "车辆2"},
    ]
    if include_repetitions:
        fields.insert(1, {"target": "plan", "field": "repetitions", "value": 3,
                          "message_index": 0, "evidence": "每个3次"})
    return {
        "cases": [
            {"role": "baseline", "case_id": "baseline", "message_index": 0, "evidence": "baseline"},
            {"role": "variant", "case_id": "capacity30", "message_index": 0, "evidence": "capacity30"},
            {"role": "variant", "case_id": "two_trucks", "message_index": 0, "evidence": "two_trucks"},
        ],
        "fields": fields,
        "issues": [],
    }


class FakeTransport:
    def __init__(self, extractions):
        self.extractions = iter(extractions)
        self.calls = []

    def post(self, method, body, *, timeout_sec):
        self.calls.append((method, body, timeout_sec))
        if method == "countTokens":
            return b'{"totalTokens":1200}'
        content = next(self.extractions)
        return json.dumps({
            "model": MODEL, "status": "completed", "id": "fake-experiment-interaction",
            "usage": {"total_input_tokens": 1200, "total_output_tokens": 200,
                      "total_thought_tokens": 100, "total_tool_use_tokens": 0},
            "steps": [{"type": "model_output", "content": [
                {"type": "text", "text": json.dumps(content, ensure_ascii=False)}
            ]}],
        }, ensure_ascii=False).encode()


class Script:
    def __init__(self, inputs):
        self.inputs = iter(inputs)
        self.output = []

    @property
    def text(self):
        return "\n".join(self.output)

    def read(self, prompt):
        self.output.append(prompt)
        action = next(self.inputs, EOFError())
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action(prompt)
        if action == "<confirm>":
            return re.search(r'“(执行实验 [0-9a-f]{12})”', prompt).group(1)
        return action

    def write(self, text):
        self.output.append(text)


def make_session(settings, scenario, replies, inputs, *, solver=None, session_id="experiment-e2e"):
    transport = FakeTransport(replies)
    solver = solver or SolverService(settings)
    io = Script(inputs)
    session = ExperimentTerminalSession(
        solver, io=io, session_id=session_id,
        budget=GeminiBudgetConfig(budget_hkd=1, max_calls=max(1, len(replies))),
        context=scenario, backend=BackendName.HGS,
        interpreter_factory=lambda budget_id: GeminiExperimentInterpreter(
            settings, budget_id=budget_id, transport=transport
        ),
    )
    return session, io, transport


def read_state(session):
    return json.loads((session.folder / "state.json").read_bytes())


def make_multitruck_executable(settings):
    one_truck = (Path(__file__).parent / "fixtures/hgs/6_1.txt").read_text()
    idle = ("=======\n"
            "0\tload 0 usable bikes;load 0 broken bikes;unload 0 usable bikes;unload 0 broken bikes\n"
            "0\tload 0 usable bikes;load 0 broken bikes;unload 0 usable bikes;unload 0 broken bikes\n")
    two_trucks = one_truck.replace("the repositioning scheme for repairman is:",
                                   idle + "the repositioning scheme for repairman is:")
    executable = settings.hgs_repo_path / "build/main"
    executable.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        "import sys\n"
        "args = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n"
        "trucks = int(args['-ntrk'])\n"
        "out = Path(f\"../Solutions/2026-09-10/6_1_t{trucks}_r1_2h_1.txt\")\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        f"out.write_text({one_truck!r} if trucks == 1 else {two_trucks!r})\n"
        "sys.stdout.write('fake HGS stdout\\n')\n"
        "sys.stderr.write('fake HGS stderr\\n')\n"
    )
    executable.chmod(0o700)


def test_fake_http_and_fake_hgs_execute_three_by_three_and_replay(settings, scenario):
    make_multitruck_executable(settings)
    session, io, transport = make_session(
        settings, scenario, [extraction()], ["同意", TEXT, "/run", "<confirm>"]
    )
    assert session.run() == 0
    state = read_state(session)
    assert state["status"] == "reported" and state["calls_invoked"] == 9
    report_path = settings.runs_dir / "experiment-reports" / session.report_id / "report.json"
    report = json.loads(report_path.read_bytes())
    assert [summary["verified_candidates"] for summary in report["summaries"]] == [3, 3, 3]
    assert [group["metrics"][0]["n"] for group in report["groups"]] == [3, 3, 3]
    assert "计划 9 次 / 上限 9 次" in io.text
    assert "LLM 预算已用于提取" in io.text
    assert [method for method, _, _ in transport.calls] == ["countTokens", "interactions"]
    assert (settings.runs_dir / "llm" / session.budget_id / "call-001" /
            "experiment-extraction.json").is_file()
    replay = ExperimentReportService(settings).analyze(session.experiment_id)
    assert replay.model_dump(mode="json") == report


def test_missing_repetitions_stays_in_clarification_and_never_prechecks(settings, scenario):
    session, io, _ = make_session(
        settings, scenario, [extraction(include_repetitions=False)],
        ["同意", TEXT, "/run", "/quit"], session_id="missing-repetitions",
    )
    assert session.run() == 0
    assert "待澄清 [missing]" in io.text
    assert not (settings.runs_dir / "experiments" / session.experiment_id).exists()


def test_all_plan_precheck_failures_are_shown_before_first_solver(settings, scenario):
    settings.hgs_repo_path.joinpath("build/main").unlink()
    session, io, _ = make_session(
        settings, scenario, [extraction()], ["同意", TEXT, "/run", "/quit"],
        session_id="precheck-all",
    )
    assert session.run() == 0
    assert io.text.count("预检查未通过") == 3
    assert "不会启动第一个 solver" in io.text
    assert not (settings.runs_dir / "experiments" / session.experiment_id).exists()


def test_plan_revision_changes_confirmation_and_old_token_cannot_execute(settings, scenario):
    first_token = []

    def save_and_cancel(prompt):
        first_token.append(re.search(r'“(执行实验 [0-9a-f]{12})”', prompt).group(1))
        return "取消"

    revised_text = "把 capacity30 改为容量31，其余计划不变。"
    revised = extraction(capacity=31, capacity_index=1)
    session, io, _ = make_session(
        settings, scenario, [extraction(), revised],
        ["同意", TEXT, "/run", save_and_cancel, revised_text, "/run",
         lambda _: first_token[0], "/quit"], session_id="stale-plan-confirmation",
    )
    assert session.run() == 0
    reviews = [json.loads((session.folder / f"review-{index:03d}-execution.json").read_bytes())
               for index in (1, 2)]
    assert reviews[0]["review_sha256"] != reviews[1]["review_sha256"]
    assert "本次执行已取消" in io.text
    assert not (settings.runs_dir / "experiments" / session.experiment_id).exists()


def test_runtime_configuration_change_invalidates_terminal_confirmation(settings, scenario, tmp_path):
    backend = HgsBackend(settings)
    solver = SolverService(settings, hgs_backend=backend)
    replacement = tmp_path / "new-hgs"
    replacement.write_bytes(backend.executable.read_bytes())
    replacement.chmod(0o700)

    def change_and_confirm(prompt):
        backend.executable = replacement
        return re.search(r'“(执行实验 [0-9a-f]{12})”', prompt).group(1)

    session, io, _ = make_session(
        settings, scenario, [extraction()],
        ["同意", TEXT, "/run", change_and_confirm, "/quit"],
        solver=solver, session_id="stale-runtime-config",
    )
    assert session.run() == 0
    assert "运行配置已变化，旧确认失效" in io.text
    assert not (settings.runs_dir / "experiments" / session.experiment_id).exists()


def test_ctrl_c_after_confirmation_is_durable_and_not_resumed(settings, scenario, monkeypatch):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(ExperimentPlanIntakeService, "execute", interrupt)
    session, io, _ = make_session(
        settings, scenario, [extraction()], ["同意", TEXT, "/run", "<confirm>"],
        session_id="experiment-interrupt",
    )
    assert session.run() == 130
    state = read_state(session)
    assert state["status"] == "interrupted"
    assert state["confirmed_review_sha256"]
    assert "不自动继续、重试或补跑" in io.text


def test_cli_experiment_mode_selects_batch_session_without_service_calls(settings, scenario,
                                                                          monkeypatch, tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(scenario.model_dump_json())
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    factory = Mock()
    factory.return_value.run.return_value = 0
    monkeypatch.setattr(cli, "ExperimentTerminalSession", factory)
    assert cli.main([
        "--mode", "experiment", "--baseline", str(baseline), "--backend", "hgs",
        "--budget-hkd", "0.21888", "--max-calls", "1",
        "--session-id", "cli-experiment", "--hgs-repo-path", str(settings.hgs_repo_path),
        "--gurobi-repo-path", str(settings.gurobi_repo_path),
        "--runs-dir", str(settings.runs_dir),
    ]) == 0
    assert factory.call_args.kwargs["context"] == scenario
    assert factory.call_args.kwargs["backend"] is BackendName.HGS
    assert not settings.runs_dir.exists()
