import pytest

from mobilityops.config import Settings
from mobilityops.domain import ScenarioSpec, SolutionRequirement
from mobilityops.solvers import ConstraintOutcome, SolverBackend
from mobilityops.solvers.hgs import ConstraintRejectedError, HgsBackend


@pytest.mark.parametrize("seed", [0, 7, 4_294_967_295])
def test_explicit_seed_rejected_before_artifacts_or_execution(settings: Settings, scenario: ScenarioSpec, seed: int) -> None:
    backend = HgsBackend(settings)
    assert isinstance(backend, SolverBackend)
    scenario = scenario.model_copy(update={"seed": seed})
    with pytest.raises(ConstraintRejectedError) as error:
        backend.solve(scenario, run_id="seed-test")
    record = next(r for r in error.value.records if r.constraint_name == "seed")
    assert record.requested_value == seed
    assert record.outcome is ConstraintOutcome.REJECTED
    assert not settings.runs_dir.exists()


@pytest.mark.parametrize("requirement", list(SolutionRequirement))
@pytest.mark.parametrize("proportion", [None, 0.2])
def test_supported_constraints(settings: Settings, scenario: ScenarioSpec, requirement: SolutionRequirement, proportion: float | None) -> None:
    scenario = scenario.model_copy(update={
        "solution_requirement": requirement, "number_of_trucks": 2, "number_of_repairers": 2,
        "broken_bike_proportion": proportion,
    })
    records = {r.constraint_name: r for r in HgsBackend(settings).check_constraints(scenario)}
    assert set(records) == {"objective_profile", "solution_requirement", "seed", "broken_bike_proportion",
                            "number_of_trucks", "number_of_repairers", "best_bound", "optimality_gap"}
    assert not any(r.outcome is ConstraintOutcome.REJECTED for r in records.values())
    assert records["number_of_trucks"].outcome is ConstraintOutcome.APPLIED
    assert records["number_of_repairers"].outcome is ConstraintOutcome.APPLIED
    assert records["best_bound"].supported is records["optimality_gap"].supported is False
    assert scenario.solver_runtime_limit_sec == 5


def test_cpp_numeric_overflow_rejected(settings: Settings, scenario: ScenarioSpec) -> None:
    with pytest.raises(ConstraintRejectedError, match="hgs_numeric_limits"):
        HgsBackend(settings).solve(scenario.model_copy(update={"truck_capacity": 2**31}), run_id="overflow")
