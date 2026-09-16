"""Strict SDK result envelope and lossless per-period route projections."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from mobilityops.domain import RepairRoute, RepairStop, ResultStatus, ScenarioSpec, TruckRoute, TruckStop
from mobilityops.solvers.gurobi.contracts import GurobiOptions


class RawGurobiResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    format: Literal["gurobi_raw_v1"]
    status: int = Field(ge=1)
    solution_count: int = Field(ge=0)
    runtime_sec: FiniteFloat = Field(ge=0)
    build_sec: FiniteFloat = Field(ge=0)
    objective_value: FiniteFloat | None
    best_bound: FiniteFloat | None
    optimality_gap: FiniteFloat | None = Field(ge=0)
    variables: dict[str, FiniteFloat]
    gurobi_version: list[int]
    imported_modules: dict[str, str]
    model_variables: int = Field(ge=0)
    model_constraints: int = Field(ge=0)
    feasibility_tolerance: Literal[1e-6]
    integer_tolerance: Literal[1e-5]


def normalized_fields(raw: RawGurobiResult, scenario: ScenarioSpec, options: GurobiOptions) -> dict:
    if raw.status == 2 and raw.solution_count == 0 or raw.status == 3 and raw.solution_count > 0:
        raise ValueError("Contradictory Gurobi status and solution count")
    if not raw.solution_count and (raw.variables or raw.objective_value is not None or raw.optimality_gap is not None):
        raise ValueError("Incumbent attributes supplied without an incumbent")
    status = {2: ResultStatus.SUCCEEDED, 3: ResultStatus.INFEASIBLE, 9: ResultStatus.TIMEOUT}.get(raw.status, ResultStatus.FAILED)
    candidate = raw.status in {2, 9} and raw.solution_count > 0
    fields = dict(status=status, feasible=False if status is ResultStatus.INFEASIBLE else None,
                  runtime_sec=raw.runtime_sec, objective_value=None, dissatisfaction=None, emission=None,
                  best_bound=raw.best_bound if raw.status in {2, 9} else None, optimality_gap=None,
                  truck_routes=(), repair_routes=())
    if not candidate:
        return fields
    if raw.objective_value is None or not raw.variables:
        raise ValueError("Missing Gurobi incumbent objective or variables")
    periods = range(1, int(scenario.operation_time_budget_sec / options.time_interval_sec) + 1)
    nodes = range(scenario.number_of_stations + 1)

    def value(name: str, *key: int) -> float:
        return raw.variables.get(f"{name}[{','.join(map(str, key))}]", 0.0)

    trucks, repairers = [], []
    for k in range(scenario.number_of_trucks):
        stops = []
        for t in periods:
            for i in nodes:
                if any(value("visit_truck", i, j, t, k) > .5 for j in nodes):
                    stops.append(TruckStop(station_id=i,
                        load_usable_bikes=round(value("reb_amount_usable-", i, t, k)),
                        unload_usable_bikes=round(value("reb_amount_usable+", i, t, k)),
                        load_broken_bikes=round(value("reb_amount_broken-", i, t, k)),
                        unload_broken_bikes=round(value("reb_amount_broken+", i, t, k))))
        trucks.append(TruckRoute(truck_id=k, stops=tuple(stops)))
    for r in range(scenario.number_of_repairers):
        stops = []
        for t in periods:
            for i in nodes:
                if any(value("visit_man", i, j, t, r) > .5 for j in nodes):
                    stops.append(RepairStop(station_id=i, repaired_bikes=round(value("repaired_amount_man", i, t, r))))
        repairers.append(RepairRoute(repairer_id=r, stops=tuple(stops)))
    fields.update(feasible=True, objective_value=raw.objective_value,
                  dissatisfaction=sum(value("dissat", i) for i in range(1, scenario.number_of_stations + 1)),
                  emission=sum(v for key, v in raw.variables.items() if key.startswith("emission[")),
                  optimality_gap=raw.optimality_gap, truck_routes=tuple(trucks), repair_routes=tuple(repairers))
    return fields
