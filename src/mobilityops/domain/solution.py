"""Backend-neutral solver result and route contracts."""

from enum import StrEnum
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


def validate_run_id(value: str) -> str:
    """Require one safe path component, without normalizing user input."""

    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", value):
        raise ValueError(
            "run_id must start with an ASCII letter or digit and contain only "
            "A-Z, a-z, 0-9, '.', '_', '-'; maximum 200 characters"
        )
    return value


class BackendName(StrEnum):
    """Known solver backend identifiers."""

    HGS = "hgs"
    GUROBI = "gurobi"


class ResultStatus(StrEnum):
    """Lifecycle and terminal states shared by all backends."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    INFEASIBLE = "infeasible"
    TIMEOUT = "timeout"
    FAILED = "failed"


class TruckStop(BaseModel):
    """One station operation in a truck route; station 0 is the depot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    station_id: int = Field(ge=0, strict=True)
    load_usable_bikes: int = Field(default=0, ge=0, strict=True)
    load_broken_bikes: int = Field(default=0, ge=0, strict=True)
    unload_usable_bikes: int = Field(default=0, ge=0, strict=True)
    unload_broken_bikes: int = Field(default=0, ge=0, strict=True)


class TruckRoute(BaseModel):
    """Ordered operations assigned to one truck."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    truck_id: int = Field(ge=0, strict=True)
    stops: tuple[TruckStop, ...] = ()


class RepairStop(BaseModel):
    """One station operation in a repairer route; station 0 is the depot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    station_id: int = Field(ge=0, strict=True)
    repaired_bikes: int = Field(default=0, ge=0, strict=True)


class RepairRoute(BaseModel):
    """Ordered operations assigned to one repairer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repairer_id: int = Field(ge=0, strict=True)
    stops: tuple[RepairStop, ...] = ()


class SolutionResult(BaseModel):
    """Normalized result without inventing metrics absent from a backend."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    run_id: str = Field(min_length=1, max_length=200, strict=True)
    backend: BackendName
    status: ResultStatus
    feasible: bool | None = Field(
        default=None,
        description="None means feasibility was not established.",
    )
    objective_value: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    dissatisfaction: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    emission: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    runtime_sec: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    best_bound: float | None = Field(
        default=None,
        allow_inf_nan=False,
        description="Optional exact-solver bound; current HGS does not report it.",
    )
    optimality_gap: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
        description="Optional non-negative fractional gap, not a percentage.",
    )
    truck_routes: tuple[TruckRoute, ...] = ()
    repair_routes: tuple[RepairRoute, ...] = ()
    warnings: tuple[str, ...] = ()
    raw_artifact_paths: dict[str, Path] = Field(default_factory=dict)
    solver_metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("run_id", mode="before")
    @classmethod
    def require_safe_run_id(cls, value: str) -> str:
        return validate_run_id(value)

    @model_validator(mode="after")
    def validate_terminal_feasibility(self) -> "SolutionResult":
        """Keep terminal status and known feasibility logically consistent."""

        if self.status is ResultStatus.SUCCEEDED and self.feasible is not True:
            raise ValueError("succeeded results must be feasible")
        if self.status is ResultStatus.INFEASIBLE and self.feasible is not False:
            raise ValueError("infeasible results must set feasible to false")
        return self
