"""Shared test fixtures — injected fakes for LLM / embedding / vector store."""

from __future__ import annotations

import json

import pytest

from src.services.kb_service import KBService
from src.services.llm_client import set_default_llm
from src.services.service import set_default_kb_service


LOCALIZED_MARKER = "(localized)"


class FakeLLM:
    """Deterministic BaseLLM stand-in.

    - intent-classification prompts → the configured intent as JSON
    - grounded-answer prompts → `answer` (or NO_ANSWER when told to)
    - localization prompts → user text + LOCALIZED_MARKER (observable pass)
    """

    def __init__(self, intent: str = "check_in_time", answer: str = "Check-in is at 15:00.",
                 no_answer: bool = False) -> None:
        self.intent = intent
        self.answer = answer
        self.no_answer = no_answer

    def complete(self, messages: list) -> dict:
        system = messages[0]["content"].lower()
        user = messages[1]["content"]
        if "classify" in system or "intent label" in system:
            return {"content": json.dumps({"intent": self.intent})}
        if "kb context" in user.lower() and "concierge" in system:
            return {"content": "NO_ANSWER" if self.no_answer else self.answer}
        # Localization pass. 🔴 Do NOT echo `user` unchanged: an echoing fake makes
        # the localized and un-localized strings identical, so every assertion
        # passes whether or not the localized text ever reaches the caller. That
        # is what hid the `output` vs `formatted_output` defect. Facts are
        # preserved (existing assertions still hold) but the pass is observable.
        return {"content": f"{user} {LOCALIZED_MARKER}"}

    def bind_tools(self, tools):
        return self

    def stream(self, messages):
        yield {"content": ""}


class FakeVectorStore:
    def __init__(self, hits: list | None = None) -> None:
        self._hits = hits or []
        self.upserted: list = []
        self.deleted: list = []

    def search(self, query: str, top_k: int = 10) -> list:
        return self._hits

    def upsert(self, documents: list) -> None:
        self.upserted = documents

    def delete(self, ids: list) -> None:
        self.deleted = ids


class FakeEmbedding:
    def embed(self, texts: list) -> list:
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]

    def embed_query(self, text: str) -> list:
        return [0.0, 0.0, 0.0, 0.0]


@pytest.fixture
def fake_llm():
    return FakeLLM()


@pytest.fixture
def install_fakes():
    """Install process-wide fakes; caller can override hits/intent via the factory."""

    def _install(intent="check_in_time", answer="Check-in is at 15:00.",
                 no_answer=False, hits=None):
        llm = FakeLLM(intent=intent, answer=answer, no_answer=no_answer)
        vs = FakeVectorStore(hits=hits or [])
        emb = FakeEmbedding()
        set_default_llm(llm)
        set_default_kb_service(KBService(vs, emb, top_k=5))
        return llm, vs, emb

    yield _install
    # teardown: clear globals
    set_default_llm(None)
    set_default_kb_service(None)


def kb_hit(doc_type="facility_facts", source_id="checkin", lang="en",
           text="Check-in 15:00", property_id="hotelA"):
    return {
        "id": f"{property_id}::{doc_type}::{source_id}",
        "metadata": {"doc_type": doc_type, "source_id": source_id, "lang": lang, "text": text},
    }


@pytest.fixture
def agent_config(monkeypatch):
    """実 LLM 1回転チェック用 config。

    `tests/integration/test_concierge_flow.py::test_normal_grounded_answer` と同じ
    hotelA/check-in 経路を使う。ハーネスは `config["llm"]` に live client を注入
    するため、compile 時に process-wide seam へ橋渡しし、KB も integration と
    同じ `kb_hit()` で seed する。
    """
    from src.graph.graph import LodgingConciergeGraph

    original_compile = LodgingConciergeGraph.compile

    def _compile_with_live_services(self, *args, **kwargs):
        llm = self.config.get("llm")
        if llm is not None:
            set_default_llm(llm)
            set_default_kb_service(KBService(FakeVectorStore([kb_hit()]), FakeEmbedding(), top_k=5))
        return original_compile(self, *args, **kwargs)

    monkeypatch.setattr(LodgingConciergeGraph, "compile", _compile_with_live_services)
    yield {}
    set_default_llm(None)
    set_default_kb_service(None)


@pytest.fixture
def case_payload() -> str:
    """`tests/integration/test_concierge_flow.py::test_normal_grounded_answer` と同じ質問。"""
    return "What time is check-in?"


@pytest.fixture
def live_input_context() -> dict:
    """SVC-C2-030 は property_id が必須。integration の既定値 hotelA を使う。"""
    return {"property_id": "hotelA"}
