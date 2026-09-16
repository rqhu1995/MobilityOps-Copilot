import hashlib
from pathlib import Path

import pytest

from mobilityops.domain import ScenarioSpec, SolutionResult
from mobilityops.services.hgs_instance import HgsInputError, load_hgs_instance


def test_actual_snapshot_dimensions_and_external_ids(scenario: ScenarioSpec, valid_result: tuple[SolutionResult, Path]) -> None:
    result, run = valid_result
    instance = load_hgs_instance(scenario, run_dir=run, input_sha256=result.solver_metadata["input_sha256"])
    assert len(instance.travel_sec) == 7
    assert instance.travel_sec[0][1] == 561.1
    assert instance.travel_sec[1][0] == 512.6  # asymmetry must not be discarded
    assert instance.stations[0].external_id == 382
    assert instance.stations[0].capacity == 23
    assert instance.stations[0].usable == 7 and instance.stations[0].broken == 1
    assert len(instance.dissatisfaction[0]) == 24
    assert instance.dissatisfaction[0][15][0] == pytest.approx(4.96223)
    assert len(instance.input_sha256) == 8


@pytest.mark.parametrize("proportion,expected", [(None, [1, 5, 5, 6, 3, 0]), (0.0, [0, 0, 0, 0, 0, 0]), (0.3, [3, 0, 5, 0, 0, 1]), (1.0, [8, 0, 15, 0, 0, 1])])
def test_broken_inventory_rules(scenario: ScenarioSpec, valid_result: tuple[SolutionResult, Path], proportion: float | None, expected: list[int]) -> None:
    result, run = valid_result
    scenario = scenario.model_copy(update={"broken_bike_proportion": proportion})
    (run / "scenario.json").write_text(scenario.model_dump_json())
    instance = load_hgs_instance(scenario, run_dir=run, input_sha256=result.solver_metadata["input_sha256"])
    assert [station.broken for station in instance.stations] == expected


def test_proportion_supports_four_columns_and_clamps_capacity(scenario: ScenarioSpec, valid_result: tuple[SolutionResult, Path]) -> None:
    result, run = valid_result
    scenario = scenario.model_copy(update={"broken_bike_proportion": 1.0})
    (run / "scenario.json").write_text(scenario.model_dump_json())
    path = run / "Instances/6_1/station_info_6.txt"
    rows = [line.split()[:4] for line in path.read_text().splitlines()]
    rows[1][3] = "100"  # target is a reference level; available capacity still limits broken bikes
    path.write_text("\n".join("\t".join(row) for row in rows) + "\n")
    hashes = {**result.solver_metadata["input_sha256"], path.name: hashlib.sha256(path.read_bytes()).hexdigest()}
    instance = load_hgs_instance(scenario, run_dir=run, input_sha256=hashes)
    assert instance.stations[0].broken == 16


@pytest.mark.parametrize("kind", ["missing", "changed", "missing-hash", "bad-hash", "symlink-file", "symlink-folder", "scenario-mismatch", "bad-scenario"])
def test_input_evidence_failures(scenario: ScenarioSpec, valid_result: tuple[SolutionResult, Path], tmp_path: Path, kind: str) -> None:
    result, run = valid_result
    hashes = result.solver_metadata["input_sha256"].copy()
    path = run / "Instances/6_1/time_matrix_6.txt"
    if kind == "missing":
        path.unlink()
    elif kind == "changed":
        path.write_bytes(path.read_bytes() + b"\n")
    elif kind == "missing-hash":
        hashes.pop(path.name)
    elif kind == "bad-hash":
        hashes[path.name] = "no recorded hash"
    elif kind == "symlink-file":
        foreign = tmp_path / "foreign-matrix.txt"
        foreign.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(foreign)
    elif kind == "symlink-folder":
        original = path.parent
        foreign = tmp_path / "other-instance"
        original.rename(foreign)
        original.symlink_to(foreign, target_is_directory=True)
    elif kind == "scenario-mismatch":
        (run / "scenario.json").write_text(scenario.model_copy(update={"truck_capacity": 26}).model_dump_json())
    else:
        (run / "scenario.json").write_text("not JSON")
    with pytest.raises(HgsInputError):
        load_hgs_instance(scenario, run_dir=run, input_sha256=hashes)


@pytest.mark.parametrize("kind,message", [
    ("bad-header", "columns"), ("missing-station", "exactly 6 stations"),
    ("negative-inventory", "integer columns"), ("integer-overflow", "exceeds HGS range"),
    ("usable-over-capacity", "usable inventory"), ("broken-over-capacity", "initial inventory"),
    ("ragged-matrix", "columns"), ("missing-matrix-row", "rows"),
    ("negative-travel", "nonnegative"), ("nan-travel", "invalid number"),
    ("overflow-travel", "finite"), ("nonzero-diagonal", "diagonal"),
    ("bad-table-size", "rows"), ("negative-dissat", "nonnegative"),
    ("invalid-encoding", "UTF-8"),
])
def test_malformed_mathematical_inputs(scenario: ScenarioSpec, valid_result: tuple[SolutionResult, Path], kind: str, message: str) -> None:
    result, run = valid_result
    filename = "station_info_6.txt"
    if any(token in kind for token in ("matrix", "travel", "diagonal")):
        filename = "time_matrix_6.txt"
    elif "table" in kind or "dissat" in kind:
        filename = "dissat_table_1.txt"
    path = run / "Instances/6_1" / filename
    rows = [line.split() for line in path.read_text().splitlines()]
    if kind == "bad-header":
        rows[0][0] = "unknown"
    elif kind in {"missing-station", "missing-matrix-row", "bad-table-size"}:
        rows.pop()
    elif kind == "negative-inventory":
        rows[1][2] = "-1"
    elif kind == "integer-overflow":
        rows[1][1] = "2147483648"
    elif kind == "usable-over-capacity":
        rows[1][2] = "24"
    elif kind == "broken-over-capacity":
        rows[1][4] = "23"
    elif kind == "ragged-matrix":
        rows[0].pop()
    elif kind in {"negative-travel", "negative-dissat"}:
        rows[0][1] = "-1"
    elif kind == "nan-travel":
        rows[0][1] = "nan"
    elif kind == "overflow-travel":
        rows[0][1] = "1e999"
    elif kind == "nonzero-diagonal":
        rows[0][0] = "1"
    path.write_bytes(b"\xff" if kind == "invalid-encoding" else ("\n".join(" ".join(row) for row in rows) + "\n").encode())
    hashes = {**result.solver_metadata["input_sha256"], filename: hashlib.sha256(path.read_bytes()).hexdigest()}
    with pytest.raises(HgsInputError, match=message):
        load_hgs_instance(scenario, run_dir=run, input_sha256=hashes)
