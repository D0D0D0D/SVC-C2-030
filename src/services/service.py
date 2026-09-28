"""AgentCore Platform v1.0 — SVC-C2-030 service injection registry.

Nodes must not construct concrete external clients, and must not store them in
State (checkpointed — must stay primitives/JSON). This module holds the
process-wide KBService so nodes can obtain the injected instance:

  - real KBService (Pinecone/pgvector + embedding client) wired in server.py
  - a fake KBService injected in tests

The LLM seam lives in src/services/llm_client.py (same pattern).
"""

from __future__ import annotations

from typing import Any

from src.services.kb_service import KBService

_DEFAULT_KB: KBService | None = None


class _EmptyKBService:
    """Deterministic empty-KB fallback used when no real KBService is wired.

    Returned by get_kb_service() ONLY when the process was never provisioned
    (unprovisioned CI / provisional STG with no vector store / embedding). It
    behaves as an empty knowledge base: retrieve() returns no chunks, so
    KBRetrieveNode sets kb_hit=False and the workflow escalates to the front
    desk with status:success — the same "not in KB" degrade path, without a
    crash. upsert()/delete() are no-ops so the operator /ingest lane also does
    not crash in an unprovisioned env. This is NOT a BaseVectorStore-backed
    service; it is a minimal duck-typed stand-in for the retrieve/ingest seam.
    """

    def retrieve(self, property_id: str, query: str) -> list[dict[str, Any]]:
        # Read-only degradation: no KB wired → no chunks → kb_hit=False →
        # front-desk escalation (never a fabricated answer).
        return []

    def upsert(self, property_id: str, doc_type: str, docs: list[dict[str, Any]]) -> int:
        # WRITE must NOT silently succeed when no KB is wired. Returning 0 would
        # let /ingest answer 200 while dropping operator-submitted docs on the
        # floor ("wrote it but it isn't there"). The fallback is read-only, so a
        # write is a hard failure the caller must see.
        raise RuntimeError(
            "KBService is not wired: /ingest cannot persist documents in a "
            "degraded (no-KB) deployment. Provision the vector store + embedding "
            "before ingesting."
        )

    def delete(self, property_id: str, doc_type: str, source_ids: list[str]) -> int:
        raise RuntimeError(
            "KBService is not wired: /ingest cannot delete documents in a "
            "degraded (no-KB) deployment. Provision the vector store first."
        )


def set_default_kb_service(kb: KBService | None) -> None:
    """Install the process-wide KBService (called once at app/test setup)."""
    global _DEFAULT_KB
    _DEFAULT_KB = kb


def get_kb_service(explicit: KBService | None = None) -> KBService:
    """Return the KBService to use: an explicitly injected one, else the default.

    When neither an explicit nor a wired default KBService is available
    (unprovisioned CI / provisional STG), return a deterministic empty-KB
    fallback instead of raising: retrieve() yields no chunks → kb_hit=False →
    front-desk escalation with status:success (never a fabricated answer).
    Production behaviour is unchanged: a real injected/wired KBService is always
    returned as-is.
    """
    # Select by identity, NOT truthiness: a valid injected KBService whose
    # __bool__ is False must still be used. Only a genuine None (nothing wired)
    # degrades to the read-only empty-KB fallback.
    kb = explicit if explicit is not None else _DEFAULT_KB
    if kb is None:
        return _EmptyKBService()  # type: ignore[return-value]
    return kb
