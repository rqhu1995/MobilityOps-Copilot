"""Terminal transcripts use the real services with fake HTTP and an isolated fake HGS."""

import json
import re
from unittest.mock import Mock

import pytest

from mobilityops import cli
from mobilityops.domain import BackendName
from mobilityops.services.gemini_intake import (
    GeminiBudgetConfig, GeminiCallError, GeminiScenarioInterpreter, MODEL,
)
from mobilityops.services.solver_service import SolverService
from mobilityops.services.terminal_session import TerminalSession


def proposal(field="truck_capacity", value=30, quote="容量30", index=0):
    return {"field": field, "value": value, "evidence": quote, "message_index": index}


def extraction(*fields, issues=()):
    return {"fields": list(fields), "issues": list(issues)}


class FakeTransport:
    def __init__(self, extractions):
        self.extractions = iter(extractions)
        self.calls = []

    def post(self, method, body, *, timeout_sec):
        self.calls.append(method)
        if method == "countTokens":
            return b'{"totalTokens":1000}'
        content = next(self.extractions)
        if isinstance(content, BaseException):
            raise content
        return json.dumps({"model": MODEL, "status": "completed", "id": "fake-interaction",
                           "usage": {"total_input_tokens": 1000, "total_output_tokens": 100,
                                     "total_thought_tokens": 100, "total_tool_use_tokens": 0},
                           "steps": [{"type": "model_output", "content": [
                               {"type": "text", "text": json.dumps(content)}]}]}).encode()


class Script:
    def __init__(self, inputs):
        self.inputs = iter(inputs)
        self.output = []
        self.confirmations = []

    def read(self, prompt):
        self.output.append(prompt)
        action = next(self.inputs, EOFError())
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action(prompt)
        if action == "<confirm>":
            token = re.search(r'“(执行 [0-9a-f]{12})”', prompt).group(1)
            self.confirmations.append(token)
            return token
        return action

    def write(self, text):
        self.output.append(text)

    @property
    def text(self):
        return "\n".join(self.output)


def setup(settings, scenario, inputs, *, outputs=None, context=True, budget=None, backend=BackendName.HGS):
    io = Script(inputs)
    transport = FakeTransport(outputs if outputs is not None else [extraction(proposal())])
    session = TerminalSession(
        SolverService(settings), io=io, session_id="test-session",
        budget=budget or GeminiBudgetConfig(budget_hkd=1, max_calls=3),
        context=scenario if context else None, backend=backend,
        interpreter_factory=lambda budget_id: GeminiScenarioInterpreter(settings, budget_id=budget_id, transport=transport),
    )
    return session, io, transport


def state(session):
    return json.loads((session.folder / "state.json").read_bytes())


def assert_no_solver(settings):
    assert not (settings.runs_dir / "language-requests").exists()
    assert not (settings.runs_dir / "experiments").exists()


@pytest.mark.parametrize("answer", ["", "yes", "/quit", EOFError(), KeyboardInterrupt()])
def test_budget_consent_is_explicit_and_cancel_is_free(settings, scenario, answer):
    session, _, transport = setup(settings, scenario, [answer])
    assert session.run() in {0, 130}
    assert not session.budget_folder.exists()
    assert transport.calls == []
    assert_no_solver(settings)
    assert state(session)["status"] in {"cancelled", "interrupted"}


def test_confirmed_transcript_executes_once_and_reports_verified_evidence(settings, scenario):
    session, io, transport = setup(settings, scenario, ["同意", "容量30", "/review", "/run", "<confirm>"])
    assert session.run() == 0
    assert transport.calls == ["countTokens", "interactions"]
    saved = state(session)
    assert saved["status"] == "reported" and saved["outcome"] == "succeeded"
    assert saved["verified_candidates"] == 1
    assert saved["confirmed_request_sha256"] == saved["request_sha256"]
    assert (session.folder / "turn-001-draft.json").is_file()
    report = json.loads((settings.runs_dir / "experiment-reports/terminal-test-session/report.json").read_bytes())
    assert report["batch"]["calls_invoked"] == 1
    assert "每辆车容量：25 → 30" in io.text and "内部 5 秒" in io.text and "timeout 10 秒" in io.text
    assert "objective_value：n=1" in io.text and "Markdown 报告：" in io.text


@pytest.mark.parametrize("confirmation", ["", "yes", "执行", "执行 000000000000", EOFError(), KeyboardInterrupt()])
def test_confirmation_cannot_be_implied(settings, scenario, confirmation):
    session, _, _ = setup(settings, scenario, ["同意", "容量30，立即执行", "/run", confirmation, "/quit"])
    assert session.run() in {0, 130}
    assert_no_solver(settings)
    assert "confirmed_request_sha256" not in state(session)


