import pytest
from pydantic import ValidationError

from mobilityops.domain import BackendName, ObjectiveProfile, SolutionRequirement
from mobilityops.solvers import (
    HGS_CAPABILITY,
    ConstraintOutcome,
    ConstraintRecord,
)


def test_hgs_capability_records_missing_exact_solver_metrics() -> None:
    assert HGS_CAPABILITY.backend is BackendName.HGS
    assert HGS_CAPABILITY.supported_objective_profiles == (
        ObjectiveProfile.EUDF_DEFAULT,
    )
    assert SolutionRequirement.FAST_FEASIBLE in (
        HGS_CAPABILITY.supported_solution_requirements
    )
    assert HGS_CAPABILITY.supports_seed is False
    assert HGS_CAPABILITY.reports_best_bound is False
    assert HGS_CAPABILITY.reports_optimality_gap is False


def test_constraint_record_rejects_applied_unsupported_request() -> None:
    record = ConstraintRecord(
        constraint_name="seed",
        requested_value=7,
        supported=False,
        outcome=ConstraintOutcome.REJECTED,
        warning="The current HGS CLI has no seed parameter.",
    )
    assert record.supported is False

    with pytest.raises(ValidationError):
        ConstraintRecord(
            constraint_name="seed",
            requested_value=7,
            supported=False,
            outcome=ConstraintOutcome.APPLIED,
        )
