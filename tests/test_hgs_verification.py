import hashlib
from pathlib import Path

import pytest

from mobilityops.domain import RepairRoute, RepairStop, ScenarioSpec, SolutionResult, TruckRoute, TruckStop
from mobilityops.services.validation import ResultValidationError, validate_result


@pytest.fixture
def tiny_case(tmp_path: Path, scenario: ScenarioSpec) -> tuple[ScenarioSpec, SolutionResult, Path]:
    """Hand-computable two-station case, independent of the production HGS fixture."""
    scenario = scenario.model_copy(update={
        "number_of_stations": 2, "truck_capacity": 3, "operation_time_budget_sec": 100.0,
        "loading_time_sec": 2, "repair_time_sec": 3,
    })
    run = tmp_path / "tiny"
    instance = run / "Instances/2_1"
    instance.mkdir(parents=True)
    table = "10 12 14 16\n9 11 13 0\n8 10 0 0\n7 0 0 0\n"
    inputs = {
        "station_info_2.txt": "station_id capacity curUsable targetUsable curBroken\n101 3 0 2 1\n205 3 2 1 0\n",
        "time_matrix_2.txt": "0 10 20\n30 0 5\n40 6 0\n",
        "dissat_table_1.txt": table, "dissat_table_2.txt": table,
    }
    for name, data in inputs.items():
        (instance / name).write_text(data)
    paths = {role: run / filename for role, filename in (
        ("scenario", "scenario.json"), ("command", "command.json"),
        ("stdout", "stdout.log"), ("stderr", "stderr.log"), ("solver_result", "solver-result.txt"),
    )}
    for path in paths.values():
        path.write_text("synthetic test evidence")
    paths["scenario"].write_text(scenario.model_dump_json())
    result = SolutionResult(
        run_id="tiny", backend="hgs", status="succeeded", feasible=True,
        objective_value=34.0152, dissatisfaction=17.0, emission=0.253496, runtime_sec=0.05,
        truck_routes=(TruckRoute(truck_id=0, stops=(
            TruckStop(station_id=0, load_usable_bikes=1), TruckStop(station_id=1, unload_usable_bikes=1),
            TruckStop(station_id=2, load_usable_bikes=1), TruckStop(station_id=0, unload_usable_bikes=1),
        )),),
        repair_routes=(RepairRoute(repairer_id=0, stops=(
            RepairStop(station_id=0), RepairStop(station_id=1, repaired_bikes=1), RepairStop(station_id=0),
        )),),
        raw_artifact_paths=paths,
        solver_metadata={
            "input_sha256": {name: hashlib.sha256(data.encode()).hexdigest() for name, data in inputs.items()},
            "reported_metrics": {"truck_travel_sec": 55.0, "repair_travel_sec": 67.2,
                                 "truck_operation_sec": 8.0, "repair_operation_sec": 3.0},
            "station_dissatisfaction": {"1": 8.0, "2": 9.0},
        },
    )
    return scenario, result, run


def change_scenario(scenario: ScenarioSpec, run: Path, **updates: object) -> ScenarioSpec:
    scenario = ScenarioSpec.model_validate({**scenario.model_dump(), **updates})
    (run / "scenario.json").write_text(scenario.model_dump_json())
    return scenario


def change_input(result: SolutionResult, run: Path, name: str, text: str) -> SolutionResult:
    path = run / "Instances/2_1" / name
    path.write_text(text)
    hashes = {**result.solver_metadata["input_sha256"], name: hashlib.sha256(path.read_bytes()).hexdigest()}
    return result.model_copy(update={"solver_metadata": {**result.solver_metadata, "input_sha256": hashes}})


