"""AgentCore Platform v1.0 — SVC-C2-030 MedicalDisclaimerGate / S-5 (inner graph)."""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode, emit_trace_event
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.hospitality import DISCLAIMER_MARKER, disclaimer_for


class MedicalDisclaimerNode(FunctionNode):
    """S-5: prepend a fixed medical disclaimer to onsen health answers.

    Only reached when kb_hit AND health_flag (KB-hit-first branch). Adds the
    disclaimer marker + text; no diagnosis; routes the
    decision to a doctor / the front desk. The disclaimer's presence is later
    verified by the post_process S-3 preservation check (it must not be dropped).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        answer = state.get("result", "")
        language = state.get("detected_language", "ja")
        disclaimer = disclaimer_for(language)

        # Idempotent: don't double-add if the marker is already present.
        if DISCLAIMER_MARKER in str(answer):
            gated = str(answer)
        else:
            gated = f"{disclaimer}\n\n{answer}".strip()

        emit_trace_event(
            "concierge_medical_disclaimer_applied",
            {"language": language, "intent": state.get("resolved_intent")},
            state,
        )
        return {"result": gated, "status": AgentStatus.SUCCESS.value}
