"""AgentCore Platform v1.0 — SVC-C2-030 EscalateToFrontDesk (inner graph)."""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode, emit_trace_event
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.hospitality import escalation_for


class EscalateToFrontDeskNode(FunctionNode):
    """Set the front-desk escalation response (not-in-KB / complaint / booking / law).

    We ship the escalation wording rather than maintaining 旅館業法 legal
    templates: law-adjacent, complaint, booking, and not-in-KB intents are all
    routed here. The result is later localized by post_process (language match).
    Sets escalate_flag so downstream/operators can trigger a human handoff.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        language = state.get("detected_language", "ja")
        message = escalation_for(language)

        emit_trace_event(
            "concierge_escalated",
            {"language": language, "intent": state.get("resolved_intent")},
            state,
        )
        return {
            "result": message,
            "escalate_flag": True,
            "status": AgentStatus.SUCCESS.value,
        }