def test_hand_calculated_metrics_and_timeline(tiny_case: tuple[ScenarioSpec, SolutionResult, Path]) -> None:
    scenario, result, run = tiny_case
    verified = validate_result(scenario, result, run_dir=run)
    report = verified.solver_metadata["validation"]
    assert report["status"] == "passed"
    metrics = report["recomputed_metrics"]
    assert metrics["truck_travel_sec"] == 55
    assert metrics["truck_operation_sec"] == 8
    assert metrics["repair_travel_sec"] == pytest.approx(67.2)
    assert metrics["repair_operation_sec"] == 3
    # Loaded legs 10 + 40, empty leg 5; independent hand calculation.
    assert metrics["emission"] == pytest.approx(0.25349625)
    assert metrics["dissatisfaction"] == 17
    assert metrics["objective_value"] == pytest.approx(34.015211107)
    truck, repairer = report["route_timings"]
    assert truck["arrivals_sec"] == [0, 12, 19, 61]
    assert truck["departures_sec"] == [2, 14, 21, 63]
    assert repairer["completion_sec"] == pytest.approx(70.2)
    station = report["stations"][0]
    assert station["external_id"] == 101
    assert station["final_usable"] == 2 and station["final_broken"] == 0
    assert [event["arrival_min_sec"] for event in station["timeline"]] == [12, 16.8]
    assert validate_result(scenario, verified, run_dir=run) == verified
    assert SolutionResult.model_validate_json(verified.model_dump_json()) == verified


@pytest.mark.parametrize("field,value", [("objective_value", 34.02), ("emission", 0.254), ("dissatisfaction", 17.01)])
def test_changed_metrics_fail(tiny_case: tuple[ScenarioSpec, SolutionResult, Path], field: str, value: float) -> None:
    scenario, result, run = tiny_case
    with pytest.raises(ResultValidationError, match=f"Metric {field} mismatch"):
        validate_result(scenario, result.model_copy(update={field: value}), run_dir=run)


@pytest.mark.parametrize("field", ["truck_travel_sec", "repair_travel_sec", "truck_operation_sec", "repair_operation_sec"])
def test_changed_aggregate_times_fail(tiny_case: tuple[ScenarioSpec, SolutionResult, Path], field: str) -> None:
    scenario, result, run = tiny_case
    totals = {**result.solver_metadata["reported_metrics"], field: 999.0}
    result = result.model_copy(update={"solver_metadata": {**result.solver_metadata, "reported_metrics": totals}})
    with pytest.raises(ResultValidationError, match=f"Metric {field} mismatch"):
        validate_result(scenario, result, run_dir=run)


@pytest.mark.parametrize("values", [{"1": 7.0, "2": 9.0}, {"1": 8.0}, {"1": 8.0, "3": 9.0}, {"1": True, "2": 9.0}])
def test_station_metric_mismatch(tiny_case: tuple[ScenarioSpec, SolutionResult, Path], values: dict) -> None:
    scenario, result, run = tiny_case
    result = result.model_copy(update={"solver_metadata": {**result.solver_metadata, "station_dissatisfaction": values}})
    with pytest.raises(ResultValidationError, match="station|numeric"):
        validate_result(scenario, result, run_dir=run)


def test_missing_optional_metrics_are_recomputed_with_warnings(tiny_case: tuple[ScenarioSpec, SolutionResult, Path]) -> None:
    scenario, result, run = tiny_case
    result = result.model_copy(update={"solver_metadata": {"input_sha256": result.solver_metadata["input_sha256"]}})
    verified = validate_result(scenario, result, run_dir=run)
    assert verified.solver_metadata["validation"]["recomputed_metrics"]["dissatisfaction"] == 17
    assert any("could not be cross-checked" in warning for warning in verified.warnings)


@pytest.mark.parametrize("budget,expected", [(70.2, None), (70.19, "repairer"), (62.0, "truck")])
def test_route_budget_includes_final_depot_service(tiny_case: tuple[ScenarioSpec, SolutionResult, Path], budget: float, expected: str | None) -> None:
    scenario, result, run = tiny_case
    scenario = change_scenario(scenario, run, operation_time_budget_sec=budget)
    if expected:
        with pytest.raises(ResultValidationError, match=expected + ".*completion"):
            validate_result(scenario, result, run_dir=run)
    else:
        assert validate_result(scenario, result, run_dir=run).solver_metadata["validation"]["status"] == "passed"


