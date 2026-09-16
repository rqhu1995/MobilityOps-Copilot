"""Independent recomputation under the audited HGS arrival-event semantics."""

from collections import defaultdict
from dataclasses import dataclass
import math

from pydantic import JsonValue

from mobilityops.domain import ScenarioSpec, SolutionResult
from mobilityops.services.hgs_instance import HgsInstance


TIME_EPS_SEC = 1e-7
VERIFIER_VERSION = "hgs-math-v1"
MODEL_WARNING = (
    "Independent verification uses HGS atomic inventory changes at arrival, fixed travel times and no waiting. "
    "It does not model within-service transfers, repair completion availability, road uncertainty or prove optimality."
)


class HgsVerificationError(ValueError):
    """A candidate contradicts the inputs, or its event order cannot be established."""


@dataclass(frozen=True)
class _Event:
    station: int
    arrival: float
    resource: str
    stop_index: int
    usable_delta: int
    broken_delta: int


@dataclass(frozen=True)
class HgsVerification:
    report: dict[str, JsonValue]
    warnings: tuple[str, ...]


def _printed_tolerance(expected: float) -> float:
    """Half a unit at six significant digits, plus a small binary-arithmetic margin."""

    quantum = 0.0 if expected == 0 else 0.5 * 10.0 ** (math.floor(math.log10(abs(expected))) - 5)
    return quantum + 64 * math.ulp(expected)


