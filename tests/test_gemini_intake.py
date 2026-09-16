"""HTTP and budget tests inject transport responses; no API key or network needed."""

from datetime import date
from io import BytesIO
import json
from unittest.mock import MagicMock, Mock
from urllib.error import HTTPError

import pytest

from mobilityops.domain.intake import LanguageInput
from mobilityops.services.gemini_intake import (
    GeminiBudgetConfig, GeminiCallError, GeminiHttpTransport, GeminiScenarioInterpreter,
    MODEL, MAX_RESPONSE_BYTES, _NoRedirects,
)


def encoded(value):
    return json.dumps(value).encode()


def response(**update):
    return {"model": MODEL, "id": "test-interaction", "status": "completed", "steps": [{"type": "thought", "signature": "opaque"}, {"type": "model_output", "content": [
        {"text": json.dumps({"fields": [{"field": "truck_capacity", "value": 30,
                    "evidence": "容量30", "message_index": 0}], "issues": []}), "type": "text"}]}],
            "usage": {"total_input_tokens": 1000, "total_output_tokens": 100, "total_thought_tokens": 200, "total_tool_use_tokens": 0}, **update}


def setup(settings, *, config=None, replies=None):
    config = config or GeminiBudgetConfig(budget_hkd=10, max_calls=2)
    folder = GeminiScenarioInterpreter.create_budget(settings, budget_id="test", config=config)
    transport = Mock()
    transport.post.side_effect = replies or [encoded({"totalTokens": 1000}), encoded(response())] * 2
    return GeminiScenarioInterpreter(settings, budget_id="test", transport=transport), transport, folder


@pytest.mark.parametrize("changes", [{"budget_hkd": 11}, {"budget_hkd": 0}, {"budget_hkd": float("inf")},
                                    {"max_calls": 7}, {"max_calls": True}, {"model": "other"},
                                    {"network_timeout_sec": 100}, {"max_output_tokens": 65536}])
def test_invalid_budget_config(changes):
    with pytest.raises(ValueError):
        GeminiBudgetConfig.model_validate({"budget_hkd": 10, "max_calls": 6, **changes})