@pytest.mark.parametrize("kind", ["usable-shortage", "broken-shortage", "station-capacity"])
def test_station_inventory_violations(tiny_case: tuple[ScenarioSpec, SolutionResult, Path], kind: str) -> None:
    scenario, result, run = tiny_case
    path = run / "Instances/2_1/station_info_2.txt"
    text = path.read_text()
    if kind == "usable-shortage":
        text = text.replace("205 3 2 1 0", "205 3 0 1 0")
    elif kind == "broken-shortage":
        text = text.replace("101 3 0 2 1", "101 3 0 2 0")
    else:
        text = text.replace("101 3 0 2 1", "101 3 2 2 1")
    result = change_input(result, run, path.name, text)
    with pytest.raises(ResultValidationError, match="inventory becomes negative or exceeds capacity"):
        validate_result(scenario, result, run_dir=run)


def test_early_inventory_violation_not_hidden_by_later_repair(tiny_case: tuple[ScenarioSpec, SolutionResult, Path]) -> None:
    scenario, result, run = tiny_case
    # Truck collects one usable bike before repair creates it; final inventory would be legal.
    result = result.model_copy(update={"truck_routes": (TruckRoute(truck_id=0, stops=(
        TruckStop(station_id=0), TruckStop(station_id=1, load_usable_bikes=1),
        TruckStop(station_id=0, unload_usable_bikes=1),
    )),)})
    with pytest.raises(ResultValidationError, match="Station 1 at 10.0 sec: inventory"):
        validate_result(scenario, result, run_dir=run)


def test_simultaneous_events_accept_only_order_independent_inventory(tiny_case: tuple[ScenarioSpec, SolutionResult, Path]) -> None:
    scenario, result, run = tiny_case
    # 1.68 * 25 == 42: initial load time 17 makes both visits arrive at 42.
    scenario = change_scenario(scenario, run, loading_time_sec=17, operation_time_budget_sec=200.0)
    result = change_input(result, run, "time_matrix_2.txt", "0 25 20\n30 0 5\n40 6 0\n")
    # Metrics independently derived from these changed routes.
    result = result.model_copy(update={
        "emission": 0.322639, "objective_value": 34.0194,
        "solver_metadata": {"input_sha256": result.solver_metadata["input_sha256"]},
    })
    verified = validate_result(scenario, result, run_dir=run)
    timeline = verified.solver_metadata["validation"]["stations"][0]["timeline"]
    assert timeline[0]["resources"] == ["repairer:0", "truck:0"]
    assert timeline[0]["all_interleavings_valid"] is True


def test_order_dependent_simultaneous_events_are_not_assumed_feasible(tiny_case: tuple[ScenarioSpec, SolutionResult, Path]) -> None:
    scenario, result, run = tiny_case
    # Zero travel makes collection and repair simultaneous at station 1.
    result = change_input(result, run, "time_matrix_2.txt", "0 0 20\n30 0 5\n40 6 0\n")
    result = result.model_copy(update={"truck_routes": (TruckRoute(truck_id=0, stops=(
        TruckStop(station_id=0), TruckStop(station_id=1, load_usable_bikes=1),
        TruckStop(station_id=0, unload_usable_bikes=1),
    )),)})
    with pytest.raises(ResultValidationError, match="simultaneous-event order is ambiguous"):
        validate_result(scenario, result, run_dir=run)


def test_idle_extra_resources_and_repeated_depots(tiny_case: tuple[ScenarioSpec, SolutionResult, Path]) -> None:
    scenario, result, run = tiny_case
    scenario = change_scenario(scenario, run, number_of_trucks=2, number_of_repairers=2)
    result = result.model_copy(update={
        "truck_routes": result.truck_routes + (TruckRoute(truck_id=1, stops=(TruckStop(station_id=0), TruckStop(station_id=0))),),
        "repair_routes": result.repair_routes + (RepairRoute(repairer_id=1, stops=(RepairStop(station_id=0), RepairStop(station_id=0))),),
    })
    verified = validate_result(scenario, result, run_dir=run)
    timings = verified.solver_metadata["validation"]["route_timings"]
    assert len(timings) == 4
    assert sum(timing["completion_sec"] == 0 for timing in timings) == 2