def verify_hgs_solution(
    scenario: ScenarioSpec, result: SolutionResult, instance: HgsInstance,
) -> HgsVerification:
    """Call after domain/route structural checks; success requires every check to pass."""

    events: list[_Event] = []
    route_timings: list[JsonValue] = []
    warnings = [MODEL_WARNING]
    totals = {
        "truck_travel_sec": 0.0, "repair_travel_sec": 0.0,
        "truck_operation_sec": 0.0, "repair_operation_sec": 0.0,
    }
    emission = 0.0

    for route in result.truck_routes:
        clock = travel = service = 0.0
        usable = broken = 0
        previous = 0
        arrivals: list[JsonValue] = []
        departures: list[JsonValue] = []
        for index, stop in enumerate(route.stops):
            leg = instance.travel_sec[previous][stop.station_id] if index else 0.0
            emission += 2.61 * (0.252 + 0.0003 * (usable + broken)) * leg / 60.0 * 0.42
            travel += leg
            clock += leg
            arrivals.append(clock)
            loaded = stop.load_usable_bikes + stop.load_broken_bikes
            unloaded = stop.unload_usable_bikes + stop.unload_broken_bikes
            if stop.station_id == 0:
                if stop.load_broken_bikes:
                    raise HgsVerificationError("Depot has no broken bikes to load")
                if index == len(route.stops) - 1 and loaded:
                    raise HgsVerificationError("Final depot operation must not load bikes")
            else:
                events.append(_Event(
                    stop.station_id, clock, f"truck:{route.truck_id}", index,
                    stop.unload_usable_bikes - stop.load_usable_bikes,
                    stop.unload_broken_bikes - stop.load_broken_bikes,
                ))
            usable += stop.load_usable_bikes - stop.unload_usable_bikes
            broken += stop.load_broken_bikes - stop.unload_broken_bikes
            duration = scenario.loading_time_sec * (loaded + unloaded)
            service += duration
            clock += duration
            departures.append(clock)
            previous = stop.station_id
        totals["truck_travel_sec"] += travel
        totals["truck_operation_sec"] += service
        route_timings.append({
            "resource": "truck", "resource_id": route.truck_id,
            "travel_sec": travel, "service_sec": service, "completion_sec": clock,
            "arrivals_sec": arrivals, "departures_sec": departures,
        })

    for route in result.repair_routes:
        clock = travel = service = 0.0
        previous = 0
        arrivals = []
        departures = []
        for index, stop in enumerate(route.stops):
            leg = instance.travel_sec[previous][stop.station_id] * 1.68 if index else 0.0
            travel += leg
            clock += leg
            arrivals.append(clock)
            if stop.station_id == 0:
                if stop.repaired_bikes:
                    raise HgsVerificationError("Repair work at the depot is not supported by the HGS model")
            else:
                events.append(_Event(
                    stop.station_id, clock, f"repairer:{route.repairer_id}", index,
                    stop.repaired_bikes, -stop.repaired_bikes,
                ))
            duration = scenario.repair_time_sec * stop.repaired_bikes
            service += duration
            clock += duration
            departures.append(clock)
            previous = stop.station_id
        totals["repair_travel_sec"] += travel
        totals["repair_operation_sec"] += service
        route_timings.append({
            "resource": "repairer", "resource_id": route.repairer_id,
            "travel_sec": travel, "service_sec": service, "completion_sec": clock,
            "arrivals_sec": arrivals, "departures_sec": departures,
        })
    for timing in route_timings:
        completion = timing["completion_sec"]
        if not math.isfinite(completion) or completion > scenario.operation_time_budget_sec + TIME_EPS_SEC:
            raise HgsVerificationError(
                f"{timing['resource']} {timing['resource_id']} completion {completion} sec "
                f"exceeds route budget {scenario.operation_time_budget_sec} sec (including final service)"
            )

    by_station: dict[int, list[_Event]] = defaultdict(list)
    for event in events:
        by_station[event.station].append(event)
    station_reports: list[JsonValue] = []
    station_dissat: dict[str, float] = {}
    for station_id, station in enumerate(instance.stations, start=1):
        usable, broken = station.usable, station.broken
        ordered = sorted(by_station[station_id], key=lambda event: (event.arrival, event.resource, event.stop_index))
        timeline: list[JsonValue] = []
        start = 0
        while start < len(ordered):
            end = start + 1
            # Nearby float timestamps are conservatively treated as simultaneous.
            while end < len(ordered) and ordered[end].arrival - ordered[end - 1].arrival <= TIME_EPS_SEC:
                end += 1
            group = ordered[start:end]
            per_resource: dict[str, list[_Event]] = defaultdict(list)
            for event in group:
                per_resource[event.resource].append(event)
            minimum_u = minimum_b = maximum_total = delta_u = delta_b = 0
            # Respect each route's stop order while bounding every cross-resource interleaving.
            for resource_events in per_resource.values():
                prefix_u = prefix_b = min_u = min_b = max_total = 0
                for event in sorted(resource_events, key=lambda item: item.stop_index):
                    prefix_u += event.usable_delta
                    prefix_b += event.broken_delta
                    min_u = min(min_u, prefix_u)
                    min_b = min(min_b, prefix_b)
                    max_total = max(max_total, prefix_u + prefix_b)
                minimum_u += min_u
                minimum_b += min_b
                maximum_total += max_total
                delta_u += prefix_u
                delta_b += prefix_b
            if usable + minimum_u < 0 or broken + minimum_b < 0 or usable + broken + maximum_total > station.capacity:
                reason = "inventory becomes negative or exceeds capacity"
                final_u, final_b = usable + delta_u, broken + delta_b
                final_is_valid = final_u >= 0 and final_b >= 0 and final_u + final_b <= station.capacity
                if len(per_resource) > 1 and final_is_valid:
                    reason = "simultaneous-event order is ambiguous; not every permitted ordering preserves inventory"
                raise HgsVerificationError(f"Station {station_id} at {group[0].arrival} sec: {reason}")
            before_u, before_b = usable, broken
            usable += delta_u
            broken += delta_b
            timeline.append({
                "arrival_min_sec": group[0].arrival, "arrival_max_sec": group[-1].arrival,
                "resources": sorted(per_resource), "events": [
                    {"resource": event.resource, "stop_index": event.stop_index,
                     "usable_delta": event.usable_delta, "broken_delta": event.broken_delta}
                    for event in group
                ],
                "usable_before": before_u, "broken_before": before_b,
                "usable_after": usable, "broken_after": broken,
                "all_interleavings_valid": True,
            })
            start = end
        dissatisfaction = instance.dissatisfaction[station_id - 1][usable][broken]
        station_dissat[str(station_id)] = dissatisfaction
        station_reports.append({
            "station_id": station_id, "external_id": station.external_id, "capacity": station.capacity,
            "initial_usable": station.usable, "initial_broken": station.broken,
            "final_usable": usable, "final_broken": broken,
            "dissatisfaction": dissatisfaction, "timeline": timeline,
        })

    dissatisfaction = sum(station_dissat.values())
    objective = 2 * dissatisfaction + 0.06 * emission + 1e-8 * sum(totals.values())
    recomputed = {**totals, "dissatisfaction": dissatisfaction, "emission": emission, "objective_value": objective}
    comparisons: dict[str, JsonValue] = {}

    def compare(name: str, reported: object, expected: float) -> None:
        if isinstance(reported, bool) or not isinstance(reported, (float, int)):
            raise HgsVerificationError(f"Missing or invalid reported numeric metric: {name}")
        try:
            reported_float = float(reported)
        except OverflowError as exc:
            raise HgsVerificationError(f"Non-finite reported metric: {name}") from exc
        if not math.isfinite(expected) or not math.isfinite(reported_float):
            raise HgsVerificationError(f"Non-finite recomputation or metric: {name}")
        tolerance = _printed_tolerance(expected)
        if abs(reported - expected) > tolerance:
            raise HgsVerificationError(
                f"Metric {name} mismatch: reported {reported}, recomputed {expected}, tolerance {tolerance}"
            )
        comparisons[name] = {"reported": reported, "recomputed": expected, "absolute_tolerance": tolerance}

    for name in ("dissatisfaction", "emission", "objective_value"):
        compare(name, getattr(result, name), recomputed[name])
    reported_totals = result.solver_metadata.get("reported_metrics", {})
    if not isinstance(reported_totals, dict):
        raise HgsVerificationError("Invalid reported_metrics metadata")
    for name, expected in totals.items():
        if name in reported_totals:
            compare(name, reported_totals[name], expected)
        else:
            warnings.append(f"HGS omits {name}; it was recomputed but its printed value could not be cross-checked.")
    reported_stations = result.solver_metadata.get("station_dissatisfaction", {})
    if not isinstance(reported_stations, dict):
        raise HgsVerificationError("Invalid station_dissatisfaction metadata")
    if reported_stations:
        if set(reported_stations) != set(station_dissat):
            raise HgsVerificationError("Reported station dissatisfaction IDs do not match the instance")
        for station_id, expected in station_dissat.items():
            compare(f"station_dissatisfaction[{station_id}]", reported_stations[station_id], expected)
    else:
        warnings.append("Per-station dissatisfaction was recomputed; HGS omits the printed station values.")

    return HgsVerification({
        "verifier": VERIFIER_VERSION, "status": "passed", "semantics": "hgs_atomic_arrival_events",
        "checked_input_sha256": instance.input_sha256,
        "operation_time_budget_sec": scenario.operation_time_budget_sec,
        "time_epsilon_sec": TIME_EPS_SEC, "printed_significant_digits": 6,
        "recomputed_metrics": recomputed, "metric_comparisons": comparisons,
        "route_timings": route_timings, "stations": station_reports,
    }, tuple(warnings))
