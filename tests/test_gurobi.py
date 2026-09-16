import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

from mobilityops.config import Settings
from mobilityops.domain import BackendName, ObjectiveProfile, ResultStatus, ScenarioSpec, SolutionResult
from mobilityops.services.gurobi_inputs import load_inputs, strict_json
from mobilityops.services.gurobi_verification import verify_candidate
from mobilityops.services.solver_service import SolverService
from mobilityops.services.validation import ResultValidationError, validate_result
from mobilityops.solvers.gurobi import contracts
from mobilityops.solvers.gurobi.backend import GurobiBackend
from mobilityops.solvers.gurobi.contracts import GurobiConstraintError, GurobiOptions, GurobiPreflightError
from mobilityops.solvers.gurobi.result import RawGurobiResult
from mobilityops.solvers.hgs import ArtifactError, HgsBackend


FIXTURES = Path(__file__).parent / "fixtures/gurobi"


@pytest.fixture
def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: ScenarioSpec) -> tuple:
    repo = tmp_path / "gurobi repo"
    hashes = {}
    for name in contracts.AUDITED_SOURCES:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fake builder placeholder, never imported\n")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(contracts, "AUDITED_SOURCES", hashes)
    data = repo / "resources/datasets/1_1"
    data.mkdir(parents=True)
    (data / "station_info_1.txt").write_text("station_id\tcapacity\tcurUsable\ttargetUsable\tcurBroken\n101\t3\t1\t2\t1\n")
    (data / "time_matrix_1.txt").write_text("0\t60\n60\t0\n")
    (data / "linear_diss_1.txt").write_text("0,0,-2,3,10,0\n")
    settings = Settings(hgs_repo_path=tmp_path / "hgs", gurobi_repo_path=repo, runs_dir=tmp_path / "runs")
    python = tmp_path / "fake python"
    payload = json.loads((FIXTURES / "worker-result.json").read_text())
    native = (FIXTURES / "native.sol").read_bytes()
    python.write_text(
        f"#!{sys.executable}\n"
        "import json,sys\nfrom pathlib import Path\n"
        "assert sys.argv[1] == '-B'\n"
        "root=Path.cwd().parent\nrequest=json.loads((root/'request.json').read_bytes())\n"
        f"payload={payload!r}\n"
        "modules=('model','model.variables','model.constraints','model.objective','parameters','parameters.config','parameters.sets','parameters.dataloader')\n"
        "payload['imported_modules']={m:str(Path(request['repo'])/(m.replace('.','/')+('/__init__.py' if m in ('model','parameters') else '.py'))) for m in modules}\n"
        "(root/'worker-result.json').write_text(json.dumps(payload))\n"
        f"(root/'native.sol').write_bytes({native!r})\n"
    )
    python.chmod(0o700)
    options = GurobiOptions(python_executable=python)
    s = scenario.model_copy(update={"number_of_stations": 1, "truck_capacity": 3,
                                    "operation_time_budget_sec": 2400.0,
                                    "objective_profile": ObjectiveProfile.GUROBI_LINEAR_DEFAULT})
    return settings, s, options, GurobiBackend(settings, options=options)


def test_gurobi_service_fake_end_to_end(case: tuple) -> None:
    settings, s, options, backend = case
    result = SolverService(settings, gurobi_backend=backend).solve(s, backend=BackendName.GUROBI, run_id="gurobi-success")
    folder = settings.runs_dir / result.run_id
    assert result.status is ResultStatus.SUCCEEDED and result.feasible is True
    assert result.objective_value == pytest.approx(8.0331762396)
    report = result.solver_metadata["validation"]
    assert report["status"] == "passed"
    assert report["recomputed_metrics"]["repair_travel_sec"] == 202
    assert report["stations"][1]["timeline"][-1]["usable"] == 3
    assert report["routes"][0]["arcs"][0] == {"period": 1, "origin": 0, "destination": 1}
    assert result == SolutionResult.model_validate_json((folder / "solution.json").read_bytes())
    assert result == validate_result(s, result, run_dir=folder)
    assert (folder / "native.sol").read_bytes() == (FIXTURES / "native.sol").read_bytes()
    assert strict_json((folder / "validation.json").read_bytes()) == report
    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob("*") if p.is_file()}
    with pytest.raises(GurobiPreflightError, match="overwrite"):
        backend.solve(s, run_id=result.run_id)
    assert hashes == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in hashes}


