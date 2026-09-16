"""Strict parser for Genetic::saveResults text; performs no filesystem I/O."""

from dataclasses import dataclass
import math
import re

from pydantic import JsonValue, ValidationError

from mobilityops.domain import RepairRoute, RepairStop, TruckRoute, TruckStop
from mobilityops.solvers.hgs.errors import HgsParseError


@dataclass(frozen=True)
class ParsedHgsOutput:
    objective_value: float
    dissatisfaction: float
    emission: float
    runtime_sec: float
    truck_routes: tuple[TruckRoute, ...]
    repair_routes: tuple[RepairRoute, ...]
    warnings: tuple[str, ...]
    metadata: dict[str, JsonValue]


_TRUCK = re.compile(
    r"(\d+)\s+load (\d+) usable bikes;\s*load (\d+) broken bikes;\s*"
    r"unload (\d+) usable bikes;\s*unload (\d+) broken bikes"
)
_REPAIR = re.compile(r"(\d+)\s+repair (\d+) bikes")
_METRICS = {
    "individual's fitness value": "objective_value",
    "individual's dissat value": "dissatisfaction",
    "individual's emission value": "emission",
    "individual's trkRoute": "truck_travel_sec",
    "individual's rpmRoute": "repair_travel_sec",
    "individual's trkOperationTime": "truck_operation_sec",
    "individual's rpmOperationTime": "repair_operation_sec",
}


def _number(text: str, name: str) -> float:
    if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", text.strip()):
        raise HgsParseError(f"Invalid numeric format for {name}")
    try:
        value = float(text)
    except ValueError as exc:
        raise HgsParseError(f"Invalid numeric value for {name}") from exc
    if not math.isfinite(value) or (name != "objective_value" and value < 0):
        raise HgsParseError(f"Non-finite or negative value for {name}")
    return value


def _integers(tokens: tuple[str, ...]) -> tuple[int, ...]:
    """HGS prints signed C++ ints; reject overflow/corruption before conversion."""

    if any(len(token) > 10 for token in tokens):
        raise HgsParseError("HGS station/operation integer exceeds signed 32-bit range")
    values = tuple(map(int, tokens))
    if any(value > 2_147_483_647 for value in values):
        raise HgsParseError("HGS station/operation integer exceeds signed 32-bit range")
    return values


def parse_hgs_output(text: str) -> ParsedHgsOutput:
    """Reject truncated/ambiguous route blocks instead of silently losing stops."""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines.pop(0) != "the best solution is:":
        raise HgsParseError("Missing HGS best-solution header")
    metrics: dict[str, float] = {}
    trucks: list[list[TruckStop]] = []
    repairers: list[list[RepairStop]] = []
    station_dissat: dict[str, JsonValue] = {}
    warnings: list[str] = []
    section = "metrics"
    seen_dissat_header = False
    for line in lines:
        if line == "the repositioning scheme for truck is:" and section == "metrics":
            section = "truck"
        elif line == "the repositioning scheme for repairman is:" and section == "truck":
            section = "repair"
        elif line == "=======":
            if section == "truck":
                trucks.append([])
            elif section == "repair":
                repairers.append([])
            else:
                raise HgsParseError("Route separator outside a route section")
        elif line.startswith("CPU time:") and section == "repair":
            metrics["runtime_sec"] = _number(line.partition(":")[2], "runtime_sec")
            section = "tail"
        elif section == "metrics":
            label, sep, value = line.partition(":")
            if not sep or label not in _METRICS:
                raise HgsParseError(f"Unknown HGS metric line: {line}")
            key = _METRICS[label]
            if key in metrics:
                raise HgsParseError(f"Duplicate HGS metric: {key}")
            metrics[key] = _number(value, key)
        elif section == "truck":
            match = _TRUCK.fullmatch(line)
            if not match or not trucks:
                raise HgsParseError(f"Malformed truck route: {line}")
            station, load_u, load_b, unload_u, unload_b = _integers(match.groups())
            try:
                trucks[-1].append(TruckStop(
                    station_id=station, load_usable_bikes=load_u, load_broken_bikes=load_b,
                    unload_usable_bikes=unload_u, unload_broken_bikes=unload_b,
                ))
            except ValidationError as exc:
                raise HgsParseError("Invalid truck stop") from exc
        elif section == "repair":
            match = _REPAIR.fullmatch(line)
            if not match or not repairers:
                raise HgsParseError(f"Malformed repair route: {line}")
            station, quantity = _integers(match.groups())
            repairers[-1].append(RepairStop(station_id=station, repaired_bikes=quantity))
        elif section == "tail":
            if line == "dissat at each station" and not seen_dissat_header:
                seen_dissat_header = True
                continue
            match = re.fullmatch(r"dissat\[(\d+)\]\s*=\s*(\S+)", line)
            if match and seen_dissat_header:
                station, value = match.groups()
                if station in station_dissat:
                    raise HgsParseError(f"Duplicate station dissatisfaction: {station}")
                station_dissat[station] = _number(value, "station_dissatisfaction")
            else:
                raise HgsParseError(f"Unexpected text after routes: {line}")
    required = {"objective_value", "dissatisfaction", "emission", "runtime_sec"}
    if missing := required - metrics.keys():
        raise HgsParseError("Missing required metrics: " + ", ".join(sorted(missing)))
    if section != "tail" or not trucks or not repairers or any(not r for r in [*trucks, *repairers]):
        raise HgsParseError("Missing or empty truck/repair route block")
    optional = set(_METRICS.values()) - required
    if optional - metrics.keys():
        warnings.append("HGS output omits optional aggregate travel/operation metrics.")
    if not station_dissat:
        warnings.append("HGS output omits per-station dissatisfaction values.")
    warnings.extend((
        "HGS text does not include an explicit feasibility flag.",
        "Route IDs are zero-based output block indices; HGS text does not print vehicle IDs.",
    ))
    return ParsedHgsOutput(
        **{key: metrics[key] for key in required},
        truck_routes=tuple(TruckRoute(truck_id=i, stops=tuple(stops)) for i, stops in enumerate(trucks)),
        repair_routes=tuple(RepairRoute(repairer_id=i, stops=tuple(stops)) for i, stops in enumerate(repairers)),
        warnings=tuple(warnings),
        metadata={
            "result_format": "hgs_genetic_save_results_v1",
            "reported_metrics": {key: value for key, value in metrics.items() if key not in required},
            "station_dissatisfaction": station_dissat,
            "runtime_semantics": (
                "HGS labels this CPU time but measures elapsed high_resolution_clock time, "
                "including initial population."
            ),
        },
    )
