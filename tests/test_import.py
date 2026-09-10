from pathlib import Path

import mobilityops
from mobilityops.config import Settings


def test_package_import() -> None:
    assert mobilityops.__version__ == "0.1.0"


def test_settings_model_import() -> None:
    settings = Settings(
        hgs_repo_path=Path("/opt/solvers/hgs"),
        gurobi_repo_path=Path("/opt/solvers/gurobi"),
        runs_dir=Path("/var/tmp/mobilityops/runs"),
    )

    assert settings.runs_dir.is_absolute()
