"""AgentCore Platform v1.0 — SVC-C2-030 KBRetrieve (inner graph)."""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode, emit_trace_event
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import get_kb_service


class KBRetrieveNode(FunctionNode):
    """Vector-search the property KB (facility_facts / onsen_analysis / culture).

    Sets kb_chunks (reference-safe: doc_type/source_id/lang/text only) and
    kb_hit (the first-branch predicate used by GroundedAnswer). Escalate-only
    intents (complaint/booking) skip retrieval — they are front-desk concerns.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, kb_service=None) -> None:
        super().__init__()
        self._kb = kb_service  # injection seam

    def execute(self, state: dict) -> dict:
        # Escalate-only intent: no grounding needed, go straight to escalation.
        if state.get("escalate_flag"):
            emit_trace_event("concierge_kb_skipped", {"reason": "escalate_only"}, state)
            return {"kb_chunks": [], "kb_hit": False, "status": AgentStatus.SUCCESS.value}

        query = state.get("validated_input", state.get("user_input", ""))
        property_id = str(state.get("input_context", {}).get("property_id", ""))

        try:
            chunks = get_kb_service(self._kb).retrieve(property_id, query)
        except Exception as exc:
            # Retrieval failure must not fabricate an answer — degrade to
            # escalation (no KB hit), not to a hallucinated response.
            emit_trace_event("concierge_kb_error", {"error": str(exc)}, state)
            return {"kb_chunks": [], "kb_hit": False, "status": AgentStatus.SUCCESS.value}

        kb_hit = len(chunks) > 0
        emit_trace_event(
            "concierge_kb_retrieved",
            {"property_id": property_id, "hit": kb_hit, "chunks": len(chunks)},
            state,
        )
        return {"kb_chunks": chunks, "kb_hit": kb_hit, "status": AgentStatus.SUCCESS.value}