@pytest.mark.parametrize("update", [
    {"seed": 0}, {"broken_bike_proportion": 0.0}, {"objective_profile": ObjectiveProfile.EUDF_DEFAULT},
    {"operation_time_budget_sec": 2401.0}, {"operation_time_budget_sec": 600.0},
    {"number_of_stations": 10000},
])
def test_rejected_before_execution(case: tuple, update: dict) -> None:
    settings, s, options, backend = case
    with pytest.raises(GurobiConstraintError):
        SolverService(settings, gurobi_backend=backend).solve(s.model_copy(update=update), backend=BackendName.GUROBI, run_id="reject")
    assert not settings.runs_dir.exists()


def test_full_parameter_mapping(case: tuple) -> None:
    _, s, options, _ = case
    assert contracts.solver_argv(s, options) == ["mobilityops-gurobi", "-S", "1", "-i", "1", "-K", "1", "-R", "1", "-NT", "4", "-tau", "600", "-Q", "3", "-tl", "60", "-tr", "300", "-r", "True"]
    assert not any(r.outcome.value == "rejected" for r in contracts.check_constraints(s, options))


def test_hgs_rejects_gurobi_objective(case: tuple) -> None:
    settings, s, _, _ = case
    records = HgsBackend(settings).check_constraints(s)
    assert next(r for r in records if r.constraint_name == "objective_profile").outcome.value == "rejected"


@pytest.mark.parametrize("run_id", ["../escape", "/absolute", ".", "..", "with/slash", " leading", "x\\y"])
def test_unsafe_run_id(case: tuple, run_id: str) -> None:
    settings, s, _, backend = case
    with pytest.raises(ValueError):
        backend.solve(s, run_id=run_id)
    assert not settings.runs_dir.exists()


@pytest.mark.parametrize("name", ["model/constraints.py", "resources/datasets/1_1/time_matrix_1.txt"])
def test_missing_source_or_input(case: tuple, name: str) -> None:
    settings, s, _, backend = case
    (settings.gurobi_repo_path / name).unlink()
    with pytest.raises(GurobiPreflightError):
        backend.solve(s, run_id="missing")
    assert not settings.runs_dir.exists()


def test_source_drift(case: tuple) -> None:
    settings, s, _, backend = case
    (settings.gurobi_repo_path / "model/constraints.py").write_text("changed")
    with pytest.raises(GurobiPreflightError, match="audit"):
        backend.solve(s, run_id="changed")
    assert not settings.runs_dir.exists()


@pytest.mark.parametrize("field", ["hgs_repo_path", "gurobi_repo_path"])
def test_run_root_not_solver_repo(case: tuple, field: str) -> None:
    settings, s, options, _ = case
    modified = settings.model_copy(update={"runs_dir": getattr(settings, field) / "runs"})
    with pytest.raises(GurobiPreflightError, match="outside"):
        GurobiBackend(modified, options=options).solve(s, run_id="bad-root")


def test_service_config_mismatch(case: tuple) -> None:
    settings, _, _, backend = case
    with pytest.raises(ValueError, match="same Settings"):
        SolverService(settings.model_copy(update={"runs_dir": settings.runs_dir / "other"}), gurobi_backend=backend)


