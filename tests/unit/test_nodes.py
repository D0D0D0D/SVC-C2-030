"""Unit tests — SVC-C2-030 nodes."""

from __future__ import annotations

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.escalate_node import EscalateToFrontDeskNode
from src.nodes.grounded_answer_node import GroundedAnswerNode
from src.nodes.intent_resolve_node import IntentResolveNode
from src.nodes.kb_retrieve_node import KBRetrieveNode
from src.nodes.medical_disclaimer_node import MedicalDisclaimerNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode, detect_language
from src.services.hospitality import DISCLAIMER_MARKER
from tests.conftest import FakeEmbedding, FakeLLM, FakeVectorStore, kb_hit
from src.services.kb_service import KBService


class TestPreProcess:
    def test_language_detection(self):
        assert detect_language("체크인 몇 시예요?") == "ko"
        assert detect_language("チェックインは何時ですか？") == "ja"
        assert detect_language("入住时间是几点") == "zh"
        assert detect_language("What time is check-in?") == "en"

    def test_empty_input_errors(self):
        out = PreProcessNode().execute({"user_input": "  "})
        assert out["status"] == AgentStatus.ERROR

    def test_pii_sanitized_in_history(self):
        out = PreProcessNode().execute({"user_input": "email me at taro@example.com", "conversation_history": []})
        assert out["status"] == AgentStatus.SUCCESS
        stored = out["conversation_history"][-1]["text"]
        assert "taro@example.com" not in stored
        assert "[REDACTED]" in stored

    def test_history_capped(self):
        history = [{"role": "guest", "text": f"t{i}"} for i in range(20)]
        out = PreProcessNode().execute({"user_input": "hi", "conversation_history": history})
        assert len(out["conversation_history"]) <= 8

    def test_trust_level_verified_external(self):
        assert PreProcessNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL


class TestIntentResolve:
    def test_health_intent_flags(self):
        node = IntentResolveNode(llm=FakeLLM(intent="onsen_pregnancy"))
        out = node.execute({"user_input": '{"query":"pregnant ok?","detected_language":"en"}'})
        assert out["health_flag"] is True
        assert out["escalate_flag"] is False
        assert out["detected_language"] == "en"

    def test_escalate_intent_flags(self):
        node = IntentResolveNode(llm=FakeLLM(intent="complaint"))
        out = node.execute({"user_input": '{"query":"room dirty"}'})
        assert out["escalate_flag"] is True
        assert out["health_flag"] is False

    def test_classification_failure_is_other(self):
        class BadLLM(FakeLLM):
            def complete(self, messages):
                return {"content": "not json"}
        out = IntentResolveNode(llm=BadLLM()).execute({"user_input": '{"query":"x"}'})
        assert out["resolved_intent"] == "other"
        assert out["status"] == AgentStatus.SUCCESS


class TestKBRetrieve:
    def _kb(self, hits):
        return KBService(FakeVectorStore(hits), FakeEmbedding(), top_k=5)

    def test_hit_sets_kb_hit(self):
        node = KBRetrieveNode(kb_service=self._kb([kb_hit()]))
        out = node.execute({"validated_input": "check-in?", "input_context": {"property_id": "hotelA"}})
        assert out["kb_hit"] is True
        assert out["kb_chunks"][0]["source_id"] == "checkin"

    def test_miss_sets_no_hit(self):
        node = KBRetrieveNode(kb_service=self._kb([]))
        out = node.execute({"validated_input": "unknown", "input_context": {"property_id": "hotelA"}})
        assert out["kb_hit"] is False

    def test_escalate_only_skips_retrieval(self):
        node = KBRetrieveNode(kb_service=self._kb([kb_hit()]))
        out = node.execute({"escalate_flag": True, "validated_input": "x"})
        assert out["kb_hit"] is False
        assert out["kb_chunks"] == []


class TestGroundedAnswer:
    def test_grounds_on_kb(self):
        node = GroundedAnswerNode(llm=FakeLLM(answer="Check-in is 15:00."))
        out = node.execute({"kb_hit": True, "kb_chunks": [{"doc_type": "facility_facts", "source_id": "c", "text": "15:00"}], "validated_input": "check-in?"})
        assert out["result"] == "Check-in is 15:00."

    def test_no_kb_hit_escalates(self):
        node = GroundedAnswerNode(llm=FakeLLM())
        out = node.execute({"kb_hit": False, "validated_input": "x"})
        assert out["escalate_flag"] is True

    def test_no_answer_token_escalates(self):
        node = GroundedAnswerNode(llm=FakeLLM(no_answer=True))
        out = node.execute({"kb_hit": True, "kb_chunks": [{"text": "irrelevant"}], "validated_input": "x"})
        assert out["escalate_flag"] is True


