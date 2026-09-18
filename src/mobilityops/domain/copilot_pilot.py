"""Result contract for one audited, no-execution Copilot pilot."""

from typing import Literal

from pydantic import Field

from mobilityops.domain.analysis import AnalysisModel
from mobilityops.domain.copilot import CopilotAction


class CopilotPilotReport(AnalysisModel):
    format: Literal["copilot_shadow_pilot_report_v1"] = (
        "copilot_shadow_pilot_report_v1"
    )
    pilot_id: str
    session_id: str
    audit_id: str
    model: Literal["gemini-3.8-flash"]
    budget_hkd: float
    max_calls: int = Field(gt=0, le=4)
    calls_reserved: int = Field(ge=0, le=4)
    calls_succeeded: int = Field(ge=0, le=4)
    reserved_hkd: str
    estimated_usage_hkd: str
    decisions: tuple[CopilotAction, ...]
    planned_solver_calls: int = Field(ge=0)
    planned_external_wait_sec: float = Field(ge=0, allow_inf_nan=False)
    execution_review_sha256: str
    audit_manifest_sha256: str
    session_status: Literal["completed"]
    audit_status: Literal["verified_no_execution"]
    solver_calls: Literal[0] = 0
    license_probes: Literal[0] = 0
