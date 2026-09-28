"""Translate the hotel KB contract to the SDK Qdrant store contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from shared.services.vector_store.base_vector_store import BaseVectorStore


CALLER_ID_PAYLOAD_KEY = "_svc_c2_030_caller_id"


class HotelKBQdrantAdapter(BaseVectorStore):
    """Preserve the KBService hit/document shape across the Qdrant boundary.

    ``KBService`` uses human-readable compound IDs and reads ``metadata`` plus
    ``text``. The SDK store accepts only unsigned integers/UUIDs and returns
    fields under ``payload``. This adapter makes both translations explicit.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    def search(self, query: str, top_k: int = 10) -> list[dict[str, Any]]:
        adapted: list[dict[str, Any]] = []
        for hit in self._store.search(query, top_k=top_k) or []:
            payload = hit.get("payload") if isinstance(hit, Mapping) else None
            metadata = dict(payload) if isinstance(payload, Mapping) else {}
            caller_id = metadata.pop(CALLER_ID_PAYLOAD_KEY, None)
            adapted.append(
                {
                    "id": (
                        caller_id
                        if caller_id is not None
                        else hit.get("id") if isinstance(hit, Mapping) else None
                    ),
                    "text": metadata.get("text", ""),
                    "metadata": metadata,
                    "score": hit.get("score") if isinstance(hit, Mapping) else None,
                    "similarity": (
                        hit.get("similarity") if isinstance(hit, Mapping) else None
                    ),
                }
            )
        return adapted

    def upsert(self, documents: list[Any]) -> None:
        translated: list[dict[str, Any]] = []
        for document in documents:
            if not isinstance(document, Mapping):
                # Delegate malformed inputs so the SDK keeps ownership of its
                # validation/error vocabulary.
                self._store.upsert(documents)
                return
            metadata = document.get("metadata", {})
            if not isinstance(metadata, Mapping):
                self._store.upsert(documents)
                return
            caller_id = document.get("id")
            text = metadata.get("text", document.get("text", ""))
            translated.append(
                {
                    "id": _sdk_point_id(caller_id),
                    "content": text,
                    "metadata": {
                        **metadata,
                        CALLER_ID_PAYLOAD_KEY: caller_id,
                    },
                }
            )
        self._store.upsert(translated)

    def delete(self, ids: list[Any]) -> None:
        self._store.delete([_sdk_point_id(caller_id) for caller_id in ids])


def _sdk_point_id(caller_id: object) -> str:
    """Map a logical hotel-document ID to the UUID shape the SDK accepts."""
    return str(uuid5(NAMESPACE_URL, f"svc-c2-030:{caller_id}"))
