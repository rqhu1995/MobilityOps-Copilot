import hashlib
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from mobilityops.config import Settings
from mobilityops.domain import ScenarioSpec, SolutionResult
from mobilityops.solvers.hgs import ArtifactError, HgsBackend, HgsPreflightError, build_hgs_argv


def install_process(
    monkeypatch: pytest.MonkeyPatch, output: bytes | None, *, returncode: int = 0,
    exception: Exception | None = None,
) -> Mock:
    def execute(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess:
        run_dir = kwargs["cwd"].parent
        if output is not None:
            path = run_dir / "Solutions/2026-09-10/6_1_t1_r1_2h_1.txt"
            path.parent.mkdir(parents=True)
            path.write_bytes(output)
        if exception:
            raise exception
        return subprocess.CompletedProcess(argv, returncode, b"stdout\xff\n", b"stderr\xfe\n")
    mock = Mock(side_effect=execute)
    monkeypatch.setattr("mobilityops.solvers.hgs.backend.subprocess.run", mock)
    return mock


def test_real_fake_executable_success_and_reuse(settings: Settings, scenario: ScenarioSpec, raw_output: bytes) -> None:
    backend = HgsBackend(settings)
    result = backend.solve(scenario, run_id="fake-success")
    run = settings.runs_dir / result.run_id
    assert result.status == "succeeded" and result.feasible is True
    assert result.best_bound is result.optimality_gap is None
    assert result.raw_artifact_paths["solver_result"].read_bytes() == raw_output
    assert (run / "stdout.log").read_bytes() == b"fake HGS stdout\n"
    assert (run / "stderr.log").read_bytes() == b"fake HGS stderr\n"
    assert SolutionResult.model_validate_json((run / "solution.json").read_bytes()) == result
    assert json.loads((run / "command.json").read_text()) == list(build_hgs_argv(backend.executable, scenario))
    assert ScenarioSpec.model_validate_json((run / "scenario.json").read_bytes()) == scenario
    provenance = json.loads((run / "process.json").read_text())
    assert provenance["returncode"] == 0 and provenance["timeout_sec"] == 10
    for name, digest in provenance["input_sha256"].items():
        assert hashlib.sha256((run / "Instances/6_1" / name).read_bytes()).hexdigest() == digest
    assert not (settings.hgs_repo_path / "Solutions").exists()
    assert json.loads((run / "validation.json").read_text()) == result.solver_metadata["validation"]
    assert result.solver_metadata["validation"]["status"] == "passed"
    assert result.raw_artifact_paths["validation"] == run / "validation.json"
    original = {str(p): p.read_bytes() for p in run.rglob("*") if p.is_file()}
    with pytest.raises(HgsPreflightError, match="already exists"):
        backend.solve(scenario, run_id=result.run_id)
    assert original == {str(p): p.read_bytes() for p in run.rglob("*") if p.is_file()}


def test_execution_boundary_and_raw_bytes(monkeypatch: pytest.MonkeyPatch, settings: Settings, scenario: ScenarioSpec, raw_output: bytes) -> None:
    process = install_process(monkeypatch, raw_output)
    result = HgsBackend(settings, timeout_grace_sec=2).solve(scenario, run_id="boundary")
    process.assert_called_once_with(
        build_hgs_argv(settings.hgs_repo_path / "build/main", scenario),
        cwd=settings.runs_dir / "boundary/work", timeout=7, capture_output=True, shell=False, check=False,
    )
    assert result.raw_artifact_paths["stdout"].read_bytes() == b"stdout\xff\n"
    assert result.raw_artifact_paths["stderr"].read_bytes() == b"stderr\xfe\n"


@pytest.mark.parametrize(("target", "message"), [
    ("repo", "HGS repo"), ("executable", "existing file"),
    ("permission", "not executable"), ("instance", "instance directory"),
    ("matrix", "Required HGS input"), ("station", "Required HGS input"),
    ("dissat", "Required HGS input"), ("truck_priority", "Required HGS input"),
    ("repair_priority", "Required HGS input"),
])
def test_preflight_missing_resources(settings: Settings, scenario: ScenarioSpec, target: str, message: str) -> None:
    if target == "repo":
        settings = settings.model_copy(update={"hgs_repo_path": settings.hgs_repo_path / "absent"})
    elif target == "executable":
        (settings.hgs_repo_path / "build/main").unlink()
    elif target == "permission":
        (settings.hgs_repo_path / "build/main").chmod(0o600)
    elif target == "instance":
        scenario = scenario.model_copy(update={"instance_id": 2})
    else:
        filename = {"matrix": "time_matrix_6.txt", "station": "station_info_6.txt",
                    "dissat": "dissat_table_3.txt", "truck_priority": "BCRF_6.txt", "repair_priority": "BCRFR_1.txt"}[target]
        (settings.hgs_repo_path / "Instances/6_1" / filename).unlink()
    with pytest.raises(HgsPreflightError, match=message):
        HgsBackend(settings).solve(scenario, run_id="missing")
    assert not settings.runs_dir.exists()


@pytest.mark.parametrize("run_id", ["", ".", "..", "../escape", "a/b", "a\\b", "/absolute", "\\absolute", "-bad", " bad", "bad ", "a\nb", "中文", "a" * 201])
def test_unsafe_run_ids_rejected(settings: Settings, scenario: ScenarioSpec, run_id: str) -> None:
    with pytest.raises(ValueError, match="run_id"):
        HgsBackend(settings).solve(scenario, run_id=run_id)
    with pytest.raises(ValidationError):
        SolutionResult(run_id=run_id, backend="hgs", status="failed")
    assert not settings.runs_dir.exists()


@pytest.mark.parametrize("run_id", ["A", "a..b", "Run_1.2-3", "a" * 200])
def test_safe_run_ids_in_model(run_id: str) -> None:
    assert SolutionResult(run_id=run_id, backend="hgs", status="failed").run_id == run_id


def test_symlink_run_dir_rejected(settings: Settings, scenario: ScenarioSpec, tmp_path: Path) -> None:
    settings.runs_dir.mkdir()
    (settings.runs_dir / "escape").symlink_to(tmp_path)
    with pytest.raises(HgsPreflightError, match="symlink|escapes"):
        HgsBackend(settings).solve(scenario, run_id="escape")


@pytest.mark.parametrize("which", ["hgs_repo_path", "gurobi_repo_path"])
def test_never_write_runs_in_solver_repos(settings: Settings, scenario: ScenarioSpec, which: str) -> None:
    settings = settings.model_copy(update={"runs_dir": getattr(settings, which) / "runs"})
    with pytest.raises(HgsPreflightError, match="solver repository"):
        HgsBackend(settings).solve(scenario, run_id="bad-location")
    assert not settings.runs_dir.exists()


def test_relative_runs_rejected_even_with_unvalidated_settings(settings: Settings, scenario: ScenarioSpec) -> None:
    settings = settings.model_copy(update={"runs_dir": Path("relative")})
    with pytest.raises(HgsPreflightError, match="absolute"):
        HgsBackend(settings).solve(scenario, run_id="relative")


@pytest.mark.parametrize("grace", [0, -1, float("inf"), float("nan"), 61])
def test_invalid_timeout_grace(settings: Settings, grace: float) -> None:
    with pytest.raises(ValueError, match="timeout_grace_sec"):
        HgsBackend(settings, timeout_grace_sec=grace)


@pytest.mark.parametrize(("mode", "status", "message"), [
    ("nonzero", "failed", "nonzero return code 7"),
    ("timeout", "timeout", "external timeout"),
    ("timeout-empty", "timeout", "external timeout"),
    ("launch", "failed", "Unable to launch"),
    ("missing", "failed", "exactly one HGS result"),
    ("broken", "failed", "parser failed"),
    ("missing-objective", "failed", "objective_value"),
    ("encoding", "failed", "parser failed"),
    ("bad-route", "failed", "validation failed"),
])
def test_execution_failures_preserve_evidence(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, scenario: ScenarioSpec, raw_output: bytes,
    mode: str, status: str, message: str,
) -> None:
    output = raw_output
    exception = None
    if mode == "timeout":
        exception = subprocess.TimeoutExpired(["main"], 10, output=b"partial stdout\xff", stderr=b"partial stderr\xfe")
    elif mode == "timeout-empty":
        exception = subprocess.TimeoutExpired(["main"], 10)
        output = None
    elif mode == "launch":
        exception = OSError("exec format error")
        output = None
    elif mode == "missing":
        output = None
    elif mode == "broken":
        output = b"not a solution"
    elif mode == "missing-objective":
        output = raw_output.replace(b"individual's fitness value: 104.039\n", b"")
    elif mode == "encoding":
        output = raw_output + b"\xff"
    elif mode == "bad-route":
        output = raw_output.replace(b"load 16 usable", b"load 99 usable")
    install_process(monkeypatch, output, returncode=7 if mode == "nonzero" else 0, exception=exception)
    result = HgsBackend(settings).solve(scenario, run_id=mode)
    run = settings.runs_dir / mode
    assert result.status == status and result.feasible is None
    assert message in result.warnings[0]
    assert result.best_bound is result.optimality_gap is None
    assert not result.truck_routes and not result.repair_routes
    if output is not None:
        assert (run / "solver-result.txt").read_bytes() == output
    if mode == "timeout":
        assert (run / "stdout.log").read_bytes() == b"partial stdout\xff"
        assert (run / "stderr.log").read_bytes() == b"partial stderr\xfe"
        assert result.solver_metadata["returncode"] is None
    if mode == "timeout-empty":
        assert (run / "stdout.log").read_bytes() == (run / "stderr.log").read_bytes() == b""
    assert SolutionResult.model_validate_json((run / "solution.json").read_bytes()) == result


def test_actual_timeout_kills_fake_child(settings: Settings, scenario: ScenarioSpec) -> None:
    executable = settings.hgs_repo_path / "build/main"
    executable.write_text(
        f"#!{sys.executable}\nimport os, sys, time\n"
        "print(os.getpid(), flush=True)\n"
        "sys.stderr.buffer.write(b'partial stderr'); sys.stderr.flush()\n"
        "time.sleep(30)\n"
    )
    result = HgsBackend(settings, timeout_grace_sec=0.2).solve(
        scenario.model_copy(update={"solver_runtime_limit_sec": 0.2}), run_id="actual-timeout",
    )
    assert result.status == "timeout"
    pid = int(result.raw_artifact_paths["stdout"].read_bytes())
    assert not Path(f"/proc/{pid}").exists()
    assert result.raw_artifact_paths["stderr"].read_bytes() == b"partial stderr"
    assert result.solver_metadata["wall_elapsed_sec"] < 5


@pytest.mark.parametrize("filename", ["scenario.json", "stdout.log", "validation.json", "solution.json.tmp"])
def test_artifact_write_failure_is_explicit(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, scenario: ScenarioSpec, raw_output: bytes, filename: str,
) -> None:
    process = install_process(monkeypatch, raw_output)
    original_open = Path.open
    def open_file(path: Path, mode: str = "r", *args: object, **kwargs: object):
        if path.name == filename and "w" in mode:
            raise PermissionError("simulated write failure")
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(ArtifactError, match="simulated write failure") as error:
        HgsBackend(settings).solve(scenario, run_id="write-failure")
    assert error.value.run_dir == settings.runs_dir / "write-failure"
    if filename == "scenario.json":
        process.assert_not_called()
    if filename in {"validation.json", "solution.json.tmp"}:
        assert (error.value.run_dir / "solver-result.txt").read_bytes() == raw_output
        assert (error.value.run_dir / "stdout.log").read_bytes() == b"stdout\xff\n"
    assert not (error.value.run_dir / "solution.json").exists()


def test_runs_root_write_failure(settings: Settings, scenario: ScenarioSpec) -> None:
    settings.runs_dir.write_text("file blocks directory")
    with pytest.raises(ArtifactError):
        HgsBackend(settings).solve(scenario, run_id="root-write-failure")


@pytest.mark.parametrize("kind", ["other-run", "duplicate", "wrong-scenario"])
def test_output_discovery_rejects_ambiguous_or_foreign_files(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, scenario: ScenarioSpec, raw_output: bytes, tmp_path: Path, kind: str,
) -> None:
    foreign = tmp_path / "foreign.txt"
    foreign.write_bytes(raw_output)
    def execute(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess:
        folder = kwargs["cwd"].parent / "Solutions/2026-09-10"
        folder.mkdir()
        path = folder / "6_1_t1_r1_2h_1.txt"
        if kind == "other-run":
            path.symlink_to(foreign)
        elif kind == "duplicate":
            path.write_bytes(raw_output)
            (folder / "6_1_t1_r1_2h_2.txt").write_bytes(raw_output)
        else:
            (folder / "10_1_t1_r1_2h_1.txt").write_bytes(raw_output)
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr("mobilityops.solvers.hgs.backend.subprocess.run", execute)
    result = HgsBackend(settings).solve(scenario, run_id=kind)
    assert result.status == "failed"
    assert "solver_result" not in result.raw_artifact_paths
    assert foreign.read_bytes() == raw_output


@pytest.mark.parametrize("kind", ["metric", "changed-input", "missing-input", "route-budget"])
def test_independent_verification_failure_retains_raw_candidate(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, scenario: ScenarioSpec, raw_output: bytes, kind: str,
) -> None:
    candidate = raw_output.replace(b"fitness value: 104.039", b"fitness value: 100.000") if kind == "metric" else raw_output
    if kind == "route-budget":
        scenario = scenario.model_copy(update={"operation_time_budget_sec": 100.0})
    def execute(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess:
        run = kwargs["cwd"].parent
        output = run / "Solutions/2026-09-11/6_1_t1_r1_2h_1.txt"
        output.parent.mkdir()
        output.write_bytes(candidate)
        matrix = run / "Instances/6_1/time_matrix_6.txt"
        if kind == "changed-input":
            matrix.write_bytes(matrix.read_bytes() + b"\n")
        elif kind == "missing-input":
            matrix.unlink()
        return subprocess.CompletedProcess(argv, 0, b"raw stdout", b"raw stderr")
    monkeypatch.setattr("mobilityops.solvers.hgs.backend.subprocess.run", execute)
    result = HgsBackend(settings).solve(scenario, run_id="verification-" + kind)
    assert result.status == "failed" and result.feasible is None
    assert not result.truck_routes and result.objective_value is None
    assert result.raw_artifact_paths["solver_result"].read_bytes() == candidate
    assert result.raw_artifact_paths["stdout"].read_bytes() == b"raw stdout"
    report = json.loads(result.raw_artifact_paths["validation"].read_bytes())
    assert report == result.solver_metadata["validation"]
    assert report["status"] == "failed" and report["errors"]
    assert result.best_bound is result.optimality_gap is None
