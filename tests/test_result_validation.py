from pathlib import Path

import pytest

from mobilityops.domain import RepairRoute, RepairStop, ResultStatus, ScenarioSpec, SolutionResult, TruckRoute, TruckStop
from mobilityops.services.validation import ResultValidationError, validate_result


def test_validation_adds_honest_warnings_idempotently(scenario: ScenarioSpec, valid_result: tuple[SolutionResult, Path]) -> None:
    result, run_dir = valid_result
    validated = validate_result(scenario, result, run_dir=run_dir)
    assert validated == validate_result(scenario, validated, run_dir=run_dir)
    assert validated.solver_metadata["validation"]["status"] == "passed"
    assert any("atomic inventory changes at arrival" in w for w in validated.warnings)


@pytest.mark.parametrize(("updates", "message"), [
    ({"objective_value": None}, "objective_value"),
    ({"runtime_sec": None}, "runtime_sec"),
    ({"dissatisfaction": None}, "dissatisfaction"),
    ({"emission": None}, "emission"),
    ({"runtime_sec": float("nan")}, "Invalid result model"),
    ({"best_bound": 1.0}, "best_bound"),
    ({"optimality_gap": 0.1}, "optimality_gap"),
    ({"feasible": False}, "Invalid result model"),
    ({"status": ResultStatus.INFEASIBLE, "feasible": True}, "Invalid result model"),
    ({"truck_routes": ()}, "one truck block"),
    ({"repair_routes": ()}, "one repairer block"),
    ({"truck_routes": (TruckRoute(truck_id=0, stops=(TruckStop(station_id=0), TruckStop(station_id=7), TruckStop(station_id=0))),)}, "station ID"),
    ({"repair_routes": (RepairRoute(repairer_id=0, stops=(RepairStop(station_id=0), RepairStop(station_id=7), RepairStop(station_id=0))),)}, "station ID"),
    ({"truck_routes": (TruckRoute(truck_id=1, stops=(TruckStop(station_id=0), TruckStop(station_id=0))),)}, "resources"),
    ({"repair_routes": (RepairRoute(repairer_id=1, stops=(RepairStop(station_id=0), RepairStop(station_id=0))),)}, "resources"),
    ({"truck_routes": (TruckRoute(truck_id=0, stops=(TruckStop(station_id=1), TruckStop(station_id=0))),)}, "depot"),
    ({"truck_routes": (TruckRoute(truck_id=0, stops=(TruckStop(station_id=0, load_usable_bikes=26), TruckStop(station_id=0, unload_usable_bikes=26))),)}, "capacity"),
    ({"truck_routes": (TruckRoute(truck_id=0, stops=(TruckStop(station_id=0, load_usable_bikes=1), TruckStop(station_id=0))),)}, "nonzero inventory"),
    ({"truck_routes": (TruckRoute(truck_id=0, stops=(TruckStop(station_id=0), TruckStop(station_id=0, unload_broken_bikes=1))),)}, "unloads unavailable"),
])
def test_invalid_results(scenario: ScenarioSpec, valid_result: tuple[SolutionResult, Path], updates: dict, message: str) -> None:
    result, run_dir = valid_result
    with pytest.raises(ResultValidationError, match=message):
        validate_result(scenario, result.model_copy(update=updates), run_dir=run_dir)


@pytest.mark.parametrize("field", ["truck_routes", "repair_routes"])
def test_duplicate_ids(scenario: ScenarioSpec, valid_result: tuple[SolutionResult, Path], field: str) -> None:
    result, run_dir = valid_result
    with pytest.raises(ResultValidationError, match="Duplicate"):
        validate_result(scenario, result.model_copy(update={field: getattr(result, field) * 2}), run_dir=run_dir)


@pytest.mark.parametrize("quantity", ["load_usable_bikes", "load_broken_bikes", "unload_usable_bikes", "unload_broken_bikes", "repaired_bikes"])
def test_negative_quantities_cannot_bypass_validation(scenario: ScenarioSpec, valid_result: tuple[SolutionResult, Path], quantity: str) -> None:
    result, run_dir = valid_result
    if quantity == "repaired_bikes":
        routes = (RepairRoute(repairer_id=0, stops=(RepairStop.model_construct(station_id=1, repaired_bikes=-1),)),)
        field = "repair_routes"
    else:
        routes = (TruckRoute(truck_id=0, stops=(TruckStop.model_construct(station_id=0, **{quantity: -1}),)),)
        field = "truck_routes"
    with pytest.raises(ResultValidationError, match="Invalid result model"):
        validate_result(scenario, result.model_copy(update={field: routes}), run_dir=run_dir)


@pytest.mark.parametrize("kind", ["foreign", "symlink", "relative", "missing", "missing-role", "wrong-run"])
def test_artifact_ownership(scenario: ScenarioSpec, valid_result: tuple[SolutionResult, Path], kind: str, tmp_path: Path) -> None:
    result, run_dir = valid_result
    paths = result.raw_artifact_paths.copy()
    foreign = tmp_path / "other-run.txt"
    foreign.write_text("other")
    if kind == "foreign":
        paths["solver_result"] = foreign
    elif kind == "symlink":
        paths["solver_result"].unlink()
        paths["solver_result"].symlink_to(foreign)
    elif kind == "relative":
        paths["solver_result"] = Path("solver-result.txt")
    elif kind == "missing":
        paths["solver_result"].unlink()
    elif kind == "missing-role":
        del paths["solver_result"]
    elif kind == "wrong-run":
        run_dir = run_dir / "different"
    with pytest.raises(ResultValidationError):
        validate_result(scenario, result.model_copy(update={"raw_artifact_paths": paths}), run_dir=run_dir)


@pytest.mark.parametrize(("status", "feasible"), [("failed", None), ("timeout", None), ("infeasible", False)])
def test_unsuccessful_status_can_have_no_solution(scenario: ScenarioSpec, valid_result: tuple[SolutionResult, Path], status: str, feasible: bool | None) -> None:
    result, run_dir = valid_result
    failed = SolutionResult(run_id=result.run_id, backend="hgs", status=status, feasible=feasible, raw_artifact_paths=result.raw_artifact_paths)
    assert validate_result(scenario, failed, run_dir=run_dir) == failed
