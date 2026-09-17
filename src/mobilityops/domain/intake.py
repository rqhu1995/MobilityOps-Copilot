"""Untrusted language proposals, deterministic clarification, and explicit requests."""

from typing import Literal

from pydantic import Field, JsonValue

from mobilityops.domain import BackendName, ScenarioSpec
from mobilityops.domain.analysis import AnalysisModel, SelectionDecision
from mobilityops.domain.experiment import ExperimentBatch, ExperimentPlan, ExperimentReport


ScenarioField = Literal[
    "scenario_name", "instance_id", "number_of_stations", "number_of_trucks",
    "number_of_repairers", "truck_capacity", "operation_time_budget_sec",
    "solver_runtime_limit_sec", "loading_time_sec", "repair_time_sec",
    "broken_bike_proportion", "seed", "objective_profile", "solution_requirement", "backend",
]


class ProposedField(AnalysisModel):
    field: ScenarioField
    value: str | int | float | bool | None
    message_index: int = Field(ge=0, strict=True)
    evidence: str = Field(min_length=1, max_length=4000)


class InterpretationIssue(AnalysisModel):
    field: ScenarioField | None
    question: str = Field(min_length=1, max_length=1000)
    evidence: str = Field(min_length=1, max_length=4000)
    message_index: int = Field(ge=0, strict=True)


class ScenarioExtraction(AnalysisModel):
    """The entire LLM output schema; it has no execution or file-access actions."""

    fields: tuple[ProposedField, ...] = Field(max_length=120)
    issues: tuple[InterpretationIssue, ...] = Field(max_length=40)


class LanguageInput(AnalysisModel):
    messages: tuple[str, ...] = Field(min_length=1, max_length=20)
    context: ScenarioSpec | None = None
    context_backend: BackendName | None = None


class Clarification(AnalysisModel):
    kind: Literal["missing", "invalid", "ambiguous", "unsupported", "provenance", "conflict"]
    field: str | None
    question: str


class ScenarioDraft(AnalysisModel):
    format: Literal["scenario_draft_v1"] = "scenario_draft_v1"
    draft_sha256: str
    input: LanguageInput
    extraction: ScenarioExtraction
    values: dict[str, JsonValue]
    requested_backend: BackendName | None
    field_sources: dict[str, Literal["context", "user_text", "schema_default"]]
    candidate: ScenarioSpec | None
    decision: SelectionDecision | None
    clarifications: tuple[Clarification, ...]
    status: Literal["needs_clarification", "incompatible", "ready"]


class ExecutionRequest(AnalysisModel):
    format: Literal["language_execution_request_v1"] = "language_execution_request_v1"
    request_sha256: str
    draft_sha256: str
    experiment_id: str
    report_id: str
    plan: ExperimentPlan


class IntakeExecution(AnalysisModel):
    request: ExecutionRequest
    batch: ExperimentBatch
    report: ExperimentReport
