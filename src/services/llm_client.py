"""AgentCore Platform v1.0 — SVC-C2-030 LLM injection seam.

`framework.llm` does not exist in the 1.0.0 wheel; the LLM interface lives in
`shared.services.llm.base_llm.BaseLLM` (complete(messages: list) -> dict).

Nodes never construct a concrete LLM. They receive a `BaseLLM` instance (real
client wired in src/api/server.py, a fake injected in tests). This module holds
a process-wide default resolver so nodes can obtain the injected client without
threading it through State (which is checkpointed — must not hold clients).
"""

from __future__ import annotations

from typing import Any

from shared.services.llm.base_llm import BaseLLM

_DEFAULT_LLM: BaseLLM | None = None


class _OfflineLLM:
    """Deterministic offline fallback used when no real LLM seam is installed.

    Intentionally a plain duck-typed object (NOT a BaseLLM subclass): BaseLLM is
    an ABC in the wheel and may declare abstract methods beyond complete(); the
    node call sites only ever invoke .complete(messages) via complete_text(), so
    a minimal object satisfies the seam without risking ABC-instantiation errors.


    Returned by get_llm() ONLY when the process was never wired (unprovisioned
    CI / provisional STG). It never calls a network: it echoes the last user
    message so the workflow degrades to a coherent, non-empty result instead of
    crashing:

      - IntentResolveNode json.loads() on this echo raises → caught → intent
        "other" → (with an offline/empty KB) escalate. No fabricated intent.
      - GroundedAnswerNode is not reached in offline mode (kb_hit is False →
        it escalates before generating), so no fabricated property fact.
      - PostProcessNode localization echoes the already-grounded escalation
        text back unchanged, keeping output non-empty and language-safe.

    This preserves the anti-hallucination contract: offline mode can only ever
    produce the deterministic front-desk escalation, never an invented answer.
    """

    def complete(self, messages: list[dict[str, Any]], **_kwargs: Any) -> dict[str, Any]:
        last_user = ""
        for msg in messages or []:
            if isinstance(msg, dict) and msg.get("role") == "user":
                last_user = str(msg.get("content", ""))
        return {"content": last_user}


def set_default_llm(llm: BaseLLM | None) -> None:
    """Install the process-wide LLM (called once at app/test setup)."""
    global _DEFAULT_LLM
    _DEFAULT_LLM = llm


def get_llm(explicit: BaseLLM | None = None) -> BaseLLM:
    """Return the LLM to use: an explicitly injected one, else the default.

    When neither an explicit nor a wired default LLM is available (unprovisioned
    CI / provisional STG), return a deterministic offline fallback instead of
    raising, so the workflow degrades to a front-desk escalation with
    status:success rather than crashing. Production behaviour is unchanged: a
    real injected/wired LLM is always returned as-is.
    """
    # Select by identity, NOT truthiness: a valid injected client whose
    # __bool__ is False must still be used (never silently swapped for the
    # offline fallback). Only a genuine None (nothing wired) degrades.
    llm = explicit if explicit is not None else _DEFAULT_LLM
    if llm is None:
        return _OfflineLLM()
    return llm


def complete_text(llm: BaseLLM, system: str, user: str) -> str:
    """Convenience: single system+user turn → assistant text.

    BaseLLM.complete(messages) returns a dict; we read the assistant content.
    Kept here so nodes don't duplicate message-shaping / response-parsing.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    response = llm.complete(messages)
    # BaseLLM.complete returns a dict; tolerate the common shapes.
    if isinstance(response, dict):
        return str(
            response.get("content")
            or response.get("text")
            or response.get("output")
            or ""
        ).strip()
    return str(response).strip()
