"""Pure mapping from scenario fields to the audited HGS CLI."""

from pathlib import Path

from mobilityops.domain import ScenarioSpec


def build_hgs_argv(executable: Path, scenario: ScenarioSpec) -> tuple[str, ...]:
    argv = (
        str(executable),
        "-i", str(scenario.instance_id),
        "-ns", str(scenario.number_of_stations),
        "-ntrk", str(scenario.number_of_trucks),
        "-nrpm", str(scenario.number_of_repairers),
        "-vcap", str(scenario.truck_capacity),
        "-tb", str(scenario.operation_time_budget_sec),
        "-tl", str(scenario.solver_runtime_limit_sec),
        "-ldT", str(scenario.loading_time_sec),
        "-rpT", str(scenario.repair_time_sec),
    )
    if scenario.broken_bike_proportion is not None:
        argv += ("-bprop", str(scenario.broken_bike_proportion))
    return argv
