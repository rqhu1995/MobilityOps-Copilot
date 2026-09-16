"""Bind normalized Gurobi results to their complete immutable run evidence."""

import math
from pathlib import Path

from mobilityops.domain import ScenarioSpec, SolutionResult
from mobilityops.services.gurobi_inputs import load_inputs, read_local, strict_json
from mobilityops.services.gurobi_verification import verify_candidate
from mobilityops.solvers.gurobi import contracts
from mobilityops.solvers.gurobi.result import RawGurobiResult, normalized_fields


def validate_gurobi_evidence(scenario: ScenarioSpec, result: SolutionResult, run_dir: Path) -> dict:
    metadata = result.solver_metadata
    request = strict_json(read_local(run_dir, run_dir / "request.json"))
    options = contracts.GurobiOptions.model_validate(strict_json(read_local(run_dir, run_dir / "options.json")))
    if metadata.get("options") != options.model_dump(mode="json"):
        raise ValueError("Gurobi options metadata differs from snapshot")
    if request["solver_argv"] != contracts.solver_argv(scenario, options) or request["runtime_sec"] != scenario.solver_runtime_limit_sec or request["threads"] != options.threads:
        raise ValueError("Gurobi execution request does not match the scenario")
    if request["source_sha256"] != contracts.AUDITED_SOURCES or metadata.get("source_sha256") != request["source_sha256"]:
        raise ValueError("Gurobi evidence uses an unaudited model source")
    records = contracts.check_constraints(scenario, options)
    if any(r.outcome.value == "rejected" for r in records):
        raise ValueError("Gurobi candidate violates capability constraints")
    hashes = metadata.get("input_sha256")
    if not isinstance(hashes, dict):
        raise ValueError("Missing Gurobi input hashes")
    data = load_inputs(scenario, run_dir, hashes)
    raw_path = run_dir / "worker-result.json"
    if result.raw_artifact_paths.get("solver_result") != raw_path:
        raise ValueError("Gurobi raw result role does not identify the canonical result")
    raw = RawGurobiResult.model_validate(strict_json(read_local(run_dir, raw_path)))
    for module, path in raw.imported_modules.items():
        suffix = module.replace(".", "/")
        expected = Path(request["repo"]) / (suffix + ("/__init__.py" if module in {"model", "parameters"} else ".py"))
        if Path(path) != expected:
            raise ValueError("Gurobi builder provenance mismatch")
    if set(raw.imported_modules) != {"model", "model.variables", "model.constraints", "model.objective", "parameters", "parameters.config", "parameters.sets", "parameters.dataloader"}:
        raise ValueError("Incomplete Gurobi builder provenance")
    for name, expected in normalized_fields(raw, scenario, options).items():
        if getattr(result, name) != expected:
            raise ValueError(f"Normalized Gurobi field disagrees with raw evidence: {name}")
    if result.feasible is not True:
        return {"status": "not_performed", "reason": "No promoted incumbent", "sdk_status": raw.status}
    native_path = run_dir / "native.sol"
    if result.raw_artifact_paths.get("native_result") != native_path:
        raise ValueError("Missing native Gurobi candidate artifact")
    lines = read_local(run_dir, native_path).decode("utf-8").splitlines()
    if lines and lines[0].startswith("# Solution for model "):
        lines = lines[1:]
    if not lines or not lines[0].startswith("# Objective value = "):
        raise ValueError("Invalid native Gurobi solution header")
    native = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        name, value = line.split()
        if name in native:
            raise ValueError("Duplicate native Gurobi variable")
        native[name] = float(value)
    if set(native) != set(raw.variables):
        raise ValueError("Native Gurobi variable set differs from the raw JSON")
    for name, value in native.items():
        if not math.isfinite(value) or not math.isclose(value, raw.variables[name], rel_tol=1e-12, abs_tol=1e-9):
            raise ValueError(f"Native Gurobi variable differs from raw JSON: {name}")
    if not math.isclose(float(lines[0].partition(" = ")[2]), raw.objective_value, rel_tol=1e-12, abs_tol=1e-9):
        raise ValueError("Native Gurobi objective differs from raw JSON")
    report = verify_candidate(scenario, options, data, raw)
    report["checked_input_sha256"] = hashes
    report["checked_source_sha256"] = request["source_sha256"]
    return report
