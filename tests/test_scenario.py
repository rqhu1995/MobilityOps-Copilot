import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mobilityops.domain import ScenarioSpec


EXAMPLE_PATH = Path(__file__).parents[1] / "examples" / "scenario_6_1.json"


def load_example_payload() -> dict[str, object]:
    return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


def test_valid_scenario_round_trip() -> None:
    scenario = ScenarioSpec.model_validate_json(
        EXAMPLE_PATH.read_text(encoding="utf-8")
    )

    restored = ScenarioSpec.model_validate_json(scenario.model_dump_json())

    assert restored == scenario
    assert restored.number_of_stations == 6
    assert restored.instance_id == 1
    assert restored.seed is None
    assert restored.broken_bike_proportion is None


def test_missing_required_fields_are_rejected() -> None:
    required_fields = {
        "scenario_name",
        "instance_id",
        "number_of_stations",
        "number_of_trucks",
        "number_of_repairers",
        "truck_capacity",
        "operation_time_budget_sec",
        "solver_runtime_limit_sec",
        "loading_time_sec",
        "repair_time_sec",
        "objective_profile",
        "solution_requirement",
    }

    for field_name in required_fields:
        payload = load_example_payload()
        payload.pop(field_name)
        with pytest.raises(ValidationError):
            ScenarioSpec.model_validate(payload)


def test_invalid_numeric_values_are_rejected() -> None:
    invalid_updates = (
        {"instance_id": 0},
        {"number_of_stations": 0},
        {"number_of_trucks": 0},
        {"number_of_repairers": 0},
        {"truck_capacity": 0},
        {"operation_time_budget_sec": 0.0},
        {"solver_runtime_limit_sec": 0.0},
        {"loading_time_sec": 0},
        {"repair_time_sec": -1},
        {"seed": -1},
        {"broken_bike_proportion": -0.1},
        {"broken_bike_proportion": 1.1},
    )

    for update in invalid_updates:
        payload = load_example_payload()
        payload.update(update)
        with pytest.raises(ValidationError):
            ScenarioSpec.model_validate(payload)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    (
        ("objective_profile", "weighted_cost"),
        ("solution_requirement", "optimal_only"),
    ),
)
def test_invalid_enums_are_rejected(field_name: str, invalid_value: str) -> None:
    payload = load_example_payload()
    payload[field_name] = invalid_value

    with pytest.raises(ValidationError):
        ScenarioSpec.model_validate(payload)
