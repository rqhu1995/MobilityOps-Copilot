"""Shared result checks and backend-specific verification of run evidence."""

from pathlib import Path

from pydantic import ValidationError

from mobilityops.domain import BackendName, ResultStatus, ScenarioSpec, SolutionResult
from mobilityops.services.hgs_instance import HgsInputError, load_hgs_instance
from mobilityops.services.hgs_verification import HgsVerificationError, verify_hgs_solution
from mobilityops.services.gurobi_result_validation import validate_gurobi_evidence
from mobilityops.services.gurobi_verification import MODEL_WARNING as GUROBI_MODEL_WARNING


_LEGACY_STRUCTURAL_WARNING = (
    "Validation checks route structure, resource counts and truck inventory/capacity at stops. "
    "It does not independently verify station inventory over time, arrival ordering, travel/operation budgets, "
    "or recompute dissatisfaction, emissions and objective from instance tables."
)
_LEGACY_FEASIBILITY_WARNING = (
    "HGS text has no explicit feasibility flag; feasibility follows the audited "
    "best-feasible selection in Genetic::run plus structural validation."
)


class ResultValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(errors))


def validate_result(
    scenario: ScenarioSpec, result: SolutionResult, *, run_dir: Path,
) -> SolutionResult:
    """Return a validated result with explicit, idempotent limitation warnings."""

    # Revalidate even model_copy/model_construct instances at the trust boundary.
    try:
        result = SolutionResult.model_validate(result.model_dump())
    except ValidationError as exc:
        raise ResultValidationError([f"Invalid result model: {exc}"]) from exc
    errors: list[str] = []
    warnings = list(result.warnings)
    if not run_dir.is_absolute() or run_dir.name != result.run_id or run_dir.is_symlink():
        errors.append("Run directory must be absolute and identify this run without a symlink")
    resolved_run = run_dir.resolve()
    if result.status is ResultStatus.SUCCEEDED:
        for field in ("objective_value", "runtime_sec"):
            if getattr(result, field) is None:
                errors.append(f"Successful result requires {field}")
    if result.backend is BackendName.HGS:
        if result.best_bound is not None or result.optimality_gap is not None:
            errors.append("HGS cannot report best_bound or optimality_gap")
        if result.status is ResultStatus.SUCCEEDED:
            for field in ("dissatisfaction", "emission"):
                if getattr(result, field) is None:
                    errors.append(f"Successful HGS result requires {field}")

    for routes, count, label, id_field in (
        (result.truck_routes, scenario.number_of_trucks, "truck", "truck_id"),
        (result.repair_routes, scenario.number_of_repairers, "repairer", "repairer_id"),
    ):
        ids = [getattr(route, id_field) for route in routes]
        if len(set(ids)) != len(ids):
            errors.append(f"Duplicate {label} IDs")
        if len(routes) > count or any(identifier >= count for identifier in ids):
            errors.append(f"{label} routes exceed scenario resources")
        if result.backend is BackendName.HGS and result.status is ResultStatus.SUCCEEDED and len(routes) != count:
            errors.append(f"HGS must report one {label} block per configured resource, including idle routes")
        for route in routes:
            if any(stop.station_id > scenario.number_of_stations for stop in route.stops):
                errors.append(f"{label} station ID outside scenario range")
            if result.backend is BackendName.HGS and (
                len(route.stops) < 2 or route.stops[0].station_id != 0 or route.stops[-1].station_id != 0
            ):
                errors.append(f"HGS {label} route must start and end at depot 0")

    for route in result.truck_routes if result.backend is BackendName.HGS else ():
        usable = broken = 0
        for stop in route.stops:
            usable -= stop.unload_usable_bikes
            broken -= stop.unload_broken_bikes
            if usable < 0 or broken < 0:
                errors.append(f"Truck {route.truck_id} unloads unavailable inventory at station {stop.station_id}")
            usable += stop.load_usable_bikes
            broken += stop.load_broken_bikes
            if usable + broken > scenario.truck_capacity:
                errors.append(f"Truck {route.truck_id} exceeds capacity at station {stop.station_id}")
        if usable != 0 or broken != 0:
            errors.append(f"Truck {route.truck_id} has nonzero inventory after its final depot operation")

    required_artifacts = {"scenario", "command", "stdout", "stderr"}
    if result.status is ResultStatus.SUCCEEDED or result.feasible is True or result.status is ResultStatus.INFEASIBLE:
        required_artifacts.add("solver_result")
    if missing := required_artifacts - result.raw_artifact_paths.keys():
        errors.append("Missing artifact roles: " + ", ".join(sorted(missing)))
    for role, path in result.raw_artifact_paths.items():
        if not path.is_absolute() or not path.resolve().is_relative_to(resolved_run):
            errors.append(f"Artifact {role} references a path outside this run")
        elif not path.is_file():
            errors.append(f"Artifact {role} is not an existing file")

    if errors:
        raise ResultValidationError(errors)
    metadata = result.solver_metadata.copy()
    if result.backend is BackendName.GUROBI and (
        result.feasible is True or result.status in {ResultStatus.SUCCEEDED, ResultStatus.INFEASIBLE}
        or result.status is ResultStatus.TIMEOUT and "sdk_status" in metadata
    ):
        try:
            metadata["validation"] = validate_gurobi_evidence(scenario, result, run_dir)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise ResultValidationError([f"Independent Gurobi verification failed: {exc}"]) from exc
        if result.feasible is True:
            warnings.append(GUROBI_MODEL_WARNING)
    if result.status is ResultStatus.SUCCEEDED and result.backend is BackendName.HGS:
        try:
            hashes = metadata.get("input_sha256")
            if not isinstance(hashes, dict):
                raise HgsInputError("Missing input_sha256 evidence for independent verification")
            instance = load_hgs_instance(scenario, run_dir=run_dir, input_sha256=hashes)
            verification = verify_hgs_solution(scenario, result, instance)
        except (HgsInputError, HgsVerificationError) as exc:
            raise ResultValidationError([f"Independent HGS verification failed: {exc}"]) from exc
        metadata["validation"] = verification.report
        warnings = [w for w in warnings if w not in {_LEGACY_STRUCTURAL_WARNING, _LEGACY_FEASIBILITY_WARNING}]
        warnings.extend(verification.warnings)
    return SolutionResult.model_validate({
        **result.model_dump(), "warnings": tuple(dict.fromkeys(warnings)), "solver_metadata": metadata,
    })
