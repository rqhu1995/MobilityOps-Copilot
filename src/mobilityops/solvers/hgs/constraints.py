"""Deterministic, side-effect-free capability decisions."""

from pydantic import JsonValue

from mobilityops.domain import ScenarioSpec
from mobilityops.solvers.contracts import HGS_CAPABILITY, ConstraintOutcome, ConstraintRecord


def check_hgs_constraints(scenario: ScenarioSpec) -> tuple[ConstraintRecord, ...]:
    cap = HGS_CAPABILITY

    def checked(name: str, value: JsonValue, supported: bool, warning: str | None = None) -> ConstraintRecord:
        return ConstraintRecord(
            constraint_name=name, requested_value=value, supported=supported,
            outcome=ConstraintOutcome.APPLIED if supported else ConstraintOutcome.REJECTED,
            warning=warning,
        )

    records = [
        checked("objective_profile", scenario.objective_profile,
                scenario.objective_profile in cap.supported_objective_profiles,
                "HGS uses its compiled eudf_default objective."),
        checked("solution_requirement", scenario.solution_requirement,
                scenario.solution_requirement in cap.supported_solution_requirements,
                "Both policies use the supplied runtime unchanged; HGS may stop early "
                "on stagnation and does not guarantee optimality."),
        ConstraintRecord(
            constraint_name="seed", requested_value=scenario.seed, supported=cap.supports_seed,
            outcome=ConstraintOutcome.IGNORED if scenario.seed is None else ConstraintOutcome.REJECTED,
            warning="HGS has no seed CLI and seeds from system time; an explicit seed cannot be honored.",
        ),
        checked("broken_bike_proportion", scenario.broken_bike_proportion, cap.supports_broken_bike_proportion,
                "Omit -bprop and preserve instance curBroken." if scenario.broken_bike_proportion is None else
                "HGS derives broken inventory using the supplied proportion."),
        checked("number_of_trucks", scenario.number_of_trucks,
                scenario.number_of_trucks == 1 or cap.supports_multiple_trucks),
        checked("number_of_repairers", scenario.number_of_repairers, cap.supports_repair_routes),
    ]
    for name in ("best_bound", "optimality_gap"):
        records.append(ConstraintRecord(
            constraint_name=name, requested_value=None, supported=False,
            outcome=ConstraintOutcome.IGNORED,
            warning=f"HGS does not report {name}; the result field remains None (not a scenario request).",
        ))
    integer_fields = ("instance_id", "number_of_stations", "number_of_trucks",
                      "number_of_repairers", "truck_capacity", "loading_time_sec", "repair_time_sec")
    oversized = [name for name in integer_fields if getattr(scenario, name) > 2_147_483_647]
    # FileHelper also casts the operation budget to a signed C++ int.
    if scenario.operation_time_budget_sec > 2_147_483_647:
        oversized.append("operation_time_budget_sec")
    if oversized:
        records.append(checked("hgs_numeric_limits", oversized, False,
                               "Values exceed the audited HGS signed 32-bit integer range."))
    return tuple(records)
