import pytest

from mobilityops.solvers.hgs import HgsParseError, parse_hgs_output


def test_actual_fixture_metrics_routes_and_metadata(raw_output: bytes) -> None:
    parsed = parse_hgs_output(raw_output.decode())
    assert (parsed.objective_value, parsed.dissatisfaction, parsed.emission, parsed.runtime_sec) == (104.039, 51.7808, 7.94776, 1.09077)
    truck = parsed.truck_routes[0]
    assert truck.truck_id == 0
    assert [s.station_id for s in truck.stops] == [0, 2, 1, 3, 6, 5, 4, 0]
    assert truck.stops[1].load_usable_bikes == 3
    assert truck.stops[1].load_broken_bikes == 5
    assert truck.stops[-1].unload_usable_bikes == 6
    assert truck.stops[-1].unload_broken_bikes == 15
    assert [s.repaired_bikes for s in parsed.repair_routes[0].stops] == [0, 5, 0]
    assert parsed.metadata["reported_metrics"]["truck_travel_sec"] == 1695.5
    assert len(parsed.metadata["station_dissatisfaction"]) == 6
    assert parsed.warnings


@pytest.mark.parametrize(("old", "new"), [
    ("individual's fitness value: 104.039\n", ""),
    ("CPU time: 1.09077\n", ""),
    ("individual's fitness value: 104.039", "individual's fitness value: nan"),
    ("individual's fitness value: 104.039", "individual's fitness value: 1_000"),
    ("individual's emission value: 7.94776", "individual's emission value: inf"),
    ("CPU time: 1.09077", "CPU time: -1"),
    ("load 16 usable", "load -16 usable"),
    ("load 16 usable", "load 2147483648 usable"),
    ("load 16 usable", "load " + "9" * 5000 + " usable"),
    ("repair 5 bikes", "repair nope bikes"),
    ("repair 5 bikes", "repair -5 bikes"),
    ("the repositioning scheme for truck is:", "unknown truck heading"),
    ("individual's fitness value: 104.039", "individual's fitness value: 104.039\nindividual's fitness value: 1"),
    ("dissat[1] = 4.96223", "dissat[1] = 4.96223\ndissat[1] = 4"),
    ("CPU time: 1.09077", "CPU time: 1.09077\nCPU time: 1"),
    ("the repositioning scheme for repairman is:", "=======\nthe repositioning scheme for repairman is:"),
    ("dissat[1] = 4.96223", "UNRECOGNIZED FOOTER"),
])
def test_malformed_output_rejected(raw_output: bytes, old: str, new: str) -> None:
    with pytest.raises(HgsParseError):
        parse_hgs_output(raw_output.decode().replace(old, new))


def test_truncated_and_empty_output(raw_output: bytes) -> None:
    for text in ("", "nonsense", raw_output.decode().split("the repositioning scheme for repairman")[0]):
        with pytest.raises(HgsParseError):
            parse_hgs_output(text)


def test_multiple_route_blocks_get_stable_zero_based_ids(raw_output: bytes) -> None:
    text = raw_output.decode().replace(
        "the repositioning scheme for repairman is:",
        "=======\n0\tload 0 usable bikes;load 0 broken bikes;unload 0 usable bikes;unload 0 broken bikes\n"
        "0\tload 0 usable bikes;load 0 broken bikes;unload 0 usable bikes;unload 0 broken bikes\n"
        "the repositioning scheme for repairman is:",
    ).replace("CPU time:", "=======\n0\trepair 0 bikes\n0\trepair 0 bikes\nCPU time:")
    parsed = parse_hgs_output(text)
    assert [r.truck_id for r in parsed.truck_routes] == [0, 1]
    assert [r.repairer_id for r in parsed.repair_routes] == [0, 1]


def test_crlf_scientific_notation_and_optional_metrics(raw_output: bytes) -> None:
    text = raw_output.decode().replace("104.039", "1.04039e2")
    text = "\r\n".join(line for line in text.splitlines() if not any(
        token in line for token in ("trkRoute:", "rpmRoute:", "trkOperationTime:", "rpmOperationTime:", "dissat[")
    ))
    parsed = parse_hgs_output(text)
    assert parsed.objective_value == 104.039
    assert any("omits optional" in w for w in parsed.warnings)
    assert any("omits per-station" in w for w in parsed.warnings)
