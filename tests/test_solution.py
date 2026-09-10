from pathlib import Path

import pytest
from pydantic import ValidationError

from mobilityops.domain import (
    BackendName,
    RepairRoute,
    RepairStop,
    ResultStatus,
    SolutionResult,
    TruckRoute,
    TruckStop,
)


def test_solution_result_serialization_round_trip() -> None:
    result = SolutionResult(
        run_id="run-6-1-001",
        backend=BackendName.HGS,
        status=ResultStatus.SUCCEEDED,
        feasible=True,
        objective_value=484.538,
        dissatisfaction=241.776,
        emission=16.4385,
        runtime_sec=5.0,
        best_bound=None,
        optimality_gap=None,
        truck_routes=(
            TruckRoute(
                truck_id=0,
                stops=(
                    TruckStop(station_id=0, load_usable_bikes=10),
                    TruckStop(station_id=1, unload_usable_bikes=10),
                    TruckStop(station_id=0),
                ),
            ),
        ),
        repair_routes=(
            RepairRoute(
                repairer_id=0,
                stops=(RepairStop(station_id=2, repaired_bikes=1),),
            ),
        ),
        warnings=("representative parser fixture",),
        raw_artifact_paths={
            "raw_log": Path("runs/run-6-1-001/raw.log"),
            "solver_result": Path("runs/run-6-1-001/hgs-result.txt"),
        },
        solver_metadata={
            "result_format": "hgs_text",
            "seed_supported": False,
        },
    )

    payload = result.model_dump(mode="json")
    restored = SolutionResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert payload["backend"] == "hgs"
    assert payload["status"] == "succeeded"
    assert payload["best_bound"] is None
    assert payload["optimality_gap"] is None
    assert payload["truck_routes"][0]["stops"][1]["station_id"] == 1
    assert payload["raw_artifact_paths"]["raw_log"].endswith("raw.log")


def test_terminal_status_requires_consistent_feasibility() -> None:
    with pytest.raises(ValidationError):
        SolutionResult(
            run_id="bad-success",
            backend=BackendName.HGS,
            status=ResultStatus.SUCCEEDED,
            feasible=None,
        )

    with pytest.raises(ValidationError):
        SolutionResult(
            run_id="bad-infeasible",
            backend=BackendName.HGS,
            status=ResultStatus.INFEASIBLE,
            feasible=True,
        )