def test_two_active_trucks_share_station_inventory(tiny_case: tuple[ScenarioSpec, SolutionResult, Path]) -> None:
    scenario, result, run = tiny_case
    scenario = change_scenario(scenario, run, number_of_trucks=2)
    result = result.model_copy(update={
        "truck_routes": tuple(TruckRoute(truck_id=i, stops=(
            TruckStop(station_id=0), TruckStop(station_id=2, load_usable_bikes=1),
            TruckStop(station_id=0, unload_usable_bikes=1),
        )) for i in range(2)),
        "repair_routes": (RepairRoute(repairer_id=0, stops=(RepairStop(station_id=0), RepairStop(station_id=0))),),
        "dissatisfaction": 22.0, "emission": 0.552923, "objective_value": 44.0332,
        "solver_metadata": {"input_sha256": result.solver_metadata["input_sha256"]},
    })
    report = validate_result(scenario, result, run_dir=run).solver_metadata["validation"]
    assert report["stations"][1]["final_usable"] == 0
    assert report["stations"][1]["timeline"][0]["resources"] == ["truck:0", "truck:1"]
    path = run / "Instances/2_1/station_info_2.txt"
    result = change_input(result, run, path.name, path.read_text().replace("205 3 2 1 0", "205 3 1 1 0"))
    with pytest.raises(ResultValidationError, match="inventory becomes negative"):
        validate_result(scenario, result, run_dir=run)


@pytest.mark.parametrize("reported", [55.00004, 55.00006])
def test_six_significant_digit_tolerance_is_not_a_loose_percentage(tiny_case: tuple[ScenarioSpec, SolutionResult, Path], reported: float) -> None:
    scenario, result, run = tiny_case
    totals = {**result.solver_metadata["reported_metrics"], "truck_travel_sec": reported}
    result = result.model_copy(update={"solver_metadata": {**result.solver_metadata, "reported_metrics": totals}})
    if reported == 55.00004:
        assert validate_result(scenario, result, run_dir=run).solver_metadata["validation"]["status"] == "passed"
    else:
        with pytest.raises(ResultValidationError, match="truck_travel_sec mismatch"):
            validate_result(scenario, result, run_dir=run)


@pytest.mark.parametrize("kind", ["broken-loading", "depot-repair"])
def test_depot_semantics(tiny_case: tuple[ScenarioSpec, SolutionResult, Path], kind: str) -> None:
    scenario, result, run = tiny_case
    if kind == "broken-loading":
        result = result.model_copy(update={"truck_routes": (TruckRoute(truck_id=0, stops=(
            TruckStop(station_id=0, load_broken_bikes=1), TruckStop(station_id=0, unload_broken_bikes=1),
        )),)})
    else:
        result = result.model_copy(update={"repair_routes": (RepairRoute(repairer_id=0, stops=(
            RepairStop(station_id=0, repaired_bikes=1), RepairStop(station_id=0),
        )),)})
    with pytest.raises(ResultValidationError, match="[Dd]epot"):
        validate_result(scenario, result, run_dir=run)


def test_fake_passed_report_cannot_bypass_recomputation(tiny_case: tuple[ScenarioSpec, SolutionResult, Path]) -> None:
    scenario, result, run = tiny_case
    result = result.model_copy(update={"objective_value": 0.0, "solver_metadata": {
        **result.solver_metadata, "validation": {"status": "passed", "verifier": "hgs-math-v1"},
    }})
    with pytest.raises(ResultValidationError, match="objective_value mismatch"):
        validate_result(scenario, result, run_dir=run)


def test_missing_hash_evidence_is_not_silently_skipped(tiny_case: tuple[ScenarioSpec, SolutionResult, Path]) -> None:
    scenario, result, run = tiny_case
    result = result.model_copy(update={"solver_metadata": {}})
    with pytest.raises(ResultValidationError, match="input_sha256"):
        validate_result(scenario, result, run_dir=run)
