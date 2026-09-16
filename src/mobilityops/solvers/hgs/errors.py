"""Explicit failures at the HGS integration boundary."""

from pathlib import Path

from mobilityops.solvers.contracts import ConstraintRecord


class ConstraintRejectedError(ValueError):
    def __init__(self, records: tuple[ConstraintRecord, ...]) -> None:
        self.records = records
        super().__init__("HGS constraint rejected: " + "; ".join(
            f"{r.constraint_name}: {r.warning}" for r in records if r.outcome == "rejected"
        ))


class HgsPreflightError(ValueError):
    """Invalid configuration, missing input, or an already reserved run ID."""


class ArtifactError(OSError):
    """Persistence failed; any files already written remain available."""

    def __init__(self, run_dir: Path, detail: str) -> None:
        self.run_dir = run_dir
        super().__init__(f"Artifact persistence failed in {run_dir}: {detail}")


class HgsParseError(ValueError):
    """The native result is missing required data or has an unknown format."""
