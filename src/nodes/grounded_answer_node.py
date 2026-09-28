"""AgentCore Platform v1.0 — SVC-C2-030 GroundedAnswer (inner graph)."""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode, emit_trace_event
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.llm_client import complete_text, get_llm

_SYSTEM = (
    "You are a lodging concierge. Answer the guest's question using ONLY the "
    "facts in the provided KB context. Do NOT invent facts. If the context does "
    "not contain the answer, reply exactly with the token NO_ANSWER. Be concise."
)

# Sentinel the LLM emits when the KB context does not support an answer — this
# routes to escalation rather than a fabricated response (anti-hallucination).
NO_ANSWER = "NO_ANSWER"


class GroundedAnswerNode(FunctionNode):
    """Generate an answer grounded strictly in retrieved KB chunks.

    Sets `result` and, when the KB cannot support an answer, sets escalate_flag
    (never fabricate property facts). The KB-hit-first branch predicate (kb_hit)
    was set upstream; here we produce the grounded text or fall back to escalate.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm=None) -> None:
        super().__init__()
        self._llm = llm  # injection seam

    def execute(self, state: dict) -> dict:
        # No KB hit (or escalate-only) ⇒ escalate, do not generate.
        if state.get("escalate_flag") or not state.get("kb_hit"):
            emit_trace_event("concierge_grounding_escalate", {"reason": "no_kb_hit"}, state)
            return {"escalate_flag": True, "status": AgentStatus.SUCCESS.value}

        query = state.get("validated_input", state.get("user_input", ""))
        chunks = state.get("kb_chunks", [])
        context = "\n\n".join(
            f"[{c.get('doc_type')}::{c.get('source_id')}] {c.get('text', '')}"
            for c in chunks
        )
        prompt = f"KB context:\n{context}\n\nGuest question: {query}"

        try:
            answer = complete_text(get_llm(self._llm), _SYSTEM, prompt)
        except Exception as exc:
            emit_trace_event("concierge_grounding_error", {"error": str(exc)}, state)
            return {"escalate_flag": True, "status": AgentStatus.SUCCESS.value}

        if not answer or answer.strip() == NO_ANSWER:
            emit_trace_event("concierge_grounding_no_answer", {}, state)
            return {"escalate_flag": True, "status": AgentStatus.SUCCESS.value}

        emit_trace_event(
            "concierge_grounded_answer",
            {"intent": state.get("resolved_intent"), "health": state.get("health_flag")},
            state,
        )
        return {"result": answer.strip(), "status": AgentStatus.SUCCESS.value}