def test_success_saves_safe_evidence_and_includes_thinking_cost(settings, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-secret-value-never-send")
    interpreter, transport, folder = setup(settings)
    extraction = interpreter.extract(LanguageInput(messages=("容量30",)))
    assert extraction.fields[0].value == 30
    assert [call.args[0] for call in transport.post.call_args_list] == ["countTokens", "interactions"]
    payload = transport.post.call_args_list[1].args[1]
    assert payload["model"] == MODEL
    count_input = transport.post.call_args_list[0].args[1]["generateContentRequest"]
    assert count_input["model"] == f"models/{MODEL}"
    assert count_input["contents"][0]["parts"] == [{"text": payload["input"]}]
    assert payload["store"] is False
    assert set(payload) == {"model", "input", "store", "generation_config"}
    assert payload["generation_config"] == {"max_output_tokens": 4096}
    assert '"additionalProperties": false' in payload["input"]
    assert '必须输出纯 JSON，不使用 Markdown 代码块' in payload["input"]
    state = json.loads((folder / "call-001/state.json").read_bytes())
    assert state["status"] == "succeeded"
    assert state["estimated_usage_hkd"] == "0.015000"
    assert float(state["reserved_hkd"]) == pytest.approx(0.21888)
    assert all(b"fake-secret-value-never-send" not in path.read_bytes() for path in folder.rglob('*') if path.is_file())


def test_call_budget_persists_across_client_instances(settings):
    interpreter, transport, folder = setup(settings, config=GeminiBudgetConfig(budget_hkd=10, max_calls=1))
    interpreter.extract(LanguageInput(messages=("容量30",)))
    second = GeminiScenarioInterpreter(settings, budget_id="test", transport=transport)
    with pytest.raises(GeminiCallError, match="exhausted"):
        second.extract(LanguageInput(messages=("容量30",)))
    assert transport.post.call_count == 2
    with pytest.raises(FileExistsError):
        GeminiScenarioInterpreter.create_budget(settings, budget_id="test", config=interpreter.config)


def test_money_cap_checked_before_network(settings):
    interpreter, transport, _ = setup(settings, config=GeminiBudgetConfig(budget_hkd=0.1, max_calls=6))
    with pytest.raises(GeminiCallError, match="exhausted"):
        interpreter.extract(LanguageInput(messages=("容量30",)))
    transport.post.assert_not_called()


@pytest.mark.parametrize("count", [16001, True, -1, None])
def test_count_tokens_must_pass_before_generation(settings, count):
    interpreter, transport, folder = setup(settings, replies=[encoded({"totalTokens": count})])
    with pytest.raises(GeminiCallError):
        interpreter.extract(LanguageInput(messages=("容量30",)))
    transport.post.assert_called_once()
    assert json.loads((folder / "ledger.json").read_bytes())["calls_reserved"] == 1
    assert not json.loads((folder / "call-001/state.json").read_bytes())["generation_started"]


@pytest.mark.parametrize("replies", [
    [GeminiCallError("HTTP 429")],
    [encoded({"totalTokens": 1000}), GeminiCallError("timeout")],
    [encoded({"totalTokens": 1000}), b"not json"],
    [encoded({"totalTokens": 1000}), encoded(response(steps=[]))],
    [encoded({"totalTokens": 1000}), encoded(response(status="incomplete"))],
    [encoded({"totalTokens": 1000}), encoded(response(usage=None))],
    [encoded({"totalTokens": 1000}), encoded(response(model="other-model"))],
])
def test_failures_keep_reservation_and_never_retry(settings, replies):
    interpreter, transport, folder = setup(settings, replies=replies)
    with pytest.raises(GeminiCallError):
        interpreter.extract(LanguageInput(messages=("容量30",)))
    assert transport.post.call_count == len(replies)
    assert json.loads((folder / "call-001/state.json").read_bytes())["status"] == "failed"
    ledger = json.loads((folder / "ledger.json").read_bytes())
    assert ledger["calls_reserved"] == 1 and float(ledger["reserved_hkd"]) > 0


def test_usage_cap_violation_halts_later_calls(settings):
    interpreter, transport, folder = setup(settings, replies=[encoded({"totalTokens": 1000}),
        encoded(response(usage={"total_input_tokens": 1000, "total_output_tokens": 3000, "total_thought_tokens": 3000, "total_tool_use_tokens": 0}))])
    with pytest.raises(GeminiCallError, match="exceeded"):
        interpreter.extract(LanguageInput(messages=("容量30",)))
    with pytest.raises(GeminiCallError, match="halted"):
        interpreter.extract(LanguageInput(messages=("容量30",)))
    assert transport.post.call_count == 2


def test_tool_call_cannot_be_executed(settings):
    raw = response(steps=[{"type": "function_call", "name": "solve", "arguments": {}}])
    interpreter, _, _ = setup(settings, replies=[encoded({"totalTokens": 1000}), encoded(raw)])
    with pytest.raises(GeminiCallError, match="forbidden"):
        interpreter.extract(LanguageInput(messages=("容量30",)))


def test_missing_key_is_checked_without_reserving_a_call(settings, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _, _, folder = setup(settings)
    interpreter = GeminiScenarioInterpreter(settings, budget_id="test")
    with pytest.raises(GeminiCallError, match="unavailable"):
        interpreter.extract(LanguageInput(messages=("容量30",)))
    assert json.loads((folder / "ledger.json").read_bytes())["calls_reserved"] == 0


@pytest.mark.parametrize("method,path", [("countTokens", f"models/{MODEL}:countTokens"),
                                        ("interactions", "interactions")])
def test_credentials_only_go_to_official_host_header(monkeypatch, method, path):
    monkeypatch.setenv("GEMINI_API_KEY", "test-credential")
    http = MagicMock()
    http.open.return_value.__enter__.return_value.read.return_value = b"{}"
    monkeypatch.setattr("mobilityops.services.gemini_intake.build_opener", Mock(return_value=http))
    assert GeminiHttpTransport().post(method, {}, timeout_sec=3) == b"{}"
    request = http.open.call_args.args[0]
    assert request.full_url == f"https://generativelanguage.googleapis.com/v1beta/{path}"
    assert "test-credential" not in request.full_url
    assert request.get_header("X-goog-api-key") == "test-credential"
    assert http.open.call_args.kwargs["timeout"] == 3


def test_legacy_budget_cannot_be_silently_reused(settings):
    _, transport, folder = setup(settings)
    legacy = json.loads((folder / "config.json").read_bytes())
    legacy.pop("api")
    (folder / "config.json").write_bytes(encoded(legacy))
    before = (folder / "ledger.json").read_bytes()
    with pytest.raises(GeminiCallError, match="read-only"):
        GeminiScenarioInterpreter(settings, budget_id="test", transport=transport)
    transport.post.assert_not_called()
    assert (folder / "ledger.json").read_bytes() == before


@pytest.mark.parametrize("status", ["in_progress", "requires_action", "incomplete", "failed", "cancelled"])
def test_only_completed_interactions_produce_candidates(settings, status):
    interpreter, transport, folder = setup(settings, replies=[encoded({"totalTokens": 1000}),
                                                             encoded(response(status=status))])
    with pytest.raises(GeminiCallError, match="complete candidate"):
        interpreter.extract(LanguageInput(messages=("容量30",)))
    state = json.loads((folder / "call-001/state.json").read_bytes())
    assert state["interaction_status"] == status
    assert state["estimated_usage_hkd"] == "0.015000"
    assert not (folder / "call-001/extraction.json").exists()
    assert transport.post.call_count == 2


@pytest.mark.parametrize("usage", [
    {"total_input_tokens": 9, "total_output_tokens": 18, "total_tool_use_tokens": 0},
    {"total_input_tokens": 9, "total_output_tokens": 18, "total_thought_tokens": True, "total_tool_use_tokens": 0},
    {"total_input_tokens": 9, "total_output_tokens": -1, "total_thought_tokens": 369, "total_tool_use_tokens": 0},
])
def test_invalid_interactions_usage_never_implies_zero_cost(settings, usage):
    interpreter, _, folder = setup(settings, replies=[encoded({"totalTokens": 1000}), encoded(response(usage=usage))])
    with pytest.raises(GeminiCallError, match="token accounting"):
        interpreter.extract(LanguageInput(messages=("容量30",)))
    state = json.loads((folder / "call-001/state.json").read_bytes())
    assert "estimated_usage_hkd" not in state
    assert state["status"] == "failed"


def test_interactions_accounting_includes_thought_tokens_and_ignores_thought_content(settings):
    raw = response(usage={"total_input_tokens": 9, "total_output_tokens": 18,
                         "total_thought_tokens": 369, "total_tool_use_tokens": 0, "total_tokens": 396})
    raw["steps"][0]["summary"] = [{"type": "text", "text": "not candidate JSON; never execute"}]
    interpreter, _, folder = setup(settings, replies=[encoded({"totalTokens": 1000}), encoded(raw)])
    assert interpreter.extract(LanguageInput(messages=("容量30",))).fields[0].value == 30
    state = json.loads((folder / "call-001/state.json").read_bytes())
    assert state["estimated_usage_hkd"] == "0.011664"


@pytest.mark.parametrize("steps", [
    [{"type": "model_output", "content": [{"type": "image", "data": "fake"}]}],
    [{"type": "model_output", "content": []}],
    [{"type": "model_output", "content": [{"type": "text", "text": None}]}],
    [{"type": "function_call", "name": "execute"}],
    [{"type": "model_output", "content": []}, {"type": "model_output", "content": []}],
])
def test_unexpected_interactions_outputs_rejected(settings, steps):
    interpreter, _, folder = setup(settings, replies=[encoded({"totalTokens": 1000}), encoded(response(steps=steps))])
    with pytest.raises(GeminiCallError):
        interpreter.extract(LanguageInput(messages=("容量30",)))
    assert not (folder / "call-001/extraction.json").exists()


def test_count_projection_reserves_protocol_headroom(settings):
    interpreter, transport, folder = setup(settings, replies=[encoded({"totalTokens": 15000})])
    with pytest.raises(GeminiCallError, match="over budget"):
        interpreter.extract(LanguageInput(messages=("容量30",)))
    transport.post.assert_called_once()
    state = json.loads((folder / "call-001/state.json").read_bytes())
    assert state["input_token_headroom"] == 1024
    assert not state["generation_started"]


@pytest.mark.parametrize("error", [HTTPError("https://unused", 403, "test-credential", {}, None),
                                  ValueError("invalid header test-credential")])
def test_http_errors_do_not_expose_credentials(monkeypatch, error):
    monkeypatch.setenv("GEMINI_API_KEY", "test-credential")
    http = MagicMock()
    http.open.side_effect = error
    monkeypatch.setattr("mobilityops.services.gemini_intake.build_opener", Mock(return_value=http))
    with pytest.raises(GeminiCallError) as caught:
        GeminiHttpTransport().post("countTokens", {}, timeout_sec=3)
    assert "test-credential" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_http_diagnostic_saved_without_provider_text_or_secrets(settings, monkeypatch):
    secret = "fake-credential-do-not-persist"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    body = encoded({"error": {"status": "INVALID_ARGUMENT", "message":
                   "Unknown name responseFormat; schema contains " + secret}})
    http = MagicMock()
    http.open.side_effect = HTTPError("https://unused", 400, secret, {}, BytesIO(body))
    monkeypatch.setattr("mobilityops.services.gemini_intake.build_opener", Mock(return_value=http))
    _, _, folder = setup(settings)
    interpreter = GeminiScenarioInterpreter(settings, budget_id="test")
    with pytest.raises(GeminiCallError):
        interpreter.extract(LanguageInput(messages=("容量30",)))
    state = json.loads((folder / "call-001/state.json").read_bytes())
    assert state["diagnostic"] == {"kind": "http_error", "http_status": 400,
            "api_status": "INVALID_ARGUMENT", "mentioned_fields": ["responseFormat"],
            "reason": "unknown_request_field", "safe_message": "Unknown name responseFormat; schema contains [REDACTED]"}
    assert secret.encode() not in b"".join(path.read_bytes() for path in folder.rglob('*') if path.is_file())
    http.open.assert_called_once()


def test_acceptance_stops_after_first_provider_error(settings, scenario):
    from pathlib import Path
    import runpy
    from mobilityops.services.scenario_intake import ScenarioIntakeService
    from mobilityops.services.solver_service import SolverService
    example = runpy.run_path(str(Path(__file__).parents[1] / "examples/stage7_gemini_acceptance.py"))
    interpreter, transport, folder = setup(settings, replies=[GeminiCallError("HTTP error", http_status=400)])
    outcomes = example["run_cases"](ScenarioIntakeService(SolverService(settings), interpreter=interpreter), scenario, folder)
    assert len(outcomes) == 6
    assert all(item["status"] == "skipped_after_failure" for item in outcomes[1:])
    assert outcomes[0]["diagnostic"]["http_status"] == 400
    transport.post.assert_called_once()


@pytest.mark.parametrize("failed", [False, True])
def test_recovery_uses_only_last_reservation_and_preserves_original_evidence(settings, scenario, failed):
    from pathlib import Path
    import runpy
    example = runpy.run_path(str(Path(__file__).parents[1] / "examples/stage7_gemini_recovery.py"))
    budget_id = example["BUDGET_ID"]
    folder = GeminiScenarioInterpreter.create_budget(settings, budget_id=budget_id,
                    config=GeminiBudgetConfig(budget_hkd=10, max_calls=6))
    transport = Mock()
    transport.post.side_effect = [encoded({"totalTokens": 1000}), encoded(response())] * 5
    interpreter = GeminiScenarioInterpreter(settings, budget_id=budget_id, transport=transport)
    for _ in range(5):
        interpreter.extract(LanguageInput(messages=("容量30",)))
    original = encoded({"passed": False, "generation_attempts": 5})
    (folder / "acceptance.json").write_bytes(original)
    last_response = response()
    last_response["steps"][1]["content"][0]["text"] = json.dumps({"fields": [
        {"field": "truck_capacity", "value": 30, "message_index": 0, "evidence": "车辆容量提高到30"}], "issues": []})
    transport.post.side_effect = [encoded({"totalTokens": 1000}),
            GeminiCallError("HTTP error", http_status=429) if failed else encoded(last_response)]
    result = example["run_recovery"](settings, scenario, transport=transport)
    assert result["passed"] is not failed
    assert result["solver_calls"] == 0
    assert result["original_evidence_unchanged"]
    assert not result["full_six_case_acceptance_passed"]
    assert result["ledger"]["calls_reserved"] == 6
    assert (folder / "acceptance.json").read_bytes() == original
    assert (folder / "recovery-001/reviewed-execution-request.json").exists() is not failed
    with pytest.raises(ValueError, match="exactly one attempt"):
        example["run_recovery"](settings, scenario, transport=transport)
    assert transport.post.call_count == 12


def test_redirect_and_oversized_response_rejected(monkeypatch):
    with pytest.raises(GeminiCallError, match="redirect"):
        _NoRedirects().redirect_request(None, None, 302, "redirect", {}, "https://other")
    monkeypatch.setenv("GEMINI_API_KEY", "test-credential")
    http = MagicMock()
    http.open.return_value.__enter__.return_value.read.return_value = b"x" * (MAX_RESPONSE_BYTES + 1)
    monkeypatch.setattr("mobilityops.services.gemini_intake.build_opener", Mock(return_value=http))
    with pytest.raises(GeminiCallError, match="size"):
        GeminiHttpTransport().post("countTokens", {}, timeout_sec=3)


def test_expired_pricing_prevents_outbound_requests(settings, monkeypatch):
    interpreter, transport, _ = setup(settings)
    monkeypatch.setattr("mobilityops.services.gemini_intake.PRICING_VALID_THROUGH", date(2020, 1, 1))
    with pytest.raises(GeminiCallError, match="expired"):
        interpreter.extract(LanguageInput(messages=("容量30",)))
    transport.post.assert_not_called()


def test_second_call_using_locked_budget_is_rejected(settings):
    import fcntl
    interpreter, transport, folder = setup(settings)
    with (folder / "budget.lock").open('r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(GeminiCallError, match="active"):
            interpreter.extract(LanguageInput(messages=("容量30",)))
    transport.post.assert_not_called()


def test_acceptance_example_runs_six_cases_without_solver(settings, scenario, monkeypatch):
    from pathlib import Path
    import runpy
    from mobilityops.services.scenario_intake import ScenarioIntakeService
    from mobilityops.services.solver_service import SolverService
    example = runpy.run_path(str(Path(__file__).parents[1] / "examples/stage7_gemini_acceptance.py"))

    def fake_post(method, body, **kwargs):
        if method == "countTokens":
            return encoded({"totalTokens": 1000})
        request = json.loads(body["input"].rsplit("\n用户数据：\n", 1)[1])
        text = request["messages"][-1]
        index = len(request["messages"]) - 1
        fields, issues = [], []
        def add(name, value):
            fields.append({"field": name, "value": value, "message_index": index, "evidence": text})
        if "提高到30" in text:
            add("truck_capacity", 30)
        elif "场景名称" in text:
            for name, value in scenario.model_dump(mode="json").items():
                if name not in {"solver_runtime_limit_sec", "seed", "broken_bike_proportion"}:
                    add(name, "晚间调度" if name == "scenario_name" else value)
        elif "求解时限5" in text:
            add("solver_runtime_limit_sec", 5.0)
        elif "时间窗" in text:
            issues.append({"field": None, "question": "请撤回未支持的时间窗要求。", "evidence": text, "message_index": index})
        elif "seed" in text:
            add("seed", 42)
        else:
            add("number_of_trucks", 2)
        value = response(steps=[{"type": "model_output", "content": [
            {"type": "text", "text": json.dumps({"fields": fields, "issues": issues})}]}])
        return encoded(value)

    interpreter, transport, folder = setup(settings, config=GeminiBudgetConfig(budget_hkd=10, max_calls=6))
    transport.post.side_effect = fake_post
    solver = SolverService(settings)
    solver.solve_selected = Mock(side_effect=AssertionError("Acceptance must not launch solver"))
    outcomes = example["run_cases"](ScenarioIntakeService(solver, interpreter=interpreter), scenario, folder)
    assert len(outcomes) == 6 and all(item["passed"] for item in outcomes)
    assert transport.post.call_count == 12
    assert (folder / "reviewed-execution-request.json").is_file()
    assert float(json.loads((folder / "ledger.json").read_bytes())["reserved_hkd"]) == pytest.approx(1.31328)
    solver.solve_selected.assert_not_called()
