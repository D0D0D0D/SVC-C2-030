"""Unit tests — SVC-C2-030 KB service (retrieve / ingest / upsert / delete)."""

from __future__ import annotations

import pytest

from src.services.kb_service import KBService, _doc_id
from tests.conftest import FakeEmbedding, FakeVectorStore, kb_hit


def _svc(hits=None):
    vs = FakeVectorStore(hits=hits or [])
    return KBService(vs, FakeEmbedding(), top_k=5), vs


class TestRetrieve:
    def test_returns_reference_safe_chunks(self):
        svc, _ = _svc([kb_hit(text="Check-in 15:00")])
        chunks = svc.retrieve("hotelA", "check-in?")
        assert chunks and chunks[0]["source_id"] == "checkin"
        assert set(chunks[0]) == {"doc_type", "source_id", "lang", "text"}

    def test_cross_property_leak_guard(self):
        # hit belongs to hotelB but caller asks for hotelA → filtered out
        svc, _ = _svc([kb_hit(property_id="hotelB")])
        assert svc.retrieve("hotelA", "q") == []

    def test_empty_query_returns_empty(self):
        svc, _ = _svc([kb_hit()])
        assert svc.retrieve("hotelA", "  ") == []

    def test_empty_property_id_fails_closed(self):
        # No property_id ⇒ namespace filter cannot apply ⇒ return nothing
        # (never serve another hotel's KB). Fail-closed tenant isolation.
        svc, _ = _svc([kb_hit(property_id="hotelB")])
        assert svc.retrieve("", "check-in?") == []


class TestUpsert:
    def test_idempotent_id(self):
        assert _doc_id("hotelA", "facility_facts", "checkin") == "hotelA::facility_facts::checkin"

    def test_upsert_embeds_and_writes(self):
        svc, vs = _svc()
        n = svc.upsert("hotelA", "facility_facts", [{"source_id": "checkin", "lang": "en", "text": "15:00"}])
        assert n == 1
        assert vs.upserted[0]["id"] == "hotelA::facility_facts::checkin"
        assert vs.upserted[0]["metadata"]["property_id"] == "hotelA"

    def test_invalid_doc_type_rejected(self):
        svc, _ = _svc()
        with pytest.raises(ValueError, match="doc_type"):
            svc.upsert("hotelA", "not_a_type", [{"source_id": "x", "text": "y"}])

    def test_missing_source_id_rejected(self):
        svc, _ = _svc()
        with pytest.raises(ValueError, match="source_id"):
            svc.upsert("hotelA", "facility_facts", [{"text": "no id"}])

    def test_missing_property_id_rejected(self):
        svc, _ = _svc()
        with pytest.raises(ValueError, match="property_id"):
            svc.upsert("", "facility_facts", [{"source_id": "x", "text": "y"}])


class TestDelete:
    def test_delete_by_source_id(self):
        svc, vs = _svc()
        n = svc.delete("hotelA", "facility_facts", ["checkin", "wifi"])
        assert n == 2
        assert vs.deleted == ["hotelA::facility_facts::checkin", "hotelA::facility_facts::wifi"]

    def test_delete_empty_noop(self):
        svc, vs = _svc()
        assert svc.delete("hotelA", "facility_facts", []) == 0
