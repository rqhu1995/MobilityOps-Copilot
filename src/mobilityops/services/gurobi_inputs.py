"""Validated local inputs for the audited time-indexed model; no SDK imports."""

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path

from mobilityops.domain import ScenarioSpec


def read_local(run_dir: Path, path: Path) -> bytes:
    relative = path.relative_to(run_dir)
    if not run_dir.is_absolute() or run_dir.is_symlink() or not path.resolve().is_relative_to(run_dir.resolve()):
        raise ValueError("Gurobi evidence escapes the run")
    if any((run_dir / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)):
        raise ValueError("Gurobi evidence cannot use symlinks")
    return path.read_bytes()


def strict_json(data: bytes) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid(value: str) -> None:
        raise ValueError("Non-finite JSON number")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)


def input_names(scenario: ScenarioSpec) -> tuple[str, ...]:
    n = scenario.number_of_stations
    return (f"station_info_{n}.txt", f"time_matrix_{n}.txt", *(f"linear_diss_{i}.txt" for i in range(1, n + 1)))


@dataclass(frozen=True)
class GurobiInput:
    capacity: tuple[int, ...]
    usable: tuple[int, ...]
    broken: tuple[int, ...]
    target: tuple[int, ...]
    travel: tuple[tuple[float, ...], ...]
    planes: tuple[tuple[tuple[float, float, float], ...], ...]


def load_inputs(scenario: ScenarioSpec, run_dir: Path, hashes: dict[str, str]) -> GurobiInput:
    saved = ScenarioSpec.model_validate(strict_json(read_local(run_dir, run_dir / "scenario.json")))
    if saved != scenario:
        raise ValueError("Saved Gurobi scenario differs from the requested scenario")
    folder = run_dir / "work/resources/datasets" / f"{scenario.number_of_stations}_{scenario.instance_id}"

    def read(name: str) -> str:
        data = read_local(run_dir, folder / name)
        if hashlib.sha256(data).hexdigest() != hashes.get(name):
            raise ValueError(f"Gurobi input hash mismatch: {name}")
        return data.decode("utf-8")

    return parse_inputs(scenario, read)


def parse_inputs(scenario: ScenarioSpec, read: Callable[[str], str]) -> GurobiInput:
    """Validate model input text without filesystem writes or SDK imports."""
    n = scenario.number_of_stations
    rows = read(f"station_info_{n}.txt").splitlines()
    if len(rows) != n + 1 or rows[0].split("\t") != ["station_id", "capacity", "curUsable", "targetUsable", "curBroken"]:
        raise ValueError("Invalid Gurobi station table header or row count")
    capacity, usable, broken, target = [10000], [10000], [0], [0]
    for row in rows[1:]:
        parts = row.split("\t")
        if len(parts) != 5 or any(not p.isascii() or not p.isdigit() or len(p) > 9 for p in parts):
            raise ValueError("Invalid station integer columns")
        _, c, u, goal, b = map(int, parts)
        if c <= 0 or u + b > c:
            raise ValueError("Invalid initial station capacity or inventory")
        capacity.append(c); usable.append(u); target.append(goal); broken.append(b)
    travel = tuple(tuple(float(v) for v in line.split("\t")) for line in read(f"time_matrix_{n}.txt").splitlines())
    if len(travel) != n + 1 or any(len(row) != n + 1 for row in travel):
        raise ValueError("Invalid Gurobi travel matrix shape")
    if any(not math.isfinite(v) or v < 0 for row in travel for v in row) or any(travel[i][i] != 0 for i in range(n + 1)):
        raise ValueError("Travel must be finite, nonnegative and zero on the diagonal")
    planes = [()]
    for station in range(1, n + 1):
        entries = []
        for line in read(f"linear_diss_{station}.txt").splitlines():
            cells = line.split(",")
            if len(cells) != 6:
                raise ValueError("Linear dissatisfaction rows must have six columns")
            coefficients = tuple(float(v) for v in cells[2:5])
            if any(not math.isfinite(v) for v in coefficients):
                raise ValueError("Linear dissatisfaction coefficients must be finite")
            entries.append(coefficients)
        if not entries:
            raise ValueError("Missing linear dissatisfaction planes")
        planes.append(tuple(entries))
    return GurobiInput(tuple(capacity), tuple(usable), tuple(broken), tuple(target), travel, tuple(planes))
