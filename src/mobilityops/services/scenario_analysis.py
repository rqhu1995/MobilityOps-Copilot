"""Read-only analysis of saved runs after fresh independent verification."""

import hashlib
import math
from pathlib import Path
import re

from pydantic import JsonValue

from mobilityops.config import Settings
from mobilityops.domain import BackendName, ResultStatus, ScenarioSpec, SolutionResult
from mobilityops.domain.analysis import (
    FieldChange, MetricDelta, RunObservation, ScenarioAnalysis, ScenarioComparison,
)
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.gurobi_inputs import strict_json
from mobilityops.services.validation import validate_result
from mobilityops.solvers.hgs.backend import required_hgs_inputs
from mobilityops.solvers.hgs.command import build_hgs_argv
from mobilityops.solvers.hgs.constraints import check_hgs_constraints
from mobilityops.solvers.contracts import ConstraintOutcome
from mobilityops.solvers.hgs.parser import parse_hgs_output


SEARCH_FIELDS = {"solver_runtime_limit_sec", "solution_requirement", "seed"}
WARNINGS = (
    "Deltas are single-run observations (variant minus baseline), not causal effects, rankings or optimality proofs.",
    "HGS has no supported seed and may return different routes on repeated runs.",
    "Best bounds and gaps belong only to their originating Gurobi run; they are never subtracted or transferred.",
    "Runtime measures differ across backends; this report does not compare solver speed.",
    "HGS arrival-event semantics and Gurobi period semantics do not guarantee real-world service timing.",
    "Hashes check local evidence consistency, not authenticity against coordinated edits of all run artifacts.",
)


def _changes(left: dict[str, JsonValue], right: dict[str, JsonValue]) -> tuple[FieldChange, ...]:
    return tuple(FieldChange(field=key, baseline=left.get(key), variant=right.get(key))
                 for key in sorted(left.keys() | right.keys()) if left.get(key) != right.get(key))


class ScenarioAnalysisService:
    def __init__(self, settings: Settings) -> None:
        self.settings = Settings.model_validate(settings.model_dump())

    def observe(self, run_id: str) -> RunObservation:
        """Load one saved run and independently reverify it without writes or execution."""
        validate_run_id(run_id)
        root = self.settings.runs_dir
        if any(p.is_symlink() for p in (root, *root.parents)):
            raise ValueError("Analysis runs root must not contain symlinks")
        run_dir = root / run_id
        hashes: dict[str, str] = {}

        def read(path: Path) -> bytes:
            relative = path.relative_to(run_dir)
            if run_dir.is_symlink() or not path.resolve().is_relative_to(run_dir.resolve()):
                raise ValueError("Analysis evidence escapes its run")
            if any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(run_dir)):
                raise ValueError("Analysis evidence cannot use symlinks")
            data = path.read_bytes()
            hashes[str(relative)] = hashlib.sha256(data).hexdigest()
            return data

        scenario = ScenarioSpec.model_validate(strict_json(read(run_dir / "scenario.json")))
        result = SolutionResult.model_validate(strict_json(read(run_dir / "solution.json")))
        if result.run_id != run_id:
            raise ValueError("Saved solution identifies a different run")
        if result.raw_artifact_paths.get("scenario") != run_dir / "scenario.json":
            raise ValueError("Scenario artifact must identify the canonical saved scenario")
        # Never trust a persisted 'passed' flag, including in failed/no-incumbent runs.
        result = result.model_copy(update={"solver_metadata": {
            key: value for key, value in result.solver_metadata.items() if key != "validation"
        }})
        process = strict_json(read(run_dir / "process.json"))
        if not isinstance(process, dict):
            raise ValueError("Invalid process evidence")
        candidate = result.feasible is True and result.status in {ResultStatus.SUCCEEDED, ResultStatus.TIMEOUT}
        if result.backend is BackendName.HGS and result.feasible is True and result.status is not ResultStatus.SUCCEEDED:
            raise ValueError("HGS cannot promote a candidate outside succeeded status")
        if candidate and result.backend is BackendName.HGS:
            if any(r.outcome is ConstraintOutcome.REJECTED for r in check_hgs_constraints(scenario)):
                raise ValueError("Saved HGS scenario violates backend constraints")
            executable = process.get("executable")
            if not isinstance(executable, str):
                raise ValueError("Missing HGS executable provenance")
            command = strict_json(read(run_dir / "command.json"))
            if command != list(build_hgs_argv(Path(executable), scenario)):
                raise ValueError("Saved HGS command differs from scenario")
            raw_path = run_dir / "solver-result.txt"
            if result.raw_artifact_paths.get("solver_result") != raw_path:
                raise ValueError("HGS result must use canonical solver-result.txt")
            parsed = parse_hgs_output(read(raw_path).decode("utf-8"))
            for field in ("objective_value", "dissatisfaction", "emission", "runtime_sec", "truck_routes", "repair_routes"):
                if getattr(parsed, field) != getattr(result, field):
                    raise ValueError(f"HGS raw output disagrees with solution: {field}")
            for key, value in parsed.metadata.items():
                if result.solver_metadata.get(key) != value:
                    raise ValueError(f"HGS raw metadata disagrees with solution: {key}")
            recorded = result.solver_metadata.get("input_sha256")
            if not isinstance(recorded, dict) or process.get("input_sha256") != recorded:
                raise ValueError("HGS input provenance differs from process evidence")
            folder = run_dir / "Instances" / f"{scenario.number_of_stations}_{scenario.instance_id}"
            for name in required_hgs_inputs(scenario):
                if hashlib.sha256(read(folder / name)).hexdigest() != recorded.get(name):
                    raise ValueError(f"HGS input hash mismatch: {name}")
        result = validate_result(scenario, result, run_dir=run_dir)
        report = result.solver_metadata.get("validation", {})
        verified = candidate and isinstance(report, dict) and report.get("status") == "passed"
        signature: dict[str, JsonValue] = {
            "backend": result.backend.value, "objective_profile": scenario.objective_profile.value,
        }
        if verified:
            signature.update({key: report.get(key) for key in (
                "verifier", "semantics", "checked_input_sha256", "time_interval_sec",
            )})
            if result.backend is BackendName.HGS:
                source = result.solver_metadata.get("executable_sha256")
                if not isinstance(source, str) or re.fullmatch(r"[0-9a-f]{64}", source) is None:
                    raise ValueError("Missing HGS executable SHA-256 provenance")
                if source != process.get("executable_sha256"):
                    raise ValueError("HGS executable provenance differs from process evidence")
                signature["implementation_sha256"] = source
            else:
                signature["implementation_sha256"] = report.get("checked_source_sha256")
                for name in ("request.json", "options.json", "worker-result.json", "native.sol"):
                    read(run_dir / name)
                # Include the mathematical input snapshots already checked by the verifier.
                folder = run_dir / "work/resources/datasets" / f"{scenario.number_of_stations}_{scenario.instance_id}"
                for name in report["checked_input_sha256"]:
                    read(folder / name)
        search_context = {key: scenario.model_dump(mode="json")[key] for key in sorted(SEARCH_FIELDS)}
        if result.backend is BackendName.HGS:
            search_context["all_input_sha256"] = result.solver_metadata.get("input_sha256")
        else:
            options = result.solver_metadata.get("options", {})
            search_context.update({
                "threads": options.get("threads") if isinstance(options, dict) else None,
                "gurobi_version": result.solver_metadata.get("gurobi_version"),
                "worker_sha256": result.solver_metadata.get("worker_sha256"),
            })
        return RunObservation(scenario=scenario, result=result, verified_candidate=verified,
                              evidence_sha256=hashes, model_signature=signature, search_context=search_context)

    def compare(self, *, baseline_run_id: str, variant_run_ids: tuple[str, ...]) -> ScenarioAnalysis:
        """Fail closed on corrupt evidence; valid non-candidates receive no deltas.

        This method never calls a solver or writes to any run directory.
        """
        ids = (baseline_run_id, *variant_run_ids)
        if not variant_run_ids or len(ids) != len(set(ids)):
            raise ValueError("Require at least one variant and distinct run IDs")
        for run_id in ids:
            validate_run_id(run_id)
        observations = tuple(self.observe(run_id) for run_id in ids)
        baseline = observations[0]
        comparisons = tuple(_compare(baseline, variant) for variant in observations[1:])
        return ScenarioAnalysis(observations=observations, comparisons=comparisons, warnings=WARNINGS)


