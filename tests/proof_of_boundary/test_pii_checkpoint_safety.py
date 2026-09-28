"""PB (domain) — guest PII must not persist raw in checkpointed State.

State is checkpointed to the DB. conversation_history is PII-sanitized before
storage (docs/02_design.md F-03). This verifies the sanitized turn does not
carry the raw email/phone the guest typed.
"""

from __future__ import annotations

from src.nodes.pre_process_node import PreProcessNode


class TestPIICheckpointSafety:
    def test_email_not_stored_raw(self):
        out = PreProcessNode().execute(
            {"user_input": "please email me at taro@example.com", "conversation_history": []}
        )
        stored = out["conversation_history"][-1]["text"]
        assert "taro@example.com" not in stored
        assert "[REDACTED]" in stored

    def test_phone_not_stored_raw(self):
        out = PreProcessNode().execute(
            {"user_input": "call me on 090-1234-5678", "conversation_history": []}
        )
        stored = out["conversation_history"][-1]["text"]
        assert "090-1234-5678" not in stored

    def test_history_bounded(self):
        history = [{"role": "guest", "text": f"turn {i}"} for i in range(30)]
        out = PreProcessNode().execute({"user_input": "hello", "conversation_history": history})
        # Bounded so the checkpoint does not grow unbounded with guest content.
        assert len(out["conversation_history"]) <= 8
