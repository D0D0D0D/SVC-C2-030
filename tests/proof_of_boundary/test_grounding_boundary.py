"""PB (domain) — grounding / anti-hallucination + medical-disclaimer preservation.

These are the boundary guarantees that make this template more than a generic RAG:
  - it MUST NOT fabricate property facts (not-in-KB → escalate, never invent), and
  - a health-adjacent onsen answer MUST retain the medical disclaimer (S-5),

verified through the real inner graph with injected fakes.
"""

from __future__ import annotations

import pytest

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import LodgingConciergeGraph
from src.services.hospitality import DISCLAIMER_MARKER, escalation_for
from tests.conftest import kb_hit


def _invoke(user_input, property_id="hotelA"):
    g = LodgingConciergeGraph()
    g.compile()
    ctx = InvocationContext(session_id="pb", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    return g.invoke(user_input, ctx=ctx, input_context={"property_id": property_id})


class TestAntiHallucination:
    def test_not_in_kb_never_fabricates(self, install_fakes):
        # LLM would happily answer, but KB is empty → must escalate, not fabricate.
        install_fakes(intent="check_in_time", answer="Check-in is at 42:00 (WRONG).", hits=[])
        out = _invoke("What time is check-in?")
        assert "42:00" not in out["output"]
        # output is the front-desk escalation wording in the guest language
        assert escalation_for("en").split(".")[0][:12].lower() in out["output"].lower() \
            or "front desk" in out["output"].lower()

    def test_llm_no_answer_token_escalates(self, install_fakes):
        install_fakes(intent="check_in_time", no_answer=True, hits=[kb_hit()])
        out = _invoke("Something not covered by the chunk")
        assert "front desk" in out["output"].lower() or "フロント" in out["output"]


class TestDisclaimerPreservation:
    def test_health_answer_retains_disclaimer(self, install_fakes):
        install_fakes(
            intent="onsen_contraindication",
            answer="This spring has contraindications.",
            hits=[kb_hit(doc_type="onsen_analysis", source_id="s1", text="Contraindications listed")],
        )
        out = _invoke("Any contraindications for this spring?")
        assert DISCLAIMER_MARKER in out["output"]

    def test_s3_gate_blocks_dropped_disclaimer(self):
        # Directly exercise the S-3 preservation gate: a health output that lost
        # the disclaimer marker must be blocked.
        from src.nodes.post_process_node import PostProcessNode
        with pytest.raises(RuntimeError, match="disclaimer"):
            PostProcessNode()._extra_security_gate_output(
                {"formatted_output": "you can enter", "_health_output": True}
            )
