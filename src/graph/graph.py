"""AgentCore Platform v1.0 — SVC-C2-030 outer graph (Cat 2)."""

# Cat 2 outer graph: fixed 5-node backbone (initialize → pre_process → main →
# post_process → finalize). Domain complexity is hidden in the `main` slot via
# ConciergeWorkflowGraphNode (GraphNode) wrapping the inner ConciergeWorkflowGraph.
# Do NOT override add_edges() — backbone wiring belongs to the framework.

from __future__ import annotations

import json
from typing import ClassVar

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus

from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


class ConciergeWorkflowGraphNode(GraphNode):
    """Wraps the inner ConciergeWorkflowGraph; assigned to the `main` slot."""

    # Re-raise inner errors as SubgraphError to fail fast.
    error_strategy: ClassVar[str] = "propagate"
    # No HITL in the initial version (conversation refine via history, not interrupt).
    propagate_hitl: ClassVar[bool] = False

    def __init__(self) -> None:
        super().__init__()
        self._subgraph = None  # cache: same inner graph + checkpointer across invoke/resume

    def get_subgraph(self):
        """Return the cached inner graph.

        Caching keeps the SAME inner graph (and its checkpointer) across invoke
        and any future resume — constructing a fresh one each call would lose the
        suspended state and loop forever.
        """
        if self._subgraph is None:
            from src.graph.domain_workflow_graph import ConciergeWorkflowGraph

            self._subgraph = ConciergeWorkflowGraph()
        return self._subgraph

    def extract_input(self, state: AgentState) -> str:
        """Pack a JSON envelope with the query + context the inner graph needs.

        The framework passes only a string into subgraph.invoke() — input_context,
        detected_language, and conversation_history do NOT propagate on their own
        so we carry them in the string.
        """
        return json.dumps(
            {
                "query": state.get("validated_input", state.get("user_input", "")),
                "detected_language": state.get("detected_language", "ja"),
                "input_context": state.get("input_context", {}),
                "conversation_history": state.get("conversation_history", []),
            },
            ensure_ascii=False,
            default=str,
        )

    def merge_output(self, state: AgentState, sub_result: dict) -> dict:
        """Map inner sub_result back into the outer state (changed keys only)."""
        return {
            "result": sub_result.get("result", ""),
            "escalate_flag": bool(sub_result.get("escalate_flag")),
            "health_flag": bool(sub_result.get("health_flag")),
            "detected_language": sub_result.get(
                "detected_language", state.get("detected_language", "ja")
            ),
            "status": sub_result.get("status"),
        }


class LodgingConciergeGraph(AgentBaseGraph):
    """Cat 2 outer graph for the Lodging Guest Multilingual Concierge Agent."""

    def get_output(self, state) -> dict:
        """Never release the ungated inner answer on an error run.

        Only `state["formatted_output"]` — the string that actually passed
        post_process's S-3 non-disclosure gate — may be published.

        AgentBaseGraph.get_output() returns
        `state["formatted_output"] or state["result"]`, and `state["result"]`
        is the ungated inner answer. That fallback fires in exactly the two cases
        where the text must NOT be released (both measured 2026-08-20):
          - S-3 raised, so `formatted_output` was never written; and
          - the rewrite came back empty, so `formatted_output` is falsy.
        Both handed the caller the very text the gate exists to withhold, the
        second one with status=success. `status` still carries the failure; the
        payload does not.
        """
        output = super().get_output(state)
        status = state.get("status")
        succeeded = status == AgentStatus.SUCCESS or status == AgentStatus.SUCCESS.value
        output["output"] = state.get("formatted_output") if succeeded else None
        return output

    @property
    def name(self) -> str:
        return "svc_c2_030"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects initialize + finalize
        history_max_turns = int(self.config.get("history_max_turns", 8))
        self._nodes["pre_process"] = PreProcessNode(history_max_turns=history_max_turns)
        self._nodes["main"] = ConciergeWorkflowGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — the framework wires the fixed backbone.