@pytest.mark.parametrize("name,content", [
    ("station_info_1.txt", "bad header\n"),
    ("time_matrix_1.txt", "0\tNaN\n60\t0\n"),
    ("time_matrix_1.txt", "0\t60\n"),
    ("linear_diss_1.txt", "0,0,inf,3,10,0\n"),
    ("linear_diss_1.txt", ""),
])
def test_bad_input_prevents_child(case: tuple, monkeypatch: pytest.MonkeyPatch, name: str, content: str) -> None:
    settings, s, _, backend = case
    (settings.gurobi_repo_path / "resources/datasets/1_1" / name).write_text(content)
    call = Mock()
    monkeypatch.setattr("mobilityops.solvers.gurobi.backend.subprocess.run", call)
    with pytest.raises(GurobiPreflightError, match="snapshot"):
        backend.solve(s, run_id="invalid-input")
    call.assert_not_called()
    assert json.loads((settings.runs_dir / "invalid-input/validation.json").read_text())["status"] == "failed"


def rewrite_fake(options: GurobiOptions, suffix: str) -> None:
    with options.python_executable.open("a") as file:
        file.write(suffix)


@pytest.mark.parametrize("status,count,expected,feasible", [
    (9, 1, ResultStatus.TIMEOUT, True), (9, 0, ResultStatus.TIMEOUT, None),
    (3, 0, ResultStatus.INFEASIBLE, False), (4, 0, ResultStatus.FAILED, None),
    (5, 0, ResultStatus.FAILED, None), (11, 1, ResultStatus.FAILED, None),
    (12, 1, ResultStatus.FAILED, None),
])
def test_sdk_terminal_states(case: tuple, status: int, count: int, expected: ResultStatus, feasible: bool | None) -> None:
    settings, s, options, backend = case
    rewrite_fake(options, f"payload.update(status={status},solution_count={count})\n" +
                 ("payload.update(variables={},objective_value=None,optimality_gap=None,best_bound=None)\n(root/'native.sol').unlink()\n" if count == 0 else "") +
                 "(root/'worker-result.json').write_text(json.dumps(payload))\n")
    result = backend.solve(s, run_id="terminal")
    assert result.status is expected and result.feasible is feasible
    if feasible is True:
        assert result.truck_routes and result.solver_metadata["validation"]["status"] == "passed"
    else:
        assert not result.truck_routes and result.objective_value is None and result.optimality_gap is None


@pytest.mark.parametrize("suffix", [
    "payload.update(status=2,solution_count=0,variables={},objective_value=None,optimality_gap=None)\n(root/'worker-result.json').write_text(json.dumps(payload))\n",
    "payload['objective_value']+=1\n(root/'worker-result.json').write_text(json.dumps(payload))\n",
    "(root/'worker-result.json').write_text('{bad json')\n",
    "(root/'worker-result.json').write_text('{\"status\":2,\"status\":9}')\n",
    "(root/'worker-result.json').unlink()\n",
    "(root/'native.sol').unlink()\n",
    "(root/'native.sol').write_bytes(b'broken native')\n",
    "(root/'worker-result.json').write_text('{\"worker_error\":\"GurobiError\",\"phase\":\"license\",\"code\":10009}')\n",
    "sys.exit(3)\n",
])
def test_failed_output_preserves_evidence(case: tuple, suffix: str) -> None:
    settings, s, options, backend = case
    rewrite_fake(options, suffix)
    result = backend.solve(s, run_id="bad-output")
    assert result.status is ResultStatus.FAILED and result.feasible is None
    assert result.objective_value is None and not result.truck_routes
    assert (settings.runs_dir / "bad-output/solution.json").is_file()
    assert result.raw_artifact_paths["stdout"].is_file()


def test_real_fake_child_timeout(case: tuple) -> None:
    settings, s, options, _ = case
    options.python_executable.write_text(f"#!{sys.executable}\nimport time,sys,os\nprint(os.getpid(),flush=True)\nprint('partial error',file=sys.stderr,flush=True)\ntime.sleep(30)\n")
    fast = options.model_copy(update={"timeout_grace_sec": .05})
    backend = GurobiBackend(settings, options=fast)
    result = backend.solve(s.model_copy(update={"solver_runtime_limit_sec": .1}), run_id="timeout")
    assert result.status is ResultStatus.TIMEOUT and result.feasible is None
    pid = int(result.raw_artifact_paths["stdout"].read_text())
    assert not Path(f"/proc/{pid}").exists()
    assert result.raw_artifact_paths["stderr"].read_text() == "partial error\n"