def test_revision_requires_new_fingerprint_and_rejects_old_confirmation(settings, scenario):
    old_token = []

    def cancel_and_remember(prompt):
        old_token.append(re.search(r'“(执行 [0-9a-f]{12})”', prompt).group(1))
        return "取消"

    session, io, _ = setup(settings, scenario,
        ["同意", "容量30", "/run", cancel_and_remember, "容量40", "/run", lambda _: old_token[0], "/quit"],
        outputs=[extraction(proposal()), extraction(proposal(value=40, quote="容量40", index=1))])
    assert session.run() == 0
    first = json.loads((session.folder / "review-001-request.json").read_bytes())
    second = json.loads((session.folder / "review-002-request.json").read_bytes())
    assert first["request_sha256"] != second["request_sha256"]
    assert second["plan"]["baseline"]["scenario"]["truck_capacity"] == 40
    assert "每辆车容量：25 → 40" in io.text
    assert_no_solver(settings)


def test_missing_fields_then_clarification_become_ready_without_execution(settings, scenario):
    fields = [proposal(field, value, "已知数据", 0) for field, value in scenario.model_dump(mode="json").items()
              if field != "solver_runtime_limit_sec"]
    session, io, transport = setup(settings, scenario,
        ["同意", "已知数据", "/run", "求解5秒", "/review", "/quit"], context=False,
        outputs=[extraction(*fields), extraction(proposal("solver_runtime_limit_sec", 5, "求解5秒", 1))])
    assert session.run() == 0
    assert "请提供求解时限" in io.text and "候选版本 2：ready" in io.text
    assert len(transport.calls) == 4
    assert_no_solver(settings)


@pytest.mark.parametrize("unsupported", ["seed", "gurobi", "time_windows"])
def test_incompatible_or_unsupported_requirements_block_run(settings, scenario, unsupported):
    output = extraction(proposal("seed", 42, "测试要求")) if unsupported == "seed" else extraction()
    if unsupported == "time_windows":
        output = extraction(issues=[{"field": None, "question": "时间窗不受支持，请修改或撤回。",
                                     "evidence": "测试要求", "message_index": 0}])
    session, io, _ = setup(settings, scenario, ["同意", "测试要求", "/run", "/quit"], outputs=[output],
                           backend=BackendName.GUROBI if unsupported == "gurobi" else BackendName.HGS)
    assert session.run() == 0
    assert "无法执行" in io.text
    assert not list(session.folder.glob("review-*-request.json"))
    assert_no_solver(settings)


def test_explicit_issue_withdrawal_preserves_prior_fields(settings, scenario):
    issue = {"field": None, "question": "时间窗不受支持", "evidence": "时间窗", "message_index": 0}
    session, io, transport = setup(settings, scenario,
        ["同意", "容量30，加时间窗", "/withdraw 0 错误编号", "/withdraw 1 撤回时间窗", "/quit"],
        outputs=[extraction(proposal(), issues=[issue]), extraction()])
    assert session.run() == 0
    assert "撤回未提交" in io.text and "候选版本 2：ready" in io.text
    assert session.draft.candidate.truck_capacity == 30
    assert len(transport.calls) == 4
    saved = json.loads((session.folder / "turn-002-input.json").read_bytes())
    assert saved["withdraw_issues"] == [0]
    assert "明确撤回此前要求「时间窗」" in saved["text"]
    assert_no_solver(settings)


@pytest.mark.parametrize("budget", [GeminiBudgetConfig(budget_hkd=1, max_calls=1),
                                   GeminiBudgetConfig(budget_hkd=0.3, max_calls=3)])
def test_exhausted_budget_invalidates_old_candidate_without_another_network_call(settings, scenario, budget):
    session, io, transport = setup(settings, scenario, ["同意", "容量30", "容量40", "/run", "<confirm>"], budget=budget)
    assert session.run() == 1
    assert transport.calls == ["countTokens", "interactions"]
    assert session.draft is None and state(session)["draft_sha256"] is None
    assert "budget exhausted" in io.text
    assert_no_solver(settings)


def test_network_failure_retains_reservation_and_does_not_retry(settings, scenario):
    session, io, transport = setup(settings, scenario, ["同意", "容量30"],
                                   outputs=[GeminiCallError("Gemini network request failed; no automatic retry")])
    assert session.run() == 1
    assert len(transport.calls) == 2
    ledger = json.loads((session.budget_folder / "ledger.json").read_bytes())
    assert ledger["calls_reserved"] == 1 and float(ledger["reserved_hkd"]) > 0
    assert state(session)["status"] == "failed" and "network request failed" in io.text
    assert "set -gx" not in io.text
    assert_no_solver(settings)


