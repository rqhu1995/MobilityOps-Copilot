from pathlib import Path

import pytest
from pydantic import ValidationError

from mobilityops.config import Settings


VALID_ENV = {
    "HGS_REPO_PATH": "/opt/solvers/hgs",
    "GUROBI_REPO_PATH": "/opt/solvers/gurobi",
    "MOBILITYOPS_RUNS_DIR": "/var/tmp/mobilityops/runs",
}


def test_settings_from_env() -> None:
    settings = Settings.from_env(VALID_ENV)

    assert settings == Settings(
        hgs_repo_path=Path("/opt/solvers/hgs"),
        gurobi_repo_path=Path("/opt/solvers/gurobi"),
        runs_dir=Path("/var/tmp/mobilityops/runs"),
    )


def test_settings_from_env_requires_all_variables() -> None:
    for variable_name in VALID_ENV:
        environ = VALID_ENV.copy()
        environ.pop(variable_name)

        with pytest.raises(
            ValueError,
            match=f"missing required environment variables: {variable_name}",
        ):
            Settings.from_env(environ)


def test_settings_rejects_relative_paths() -> None:
    valid_values = {
        "hgs_repo_path": Path("/opt/solvers/hgs"),
        "gurobi_repo_path": Path("/opt/solvers/gurobi"),
        "runs_dir": Path("/var/tmp/mobilityops/runs"),
    }

    for field_name in valid_values:
        values = valid_values.copy()
        values[field_name] = Path("relative/path")

        with pytest.raises(ValidationError, match="path must be absolute"):
            Settings.model_validate(values)
