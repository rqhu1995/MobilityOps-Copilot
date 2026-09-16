from unittest.mock import Mock

import pytest

from mobilityops.config import Settings
from mobilityops.domain import BackendName, ScenarioSpec
from mobilityops.services.solver_service import SolverService
from mobilityops.services.validation import ResultValidationError
from mobilityops.solvers.hgs import ConstraintRejectedError, HgsBackend


def test_service_end_to_end_fake(settings: Settings, scenario: ScenarioSpec) -> None:
    result = SolverService(settings).solve(scenario, backend=BackendName.HGS, run_id="service-success")
    assert result.status == "succeeded"
    assert result.truck_routes and result.repair_routes
    assert result.best_bound is result.optimality_gap is None


def test_service_rejects_mismatched_backend_settings_before_execution(settings: Settings) -> None:
    other_settings = settings.model_copy(update={"runs_dir": settings.runs_dir.parent / "other-runs"})
    backend = HgsBackend(other_settings)
    backend.solve = Mock()
    with pytest.raises(ValueError, match="same Settings"):
        SolverService(settings, hgs_backend=backend)
    backend.solve.assert_not_called()
    assert not settings.runs_dir.exists()
    assert not other_settings.runs_dir.exists()


def test_service_rejects_seed_before_solve(settings: Settings, scenario: ScenarioSpec) -> None:
    backend = HgsBackend(settings)
    backend.solve = Mock()
    with pytest.raises(ConstraintRejectedError):
        SolverService(settings, hgs_backend=backend).solve(scenario.model_copy(update={"seed": 9}), backend=BackendName.HGS, run_id="seed")
    backend.solve.assert_not_called()
    assert not settings.runs_dir.exists()


def test_gurobi_not_registered(settings: Settings, scenario: ScenarioSpec) -> None:
    with pytest.raises(ValueError, match="not registered"):
        SolverService(settings).solve(scenario, backend=BackendName.GUROBI, run_id="unsupported")
    assert not settings.runs_dir.exists()


@pytest.mark.parametrize("update", [{"run_id": "other-run"}, {"backend": BackendName.GUROBI}, {"best_bound": 1.0}])
def test_service_checks_returned_contract(settings: Settings, scenario: ScenarioSpec, update: dict) -> None:
    backend = HgsBackend(settings)
    result = backend.solve(scenario, run_id="service-contract")
    backend.solve = Mock(return_value=result.model_copy(update=update))
    with pytest.raises(ResultValidationError):
        SolverService(settings, hgs_backend=backend).solve(scenario, backend=BackendName.HGS, run_id="service-contract")
