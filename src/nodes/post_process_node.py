"""AgentCore Platform v1.0 — SVC-C2-030 post_process (HospitalityLocalize / S-3)."""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode, detect_pii, emit_trace_event
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.hospitality import (
    DISCLAIMER_MARKER,
    localization_system_prompt,
    normalize_language,
)
from src.services.llm_client import complete_text, get_llm


class PostProcessNode(FunctionNode):
    """Localize the answer to the guest's language + おもてなし tone, then S-3.

    Both routes — normal grounded answer AND front-desk escalation — pass through
    here so the final output always matches the detected language (F-02). The
    tone/register is instructed to the LLM (no template set). S-3 then verifies
    non-disclosure of other guests' PII and preservation of the medical
    disclaimer (preservation variant).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm=None) -> None:
        super().__init__()
        self._llm = llm  # injection seam

    def execute(self, state: dict) -> dict:
        result = str(state.get("result", ""))
        language = normalize_language(state.get("detected_language", "ja"))
        # A health-adjacent answer carries the disclaimer marker (set by S-5).
        health_answer = DISCLAIMER_MARKER in result

        try:
            localized = complete_text(
                get_llm(self._llm), localization_system_prompt(language), result
            )
        except Exception as exc:
            # Localization failure ⇒ fall back to the raw (already-grounded)
            # answer rather than dropping content.
            emit_trace_event("concierge_localize_error", {"error": str(exc)}, state)
            localized = result

        # Preservation: if the LLM dropped the disclaimer from a health answer,
        # restore the original (which carried it). The S-3 gate below is the hard
        # backstop; this keeps a benign LLM omission from failing the request.
        if health_answer and DISCLAIMER_MARKER not in localized:
            localized = result

        # An empty rewrite is a localization failure, not an answer. Substitute the
        # grounded answer BEFORE the S-3 gate so the substituted text is itself
        # gated. (Without this, an empty `formatted_output` is falsy and
        # get_output()'s `formatted_output or result` would publish the ungated
        # inner answer with status=success — measured 2026-08-20.)
        if not localized.strip():
            emit_trace_event(
                "concierge_localize_empty",
                {"language": language, "reason": "empty_localized_output"},
                state,
            )
            localized = result

        # NOTE — deliberately NOT dropping a PII-bearing rewrite in favour of the
        # grounded answer. That looks like graceful degradation but is a safety
        # regression: detect_pii does not detect bare CJK personal names, so a
        # rewrite reading "Dear Guest, 山田太郎に…" trips only on "Dear Guest",
        # and substituting the raw answer would PUBLISH the undetected name that
        # S-3 was about to block. A PII finding must fail closed; the prompt (see
        # hospitality.localization_system_prompt) is what keeps the common
        # salutation case from ever reaching the gate.

        emit_trace_event(
            "concierge_localized",
            {"language": language, "escalated": bool(state.get("escalate_flag"))},
            state,
        )
        # `_health_output` is a transient flag consumed only by the S-3 gate on
        # this same return dict; it is popped there and never reaches State.
        # 🔴 `formatted_output`, NOT `output`. AgentBaseGraph.get_output() returns
        # `state["formatted_output"] or state["result"]` and never reads
        # `state["output"]`, so writing `output` here silently discarded the whole
        # localization step and handed the caller the un-localized inner answer
        # (measured with a live LLM 2026-08-20). F-02 language matching was never
        # actually delivered.
        return {
            "formatted_output": localized,
            "_health_output": health_answer,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_output(self, result: dict) -> dict:
        """S-3 domain checks: non-disclosure + medical-disclaimer preservation.

        - Non-disclosure: reject if the guest-facing output contains PII (other
          guests' data must not leak; the framework gate already scans creds).
        - Preservation variant: a health-adjacent answer MUST retain the medical
          disclaimer marker — reject if it was dropped.
        """
        health_output = bool(result.pop("_health_output", False))
        output = result.get("formatted_output", "")

        if isinstance(output, str) and output:
            if detect_pii(output):
                raise RuntimeError(
                    "S-3 output gate: PII detected in guest-facing output "
                    "(non-disclosure). Output blocked."
                )
            if health_output and DISCLAIMER_MARKER not in output:
                raise RuntimeError(
                    "S-3 output gate: medical disclaimer missing from a "
                    "health-adjacent answer (preservation). Output blocked."
                )
        return result
