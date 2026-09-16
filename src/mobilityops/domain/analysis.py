"""Serializable decisions and observations; no inferred solver ranking."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from mobilityops.domain import BackendName, ScenarioSpec, SolutionResult
from mobilityops.solvers.contracts import BackendCapability, ConstraintRecord


class AnalysisModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BackendAssessment(AnalysisModel):
    backend: BackendName
    capability: BackendCapability
    registered: bool
    compatible: bool | None
    eligible: bool
    runtime_readiness: Literal["not_checked"] = "not_checked"
    constraints: tuple[ConstraintRecord, ...] = ()
    reasons: tuple[str, ...] = ()


class SelectionDecision(AnalysisModel):
    scenario: ScenarioSpec
    requested_backend: BackendName | None
    selected_backend: BackendName | None
    assessments: tuple[BackendAssessment, ...]
    reason: str


class SelectedSolution(AnalysisModel):
    decision: SelectionDecision
    result: SolutionResult


class FieldChange(AnalysisModel):
    field: str
    baseline: JsonValue
    variant: JsonValue


class RunObservation(AnalysisModel):
    scenario: ScenarioSpec
    result: SolutionResult
    verified_candidate: bool
    evidence_sha256: dict[str, str]
    model_signature: dict[str, JsonValue]
    search_context: dict[str, JsonValue]


class MetricDelta(AnalysisModel):
    metric: Literal["objective_value", "dissatisfaction", "emission"]
    baseline: float = Field(allow_inf_nan=False)
    variant: float = Field(allow_inf_nan=False)
    difference: float = Field(allow_inf_nan=False, description="Variant minus baseline.")
    reported_rounding_allowance: float = Field(ge=0, allow_inf_nan=False)
    exceeds_reported_rounding: bool


class ScenarioComparison(AnalysisModel):
    baseline_run_id: str
    variant_run_id: str
    kind: Literal["same_scenario_observations", "scenario_observations", "not_comparable"]
    reasons: tuple[str, ...]
    scenario_changes: tuple[FieldChange, ...]
    search_changes: tuple[FieldChange, ...]
    deltas: tuple[MetricDelta, ...] = ()


class ScenarioAnalysis(AnalysisModel):
    format: Literal["scenario_analysis_v1"] = "scenario_analysis_v1"
    observations: tuple[RunObservation, ...]
    comparisons: tuple[ScenarioComparison, ...]
    warnings: tuple[str, ...]
