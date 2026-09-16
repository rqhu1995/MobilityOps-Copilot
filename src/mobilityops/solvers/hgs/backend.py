"""HGS adapter with isolated inputs, bounded execution and durable raw evidence."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time

from pydantic import JsonValue

from mobilityops.config import Settings
from mobilityops.domain import BackendName, ResultStatus, ScenarioSpec, SolutionResult
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.hgs_instance import parse_hgs_instance
from mobilityops.services.validation import ResultValidationError, validate_result
from mobilityops.solvers.contracts import HGS_CAPABILITY, BackendCapability, ConstraintOutcome, ConstraintRecord
from mobilityops.solvers.hgs.command import build_hgs_argv
from mobilityops.solvers.hgs.constraints import check_hgs_constraints
from mobilityops.solvers.hgs.errors import ArtifactError, ConstraintRejectedError, HgsParseError, HgsPreflightError
from mobilityops.solvers.hgs.parser import parse_hgs_output


def required_hgs_inputs(scenario: ScenarioSpec) -> tuple[str, ...]:
    """Only files actually read by the audited Instance.cpp (no solver source)."""

    n = scenario.number_of_stations
    return (
        f"time_matrix_{n}.txt", f"station_info_{n}.txt",
        *(f"{prefix}_{i}.txt" for i in range(1, n + 1)
          for prefix in ("dissat_table", "BCRF", "BCRFR")),
    )


def _write_json(path: Path, value: JsonValue) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


class HgsBackend:
    """Run an existing trusted executable; never build or write into a solver repo."""

    def __init__(
        self, settings: Settings, *, executable: Path | None = None,
        timeout_grace_sec: float = 5.0,
    ) -> None:
        if not math.isfinite(timeout_grace_sec) or not 0 < timeout_grace_sec <= 60:
            raise ValueError("timeout_grace_sec must be finite and in (0, 60]")
        self.settings = settings
        self.executable = executable if executable is not None else settings.hgs_repo_path / "build" / "main"
        self.timeout_grace_sec = timeout_grace_sec

    @property
    def capability(self) -> BackendCapability:
        return HGS_CAPABILITY

    def check_constraints(self, scenario: ScenarioSpec) -> tuple[ConstraintRecord, ...]:
        return check_hgs_constraints(scenario)

    def _preflight(self, scenario: ScenarioSpec, run_id: str) -> tuple[Path, Path, Path]:
        validate_run_id(run_id)
        repo = self.settings.hgs_repo_path
        if not repo.is_absolute() or not repo.is_dir():
            raise HgsPreflightError(f"HGS repo is not an absolute existing directory: {repo}")
        if not self.executable.is_absolute() or not self.executable.is_file():
            raise HgsPreflightError(f"HGS executable is not an absolute existing file: {self.executable}")
        if not os.access(self.executable, os.X_OK):
            raise HgsPreflightError(f"HGS executable is not executable: {self.executable}")
        instance = repo / "Instances" / f"{scenario.number_of_stations}_{scenario.instance_id}"
        if not instance.is_dir():
            raise HgsPreflightError(f"HGS instance directory does not exist: {instance}")
        for name in required_hgs_inputs(scenario):
            if not (instance / name).is_file():
                raise HgsPreflightError(f"Required HGS input file is missing: {instance / name}")
        if not self.settings.runs_dir.is_absolute():
            raise HgsPreflightError("runs_dir must be absolute")
        root = self.settings.runs_dir.resolve()
        for solver_repo in (repo, self.settings.gurobi_repo_path):
            if root.is_relative_to(solver_repo.resolve()):
                raise HgsPreflightError("runs_dir must not be inside a solver repository")
        run_dir = root / run_id
        if not run_dir.resolve().is_relative_to(root) or run_dir.is_symlink():
            raise HgsPreflightError("Run directory escapes runs_dir or is a symlink")
        if run_dir.exists():
            raise HgsPreflightError(f"Run directory already exists; refusing overwrite: {run_dir}")
        if not math.isfinite(scenario.solver_runtime_limit_sec + self.timeout_grace_sec):
            raise HgsPreflightError("External timeout must be finite")
        return instance, root, run_dir

    def preflight(self, scenario: ScenarioSpec, *, run_id: str) -> float:
        """Check local readiness without creating files or starting a process."""
        scenario = ScenarioSpec.model_validate(scenario.model_dump())
        records = self.check_constraints(scenario)
        if any(r.outcome is ConstraintOutcome.REJECTED for r in records):
            raise ConstraintRejectedError(records)
        instance, _, _ = self._preflight(scenario, run_id)
        inputs = {name: (instance / name).read_bytes() for name in required_hgs_inputs(scenario)}
        parse_hgs_instance(scenario, lambda name: inputs[name].decode("utf-8"), {})
        return scenario.solver_runtime_limit_sec + self.timeout_grace_sec

    def solve(self, scenario: ScenarioSpec, *, run_id: str) -> SolutionResult:
        scenario = ScenarioSpec.model_validate(scenario.model_dump())
        records = self.check_constraints(scenario)
        if any(record.outcome is ConstraintOutcome.REJECTED for record in records):
            raise ConstraintRejectedError(records)
        instance, root, run_dir = self._preflight(scenario, run_id)
        try:
            root.mkdir(parents=True, exist_ok=True)
            # Atomic reservation handles concurrent callers using the same run ID.
            try:
                run_dir.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise HgsPreflightError(f"Run directory already exists; refusing overwrite: {run_dir}") from exc
            return self._run(scenario, records, instance, run_dir)
        except OSError as exc:
            raise ArtifactError(run_dir, str(exc)) from exc

    def _run(
        self, scenario: ScenarioSpec, records: tuple[ConstraintRecord, ...],
        instance: Path, run_dir: Path,
    ) -> SolutionResult:
        argv = build_hgs_argv(self.executable.resolve(), scenario)
        timeout = scenario.solver_runtime_limit_sec + self.timeout_grace_sec
        artifacts = {role: run_dir / name for role, name in (
            ("scenario", "scenario.json"), ("command", "command.json"),
            ("stdout", "stdout.log"), ("stderr", "stderr.log"), ("process", "process.json"),
        )}
        artifacts["scenario"].write_text(scenario.model_dump_json(indent=2) + "\n", encoding="utf-8")
        _write_json(artifacts["command"], list(argv))
        cwd = run_dir / "work"
        cwd.mkdir()
        snapshot = run_dir / "Instances" / instance.name
        snapshot.mkdir(parents=True)
        input_hashes: dict[str, JsonValue] = {}
        for name in required_hgs_inputs(scenario):
            data = (instance / name).read_bytes()
            (snapshot / name).write_bytes(data)
            input_hashes[name] = hashlib.sha256(data).hexdigest()
        (run_dir / "Solutions").mkdir()
        metadata: dict[str, JsonValue] = {
            "cwd": str(cwd), "executable": str(self.executable.resolve()),
            "executable_sha256": hashlib.sha256(self.executable.read_bytes()).hexdigest(),
            "source_instance": str(instance.resolve()), "input_sha256": input_hashes,
            "timeout_sec": timeout, "timeout_grace_sec": self.timeout_grace_sec,
            "constraint_records": [record.model_dump(mode="json") for record in records],
            "seed_supported": False,
        }
        # Persist provenance before starting, even if execution is interrupted.
        _write_json(artifacts["process"], metadata)
        stdout = stderr = b""
        returncode: int | None = None
        timed_out = False
        process_error: str | None = None
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv, cwd=cwd, timeout=timeout, capture_output=True,
                shell=False, check=False,
            )
            stdout, stderr, returncode = completed.stdout, completed.stderr, completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = exc.stdout or b"", exc.stderr or b""
            timed_out = True
            process_error = f"HGS exceeded external timeout ({timeout} seconds); the child was killed and waited for."
        except OSError as exc:
            process_error = f"Unable to launch HGS: {exc}"
        elapsed = time.monotonic() - started
        # Bytes remain untouched. Parsing/decoding happens only after raw persistence.
        artifacts["stdout"].write_bytes(stdout)
        artifacts["stderr"].write_bytes(stderr)
        metadata.update(
            returncode=returncode, timed_out=timed_out,
            wall_elapsed_sec=elapsed, process_error=process_error,
        )
        _write_json(artifacts["process"], metadata)

        native = sorted((run_dir / "Solutions").glob("*/*.txt"))
        output_error: str | None = None
        raw_result: bytes | None = None
        prefix = (
            f"{scenario.number_of_stations}_{scenario.instance_id}"
            f"_t{scenario.number_of_trucks}_r{scenario.number_of_repairers}_"
        )
        safe_native: list[Path] = []
        for path in native:
            if not path.resolve().is_relative_to(run_dir) or path.is_symlink() or path.parent.is_symlink():
                output_error = "HGS result path references a file outside this run or a symlink"
                continue
            artifacts[f"native_result_{len(safe_native)}"] = path
            safe_native.append(path)
        if output_error is None:
            if len(safe_native) != 1:
                output_error = f"Expected exactly one HGS result file; found {len(safe_native)}"
            elif not safe_native[0].name.startswith(prefix):
                output_error = "HGS result filename does not match the current scenario"
            else:
                raw_result = safe_native[0].read_bytes()
                artifacts["solver_result"] = run_dir / "solver-result.txt"
                artifacts["solver_result"].write_bytes(raw_result)

        def failed(status: ResultStatus, reason: str, validation_errors: tuple[str, ...] = ()) -> SolutionResult:
            return SolutionResult(
                run_id=run_dir.name, backend=BackendName.HGS, status=status, feasible=None,
                warnings=(reason,), raw_artifact_paths=artifacts,
                solver_metadata={
                    **metadata, "error": reason, "validation_errors": list(validation_errors),
                    "validation": {
                        "status": "failed" if validation_errors else "not_performed",
                        "errors": list(validation_errors), "reason": reason,
                    },
                },
            )

        if timed_out:
            result = failed(ResultStatus.TIMEOUT, process_error or "HGS timed out")
        elif process_error or returncode != 0:
            result = failed(ResultStatus.FAILED, process_error or f"HGS exited with nonzero return code {returncode}")
        elif output_error:
            result = failed(ResultStatus.FAILED, output_error)
        else:
            try:
                assert raw_result is not None
                parsed = parse_hgs_output(raw_result.decode("utf-8", errors="strict"))
                result = SolutionResult(
                    run_id=run_dir.name, backend=BackendName.HGS, status=ResultStatus.SUCCEEDED, feasible=True,
                    objective_value=parsed.objective_value, dissatisfaction=parsed.dissatisfaction,
                    emission=parsed.emission, runtime_sec=parsed.runtime_sec,
                    truck_routes=parsed.truck_routes, repair_routes=parsed.repair_routes,
                    warnings=parsed.warnings, raw_artifact_paths=artifacts,
                    solver_metadata={**metadata, **parsed.metadata},
                )
                result = validate_result(scenario, result, run_dir=run_dir)
            except (HgsParseError, UnicodeDecodeError) as exc:
                result = failed(ResultStatus.FAILED, f"HGS parser failed: {exc}")
            except ResultValidationError as exc:
                result = failed(ResultStatus.FAILED, f"HGS result validation failed: {exc}", exc.errors)
        # No invalid routes/metrics are promoted on failure; raw output is retained.
        result = validate_result(scenario, result, run_dir=run_dir)
        verification_path = run_dir / "validation.json"
        _write_json(verification_path, result.solver_metadata["validation"])
        result = SolutionResult.model_validate({
            **result.model_dump(),
            "raw_artifact_paths": {**result.raw_artifact_paths, "validation": verification_path},
        })
        temporary = run_dir / "solution.json.tmp"
        temporary.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(run_dir / "solution.json")
        return result
