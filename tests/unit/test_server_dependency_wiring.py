"""Startup wiring contracts: posture, atomicity, and runtime lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from src.services.llm_client import get_llm
from src.services.service import get_kb_service


def _enabled_config() -> dict:
    return {
        "kb_top_k": 7,
        "llm": {"api_key": "injected-outside-this-test"},
        "vector_store": {
            "enabled": True,
            "url": "http://qdrant:6333",
            "collection_name": "hotel_kb",
            "namespace": "svc",
            "agent_name": "svc_c2_030",
            "vector_size": 1536,
            "distance": "cosine",
            "timeout_s": 30,
            "write_timeout_s": 60,
            "max_batch_documents": 1000,
            "max_top_k": 100,
        },
        "embedding": {
            "base_url": "https://example.openai.azure.com/",
            "model": "text-embedding-3-small",
            "api_version": "2024-02-15-preview",
            "dimensions": 1536,
            "provider": "azure",
            "timeout_s": 30,
            "batch_size": 16,
            "max_retries": 5,
            "base_delay": 1.0,
            "max_delay": 60.0,
            "max_cache_size": 10000,
        },
    }


class _SecretProvider:
    def require(self, name: str) -> str:
        assert name == "HOTEL_KB_EMBEDDING_API_KEY"
        return "embedding-secret"


class _Runtime:
    instances: list["_Runtime"] = []

    def __init__(self) -> None:
        self.closed = False
        self.__class__.instances.append(self)

    async def aclose(self, *, timeout=None) -> None:
        assert timeout == 5.0
        self.closed = True


def test_disabled_configuration_preserves_offline_fallback(monkeypatch):
    from src.api import server

    asyncio.run(server._shutdown_dependencies())
    monkeypatch.setattr(
        server,
        "QdrantVectorStore",
        lambda *_args, **_kwargs: pytest.fail("disabled store must not be constructed"),
    )

    for config in ({}, {"vector_store": {"enabled": False}}):
        asyncio.run(
            server._wire_dependencies(
                config=config,
                secret_provider=_SecretProvider(),
            )
        )

    assert get_kb_service().retrieve("hotelA", "check-in") == []


def test_enabled_invalid_configuration_aborts_startup():
    from src.api import server

    asyncio.run(server._shutdown_dependencies())
    with pytest.raises(ValueError, match="missing required keys"):
        asyncio.run(
            server._wire_dependencies(
                config={"vector_store": {"enabled": True, "url": "http://qdrant:6333"}},
                secret_provider=_SecretProvider(),
            )
        )


def test_wiring_is_all_or_nothing_and_closes_runtime_on_failure(monkeypatch):
    from src.api import server

    asyncio.run(server._shutdown_dependencies())
    _Runtime.instances.clear()
    fake_llm = object()
    monkeypatch.setattr(server, "AsyncServiceRuntime", _Runtime)
    monkeypatch.setattr(server, "_build_anthropic_client", lambda _config: fake_llm)
    monkeypatch.setattr(server, "QdrantVectorStore", lambda _config, *, runtime: object())

    def _embedding_failure(_config, *, runtime):
        raise RuntimeError("embedding construction failed")

    monkeypatch.setattr(server, "AzureEmbedding", _embedding_failure)

    with pytest.raises(RuntimeError, match="embedding construction failed"):
        asyncio.run(
            server._wire_dependencies(
                config=_enabled_config(), secret_provider=_SecretProvider()
            )
        )

    assert get_llm() is not fake_llm
    assert get_kb_service().retrieve("hotelA", "check-in") == []
    assert _Runtime.instances[0].closed is True
    assert server._service_runtime is None


def test_successful_wiring_shares_and_closes_one_runtime(monkeypatch):
    from src.api import server

    asyncio.run(server._shutdown_dependencies())
    _Runtime.instances.clear()
    fake_llm = object()
    constructed: dict[str, object] = {}

    class _Store:
        def __init__(self, config, *, runtime):
            constructed["store_config"] = config
            constructed["store_runtime"] = runtime

        def search(self, query, top_k=10):
            return []

        def upsert(self, documents):
            return None

        def delete(self, ids):
            return None

    class _Embedding:
        def __init__(self, config, *, runtime):
            constructed["embedding_config"] = config
            constructed["embedding_runtime"] = runtime

        def embed(self, texts):
            return [[0.0] * 1536 for _ in texts]

    monkeypatch.setattr(server, "AsyncServiceRuntime", _Runtime)
    monkeypatch.setattr(server, "_build_anthropic_client", lambda _config: fake_llm)
    monkeypatch.setattr(server, "QdrantVectorStore", _Store)
    monkeypatch.setattr(server, "AzureEmbedding", _Embedding)

    asyncio.run(
        server._wire_dependencies(
            config=_enabled_config(), secret_provider=_SecretProvider()
        )
    )

    runtime = _Runtime.instances[0]
    assert get_llm() is fake_llm
    assert constructed["store_runtime"] is runtime
    assert constructed["embedding_runtime"] is runtime
    assert constructed["store_config"]["embedding_api_key"] == "embedding-secret"
    assert constructed["embedding_config"]["api_key"] == "embedding-secret"
    assert server._service_runtime is runtime

    asyncio.run(server._shutdown_dependencies())
    assert runtime.closed is True
    assert server._service_runtime is None