class TestMedicalDisclaimer:
    def test_disclaimer_prepended(self):
        out = MedicalDisclaimerNode().execute({"result": "You may enter.", "detected_language": "en"})
        assert DISCLAIMER_MARKER in out["result"]
        assert "You may enter." in out["result"]

    def test_idempotent(self):
        first = MedicalDisclaimerNode().execute({"result": "ok", "detected_language": "en"})
        second = MedicalDisclaimerNode().execute({"result": first["result"], "detected_language": "en"})
        assert second["result"].count(DISCLAIMER_MARKER) == 1


class TestEscalate:
    def test_sets_escalation_message(self):
        out = EscalateToFrontDeskNode().execute({"detected_language": "ja"})
        assert out["escalate_flag"] is True
        assert "フロント" in out["result"]


class TestInnerRoute:
    def _graph(self):
        from src.graph.domain_workflow_graph import ConciergeWorkflowGraph
        return ConciergeWorkflowGraph()

    def test_escalate_wins_over_health(self):
        # If both flags are set, escalate is evaluated first → front desk, never S-5.
        g = self._graph()
        assert g.route({"escalate_flag": True, "kb_hit": True, "health_flag": True}) == "escalate"

    def test_health_routes_to_disclaimer(self):
        g = self._graph()
        assert g.route({"escalate_flag": False, "kb_hit": True, "health_flag": True}) == "medical_disclaimer"

    def test_normal_routes_to_end(self):
        from langgraph.graph import END
        g = self._graph()
        assert g.route({"escalate_flag": False, "kb_hit": True, "health_flag": False}) == END

    def test_error_routes_to_end(self):
        from langgraph.graph import END
        g = self._graph()
        assert g.route({"status": "error"}) == END


class TestPostProcess:
    def test_localizes_output(self):
        out = PostProcessNode(llm=FakeLLM()).execute({"result": "Check-in 15:00", "detected_language": "en"})
        assert out["formatted_output"]
        assert out["status"] == AgentStatus.SUCCESS

    def test_s3_blocks_pii_in_output(self):
        node = PostProcessNode(llm=FakeLLM())
        import pytest
        with pytest.raises(RuntimeError, match="PII"):
            node._extra_security_gate_output(
                {"formatted_output": "call taro@example.com", "_health_output": False}
            )

    def test_s3_preservation_rejects_dropped_disclaimer(self):
        node = PostProcessNode(llm=FakeLLM())
        import pytest
        with pytest.raises(RuntimeError, match="disclaimer"):
            node._extra_security_gate_output(
                {"formatted_output": "no disclaimer here", "_health_output": True}
            )

    def test_s3_passes_health_output_with_marker(self):
        node = PostProcessNode(llm=FakeLLM())
        res = node._extra_security_gate_output(
            {"formatted_output": f"{DISCLAIMER_MARKER} safe info", "_health_output": True}
        )
        assert "_health_output" not in res  # transient flag popped


class TestOuterGetOutputPublishesOnlyGatedText:
    """`LodgingConciergeGraph.get_output()` is a pure projection of State, so it is
    tested directly. It must publish ONLY `formatted_output` — the string that
    passed post_process's S-3 gate — and never fall back to the ungated `result`.

    post_process also substitutes an empty rewrite before the gate, so in a normal
    run `formatted_output` is non-empty; this pins the boundary invariant itself so
    the leak cannot come back if that upstream guard is ever changed.
    """

    @staticmethod
    def _graph():
        from src.graph.graph import LodgingConciergeGraph

        return LodgingConciergeGraph()

    def test_absent_formatted_output_does_not_fall_back_to_result(self):
        out = self._graph().get_output(
            {"status": "success", "result": "UNGATED inner answer"}
        )
        assert not out["output"]

    def test_empty_formatted_output_does_not_fall_back_to_result(self):
        out = self._graph().get_output(
            {"status": "success", "formatted_output": "", "result": "UNGATED inner answer"}
        )
        assert out["output"] != "UNGATED inner answer"

    def test_error_run_publishes_no_payload(self):
        out = self._graph().get_output(
            {"status": "error", "formatted_output": "gated text", "result": "UNGATED"}
        )
        assert out["output"] is None

    def test_gated_text_is_published_on_success(self):
        out = self._graph().get_output(
            {"status": "success", "formatted_output": "gated text", "result": "UNGATED"}
        )
        assert out["output"] == "gated text"
