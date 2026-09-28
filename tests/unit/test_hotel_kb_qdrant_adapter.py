"""Contract tests for the SDK Qdrant ↔ hotel KB adapter."""

from __future__ import annotations

from uuid import UUID

from src.services.hotel_kb_qdrant_adapter import HotelKBQdrantAdapter
from src.services.kb_service import KBService
from tests.conftest import FakeEmbedding


class _RawQdrant:
    def __init__(self, hits=None) -> None:
        self.hits = hits or []
        self.upserted: list = []
        self.deleted: list = []

    def search(self, query: str, top_k: int = 10) -> list:
        return self.hits

    def upsert(self, documents: list) -> None:
        self.upserted = documents

    def delete(self, ids: list) -> None:
        self.deleted = ids


def test_qdrant_payload_hit_is_mapped_to_kb_metadata_and_text():
    raw = _RawQdrant(
        [
            {
                "id": "4ef49c95-5df9-43eb-a0c2-80a14e898c4d",
                "payload": {
                    "_svc_c2_030_caller_id": "hotelA::facility_facts::checkin",
                    "property_id": "hotelA",
                    "doc_type": "facility_facts",
                    "source_id": "checkin",
                    "lang": "en",
                    "text": "Check-in is at 15:00.",
                },
                "score": 0.91,
                "similarity": 0.955,
            }
        ]
    )

    hit = HotelKBQdrantAdapter(raw).search("check-in", top_k=3)[0]

    assert hit["id"] == "hotelA::facility_facts::checkin"
    assert hit["text"] == "Check-in is at 15:00."
    assert hit["metadata"]["source_id"] == "checkin"
    assert "payload" not in hit
    assert "_svc_c2_030_caller_id" not in hit["metadata"]


def test_adapted_qdrant_hit_is_consumed_by_kb_service():
    raw = _RawQdrant(
        [
            {
                "id": "storage-id",
                "payload": {
                    "_svc_c2_030_caller_id": "hotelA::culture::tea",
                    "property_id": "hotelA",
                    "doc_type": "culture",
                    "source_id": "tea",
                    "lang": "ja",
                    "text": "お茶は客室にあります。",
                },
                "score": 0.8,
                "similarity": 0.9,
            }
        ]
    )
    service = KBService(HotelKBQdrantAdapter(raw), FakeEmbedding())

    assert service.retrieve("hotelA", "お茶") == [
        {
            "doc_type": "culture",
            "source_id": "tea",
            "lang": "ja",
            "text": "お茶は客室にあります。",
        }
    ]


def test_upsert_and_delete_translate_compound_ids_to_the_same_uuid():
    raw = _RawQdrant()
    adapter = HotelKBQdrantAdapter(raw)
    logical_id = "hotelA::facility_facts::checkin"

    adapter.upsert(
        [
            {
                "id": logical_id,
                "values": [0.1, 0.2],
                "metadata": {
                    "property_id": "hotelA",
                    "doc_type": "facility_facts",
                    "source_id": "checkin",
                    "lang": "en",
                    "text": "Check-in is at 15:00.",
                },
            }
        ]
    )
    adapter.delete([logical_id])

    written = raw.upserted[0]
    UUID(written["id"])
    assert written["content"] == "Check-in is at 15:00."
    assert written["metadata"]["_svc_c2_030_caller_id"] == logical_id
    assert "values" not in written
    assert raw.deleted == [written["id"]]
