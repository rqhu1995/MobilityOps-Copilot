"""Small contracts shared by solver backend implementations."""

from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from mobilityops.domain.scenario import (
    ObjectiveProfile,
    ScenarioSpec,
    SolutionRequirement,
)
from mobilityops.domain.solution import BackendName, SolutionResult


class ConstraintOutcome(StrEnum):
    """How a backend handled one requested constraint or feature."""

    APPLIED = "applied"
    ADJUSTED = "adjusted"
    IGNORED = "ignored"
    REJECTED = "rejected"


class ConstraintRecord(BaseModel):
    """Traceable result of checking one requested value against a backend."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    constraint_name: str = Field(min_length=1, max_length=120, strict=True)
    requested_value: JsonValue
    supported: bool
    outcome: ConstraintOutcome
    warning: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def reject_impossible_unsupported_outcome(self) -> "ConstraintRecord":
        """Unsupported requests cannot be recorded as applied or adjusted."""

        if not self.supported and self.outcome in {
            ConstraintOutcome.APPLIED,
            ConstraintOutcome.ADJUSTED,
        }:
            raise ValueError("unsupported constraints cannot be applied or adjusted")
        return self


class BackendCapability(BaseModel):
    """Stable, serializable feature declaration for a solver backend."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: BackendName
    supported_objective_profiles: tuple[ObjectiveProfile, ...]
    supported_solution_requirements: tuple[SolutionRequirement, ...]
    supports_seed: bool
    supports_broken_bike_proportion: bool
    supports_multiple_trucks: bool
    supports_repair_routes: bool
    reports_best_bound: bool
    reports_optimality_gap: bool


HGS_CAPABILITY = BackendCapability(
    backend=BackendName.HGS,
    supported_objective_profiles=(ObjectiveProfile.EUDF_DEFAULT,),
    supported_solution_requirements=(
        SolutionRequirement.FAST_FEASIBLE,
        SolutionRequirement.BEST_AVAILABLE,
    ),
    supports_seed=False,
    supports_broken_bike_proportion=True,
    supports_multiple_trucks=True,
    supports_repair_routes=True,
    reports_best_bound=False,
    reports_optimality_gap=False,
)


@runtime_checkable
class SolverBackend(Protocol):
    """Interface implemented later by concrete, side-effecting adapters."""

    @property
    def capability(self) -> BackendCapability:
        """Return the backend's static capability declaration."""

        ...

    def check_constraints(
        self, scenario: ScenarioSpec
    ) -> tuple[ConstraintRecord, ...]:
        """Assess a scenario without running the solver."""

        ...

    def solve(self, scenario: ScenarioSpec, *, run_id: str) -> SolutionResult:
        """Solve a validated scenario and return the normalized result."""

        ...
