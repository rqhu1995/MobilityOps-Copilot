"""Finite serial experiments over SolverService; no retries or implicit backend changes."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time

from mobilityops.config import Settings
from mobilityops.domain import ResultStatus, SolutionResult
from mobilityops.domain.experiment import (
    CasePrecheck, ExperimentAttempt, ExperimentBatch, ExperimentPlan, ExperimentPrecheck,
)
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.gurobi_inputs import read_local, strict_json
from mobilityops.services.solver_service import BackendSelectionError, SolverService
from mobilityops.services.validation import ResultValidationError
from mobilityops.solvers.gurobi.contracts import GurobiConstraintError, GurobiPreflightError
from mobilityops.solvers.hgs.errors import ArtifactError, ConstraintRejectedError, HgsPreflightError


def checked_root(settings: Settings, category: str) -> Path:
    root = settings.runs_dir
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("Experiment paths must not contain symlinks")
    if any(root.resolve().is_relative_to(p.resolve()) for p in (settings.hgs_repo_path, settings.gurobi_repo_path)):
        raise ValueError("Experiment artifacts must stay outside solver repositories")
    target = root / category
    if target.is_symlink():
        raise ValueError("Experiment paths must not contain symlinks")
    return target


def experiment_path(settings: Settings, experiment_id: str) -> Path:
    validate_run_id(experiment_id)
    if len(experiment_id) > 160:
        raise ValueError("Experiment ID must have at most 160 characters")
    return checked_root(settings, "experiments") / experiment_id


def write_json(path: Path, value: object) -> None:
    """Atomically publish one checkpoint inside an exclusively reserved directory."""
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def scheduled_attempts(plan: ExperimentPlan, experiment_id: str) -> tuple[ExperimentAttempt, ...]:
    return tuple(ExperimentAttempt(case_id=case.case_id, repetition=repeat,
                                   run_id=f"{experiment_id}-c{index:03d}-r{repeat:04d}")
                 for index, case in enumerate(plan.cases, 1)
                 for repeat in range(1, case.repetitions + 1))


def result_outcome(result: SolutionResult) -> str:
    if result.status is ResultStatus.TIMEOUT:
        return "timeout_with_candidate" if result.feasible is True else "timeout_no_candidate"
    if result.status is ResultStatus.SUCCEEDED:
        return "succeeded"
    if result.status is ResultStatus.INFEASIBLE:
        return "infeasible"
    if result.status is not ResultStatus.FAILED:
        raise ValueError("Solver returned a nonterminal result")
    metadata = result.solver_metadata
    validation = metadata.get("validation", {})
    if isinstance(validation, dict) and validation.get("status") == "failed":
        return "validation_failed"
    if metadata.get("returncode") is None and not metadata.get("timed_out"):
        return "start_failed"
    return "failed"


class ExperimentService:
    def __init__(self, solver_service: SolverService) -> None:
        self.solver_service = solver_service
        self.settings = Settings.model_validate(solver_service.settings.model_dump())

    def precheck(self, plan: ExperimentPlan, *, experiment_id: str) -> ExperimentPrecheck:
        """Check every case and every prospective run ID without writes or processes."""
        plan = ExperimentPlan.model_validate(plan.model_dump())
        folder = experiment_path(self.settings, experiment_id)
        if folder.exists() or folder.is_symlink():
            raise ValueError("Experiment already exists; refusing overwrite")
        attempts = scheduled_attempts(plan, experiment_id)
        checked = []
        for case in plan.cases:
            decision = self.solver_service.select(case.scenario, backend=case.backend)
            errors = []
            timeout = None
            case_attempts = [a for a in attempts if a.case_id == case.case_id]
            if decision.selected_backend is None:
                errors.append(decision.reason)
            else:
                try:
                    timeout = self.solver_service.preflight(case.scenario, run_id=case_attempts[0].run_id,
                                                            backend=case.backend)
                    if timeout != case.external_timeout_sec:
                        errors.append("Configured external timeout differs from the explicit plan; no adjustment was made.")
                except (ValueError, OSError) as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
            for attempt in case_attempts:
                run = self.settings.runs_dir / attempt.run_id
                if run.exists() or run.is_symlink():
                    errors.append(f"Run ID already exists: {attempt.run_id}")
            checked.append(CasePrecheck(case_id=case.case_id, decision=decision,
                                       actual_external_timeout_sec=timeout, errors=tuple(errors)))
        waiting = math.fsum(c.external_timeout_sec * c.repetitions for c in plan.cases)
        return ExperimentPrecheck(cases=tuple(checked), allowed=all(not c.errors for c in checked),
                                  planned_calls=plan.planned_calls, planned_external_wait_sec=waiting,
                                  budget_may_skip_runs=plan.planned_calls > plan.max_calls or waiting > plan.external_wait_budget_sec)

    def execute(self, plan: ExperimentPlan, *, experiment_id: str) -> ExperimentBatch:
        """Execute once in case/repetition order. Budget is reserved, never refunded.

        `stop` stops after any outcome lacking a verified candidate, including an
        infeasible result. Checkpoint write failures always abort execution.
        KeyboardInterrupt/SystemExit are recorded and re-raised. No resume or retry.
        """
        plan = ExperimentPlan.model_validate(plan.model_dump())
        started = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        precheck = self.precheck(plan, experiment_id=experiment_id)
        folder = experiment_path(self.settings, experiment_id)
        folder.parent.mkdir(parents=True, exist_ok=True)
        folder.mkdir(mode=0o700)
        (folder / "attempts").mkdir()
        write_json(folder / "plan.json", plan.model_dump(mode="json"))
        write_json(folder / "precheck.json", precheck.model_dump(mode="json"))
        attempts = list(scheduled_attempts(plan, experiment_id))
        cases = {c.case_id: c for c in plan.cases}
        checks = {c.case_id: c for c in precheck.cases}
        calls, reserved = 0, 0.0

        def checkpoint(status: str, index: int | None = None) -> ExperimentBatch:
            batch = ExperimentBatch(experiment_id=experiment_id, plan=plan, precheck=precheck,
                                    status=status, attempts=tuple(attempts), started_at=started_at,
                                    elapsed_sec=time.monotonic() - started, calls_invoked=calls,
                                    reserved_external_wait_sec=reserved)
            if index is not None:
                write_json(folder / "attempts" / f"{index + 1:05d}.json", attempts[index].model_dump(mode="json"))
            write_json(folder / "batch.json", batch.model_dump(mode="json"))
            return batch

        checkpoint("running")
        skip = "skipped_precheck" if not precheck.allowed else None
        final_status = "precheck_rejected" if skip else "completed"
        for index, attempt in enumerate(attempts):
            case = cases[attempt.case_id]
            decision = checks[case.case_id].decision
            next_reserved = math.fsum([reserved, case.external_timeout_sec])
            if skip is None and (calls >= plan.max_calls or next_reserved > plan.external_wait_budget_sec):
                skip, final_status = "skipped_budget", "budget_exhausted"
            if skip is not None:
                attempts[index] = attempt.model_copy(update={"status": skip, "decision": decision,
                                                            "diagnostic": "See batch precheck, budget and failure policy."})
                checkpoint("running", index)
                continue
            call_started = time.monotonic()
            try:
                fresh = self.solver_service.select(case.scenario, backend=case.backend)
                decision = fresh
                if fresh.selected_backend is None or fresh.selected_backend != checks[case.case_id].decision.selected_backend:
                    raise BackendSelectionError(fresh)
                timeout = self.solver_service.preflight(case.scenario, run_id=attempt.run_id, backend=case.backend)
                if timeout != case.external_timeout_sec:
                    raise ValueError("External timeout changed after batch precheck")
            except (ValueError, OSError) as exc:
                outcome = "constraint_rejected" if isinstance(exc, BackendSelectionError) else "preflight_failed"
                if isinstance(exc, BackendSelectionError):
                    decision = exc.decision
                attempts[index] = attempt.model_copy(update={"status": outcome, "decision": decision,
                                                            "diagnostic": f"{type(exc).__name__}: {exc}"})
            else:
                calls += 1
                reserved = next_reserved
                attempts[index] = attempt.model_copy(update={"status": "running", "invoked": True, "decision": decision})
                checkpoint("running", index)
                try:
                    selected = self.solver_service.solve_selected(case.scenario, run_id=attempt.run_id, backend=case.backend)
                    attempts[index] = attempts[index].model_copy(update={"decision": selected.decision})
                    if selected.decision != decision:
                        raise ValueError("Execution selection differs from the saved decision")
                    result = selected.result
                    outcome = result_outcome(result)
                    attempts[index] = attempts[index].model_copy(update={
                        "status": outcome, "decision": selected.decision, "result_status": result.status,
                        "feasible": result.feasible,
                        "diagnostic": "; ".join(result.warnings) if outcome not in {"succeeded", "timeout_with_candidate"} else None,
                    })
                except (KeyboardInterrupt, SystemExit):
                    attempts[index] = attempts[index].model_copy(update={"status": "interrupted", "elapsed_sec": time.monotonic() - call_started,
                                                                        "diagnostic": "Execution interrupted; incomplete evidence is retained."})
                    checkpoint("interrupted", index)
                    raise
                except Exception as exc:
                    if isinstance(exc, BackendSelectionError):
                        attempts[index] = attempts[index].model_copy(update={"decision": exc.decision})
                    if isinstance(exc, (BackendSelectionError, ConstraintRejectedError, GurobiConstraintError)):
                        outcome = "constraint_rejected"
                    elif isinstance(exc, (HgsPreflightError, GurobiPreflightError)):
                        outcome = "preflight_failed"
                    elif isinstance(exc, ResultValidationError):
                        outcome = "validation_failed"
                    else:
                        outcome = "failed"
                    attempts[index] = attempts[index].model_copy(update={"status": outcome, "elapsed_sec": time.monotonic() - call_started,
                                                                        "diagnostic": f"{type(exc).__name__}; inspect retained run evidence."})
                    if isinstance(exc, ArtifactError):
                        checkpoint("stopped", index)
                        raise
            attempts[index] = attempts[index].model_copy(update={"elapsed_sec": time.monotonic() - call_started})
            checkpoint("running", index)
            if attempts[index].status not in {"succeeded", "timeout_with_candidate"} and plan.failure_policy == "stop":
                skip, final_status = "skipped_failure", "stopped"
        return checkpoint(final_status)


def load_batch(settings: Settings, experiment_id: str) -> tuple[ExperimentBatch, dict[str, str]]:
    """Cross-check checkpoints and the full schedule, without trusting a report."""
    folder = experiment_path(settings, experiment_id)
    hashes = {}

    def read(name: str) -> object:
        data = read_local(folder, folder / name)
        hashes[name] = hashlib.sha256(data).hexdigest()
        return strict_json(data)

    batch = ExperimentBatch.model_validate(read("batch.json"))
    if batch.experiment_id != experiment_id or batch.plan != ExperimentPlan.model_validate(read("plan.json")):
        raise ValueError("Batch identity or plan differs from its snapshot")
    if batch.precheck != ExperimentPrecheck.model_validate(read("precheck.json")):
        raise ValueError("Batch precheck differs from its snapshot")
    expected = scheduled_attempts(batch.plan, experiment_id)
    if [(a.case_id, a.repetition, a.run_id) for a in batch.attempts] != [(a.case_id, a.repetition, a.run_id) for a in expected]:
        raise ValueError("Batch attempts differ from the planned schedule")
    if [c.case_id for c in batch.precheck.cases] != [c.case_id for c in batch.plan.cases]:
        raise ValueError("Precheck cases differ from the plan")
    if batch.precheck.allowed != all(not c.errors for c in batch.precheck.cases):
        raise ValueError("Precheck eligibility contradicts its errors")
    if batch.precheck.planned_calls != batch.plan.planned_calls:
        raise ValueError("Precheck count differs from the plan")
    for check, case in zip(batch.precheck.cases, batch.plan.cases, strict=True):
        if check.decision.scenario != case.scenario or check.decision.requested_backend != case.backend:
            raise ValueError("Precheck selection differs from the planned request")
    cases = {c.case_id: c for c in batch.plan.cases}
    for index, attempt in enumerate(batch.attempts):
        record = folder / "attempts" / f"{index + 1:05d}.json"
        if record.exists() or record.is_symlink():
            if attempt != ExperimentAttempt.model_validate(read(f"attempts/{index + 1:05d}.json")):
                raise ValueError("Attempt checkpoint differs from batch state")
        elif attempt.status != "pending":
            raise ValueError("Missing attempt checkpoint")
        decision = attempt.decision
        case = cases[attempt.case_id]
        if decision is not None and (decision.scenario != case.scenario or decision.requested_backend != case.backend):
            raise ValueError("Saved selection differs from the planned request")
        if attempt.result_status is not None:
            expected = {
                ResultStatus.SUCCEEDED: {"succeeded"},
                ResultStatus.TIMEOUT: {"timeout_with_candidate" if attempt.feasible is True else "timeout_no_candidate"},
                ResultStatus.INFEASIBLE: {"infeasible"},
                ResultStatus.FAILED: {"failed", "start_failed", "validation_failed"},
            }.get(attempt.result_status, set())
            if not attempt.invoked or attempt.status not in expected:
                raise ValueError("Attempt outcome contradicts its returned result status")
    calls = sum(a.invoked for a in batch.attempts)
    reserved = math.fsum(cases[a.case_id].external_timeout_sec for a in batch.attempts if a.invoked)
    if calls != batch.calls_invoked or not math.isclose(reserved, batch.reserved_external_wait_sec, rel_tol=1e-12):
        raise ValueError("Batch accounting differs from attempt records")
    if calls > batch.plan.max_calls or reserved > batch.plan.external_wait_budget_sec:
        raise ValueError("Batch exceeded its explicit budget")
    return batch, hashes
