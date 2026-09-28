"""Integration tests — SVC-C2-030 full outer+inner graph flow."""

from __future__ import annotations

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import LodgingConciergeGraph
from src.services.hospitality import DISCLAIMER_MARKER
from tests.conftest import LOCALIZED_MARKER, FakeLLM, kb_hit


def _invoke(user_input, property_id="hotelA"):
    g = LodgingConciergeGraph()
    g.compile()
    ctx = InvocationContext(session_id="s1", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    return g.invoke(user_input, ctx=ctx, input_context={"property_id": property_id})


class TestConciergeFlow:
    def test_normal_grounded_answer(self, install_fakes):
        install_fakes(intent="check_in_time", answer="Check-in is at 15:00.", hits=[kb_hit()])
        out = _invoke("What time is check-in?")
        assert out["status"] in ("success", "SUCCESS") or str(out["status"]).endswith("SUCCESS")
        assert "15:00" in out["output"]

    def test_complaint_escalates_to_front_desk(self, install_fakes):
        install_fakes(intent="complaint", hits=[])
        out = _invoke("The room is dirty")
        assert "front desk" in out["output"].lower() or "フロント" in out["output"]

    def test_not_in_kb_escalates(self, install_fakes):
        install_fakes(intent="check_in_time", hits=[])  # answerable intent but empty KB
        out = _invoke("Some obscure question")
        assert out["output"]  # localized escalation message, not a fabricated fact

    def test_onsen_health_gets_disclaimer(self, install_fakes):
        install_fakes(
            intent="onsen_pregnancy",
            answer="Pregnant guests should take care.",
            hits=[kb_hit(doc_type="onsen_analysis", source_id="spring1", text="Contraindications: pregnancy")],
        )
        out = _invoke("Can pregnant guests enter this spring?")
        assert DISCLAIMER_MARKER in out["output"]


class TestLocalizedAnswerReachesTheCaller:
    """F-02: the guest-facing answer the caller receives must be the LOCALIZED one.

    `AgentBaseGraph.get_output()` surfaces `state["formatted_output"] or
    state["result"]` and never reads `state["output"]`. While post_process wrote
    `output`, the whole localization step (an LLM call) was silently discarded and
    the caller got the un-localized inner answer. Measured with a live LLM
    2026-08-20; the shared fake hid it by echoing the answer unchanged.
    """

    def test_normal_answer_is_the_localized_text(self, install_fakes):
        install_fakes(intent="check_in_time", answer="Check-in is at 15:00.", hits=[kb_hit()])
        out = _invoke("What time is check-in?")
        assert "15:00" in out["output"]
        # A failure here means the caller got the un-localized inner answer,
        # i.e. post_process's result never reached get_output().
        assert LOCALIZED_MARKER in out["output"]

    def test_escalation_is_also_localized(self, install_fakes):
        """Both routes pass through post_process, so both must be localized."""
        install_fakes(intent="complaint", hits=[])
        out = _invoke("The room is dirty")
        assert LOCALIZED_MARKER in out["output"]


class _RewriteLLM(FakeLLM):
    """FakeLLM whose LOCALIZATION turn is caller-controlled (adversarial rewrites)."""

    def __init__(self, rewrite, **kw):
        super().__init__(**kw)
        self._rewrite = rewrite

    def complete(self, messages: list) -> dict:
        if "rewrite" in messages[0]["content"].lower():
            return {"content": self._rewrite(messages[1]["content"])}
        return super().complete(messages)


def _invoke_with(llm, question="What time is check-in?", hits=None):
    from src.services.kb_service import KBService
    from src.services.llm_client import set_default_llm
    from src.services.service import set_default_kb_service
    from tests.conftest import FakeEmbedding, FakeVectorStore

    set_default_llm(llm)
    set_default_kb_service(
        KBService(FakeVectorStore(hits if hits is not None else [kb_hit()]), FakeEmbedding(), top_k=5)
    )
    try:
        return _invoke(question)
    finally:
        set_default_llm(None)
        set_default_kb_service(None)


class TestOnlyTheGatedTextIsPublished:
    """`get_output()` may publish ONLY the string that passed post_process's S-3 gate.

    The framework default is `state["formatted_output"] or state["result"]`, and
    `result` is the ungated inner answer. Both branches below reached the caller
    before this was fixed (measured with a live LLM 2026-08-20).
    """

    def test_s3_block_publishes_no_payload_instead_of_the_ungated_answer(self):
        """S-3 raises → formatted_output unwritten → the fallback used to leak `result`."""
        salute = lambda text: f"Dear Guest, {text}"  # noqa: E731
        llm = _RewriteLLM(salute, answer="Contact Taro Yamada for details.")
        out = _invoke_with(llm, "Who handles this?")
        assert str(out["status"]).lower().endswith("error")
        assert not out["output"]
        assert "Taro Yamada" not in str(out.get("output") or "")

    def test_empty_rewrite_does_not_publish_the_ungated_answer(self):
        """An empty rewrite is falsy, so `formatted_output or result` leaked `result`
        with status=success. The empty rewrite now falls back BEFORE the S-3 gate,
        so whatever is published has been gated."""
        llm = _RewriteLLM(lambda text: "   ", answer="Check-in is at 15:00.")
        out = _invoke_with(llm)
        assert str(out["status"]).lower().endswith("success")
        assert "15:00" in out["output"]

    def test_empty_rewrite_over_a_pii_answer_is_still_blocked(self):
        """Proves the empty-rewrite substitution is RE-GATED, not a way around S-3."""
        llm = _RewriteLLM(lambda text: "", answer="Contact Taro Yamada for details.")
        out = _invoke_with(llm, "Who handles this?")
        assert str(out["status"]).lower().endswith("error")
        assert "Taro Yamada" not in str(out.get("output") or "")


class TestPIIFindingFailsClosed:
    """A PII finding must fail the request, never be traded for the raw answer.

    Dropping a PII-bearing rewrite in favour of the grounded answer looks like
    graceful degradation but launders the detector's blind spots: detect_pii does
    not detect bare CJK personal names, so the substitution would PUBLISH a name
    that S-3 was in the middle of blocking.
    """

    def test_bare_cjk_name_is_not_laundered_by_substituting_the_raw_answer(self):
        salute = lambda text: f"Dear Guest, {text}"  # noqa: E731
        llm = _RewriteLLM(salute, answer="山田太郎にご連絡ください。")
        out = _invoke_with(llm, "誰に連絡すればいいですか？")
        assert str(out["status"]).lower().endswith("error")
        assert "山田太郎" not in str(out.get("output") or "")

    def test_the_detector_really_is_blind_to_that_name(self):
        """Guards the premise above: if detect_pii ever learns bare CJK names the
        test stops proving anything, and this fails loudly instead."""
        from framework.nodes.function_node import detect_pii

        assert detect_pii("山田太郎にご連絡ください。") == []
        assert detect_pii("Dear Guest, 山田太郎にご連絡ください。")
