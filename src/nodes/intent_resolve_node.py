"""AgentCore Platform v1.0 — SVC-C2-030 IntentResolve (inner graph)."""

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode, emit_trace_event
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services import ontology
from src.services.llm_client import complete_text, get_llm

_SYSTEM = (
    "You classify a lodging guest's message into a single intent label. "
    "Return STRICT JSON: {\"intent\": \"<label>\"}. Choose the closest label "
    "from this set (or \"other\" if none fits): "
)


class IntentResolveNode(FunctionNode):
    """Resolve the guest message to a minimal lodging intent + flags.

    The ontology is deliberately minimal: it only decides escalate-only intents
    and the onsen health signal. Fine-grained classification is delegated to the
    LLM (few-shot over ontology anchor labels).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm=None) -> None:
        super().__init__()
        self._llm = llm  # injection seam (tests pass a fake; else default)

    def execute(self, state: dict) -> dict:
        # The outer GraphNode passes only a string into the inner graph
        # (input_context does NOT propagate). extract_input() packs a JSON
        # envelope; unpack it here into inner-graph fields.
        query, unpacked = _unpack_envelope(state.get("user_input", ""))

        labels = ", ".join(ontology.all_intent_labels())
        try:
            raw = complete_text(get_llm(self._llm), _SYSTEM + labels, query)
            intent = json.loads(raw).get("intent", "other")
        except Exception:
            # Classification failure ⇒ treat as unknown; downstream grounding /
            # escalation still protects correctness (never fabricate).
            intent = "other"

        health = ontology.is_health_intent(intent)
        escalate = ontology.is_escalate_intent(intent)

        emit_trace_event(
            "concierge_intent_resolved",
            {"intent": intent, "health": health, "escalate_only": escalate},
            state,
        )

        return {
            "validated_input": query,
            "detected_language": unpacked.get("detected_language", "ja"),
            "input_context": unpacked.get("input_context", {}),
            "conversation_history": unpacked.get("conversation_history", []),
            "resolved_intent": intent,
            "health_flag": bool(health),
            "escalate_flag": bool(escalate),
            "status": AgentStatus.SUCCESS.value,
        }


def _unpack_envelope(raw: str) -> tuple[str, dict]:
    """Unpack the JSON envelope produced by ConciergeWorkflowGraphNode.extract_input().

    Falls back to treating `raw` as the plain query if it is not an envelope.
    """
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "query" in data:
            return str(data.get("query", "")), data
    except (ValueError, TypeError):
        pass
    return raw, {}
