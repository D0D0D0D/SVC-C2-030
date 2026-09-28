"""AgentCore Platform v1.0 — SVC-C2-030 KB service (retrieve + ingest/upsert).

The KB *content* is supplied by the operator (adopting hotel); the agent owns the
*data model* and the *ingest/upsert path* so operators never need a separate
tool (docs/02_design.md §KB 投入・更新の口). This service wraps the injected
BaseVectorStore + BaseEmbedding seams:

  - retrieve()  → the /invoke grounding path (KBRetrieve node)
  - upsert()    → the /ingest path (bulk load + differential update)
  - delete()    → the /ingest path (remove stale source docs)

Idempotency key: (property_id, doc_type, source_id) is encoded into each
document id, so re-ingesting the same source overwrites rather than duplicates.
KB documents are per-property namespaced by prefixing the id with property_id.

No credentials or business logic here; embedding/vector-store clients are
injected (real clients wired in server.py, fakes in tests).
"""

from __future__ import annotations

from typing import Any

from shared.services.embedding.base_embedding import BaseEmbedding
from shared.services.vector_store.base_vector_store import BaseVectorStore

# Valid KB document types (docs/02_design.md §KB スキーマ).
VALID_DOC_TYPES: frozenset[str] = frozenset(
    {"facility_facts", "onsen_analysis", "culture"}
)


def _doc_id(property_id: str, doc_type: str, source_id: str) -> str:
    """Deterministic idempotency key: (property_id, doc_type, source_id)."""
    return f"{property_id}::{doc_type}::{source_id}"


class KBService:
    """Retrieve / ingest / delete over a per-property vector KB."""

    def __init__(
        self,
        vector_store: BaseVectorStore,
        embedding: BaseEmbedding,
        top_k: int = 5,
    ) -> None:
        self._vs = vector_store
        self._emb = embedding
        self._top_k = top_k

    # ── /invoke path ──────────────────────────────────────────────────────────

    def retrieve(self, property_id: str, query: str) -> list[dict[str, Any]]:
        """Return KB chunks relevant to `query` for a single property.

        Reference-safe: chunks carry {doc_type, source_id, lang, text} only —
        no other guests' PII (S-3 / State Constraints). Results are filtered to
        the property namespace.

        Fail-closed on a missing property_id: without it the per-property
        namespace filter cannot be applied, so returning any hit would leak
        another hotel's KB. Return nothing instead (→ escalation, never a
        cross-tenant answer).
        """
        if not query or not query.strip():
            return []
        if not property_id:
            return []
        hits = self._vs.search(query=query, top_k=self._top_k)
        chunks: list[dict[str, Any]] = []
        prefix = f"{property_id}::"
        for h in hits:
            doc_id = str(h.get("id", ""))
            if not doc_id.startswith(prefix):
                continue  # cross-property leakage guard (always enforced)
            meta = h.get("metadata", h)
            chunks.append(
                {
                    "doc_type": meta.get("doc_type", ""),
                    "source_id": meta.get("source_id", ""),
                    "lang": meta.get("lang", ""),
                    "text": meta.get("text", h.get("text", "")),
                }
            )
        return chunks

    # ── /ingest path ──────────────────────────────────────────────────────────

    def upsert(
        self,
        property_id: str,
        doc_type: str,
        docs: list[dict[str, Any]],
    ) -> int:
        """Bulk load / differential update KB documents for a property.

        Each doc: {source_id, lang, text}. Idempotent on
        (property_id, doc_type, source_id): re-ingesting overwrites.
        Returns the number of documents upserted.
        """
        if doc_type not in VALID_DOC_TYPES:
            raise ValueError(
                f"unknown doc_type {doc_type!r}; expected one of {sorted(VALID_DOC_TYPES)}"
            )
        if not property_id:
            raise ValueError("property_id is required for ingest")
        if not docs:
            return 0

        texts = [str(d.get("text", "")) for d in docs]
        vectors = self._emb.embed(texts)
        payload: list[dict[str, Any]] = []
        for d, vector in zip(docs, vectors):
            source_id = str(d.get("source_id", ""))
            if not source_id:
                raise ValueError("each ingest doc requires a source_id")
            payload.append(
                {
                    "id": _doc_id(property_id, doc_type, source_id),
                    "values": vector,
                    "metadata": {
                        "property_id": property_id,
                        "doc_type": doc_type,
                        "source_id": source_id,
                        "lang": str(d.get("lang", "")),
                        "text": str(d.get("text", "")),
                    },
                }
            )
        self._vs.upsert(payload)
        return len(payload)

    def delete(
        self,
        property_id: str,
        doc_type: str,
        source_ids: list[str],
    ) -> int:
        """Remove stale KB documents by source_id. Returns the count deleted."""
        if not source_ids:
            return 0
        ids = [_doc_id(property_id, doc_type, sid) for sid in source_ids]
        self._vs.delete(ids)
        return len(ids)
