from pathlib import Path

import pytest

from mobilityops.domain import ScenarioSpec, SolutionRequirement
from mobilityops.solvers.hgs import build_hgs_argv


@pytest.mark.parametrize("proportion", [None, 0.0, 0.25, 1.0])
@pytest.mark.parametrize("requirement", list(SolutionRequirement))
def test_entire_argv(scenario: ScenarioSpec, proportion: float | None, requirement: SolutionRequirement) -> None:
    scenario = scenario.model_copy(update={"broken_bike_proportion": proportion, "solution_requirement": requirement})
    expected = (
        "/path with spaces/main", "-i", "1", "-ns", "6", "-ntrk", "1", "-nrpm", "1",
        "-vcap", "25", "-tb", "7200.0", "-tl", "5.0", "-ldT", "60", "-rpT", "300",
    )
    if proportion is not None:
        expected += ("-bprop", str(proportion))
    assert build_hgs_argv(Path("/path with spaces/main"), scenario) == expected


def test_all_parameters_follow_a_different_scenario(scenario: ScenarioSpec) -> None:
    scenario = ScenarioSpec.model_validate({**scenario.model_dump(),
        "instance_id": 2, "number_of_stations": 10, "number_of_trucks": 3, "number_of_repairers": 2,
        "truck_capacity": 18, "operation_time_budget_sec": 6000.5, "solver_runtime_limit_sec": 2.5,
        "loading_time_sec": 45, "repair_time_sec": 200, "broken_bike_proportion": 0.3,
    })
    assert build_hgs_argv(Path("/solver/main"), scenario) == (
        "/solver/main", "-i", "2", "-ns", "10", "-ntrk", "3", "-nrpm", "2", "-vcap", "18",
        "-tb", "6000.5", "-tl", "2.5", "-ldT", "45", "-rpT", "200", "-bprop", "0.3",
    )
