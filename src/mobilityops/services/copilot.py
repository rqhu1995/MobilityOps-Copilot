"""Validate Gemini's bounded tool choice before any local service is called."""

import json
import re
from typing import Protocol

from mobilityops.domain.copilot import (
    CopilotDecision,
    CopilotEvidenceItem,
    CopilotModelInput,
)
from mobilityops.domain.solution import validate_run_id


class CopilotInterpreter(Protocol):
    def decide(self, request: CopilotModelInput) -> CopilotDecision: ...


class CopilotDecisionService:
    def __init__(self, interpreter: CopilotInterpreter) -> None:
        self.interpreter = interpreter

    def decide(self, request: CopilotModelInput) -> CopilotDecision:
        request = CopilotModelInput.model_validate(request.model_dump())
        if any(not message.strip() or len(message) > 4000 for message in request.messages):
            raise ValueError("Each Copilot message must contain 1..4000 characters")
        if sum(map(len, request.messages)) > 20000:
            raise ValueError("The Copilot conversation must contain at most 20000 characters")
        decision = CopilotDecision.model_validate(
            self.interpreter.decide(request).model_dump()
        )
        if decision.action not in request.available_actions:
            raise ValueError("Gemini selected an unavailable Copilot action")
        evidence = {item.evidence_id: item for item in request.evidence}
        if len(set(decision.evidence_ids)) != len(decision.evidence_ids):
            raise ValueError("Gemini repeated a Copilot evidence ID")
        if any(item not in evidence for item in decision.evidence_ids):
            raise ValueError("Gemini cited unknown Copilot evidence")
        if decision.variant_case_id is not None:
            validate_run_id(decision.variant_case_id)
        if decision.action == "propose_next_experiment" and decision.variant_case_id is None:
            raise ValueError("A next-experiment proposal requires an explicit variant case ID")
        if (decision.action not in {"propose_next_experiment", "explain_experiment"}
                and decision.variant_case_id is not None):
            raise ValueError("This Copilot action does not accept a variant case ID")
        if decision.action == "answer":
            self._validate_grounded_answer(request, decision, evidence)
        return decision

    @staticmethod
    def _validate_grounded_answer(
        request: CopilotModelInput,
        decision: CopilotDecision,
        evidence: dict[str, CopilotEvidenceItem],
    ) -> None:
        if request.evidence and not decision.evidence_ids:
            raise ValueError("Evidence-aware Copilot answers must cite evidence")
        cited = " ".join(
            json.dumps(
                evidence[item].payload,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            for item in decision.evidence_ids
        )
        source = cited + " " + " ".join(request.messages)
        response_claims = decision.response
        for evidence_id in decision.evidence_ids:
            response_claims = response_claims.replace(evidence_id, "")
        response_claims = re.sub(
            r"(?m)^(\s*)\d+[.)、]\s+",
            r"\1",
            response_claims,
        )
        numbers = re.findall(
            r"(?<![A-Za-z0-9_])-?\d+(?:\.\d+)?%?",
            response_claims,
        )
        if any(number not in source for number in numbers):
            raise ValueError("Copilot answer introduced a number absent from cited evidence")
