"""Explicit, finite experiment plans and durable execution/report contracts."""

from typing import Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from mobilityops.domain import BackendName, ResultStatus, ScenarioSpec
from mobilityops.domain.analysis import AnalysisModel, FieldChange, RunObservation, SelectionDecision
from mobilityops.domain.solution import validate_run_id


class ExperimentCase(AnalysisModel):
    case_id: str = Field(max_length=40)
    scenario: ScenarioSpec
    backend: BackendName | None
    repetitions: int = Field(gt=0, le=1000, strict=True)
    external_timeout_sec: float = Field(gt=0, le=86400, allow_inf_nan=False)

    @field_validator("case_id")
    @classmethod
    def safe_id(cls, value: str) -> str:
        return validate_run_id(value)

    @model_validator(mode="after")
    def timeout_includes_grace(self) -> "ExperimentCase":
        if self.external_timeout_sec <= self.scenario.solver_runtime_limit_sec:
            raise ValueError("External timeout must exceed the unchanged internal runtime limit")
        return self


class ExperimentPlan(AnalysisModel):
    format: Literal["experiment_plan_v1"] = "experiment_plan_v1"
    baseline: ExperimentCase
    variants: tuple[ExperimentCase, ...] = ()
    max_calls: int = Field(gt=0, le=10000, strict=True)
    external_wait_budget_sec: float = Field(gt=0, le=864000000, allow_inf_nan=False)
    failure_policy: Literal["continue", "stop"]

    @property
    def cases(self) -> tuple[ExperimentCase, ...]:
        return (self.baseline, *self.variants)

    @property
    def planned_calls(self) -> int:
        return sum(case.repetitions for case in self.cases)

    @model_validator(mode="after")
    def distinct_bounded_cases(self) -> "ExperimentPlan":
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("Case IDs must be distinct")
        if self.planned_calls > 10000:
            raise ValueError("At most 10000 planned calls are supported")
        return self


class CasePrecheck(AnalysisModel):
    case_id: str
    decision: SelectionDecision
    actual_external_timeout_sec: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    errors: tuple[str, ...] = ()
    license_readiness: Literal["not_checked"] = "not_checked"


class ExperimentPrecheck(AnalysisModel):
    cases: tuple[CasePrecheck, ...]
    allowed: bool
    planned_calls: int
    planned_external_wait_sec: float
    budget_may_skip_runs: bool


AttemptStatus = Literal[
    "pending", "running", "constraint_rejected", "preflight_failed", "start_failed",
    "failed", "validation_failed", "timeout_no_candidate", "timeout_with_candidate",
    "succeeded", "infeasible", "interrupted", "skipped_budget", "skipped_failure", "skipped_precheck",
]


class ExperimentAttempt(AnalysisModel):
    case_id: str
    repetition: int = Field(gt=0)
    run_id: str
    status: AttemptStatus = "pending"
    invoked: bool = False
    decision: SelectionDecision | None = None
    result_status: ResultStatus | None = None
    feasible: bool | None = None
    elapsed_sec: float = Field(default=0, ge=0, allow_inf_nan=False)
    diagnostic: str | None = None

    @field_validator("run_id")
    @classmethod
    def safe_id(cls, value: str) -> str:
        return validate_run_id(value)


class ExperimentBatch(AnalysisModel):
    format: Literal["experiment_batch_v1"] = "experiment_batch_v1"
    experiment_id: str
    plan: ExperimentPlan
    precheck: ExperimentPrecheck
    status: Literal["running", "completed", "precheck_rejected", "budget_exhausted", "stopped", "interrupted"]
    attempts: tuple[ExperimentAttempt, ...]
    started_at: str
    elapsed_sec: float = Field(ge=0, allow_inf_nan=False)
    calls_invoked: int = Field(ge=0)
    reserved_external_wait_sec: float = Field(ge=0, allow_inf_nan=False)


class DescriptiveMetric(AnalysisModel):
    metric: Literal["objective_value", "dissatisfaction", "emission"]
    n: int = Field(gt=0)
    minimum: float = Field(allow_inf_nan=False)
    median: float = Field(allow_inf_nan=False)
    maximum: float = Field(allow_inf_nan=False)


class ExperimentGroup(AnalysisModel):
    group_id: str
    case_id: str
    model_signature: dict[str, JsonValue]
    search_context: dict[str, JsonValue]
    run_ids: tuple[str, ...]
    metrics: tuple[DescriptiveMetric, ...]


class CaseSummary(AnalysisModel):
    case_id: str
    scenario_changes: tuple[FieldChange, ...]
    search_changes: tuple[FieldChange, ...]
    planned: int
    invoked: int
    verified_candidates: int
    execution_status_counts: dict[str, int]
    excluded_run_ids: tuple[str, ...]


class GroupComparison(AnalysisModel):
    baseline_group_id: str | None
    variant_group_id: str | None
    variant_case_id: str
    kind: Literal["scenario_observations", "same_scenario_observations", "search_conditions_changed", "not_comparable"]
    reasons: tuple[str, ...]
    median_differences: dict[str, float]
    search_changes: tuple[FieldChange, ...] = ()


class AttemptReview(AnalysisModel):
    run_id: str
    status: Literal["verified_candidate", "no_candidate", "excluded", "not_completed"]
    diagnostic: str | None = None
    observation: RunObservation | None = None


class ExperimentReport(AnalysisModel):
    format: Literal["experiment_report_v1"] = "experiment_report_v1"
    experiment_id: str
    batch: ExperimentBatch
    source_sha256: dict[str, str]
    summaries: tuple[CaseSummary, ...]
    reviews: tuple[AttemptReview, ...]
    groups: tuple[ExperimentGroup, ...]
    comparisons: tuple[GroupComparison, ...]
    warnings: tuple[str, ...]
