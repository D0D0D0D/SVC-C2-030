"""AgentCore Platform v1.0 — SVC-C2-030 inner concierge workflow graph."""

# Inner graph for the Cat 2 template. Instantiated by
# ConciergeWorkflowGraphNode.get_subgraph() in graph.py. Inherits BaseGraph
# (fully custom topology): the outer AgentBaseGraph backbone already applies the
# S-1..S-4 gates, so the inner graph must NOT be an AgentBaseGraph (would double
# the AgentState / S-gates — docs/02_design.md F-01).

from __future__ import annotations

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus

from src.nodes.escalate_node import EscalateToFrontDeskNode
from src.nodes.grounded_answer_node import GroundedAnswerNode
from src.nodes.intent_resolve_node import IntentResolveNode
from src.nodes.kb_retrieve_node import KBRetrieveNode
from src.nodes.medical_disclaimer_node import MedicalDisclaimerNode
from src.schemas.state import State


class ConciergeWorkflowGraph(BaseGraph):
    """Multi-step lodging concierge workflow.

    Topology:
        START → intent_resolve → kb_retrieve → grounded_answer
              → {route} → medical_disclaimer  (kb_hit AND health_flag)
                        → escalate            (escalate_flag / no kb_hit)
                        → END                 (normal grounded answer)
        medical_disclaimer → END
        escalate → END

    The KB-hit-first decision is made in grounded_answer (sets escalate_flag
    when there is no KB support); route() then dispatches.
    """

    @property
    def name(self) -> str:
        return "lodging_concierge_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory inner-graph config; LLM/KB are injected via seams.
        return None

    def register_nodes(self) -> None:
        # No super() — BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize/finalize (outer backbone concern).
        self._nodes["intent_resolve"] = IntentResolveNode()
        self._nodes["kb_retrieve"] = KBRetrieveNode()
        self._nodes["grounded_answer"] = GroundedAnswerNode()
        self._nodes["medical_disclaimer"] = MedicalDisclaimerNode()
        self._nodes["escalate"] = EscalateToFrontDeskNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "intent_resolve")
        self._sg.add_edge("intent_resolve", "kb_retrieve")
        self._sg.add_edge("kb_retrieve", "grounded_answer")
        self._sg.add_conditional_edges(
            "grounded_answer",
            self.route,
            {
                "medical_disclaimer": "medical_disclaimer",
                "escalate": "escalate",
                END: END,
            },
        )
        self._sg.add_edge("medical_disclaimer", END)
        self._sg.add_edge("escalate", END)

    def route(self, state: State) -> str:
        """Dispatch after grounded_answer.

        Annotated with `State` (not AgentState) so custom fields (health_flag,
        escalate_flag) are visible — an AgentState annotation would project them
        away and always fall through.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        if state.get("escalate_flag"):
            return "escalate"
        if state.get("kb_hit") and state.get("health_flag"):
            return "medical_disclaimer"
        return END

    def get_output(self, state: State) -> dict:
        """Shape sub_result for the outer merge_output().

        `result` holds the grounded/disclaimer/escalation answer. Do not resolve
        any sensitive values here — the outer post_process localizes and S-3
        checks it.
        """
        return {
            "result": state.get("result", ""),
            "escalate_flag": bool(state.get("escalate_flag")),
            "health_flag": bool(state.get("health_flag")),
            "detected_language": state.get("detected_language", "ja"),
            "status": state.get("status"),
        }