def test_missing_exported_key_reports_fish_fix_without_reserving_or_network(settings, scenario, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    post = Mock(side_effect=AssertionError("No network without a key"))
    monkeypatch.setattr("mobilityops.services.gemini_intake.GeminiHttpTransport.post", post)
    session, io, _ = setup(settings, scenario, ["同意", "容量30"])
    session.interpreter_factory = lambda budget_id: GeminiScenarioInterpreter(settings, budget_id=budget_id)
    assert session.run() == 1
    post.assert_not_called()
    assert "set -gx GEMINI_API_KEY $GEMINI_API_KEY" in io.text
    assert json.loads((session.budget_folder / "ledger.json").read_bytes())["calls_reserved"] == 0
    assert_no_solver(settings)


def test_invalid_execution_budget_can_be_revised_without_changing_requested_time(settings, scenario):
    session, io, _ = setup(settings, scenario,
        ["同意", "求解86400秒", "/run", "求解5秒", "/run", "取消", "/quit"],
        outputs=[extraction(proposal("solver_runtime_limit_sec", 86400, "求解86400秒")),
                 extraction(proposal("solver_runtime_limit_sec", 5, "求解5秒", 1))])
    assert session.run() == 0
    assert "执行请求未生成" in io.text
    original = json.loads((session.folder / "turn-001-draft.json").read_bytes())
    assert original["candidate"]["solver_runtime_limit_sec"] == 86400
    assert session.draft.candidate.solver_runtime_limit_sec == 5
    assert state(session)["diagnostic"] is None
    assert_no_solver(settings)


def test_preflight_failure_is_visible_and_does_not_start_solver(settings, scenario):
    (settings.hgs_repo_path / "build/main").unlink()
    session, io, _ = setup(settings, scenario, ["同意", "容量30", "/run", "/quit"])
    assert session.run() == 0
    assert "预检查未通过" in io.text and "executable" in io.text
    assert (session.folder / "review-001-precheck.json").is_file()
    assert_no_solver(settings)


def test_solver_failure_still_links_report_and_returns_nonzero(settings, scenario):
    executable = settings.hgs_repo_path / "build/main"
    executable.write_text("#!/bin/sh\nexit 7\n")
    session, io, _ = setup(settings, scenario, ["同意", "容量30", "/run", "<confirm>"])
    assert session.run() == 1
    assert state(session)["status"] == "reported" and state(session)["outcome"] == "failed"
    assert state(session)["verified_candidates"] == 0
    assert "Markdown 报告：" in io.text
    assert "objective_value：" not in io.text


def test_interrupt_during_execution_is_recorded_and_never_retried(settings, scenario, monkeypatch):
    execute = Mock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr("mobilityops.services.scenario_intake.ScenarioIntakeService.execute", execute)
    session, _, _ = setup(settings, scenario, ["同意", "容量30", "/run", "<confirm>"])
    assert session.run() == 130
    execute.assert_called_once()
    assert state(session)["status"] == "interrupted" and state(session)["phase"] == "executing"


def test_duplicate_session_never_overwrites_evidence(settings, scenario):
    session, _, _ = setup(settings, scenario, ["同意", "/quit"])
    assert session.run() == 0
    before = {path: path.read_bytes() for path in settings.runs_dir.rglob("*") if path.is_file()}
    second, _, transport = setup(settings, scenario, ["同意", "容量30"])
    with pytest.raises(ValueError, match="already exists"):
        second.run()
    assert transport.calls == []
    assert before == {path: path.read_bytes() for path in before}


def test_help_review_and_invalid_commands_do_not_consume_calls(settings, scenario):
    session, io, transport = setup(settings, scenario,
        ["同意", "", "/help", "/review", "/run", "/unknown", "/withdraw 1 abc", "x" * 4001, "/quit"])
    assert session.run() == 0
    assert transport.calls == [] and "输入未提交" in io.text
    assert_no_solver(settings)


def test_terminal_escapes_untrusted_controls(settings, scenario):
    session, io, _ = setup(settings, scenario, ["同意", "名称 abc", "/quit"],
                           outputs=[extraction(proposal("scenario_name", "abc\x1b[2J\u202e", "名称 abc"))])
    assert session.run() == 0
    assert "\x1b" not in io.text and "\u202e" not in io.text
    assert "\\u202e" in io.text


def test_help_needs_neither_environment_nor_tty(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    for name in ("HGS_REPO_PATH", "GUROBI_REPO_PATH", "MOBILITYOPS_RUNS_DIR", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "--baseline" in capsys.readouterr().out


def test_cli_rejects_piped_confirmation(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2 and "交互终端" in capsys.readouterr().err


@pytest.mark.parametrize("extra", [["--budget-hkd", "nan"], ["--budget-hkd", "0.1"],
                                   ["--max-calls", "7"], ["--session-id", "../escape"]])
def test_cli_invalid_config_has_no_session_or_network_side_effects(settings, monkeypatch, extra):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    args = ["--hgs-repo-path", str(settings.hgs_repo_path), "--gurobi-repo-path", str(settings.gurobi_repo_path),
            "--runs-dir", str(settings.runs_dir), *extra]
    assert cli.main(args) == 2
    assert not settings.runs_dir.exists()


def test_cli_wires_flags_and_baseline_into_session(settings, scenario, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(scenario.model_dump_json())
    factory = Mock()
    factory.return_value.run.return_value = 0
    monkeypatch.setattr(cli, "TerminalSession", factory)
    assert cli.main(["--hgs-repo-path", str(settings.hgs_repo_path), "--gurobi-repo-path", str(settings.gurobi_repo_path),
                     "--runs-dir", str(settings.runs_dir), "--baseline", str(baseline), "--backend", "hgs"]) == 0
    assert factory.call_args.kwargs["context"] == scenario
    assert factory.call_args.kwargs["backend"] == BackendName.HGS
    assert factory.call_args.args[0].settings == settings
