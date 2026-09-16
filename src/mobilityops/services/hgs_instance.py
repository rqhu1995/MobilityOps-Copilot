"""Read and validate the mathematical inputs from one HGS run snapshot."""

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
from collections.abc import Callable, Mapping

from pydantic import ValidationError

from mobilityops.domain import ScenarioSpec


class HgsInputError(ValueError):
    """The current run lacks intact, well-formed mathematical input evidence."""


@dataclass(frozen=True)
class HgsStation:
    external_id: int
    capacity: int
    usable: int
    broken: int


@dataclass(frozen=True)
class HgsInstance:
    stations: tuple[HgsStation, ...]  # index 0 represents route station ID 1
    travel_sec: tuple[tuple[float, ...], ...]  # includes depot row/column 0
    dissatisfaction: tuple[tuple[tuple[float, ...], ...], ...]
    input_sha256: dict[str, str]


def _read_local(path: Path, run_dir: Path) -> bytes:
    try:
        relative = path.relative_to(run_dir)
        if not path.resolve().is_relative_to(run_dir.resolve()):
            raise HgsInputError(f"Input evidence escapes this run: {relative}")
        if any((run_dir / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)):
            raise HgsInputError(f"Input evidence must not use symlinks: {relative}")
        return path.read_bytes()
    except (OSError, ValueError) as exc:
        if isinstance(exc, HgsInputError):
            raise
        raise HgsInputError(f"Cannot read input evidence {path.name}: {exc}") from exc


def _matrix(text: str, size: int, name: str) -> tuple[tuple[float, ...], ...]:
    lines = text.splitlines()
    if len(lines) != size:
        raise HgsInputError(f"{name} must contain exactly {size} rows")
    rows: list[tuple[float, ...]] = []
    number = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
    for index, line in enumerate(lines):
        cells = line.split()
        if len(cells) != size:
            raise HgsInputError(f"{name} row {index} must contain exactly {size} columns")
        if any(not number.fullmatch(cell) for cell in cells):
            raise HgsInputError(f"{name} row {index} contains an invalid number")
        row = tuple(float(cell) for cell in cells)
        if any(not math.isfinite(value) or value < 0 for value in row):
            raise HgsInputError(f"{name} values must be finite and nonnegative")
        rows.append(row)
    return tuple(rows)


def load_hgs_instance(
    scenario: ScenarioSpec, *, run_dir: Path, input_sha256: Mapping[str, object],
) -> HgsInstance:
    """Use fixed local paths and recorded SHA-256 values; never reopen the solver repo."""

    if not run_dir.is_absolute() or run_dir.is_symlink():
        raise HgsInputError("Snapshot run_dir must be absolute and not a symlink")
    try:
        saved = ScenarioSpec.model_validate_json(_read_local(run_dir / "scenario.json", run_dir))
    except ValidationError as exc:
        raise HgsInputError("Invalid scenario.json in this run") from exc
    if saved != scenario:
        raise HgsInputError("scenario.json does not match the scenario being validated")
    n = scenario.number_of_stations
    folder = run_dir / "Instances" / f"{n}_{scenario.instance_id}"
    checked_hashes: dict[str, str] = {}

    def read(name: str) -> str:
        expected = input_sha256.get(name)
        if not isinstance(expected, str) or not re.fullmatch("[0-9a-f]{64}", expected):
            raise HgsInputError(f"Missing or invalid recorded input SHA-256: {name}")
        data = _read_local(folder / name, run_dir)
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected:
            raise HgsInputError(f"Input snapshot SHA-256 mismatch: {name}")
        checked_hashes[name] = digest
        try:
            return data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise HgsInputError(f"Input snapshot is not UTF-8: {name}") from exc

    return parse_hgs_instance(scenario, read, checked_hashes)


def parse_hgs_instance(
    scenario: ScenarioSpec, read: Callable[[str], str], checked_hashes: dict[str, str],
) -> HgsInstance:
    """Validate mathematical input text using the same rules as run replay."""
    n = scenario.number_of_stations
    lines = read(f"station_info_{n}.txt").splitlines()
    if len(lines) != n + 1:
        raise HgsInputError(f"Station table must contain a header and exactly {n} stations")
    header = lines[0].split()
    expected_header = ["station_id", "capacity", "curUsable", "targetUsable", "curBroken"]
    if header != expected_header and not (
        scenario.broken_bike_proportion is not None and header == expected_header[:-1]
    ):
        raise HgsInputError("Unrecognized station table columns")
    stations: list[HgsStation] = []
    for station_id, line in enumerate(lines[1:], start=1):
        cells = line.split()
        if len(cells) != len(header) or any(not re.fullmatch(r"[0-9]{1,10}", cell) for cell in cells):
            raise HgsInputError(f"Invalid integer columns at station {station_id}")
        values = tuple(map(int, cells))
        if any(value > 2_147_483_647 for value in values):
            raise HgsInputError(f"Station {station_id} integer exceeds HGS range")
        external, capacity, usable, target = values[:4]
        if usable > capacity:
            raise HgsInputError(f"Station {station_id} initial usable inventory exceeds capacity")
        if scenario.broken_bike_proportion is None:
            broken = values[4]
        else:
            # Match HGS's double multiplication followed by ceil, including clamping.
            broken = 0 if usable > target else min(
                math.ceil(scenario.broken_bike_proportion * (target - usable)), capacity - usable,
            )
        if usable + broken > capacity:
            raise HgsInputError(f"Station {station_id} initial inventory exceeds capacity")
        stations.append(HgsStation(external, capacity, usable, broken))

    name = f"time_matrix_{n}.txt"
    travel = _matrix(read(name), n + 1, name)
    if any(travel[i][i] != 0 for i in range(n + 1)):
        raise HgsInputError("Travel time matrix diagonal must be zero")
    tables = tuple(
        _matrix(read(f"dissat_table_{i}.txt"), station.capacity + 1, f"dissat_table_{i}.txt")
        for i, station in enumerate(stations, start=1)
    )
    return HgsInstance(tuple(stations), travel, tables, checked_hashes)
