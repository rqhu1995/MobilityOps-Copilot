"""Application path configuration loaded from process environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator


class Settings(BaseModel):
    """Validated filesystem locations used by MobilityOps-Copilot."""

    model_config = ConfigDict(frozen=True)

    hgs_repo_path: Path
    gurobi_repo_path: Path
    runs_dir: Path

    @field_validator("hgs_repo_path", "gurobi_repo_path", "runs_dir")
    @classmethod
    def require_absolute_path(cls, value: Path) -> Path:
        """Reject ambiguous relative paths at the process boundary."""

        if not value.is_absolute():
            raise ValueError("path must be absolute")
        return value

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Create settings from the three supported environment variables."""

        source = os.environ if environ is None else environ
        variables = {
            "HGS_REPO_PATH": "hgs_repo_path",
            "GUROBI_REPO_PATH": "gurobi_repo_path",
            "MOBILITYOPS_RUNS_DIR": "runs_dir",
        }
        missing = [name for name in variables if not source.get(name)]
        if missing:
            names = ", ".join(missing)
            raise ValueError(f"missing required environment variables: {names}")

        values = {
            field: Path(source[name]).expanduser()
            for name, field in variables.items()
        }
        return cls.model_validate(values)
