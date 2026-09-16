"""Explicit backend orchestration; Gurobi requires a configured interpreter."""

from pydantic import JsonValue

from mobilityops.config import Settings
from mobilityops.domain import BackendName, ScenarioSpec, SolutionResult
from mobilityops.domain.solution import validate_run_id
from mobilityops.domain.analysis import BackendAssessment, SelectedSolution, SelectionDecision
from mobilityops.services.validation import ResultValidationError, validate_result
from mobilityops.solvers.contracts import ConstraintOutcome, HGS_CAPABILITY, SolverBackend
from mobilityops.solvers.hgs.backend import HgsBackend
from mobilityops.solvers.hgs.errors import ConstraintRejectedError
from mobilityops.solvers.gurobi.backend import GurobiBackend
from mobilityops.solvers.gurobi.contracts import GUROBI_CAPABILITY, GurobiConstraintError


class BackendSelectionError(ValueError):
    def __init__(self, decision: SelectionDecision) -> None:
        self.decision = decision
        super().__init__(decision.reason)


class SolverService:
    """Check constraints, dispatch and independently enforce backend contracts."""

    def __init__(self, settings: Settings, *, hgs_backend: HgsBackend | None = None,
                 gurobi_backend: GurobiBackend | None = None) -> None:
        if hgs_backend is not None and hgs_backend.settings != settings:
            raise ValueError("Injected HGS backend and SolverService must use the same Settings")
        self.settings = settings
        self._backends: dict[BackendName, SolverBackend] = {
            BackendName.HGS: hgs_backend if hgs_backend is not None else HgsBackend(settings),
        }
        if gurobi_backend is not None:
            if gurobi_backend.settings != settings:
                raise ValueError("Injected Gurobi backend and SolverService must use the same Settings")
            self._backends[BackendName.GUROBI] = gurobi_backend

    @property
    def registered_backends(self) -> tuple[BackendName, ...]:
        """Registered adapters, without checking files or starting a process."""
        return tuple(self._backends)

    def execution_configuration(self, backend: BackendName) -> dict[str, JsonValue]:
        """Snapshot explicit adapter settings for review; never inspect credentials.

        This describes configuration, not executable, input or license readiness.
        """
        backend = BackendName(backend)
        solver = self._backends.get(backend)
        if solver is None:
            raise ValueError(f"Backend {backend} is not registered; explicitly configure its adapter first")
        if solver.settings != self.settings:
            raise ValueError("Adapter and SolverService settings changed; rebuild the service before execution")
        configuration: dict[str, JsonValue] = {
            "backend": backend.value, "settings": self.settings.model_dump(mode="json"),
        }
        if isinstance(solver, GurobiBackend):
            configuration.update(options=solver.options.model_dump(mode="json"),
                                 instance_directory=str(solver.instance_directory) if solver.instance_directory else None)
        elif isinstance(solver, HgsBackend):
            configuration["options"] = {"executable": str(solver.executable),
                                        "timeout_grace_sec": solver.timeout_grace_sec}
        else:
            raise ValueError("This adapter does not expose a reviewable execution configuration")
        return configuration

    def assess(self, scenario: ScenarioSpec) -> tuple[BackendAssessment, ...]:
        """Pure capability checks; do not probe files, executables or licenses."""
        scenario = ScenarioSpec.model_validate(scenario.model_dump())
        assessments = []
        for capability in (HGS_CAPABILITY, GUROBI_CAPABILITY):
            backend = capability.backend
            solver = self._backends.get(backend)
            if solver is None:
                assessments.append(BackendAssessment(
                    backend=backend, capability=capability, registered=False,
                    compatible=None, eligible=False, reasons=("Backend is not registered.",),
                ))
                continue
            records = solver.check_constraints(scenario)
            reasons = tuple(
                f"{record.constraint_name}: {record.warning or 'Request rejected.'}"
                for record in records if record.outcome is ConstraintOutcome.REJECTED
            )
            assessments.append(BackendAssessment(
                backend=backend, capability=solver.capability, registered=True,
                compatible=not reasons, eligible=not reasons, constraints=records, reasons=reasons,
            ))
        return tuple(assessments)

    def select(self, scenario: ScenarioSpec, *, backend: BackendName | None = None) -> SelectionDecision:
        """Honor an explicit choice, otherwise require exactly one eligible backend."""
        scenario = ScenarioSpec.model_validate(scenario.model_dump())
        requested = BackendName(backend) if backend is not None else None
        assessments = self.assess(scenario)
        eligible = tuple(a.backend for a in assessments if a.eligible)
        selected = None
        if requested is not None:
            if requested in eligible:
                selected = requested
                reason = "Explicit backend satisfies the scenario constraints; runtime readiness is unchecked."
            else:
                reason = f"Requested backend {requested} is not eligible; no fallback or scenario rewrite."
        elif len(eligible) == 1:
            selected = eligible[0]
            reason = "Exactly one registered backend satisfies the scenario constraints; runtime readiness is unchecked."
        else:
            reason = "No eligible backend." if not eligible else "Multiple eligible backends; explicit choice required."
        return SelectionDecision(
            scenario=scenario, requested_backend=requested, selected_backend=selected,
            assessments=assessments, reason=reason,
        )

    def preflight(self, scenario: ScenarioSpec, *, run_id: str,
                  backend: BackendName | None = None) -> float:
        """Explicit read-only local readiness check, separate from assess/select."""
        validate_run_id(run_id)
        decision = self.select(scenario, backend=backend)
        if decision.selected_backend is None:
            raise BackendSelectionError(decision)
        return self._backends[decision.selected_backend].preflight(decision.scenario, run_id=run_id)

    def solve_selected(self, scenario: ScenarioSpec, *, run_id: str,
                       backend: BackendName | None = None) -> SelectedSolution:
        """Select afresh, then use the existing preflight, execution and verification."""
        validate_run_id(run_id)
        decision = self.select(scenario, backend=backend)
        if decision.selected_backend is None:
            raise BackendSelectionError(decision)
        result = self.solve(decision.scenario, backend=decision.selected_backend, run_id=run_id)
        return SelectedSolution(decision=decision, result=result)

    def solve(self, scenario: ScenarioSpec, *, backend: BackendName, run_id: str) -> SolutionResult:
        validate_run_id(run_id)
        backend = BackendName(backend)
        scenario = ScenarioSpec.model_validate(scenario.model_dump())
        if backend not in self._backends:
            raise ValueError(f"Backend {backend} is not registered; explicitly configure its adapter first")
        solver = self._backends[backend]
        records = solver.check_constraints(scenario)
        if any(record.outcome is ConstraintOutcome.REJECTED for record in records):
            if backend is BackendName.GUROBI:
                raise GurobiConstraintError(records)
            raise ConstraintRejectedError(records)
        result = solver.solve(scenario, run_id=run_id)
        if result.backend != backend or result.run_id != run_id:
            raise ResultValidationError(["Backend returned a result for a different backend or run ID"])
        return validate_result(scenario, result, run_dir=self.settings.runs_dir.resolve() / run_id)
