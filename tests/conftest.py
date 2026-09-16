from pathlib import Path
import hashlib
import sys

import pytest

from mobilityops.config import Settings
from mobilityops.domain import BackendName, ResultStatus, ScenarioSpec, SolutionResult
from mobilityops.solvers.hgs.backend import required_hgs_inputs
from mobilityops.solvers.hgs.parser import parse_hgs_output


FIXTURE = Path(__file__).parent / "fixtures/hgs/6_1.txt"
INSTANCE_FIXTURE = FIXTURE.parent / "instance_6_1"


def write_instance_snapshot(folder: Path) -> dict[str, str]:
    folder.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for source in INSTANCE_FIXTURE.iterdir():
        data = source.read_bytes()
        (folder / source.name).write_bytes(data)
        hashes[source.name] = hashlib.sha256(data).hexdigest()
    return hashes


@pytest.fixture
def scenario() -> ScenarioSpec:
    return ScenarioSpec.model_validate_json(
        (Path(__file__).parents[1] / "examples/scenario_6_1.json").read_text()
    )


@pytest.fixture
def raw_output() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture
def settings(tmp_path: Path, scenario: ScenarioSpec, raw_output: bytes) -> Settings:
    repo = tmp_path / "hgs repo"
    instance = repo / "Instances/6_1"
    instance.mkdir(parents=True)
    for name in required_hgs_inputs(scenario):
        (instance / name).write_text("test input\n")
    write_instance_snapshot(instance)
    executable = repo / "build/main"
    executable.parent.mkdir()
    executable.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        "import sys\n"
        "assert Path('../Instances/6_1/station_info_6.txt').is_file()\n"
        "out = Path('../Solutions/2026-09-10/6_1_t1_r1_2h_1.txt')\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        f"out.write_bytes({raw_output!r})\n"
        "sys.stdout.buffer.write(b'fake HGS stdout\\n')\n"
        "sys.stderr.buffer.write(b'fake HGS stderr\\n')\n"
    )
    executable.chmod(0o700)
    return Settings(hgs_repo_path=repo, gurobi_repo_path=tmp_path / "gurobi", runs_dir=tmp_path / "runs")


@pytest.fixture
def valid_result(tmp_path: Path, raw_output: bytes, scenario: ScenarioSpec) -> tuple[SolutionResult, Path]:
    run_dir = tmp_path / "validation-run"
    run_dir.mkdir()
    artifacts = {role: run_dir / name for role, name in (
        ("scenario", "scenario.json"), ("command", "command.json"),
        ("stdout", "stdout.log"), ("stderr", "stderr.log"), ("solver_result", "solver-result.txt"),
    )}
    for path in artifacts.values():
        path.write_bytes(b"fixture")
    artifacts["scenario"].write_text(scenario.model_dump_json())
    artifacts["solver_result"].write_bytes(raw_output)
    hashes = write_instance_snapshot(run_dir / "Instances/6_1")
    parsed = parse_hgs_output(raw_output.decode())
    return SolutionResult(
        run_id=run_dir.name, backend=BackendName.HGS, status=ResultStatus.SUCCEEDED, feasible=True,
        objective_value=parsed.objective_value, dissatisfaction=parsed.dissatisfaction,
        emission=parsed.emission, runtime_sec=parsed.runtime_sec,
        truck_routes=parsed.truck_routes, repair_routes=parsed.repair_routes,
        raw_artifact_paths=artifacts, solver_metadata={**parsed.metadata, "input_sha256": hashes}, warnings=parsed.warnings,
    ), run_dir
