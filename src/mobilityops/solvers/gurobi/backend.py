"""Isolated Gurobi execution using audited builders and independent verification."""

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time

from mobilityops.config import Settings
from mobilityops.domain import BackendName, ResultStatus, ScenarioSpec, SolutionResult
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.gurobi_inputs import input_names, load_inputs, parse_inputs, strict_json
from mobilityops.services.validation import ResultValidationError, validate_result
from mobilityops.solvers.gurobi import contracts
from mobilityops.solvers.gurobi.result import RawGurobiResult, normalized_fields
from mobilityops.solvers.hgs.errors import ArtifactError
from mobilityops.solvers.contracts import BackendCapability, ConstraintRecord


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class GurobiBackend:
    def __init__(self, settings: Settings, *, options: contracts.GurobiOptions,
                 instance_directory: Path | None = None) -> None:
        self.settings = Settings.model_validate(settings.model_dump())
        self.options = contracts.GurobiOptions.model_validate(options.model_dump())
        if instance_directory is not None and not instance_directory.is_absolute():
            raise ValueError("instance_directory must be absolute")
        self.instance_directory = instance_directory

    @property
    def capability(self) -> BackendCapability:
        return contracts.GUROBI_CAPABILITY

    def check_constraints(self, scenario: ScenarioSpec) -> tuple[ConstraintRecord, ...]:
        return contracts.check_constraints(scenario, self.options)

    def _prepare(self, scenario: ScenarioSpec, run_id: str) -> tuple[Path, Path, Path, dict[str, bytes], float, Path]:
        scenario = ScenarioSpec.model_validate(scenario.model_dump())
        validate_run_id(run_id)
        records = self.check_constraints(scenario)
        if any(r.outcome.value == "rejected" for r in records):
            raise contracts.GurobiConstraintError(records)
        repo = self.settings.gurobi_repo_path.resolve()
        root = self.settings.runs_dir.resolve()
        python = self.options.python_executable
        if not repo.is_dir() or not python.is_file() or not os.access(python, os.X_OK):
            raise contracts.GurobiPreflightError("Gurobi repository or Python executable is unavailable")
        if any(root.is_relative_to(path.resolve()) for path in (repo, self.settings.hgs_repo_path)):
            raise contracts.GurobiPreflightError("Run directory must be outside both solver repositories")
        run_dir = root / run_id
        if run_dir.exists() or run_dir.is_symlink() or not run_dir.resolve().is_relative_to(root):
            raise contracts.GurobiPreflightError("Run directory exists or is unsafe; refusing overwrite")
        timeout = scenario.solver_runtime_limit_sec + self.options.timeout_grace_sec
        if not math.isfinite(timeout) or timeout > 86400:
            raise contracts.GurobiPreflightError("External timeout must be finite and at most one day")
        try:
            for name, digest in contracts.AUDITED_SOURCES.items():
                if hashlib.sha256((repo / name).read_bytes()).hexdigest() != digest:
                    raise contracts.GurobiPreflightError(f"Gurobi source changed; a new audit is required: {name}")
            instance = self.instance_directory if self.instance_directory is not None else repo / "resources/datasets" / f"{scenario.number_of_stations}_{scenario.instance_id}"
            inputs = {name: (instance / name).read_bytes() for name in input_names(scenario)}
        except OSError as exc:
            raise contracts.GurobiPreflightError("Required Gurobi model or input file is unavailable") from exc
        return repo, root, run_dir, inputs, timeout, instance

    def preflight(self, scenario: ScenarioSpec, *, run_id: str) -> float:
        """Readiness checks including input parsing; never starts a license environment."""
        _, _, _, inputs, timeout, _ = self._prepare(scenario, run_id)
        parse_inputs(scenario, lambda name: inputs[name].decode("utf-8"))
        return timeout

    def solve(self, scenario: ScenarioSpec, *, run_id: str) -> SolutionResult:
        scenario = ScenarioSpec.model_validate(scenario.model_dump())
        repo, root, run_dir, inputs, timeout, instance = self._prepare(scenario, run_id)
        try:
            root.mkdir(parents=True, exist_ok=True)
            try:
                run_dir.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise contracts.GurobiPreflightError("Run directory already reserved") from exc
            return self._run(scenario, run_dir, repo, inputs, timeout, instance)
        except OSError as exc:
            raise ArtifactError(run_dir, "Gurobi artifact persistence failed") from exc

    def _run(self, scenario: ScenarioSpec, run_dir: Path, repo: Path, inputs: dict[str, bytes], timeout: float,
             instance: Path) -> SolutionResult:
        options = self.options
        worker = Path(__file__).with_name("worker.py").resolve()
        argv = [str(options.python_executable), "-B", str(worker)]
        write_json(run_dir / "scenario.json", scenario.model_dump(mode="json"))
        write_json(run_dir / "options.json", options.model_dump(mode="json"))
        write_json(run_dir / "command.json", argv)
        request = {"repo": str(repo), "source_sha256": contracts.AUDITED_SOURCES,
                   "solver_argv": contracts.solver_argv(scenario, options), "runtime_sec": scenario.solver_runtime_limit_sec,
                   "threads": options.threads}
        write_json(run_dir / "request.json", request)
        cwd = run_dir / "work"
        snapshot = cwd / "resources/datasets" / f"{scenario.number_of_stations}_{scenario.instance_id}"
        snapshot.mkdir(parents=True)
        hashes = {}
        for name, content in inputs.items():
            (snapshot / name).write_bytes(content)
            hashes[name] = hashlib.sha256(content).hexdigest()
        metadata = {"options": options.model_dump(mode="json"), "input_sha256": hashes, "source_instance": str(instance.resolve()),
                    "source_sha256": contracts.AUDITED_SOURCES, "worker_sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
                    "cwd": str(cwd), "timeout_sec": timeout}
        write_json(run_dir / "process.json", metadata)
        # Validate the exact bytes that the child will read before licensing/building.
        try:
            load_inputs(scenario, run_dir, hashes)
        except (ValueError, OSError) as exc:
            write_json(run_dir / "validation.json", {"status": "failed", "reason": str(exc)})
            raise contracts.GurobiPreflightError(f"Invalid Gurobi snapshot: {exc}") from exc
        started = time.monotonic()
        process_error, timed_out, returncode = None, False, None
        stdout = stderr = b""
        try:
            completed = subprocess.run(argv, cwd=cwd, timeout=timeout, capture_output=True, shell=False, check=False)
            stdout, stderr, returncode = completed.stdout, completed.stderr, completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = exc.stdout or b"", exc.stderr or b""
            timed_out, process_error = True, "Gurobi process exceeded external timeout; child killed and reaped"
        except OSError:
            process_error = "Unable to launch configured Gurobi Python executable"
        (run_dir / "stdout.log").write_bytes(stdout)
        (run_dir / "stderr.log").write_bytes(stderr)
        metadata.update(returncode=returncode, timed_out=timed_out, wall_elapsed_sec=time.monotonic() - started)
        write_json(run_dir / "process.json", metadata)
        artifacts = {name: run_dir / filename for name, filename in (
            ("scenario", "scenario.json"), ("command", "command.json"), ("options", "options.json"),
            ("request", "request.json"), ("process", "process.json"), ("stdout", "stdout.log"), ("stderr", "stderr.log"),
        )}
        for role, name in (("solver_result", "worker-result.json"), ("native_result", "native.sol"), ("worker_phase", "worker-phase.json")):
            path = run_dir / name
            if path.is_file() and not path.is_symlink():
                artifacts[role] = path

        def failed(reason: str, status: ResultStatus = ResultStatus.FAILED, validation: bool = False) -> SolutionResult:
            return SolutionResult(run_id=run_dir.name, backend=BackendName.GUROBI, status=status,
                                  raw_artifact_paths=artifacts, warnings=(reason,),
                                  solver_metadata={**metadata, "error": reason, "validation": {
                                      "status": "failed" if validation else "not_performed", "reason": reason}})

        if timed_out:
            result = failed(process_error, ResultStatus.TIMEOUT)
        elif process_error or returncode != 0:
            result = failed(process_error or f"Gurobi child exited with status {returncode}")
        elif "solver_result" not in artifacts:
            result = failed("Gurobi child produced no complete raw result")
        else:
            try:
                payload = strict_json(artifacts["solver_result"].read_bytes())
                if isinstance(payload, dict) and "worker_error" in payload:
                    # Worker exports only safe type/phase/code. Do not echo arbitrary raw fields.
                    result = failed("Gurobi worker failed; see safe worker-result.json diagnostics")
                else:
                    raw = RawGurobiResult.model_validate(payload)
                    warnings = ()
                    if raw.status == 9:
                        warnings = ("Gurobi reached its time limit; candidate availability is reported separately as feasible.",)
                    elif raw.status == 3:
                        warnings = ("Infeasibility was reported only for this Gurobi period model, not HGS or a continuous physical model.",)
                    elif raw.status != 2:
                        warnings = (f"Gurobi SDK status {raw.status} is not promoted by this adapter version.",)
                    result = SolutionResult(
                        run_id=run_dir.name, backend=BackendName.GUROBI, raw_artifact_paths=artifacts,
                        warnings=warnings,
                        **normalized_fields(raw, scenario, options),
                        solver_metadata={**metadata, "sdk_status": raw.status, "solution_count": raw.solution_count,
                                         "gurobi_version": raw.gurobi_version,
                                         "validation": {"status": "not_performed", "reason": "No promoted incumbent"}},
                    )
                    result = validate_result(scenario, result, run_dir=run_dir)
            except (ValueError, ResultValidationError, KeyError, TypeError) as exc:
                result = failed(f"Gurobi result rejected: {exc}", validation=True)
        result = validate_result(scenario, result, run_dir=run_dir)
        validation_path = run_dir / "validation.json"
        write_json(validation_path, result.solver_metadata["validation"])
        result = SolutionResult.model_validate({**result.model_dump(), "raw_artifact_paths": {**artifacts, "validation": validation_path}})
        write_json(run_dir / "solution.json", result.model_dump(mode="json"))
        return result