def _compare(baseline: RunObservation, variant: RunObservation) -> ScenarioComparison:
    differences = _changes(baseline.scenario.model_dump(mode="json"), variant.scenario.model_dump(mode="json"))
    scenario_changes = tuple(c for c in differences if c.field not in SEARCH_FIELDS | {"scenario_name"})
    search_changes = _changes(baseline.search_context, variant.search_context)
    reasons = []
    if not baseline.verified_candidate or not variant.verified_candidate:
        reasons.append("Both runs must contain independently verified feasible candidates.")
    for change in _changes(baseline.model_signature, variant.model_signature):
        reasons.append(f"Model or input signature differs: {change.field}.")
    for field in ("instance_id", "number_of_stations"):
        if getattr(baseline.scenario, field) != getattr(variant.scenario, field):
            reasons.append(f"Instance identity differs: {field}.")
    deltas = []
    if not reasons:
        for metric in ("objective_value", "dissatisfaction", "emission"):
            left, right = getattr(baseline.result, metric), getattr(variant.result, metric)
            if left is None or right is None:
                reasons.append(f"Missing verified metric: {metric}.")
                break
            allowance = 0.0
            if baseline.result.backend is BackendName.HGS:
                # Six-significant-digit text precision, not stochastic uncertainty.
                allowance = sum(0.5 * 10 ** (math.floor(math.log10(abs(v))) - 5) if v else 0.0
                                for v in (left, right))
            delta = right - left
            if not math.isfinite(delta):
                reasons.append(f"Metric delta is not finite: {metric}.")
                break
            deltas.append(MetricDelta(metric=metric, baseline=left, variant=right, difference=delta,
                                      reported_rounding_allowance=allowance,
                                      exceeds_reported_rounding=abs(delta) > allowance + 64 * max(math.ulp(left), math.ulp(right))))
    return ScenarioComparison(
        baseline_run_id=baseline.result.run_id, variant_run_id=variant.result.run_id,
        kind="not_comparable" if reasons else "scenario_observations" if scenario_changes else "same_scenario_observations",
        reasons=tuple(reasons), scenario_changes=scenario_changes, search_changes=search_changes,
        deltas=() if reasons else tuple(deltas),
    )