@pytest.mark.parametrize("filename", ["scenario.json", "stdout.log", "validation.json.tmp", "solution.json.tmp"])
def test_persistence_failures(case: tuple, monkeypatch: pytest.MonkeyPatch, filename: str) -> None:
    _, s, _, backend = case
    original = Path.open
    def fail(path: Path, *args, **kwargs):
        if path.name == filename or path.name == filename + ".tmp":
            raise PermissionError("read-only artifact")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(ArtifactError):
        backend.solve(s, run_id="write-error")


@pytest.fixture
def verified(case: tuple) -> tuple:
    settings, s, options, backend = case
    result = backend.solve(s, run_id="verify")
    assert result.status is ResultStatus.SUCCEEDED, result.warnings
    folder = settings.runs_dir / "verify"
    data = load_inputs(s, folder, result.solver_metadata["input_sha256"])
    raw = RawGurobiResult.model_validate(strict_json((folder / "worker-result.json").read_bytes()))
    return s, options, data, raw, result, folder


@pytest.mark.parametrize("variable", [
    "station_inv_usable[1,2]", "station_inv_broken[1,2]", "prev_truck_inv_usable[0,1,1,0]",
    "prev_truck_inv_broken[0,1,1,0]", "reb_amount_usable+[1,2,0]", "reb_amount_usable-[0,1,0]",
    "reb_amount_broken+[1,2,0]", "reb_amount_broken-[0,1,0]", "repaired_amount_man[1,2,0]",
    "emission[0,1,1,0]", "upper_bound[1,0]", "lower_bound[1,0]", "upper_bound_man[2,0]",
    "lower_bound_man[2,0]", "stop_time[0]", "stop_time_rpm[0]", "aux_stop[1,0]",
    "aux_stop_rpm[1,0]", "visit_truck[0,1,1,0]", "visit_man[0,1,1,0]",
    "operating_time_truck[0]", "operating_time_rpm[0]", "dissat[1]",
])
def test_independent_equations_reject_corruption(verified: tuple, variable: str) -> None:
    s, options, data, raw, _, _ = verified
    corrupted = {**raw.variables, variable: raw.variables[variable] + 1}
    with pytest.raises(ValueError):
        verify_candidate(s, options, data, raw.model_copy(update={"variables": corrupted}))


def test_epigraph_slack_is_valid_for_incumbent(verified: tuple) -> None:
    s, options, data, raw, _, _ = verified
    variables = {**raw.variables, "dissat[1]": raw.variables["dissat[1]"] + 1}
    objective = raw.objective_value + 2
    candidate = raw.model_copy(update={"status": 9, "variables": variables, "objective_value": objective,
                                       "optimality_gap": abs(objective - raw.best_bound) / abs(objective)})
    report = verify_candidate(s, options, data, candidate)
    assert report["dissatisfaction_envelopes"][0]["slack"] == 1


@pytest.mark.parametrize("update", [
    {"objective_value": 900.0}, {"best_bound": 900.0}, {"optimality_gap": .5},
    {"variables": {}}, {"model_variables": 0},
])
def test_invalid_metrics_and_shape(verified: tuple, update: dict) -> None:
    s, options, data, raw, _, _ = verified
    with pytest.raises(ValueError):
        verify_candidate(s, options, data, raw.model_copy(update=update))


@pytest.mark.parametrize("artifact", ["scenario.json", "options.json", "request.json", "worker-result.json", "native.sol", "work/resources/datasets/1_1/linear_diss_1.txt"])
def test_revalidation_rejects_changed_evidence(verified: tuple, artifact: str) -> None:
    s, _, _, _, result, folder = verified
    (folder / artifact).write_text("tampered")
    with pytest.raises(ResultValidationError):
        validate_result(s, result, run_dir=folder)


