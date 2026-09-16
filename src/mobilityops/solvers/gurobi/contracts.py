"""Explicit Gurobi options, capabilities and audited source identity."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mobilityops.domain import BackendName, ObjectiveProfile, ScenarioSpec, SolutionRequirement
from mobilityops.solvers.contracts import BackendCapability, ConstraintOutcome, ConstraintRecord


class GurobiOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    python_executable: Path
    time_interval_sec: int = Field(default=600, gt=0, strict=True)
    threads: int = Field(default=1, gt=0, le=64, strict=True)
    timeout_grace_sec: float = Field(default=5.0, gt=0, le=60, allow_inf_nan=False)

    @field_validator("python_executable")
    @classmethod
    def absolute_python(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("python_executable must be absolute")
        return value


GUROBI_CAPABILITY = BackendCapability(
    backend=BackendName.GUROBI,
    supported_objective_profiles=(ObjectiveProfile.GUROBI_LINEAR_DEFAULT,),
    supported_solution_requirements=tuple(SolutionRequirement),
    supports_seed=False, supports_broken_bike_proportion=False,
    supports_multiple_trucks=True, supports_repair_routes=True,
    reports_best_bound=True, reports_optimality_gap=True,
)

# Source hashes only, never solver source. Changing the model requires a new audit.
AUDITED_SOURCES = {
    "model/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "model/objective.py": "97d4a4c3751d47ccad55232e0f6a655d01683c4e85f34ff6af7fac494f96ffb3",
    "model/constraints.py": "b80e8d525687614e81d917df981c459ccf1cd8f51051cd6c5c51fe2e27485bfb",
    "model/variables.py": "ef4b333d447eeb60ecf28190c365263bb3e9ad01e0876eee47959963c68e86f0",
    "parameters/__init__.py": "01ba4719c80b6fe911b091a7c05124b64eeece964e09c058ef8f9805daca546b",
    "parameters/dataloader.py": "01b17026a58bc3328e9cecffbb378e8f8a448293349435a9d7f09edd2f9595a9",
    "parameters/sets.py": "4359e7b5e33541df5593282e81c8be1d44668a754abf47f34a3bf5414d6ba723",
    "parameters/config.py": "1f9836f64d6418854a708ff96dcd8c34b5bd53aba3478a19d0e7f95db638c48a",
}


class GurobiPreflightError(ValueError):
    """Configuration or input rejected before optimization."""


class GurobiConstraintError(ValueError):
    def __init__(self, records: tuple[ConstraintRecord, ...]) -> None:
        self.records = records
        super().__init__("Gurobi constraints rejected: " + "; ".join(
            r.constraint_name for r in records if r.outcome is ConstraintOutcome.REJECTED
        ))


def check_constraints(scenario: ScenarioSpec, options: GurobiOptions) -> tuple[ConstraintRecord, ...]:
    checks = [
        ("objective_profile", scenario.objective_profile, scenario.objective_profile is ObjectiveProfile.GUROBI_LINEAR_DEFAULT,
         "Requires explicit gurobi_linear_default; HGS scores/bounds are not interchangeable."),
        ("solution_requirement", scenario.solution_requirement, True, "Keep the supplied runtime; no first-solution stop."),
        ("seed", scenario.seed, scenario.seed is None, "Explicit seeds are not supported by this adapter version."),
        ("broken_bike_proportion", scenario.broken_bike_proportion, scenario.broken_bike_proportion is None,
         "Use original curBroken; no implicit input transformation."),
        ("time_grid", scenario.operation_time_budget_sec,
         scenario.operation_time_budget_sec % options.time_interval_sec == 0,
         "Operation budget must be an exact multiple of the explicit time interval."),
        ("number_of_trucks", scenario.number_of_trucks, True, "One time-indexed route per truck."),
        ("number_of_repairers", scenario.number_of_repairers, True, "Repairers enabled; positive resource counts."),
        ("best_bound", None, True, "Only a finite bound reported for this Gurobi model."),
        ("optimality_gap", None, True, "Only the SDK's finite fractional gap for an incumbent."),
    ]
    # Bound materialization costs before creating large Cartesian products.
    periods = scenario.operation_time_budget_sec / options.time_interval_sec
    estimate = (scenario.number_of_stations + 1) ** 2 * int(periods) * (4 * scenario.number_of_trucks + scenario.number_of_repairers)
    checks.append(("model_size", estimate if estimate < 1e100 else "oversized",
                   2 <= periods <= 1000 and estimate <= 100_000,
                   "This adapter accepts 2..1000 periods and at most 100000 arc-variable slots."))
    return tuple(ConstraintRecord(
        constraint_name=name, requested_value=value, supported=ok,
        outcome=ConstraintOutcome.IGNORED if name in {"seed", "broken_bike_proportion"} and ok
        else ConstraintOutcome.APPLIED if ok else ConstraintOutcome.REJECTED,
        warning=warning,
    ) for name, value, ok, warning in checks)


def solver_argv(scenario: ScenarioSpec, options: GurobiOptions) -> list[str]:
    return ["mobilityops-gurobi", "-S", str(scenario.number_of_stations), "-i", str(scenario.instance_id),
            "-K", str(scenario.number_of_trucks), "-R", str(scenario.number_of_repairers),
            "-NT", str(int(scenario.operation_time_budget_sec / options.time_interval_sec)),
            "-tau", str(options.time_interval_sec), "-Q", str(scenario.truck_capacity),
            "-tl", str(scenario.loading_time_sec), "-tr", str(scenario.repair_time_sec), "-r", "True"]
