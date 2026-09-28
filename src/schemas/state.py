"""AgentCore Platform v1.0 — SVC-C2-030 State schema."""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.

from __future__ import annotations

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Lodging Guest Multilingual Concierge Agent state.

    Inherits all shared fields (user_input, validated_input, status,
    session_id, node_history, error_log, input_context, result,
    formatted_output, hitl_*, ...) from AgentState.

    Domain-specific fields below. All are primitives or JSON-serializable
    containers (flat TypedDict) — no Pydantic, no credentials, no other
    guests' PII (kb_chunks carry reference ids and grounded text only).
    """

    # Language handling (InputParse / LanguageDetect)
    detected_language: str            # "ja" | "en" | "zh" | "ko"

    # Multi-turn context. PII-sanitised (S-2) and capped to the most recent
    # N turns before being stored — never raw user_input (checkpoint DB safety).
    conversation_history: list        # list[dict]: {"role": str, "text": str}

    # Intent resolution (minimal lodging ontology + onsen health flag)
    resolved_intent: str              # ontology intent key, e.g. "check_in_time"
    health_flag: bool                 # onsen health question → route via S-5
    escalate_flag: bool               # not-in-KB / complaint / booking / law → front desk

    # KB retrieval (facility_facts / onsen_analysis / culture)
    kb_chunks: list                   # list[dict]: {"doc_type","source_id","lang","text"}
    kb_hit: bool                      # KB retrieval hit (first branch predicate)

    # Answer assembly. `result` (inherited) holds the grounded/escalation answer;
    # the localized guest-facing final answer goes to `formatted_output`
    # (inherited) — that is the key AgentBaseGraph.get_output() surfaces to the
    # caller. A template-local `output` field is NOT read by the framework.