def test_claimed_pass_does_not_bypass_revalidation(verified: tuple) -> None:
    s, _, _, _, result, folder = verified
    changed = result.model_copy(update={"objective_value": result.objective_value + 1})
    with pytest.raises(ResultValidationError, match="disagrees"):
        validate_result(s, changed, run_dir=folder)


def test_internal_symlink_evidence_rejected(verified: tuple) -> None:
    s, _, _, _, result, folder = verified
    original = folder / "native.sol"
    original.rename(folder / "other.sol")
    original.symlink_to(folder / "other.sol")
    with pytest.raises(ResultValidationError, match="symlinks"):
        validate_result(s, result, run_dir=folder)


def test_period_budget_rejects_consistent_but_slow_operations(verified: tuple) -> None:
    s, options, data, raw, _, _ = verified
    slow = s.model_copy(update={"loading_time_sec": 600})
    values = dict(raw.variables)
    for t in range(1, 5):
        extra = 540 * (1 if t == 1 else 2)
        values[f"upper_bound[{t},0]"] += extra
        values[f"lower_bound[{t},0]"] += extra
    values["operating_time_truck[0]"] += 1080
    candidate = raw.model_copy(update={"variables": values, "objective_value": raw.objective_value + 1080e-8})
    with pytest.raises(ValueError, match="period upper budget"):
        verify_candidate(slow, options, data, candidate)


def test_unknown_variable_is_not_ignored(verified: tuple) -> None:
    s, options, data, raw, _, _ = verified
    with pytest.raises(ValueError, match="variable set"):
        verify_candidate(s, options, data, raw.model_copy(update={"variables": {**raw.variables, "extra[0]": 0.0}}))


def test_nonintegral_operation_rejected(verified: tuple) -> None:
    s, options, data, raw, _, _ = verified
    with pytest.raises(ValueError, match="Non-integral"):
        verify_candidate(s, options, data, raw.model_copy(update={"variables": {**raw.variables, "reb_amount_usable+[1,2,0]": 1.2}}))


def test_run_symlink_rejected(case: tuple) -> None:
    settings, s, _, backend = case
    settings.runs_dir.mkdir()
    (settings.runs_dir / "linked").symlink_to(settings.gurobi_repo_path, target_is_directory=True)
    with pytest.raises(GurobiPreflightError, match="unsafe"):
        backend.solve(s, run_id="linked")


@pytest.mark.parametrize("remove", [False, True])
def test_unavailable_python(case: tuple, remove: bool) -> None:
    _, s, options, backend = case
    if remove:
        options.python_executable.unlink()
    else:
        options.python_executable.chmod(0o600)
    with pytest.raises(GurobiPreflightError, match="unavailable"):
        backend.solve(s, run_id="no-python")


def test_launch_failure_is_saved(case: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, s, _, backend = case
    monkeypatch.setattr("mobilityops.solvers.gurobi.backend.subprocess.run", Mock(side_effect=OSError("launch failure")))
    result = backend.solve(s, run_id="launch-error")
    assert result.status is ResultStatus.FAILED
    assert (settings.runs_dir / "launch-error/solution.json").is_file()


def test_explicit_input_directory(case: tuple) -> None:
    settings, s, options, _ = case
    source = settings.gurobi_repo_path / "resources/datasets/1_1"
    renamed = settings.runs_dir.parent / "custom data"
    source.rename(renamed)
    backend = GurobiBackend(settings, options=options, instance_directory=renamed)
    assert backend.solve(s, run_id="custom-data").feasible is True


@pytest.mark.parametrize("update", [{"threads": 0}, {"time_interval_sec": 0}, {"timeout_grace_sec": 0},
                                     {"timeout_grace_sec": float("inf")}, {"python_executable": Path("relative")}])
def test_invalid_options(case: tuple, update: dict) -> None:
    _, _, options, _ = case
    with pytest.raises(ValueError):
        GurobiOptions.model_validate({**options.model_dump(), **update})
