"""Validated, backend-neutral scenario input contract."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ObjectiveProfile(StrEnum):
    """Objective formulations accepted by the current domain contract."""

    EUDF_DEFAULT = "eudf_default"
    GUROBI_LINEAR_DEFAULT = "gurobi_linear_default"


class SolutionRequirement(StrEnum):
    """Requested search outcome used by orchestration and backend selection."""

    FAST_FEASIBLE = "fast_feasible"
    BEST_AVAILABLE = "best_available"


class ScenarioSpec(BaseModel):
    """A deterministic scenario contract with explicit backend objective profiles.

    Backend-specific genetic algorithm tuning remains outside this domain model.
    See the HGS and Gurobi adapter contracts for exact mappings and differences.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    scenario_name: str = Field(
        min_length=1,
        max_length=120,
        strict=True,
        description="Stable human-readable scenario label; not passed to HGS.",
    )
    instance_id: int = Field(
        gt=0,
        strict=True,
        description="Instance folder suffix mapped to HGS -i/--inst_no.",
    )
    number_of_stations: int = Field(
        gt=0,
        strict=True,
        description="Number of non-depot stations mapped to HGS -ns.",
    )
    number_of_trucks: int = Field(
        gt=0,
        strict=True,
        description="Truck count mapped to HGS -ntrk.",
    )
    number_of_repairers: int = Field(
        gt=0,
        strict=True,
        description="On-site repairer count mapped to HGS -nrpm.",
    )
    truck_capacity: int = Field(
        gt=0,
        strict=True,
        description="Bike capacity per truck mapped to HGS -vcap.",
    )
    operation_time_budget_sec: float = Field(
        gt=0,
        allow_inf_nan=False,
        strict=True,
        description="Route operation budget in seconds mapped to HGS -tb.",
    )
    solver_runtime_limit_sec: float = Field(
        gt=0,
        allow_inf_nan=False,
        strict=True,
        description="Requested solver search limit in seconds; enforced by the selected adapter.",
    )
    loading_time_sec: int = Field(
        gt=0,
        strict=True,
        description="Per-bike loading/unloading time in seconds mapped to HGS -ldT.",
    )
    repair_time_sec: int = Field(
        gt=0,
        strict=True,
        description="Per-bike repair time in seconds mapped to HGS -rpT.",
    )
    broken_bike_proportion: float | None = Field(
        default=None,
        ge=0,
        le=1,
        allow_inf_nan=False,
        strict=True,
        description=(
            "Optional ratio mapped to HGS -bprop; None preserves broken-bike "
            "inventories from the instance data."
        ),
    )
    seed: int | None = Field(
        default=None,
        ge=0,
        le=4_294_967_295,
        strict=True,
        description=(
            "Optional 32-bit orchestration seed; the current HGS CLI cannot "
            "accept it; the first Gurobi adapter also rejects explicit seed requests."
        ),
    )
    objective_profile: ObjectiveProfile = Field(
        description="Explicit formulation: HGS eudf_default or Gurobi gurobi_linear_default."
    )
    solution_requirement: SolutionRequirement = Field(
        description="Requested outcome policy used by orchestration."
    )
