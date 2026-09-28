"""AGENTIC STAR Marketplace container entry adapter for SVC-C2-030."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from framework.secrets.base import SecretProvider
from framework.utils.config_loader import load_agent_config
from shared.bootstrap import run_agent_marketplace
from shared.secrets.env_provider import EnvProvider
from shared.services.embedding.azure_embedding import AzureEmbedding
from shared.services.runtime import AsyncServiceRuntime
from shared.services.vector_store.qdrant_vector_store import QdrantVectorStore

from src.graph.graph import LodgingConciergeGraph
from src.services.azure_llm import AzureLLM
from src.services.hotel_kb_qdrant_adapter import HotelKBQdrantAdapter
from src.services.kb_service import KBService
from src.services.llm_client import set_default_llm
from src.services.service import set_default_kb_service

_NAMESPACE = "svc"
_AGENT_NAME = "SVC-C2-030"
_PROVIDER_AGENT_NAME = "svc_c2_030"

_PROPERTY_ID_ENV = "MARKETPLACE_PROPERTY_ID"
_EMBEDDING_API_KEY_SECRET = "HOTEL_KB_EMBEDDING_API_KEY"
_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_REQUIRED_SECRET_KEYS = (
    *_AZURE_OPENAI_SECRET_KEYS,
    _EMBEDDING_API_KEY_SECRET,
    _PROPERTY_ID_ENV,
)
_ALLOWED_SECRET_KEYS = frozenset(_REQUIRED_SECRET_KEYS)

_QDRANT_CONFIG_KEYS = frozenset(
    {
        "url",
        "collection_name",
        "namespace",
        "agent_name",
        "vector_size",
        "distance",
        "timeout_s",
        "write_timeout_s",
        "max_batch_documents",
        "max_top_k",
    }
)
_EMBEDDING_CONFIG_KEYS = frozenset(
    {
        "base_url",
        "model",
        "api_version",
        "dimensions",
        "provider",
        "timeout_s",
        "batch_size",
        "max_retries",
        "base_delay",
        "max_delay",
        "max_cache_size",
    }
)


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Read only this agent's declared values from the process environment."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_SECRET_KEYS:
            return default
        value = super().get(key)
        return value if value else default


class _MarketplaceReadOnlyKBService(KBService):
    """Explicit empty KB paired with AzureLLM when Qdrant is disabled."""

    def __init__(self) -> None:
        # KBService's concrete dependencies intentionally do not exist in this
        # disabled posture; every public operation is overridden below.
        pass

    def retrieve(self, property_id: str, query: str) -> list[dict[str, Any]]:
        del property_id, query
        return []

    def upsert(
        self, property_id: str, doc_type: str, docs: list[dict[str, Any]]
    ) -> int:
        del property_id, doc_type, docs
        raise RuntimeError("Marketplace KB writes are not available")

    def delete(self, property_id: str, doc_type: str, source_ids: list[str]) -> int:
        del property_id, doc_type, source_ids
        raise RuntimeError("Marketplace KB writes are not available")


def _clean_required_environment_value(provider: SecretProvider, key: str) -> str:
    """Resolve an adapter-owned identifier without silently repairing it."""

    value = provider.require(key)
    if value != value.strip() or not value.strip() or "\n" in value or "\r" in value:
        raise ValueError(
            f"Marketplace environment variable {key!r} must be a clean non-empty string"
        )
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _clean_required_environment_value(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


def _marketplace_input_context(provider: SecretProvider) -> dict[str, str]:
    """Build the complete trusted context accepted by the Marketplace boundary."""

    return {"property_id": _clean_required_environment_value(provider, _PROPERTY_ID_ENV)}


def _validate_config_section(
    raw: Mapping[str, Any],
    *,
    section: str,
    allowed: frozenset[str],
    required: frozenset[str],
) -> dict[str, Any]:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"config.{section} has unknown keys: {unknown}")
    missing = sorted(key for key in required if raw.get(key) in (None, ""))
    if missing:
        raise ValueError(f"config.{section} is missing required keys: {missing}")
    return {key: raw[key] for key in required}


def _build_marketplace_dependencies(
    provider: SecretProvider, config: Mapping[str, Any]
) -> tuple[AzureLLM, KBService, AsyncServiceRuntime | None]:
    """Construct the LLM/KB pair completely before either seam is published."""

    llm = AzureLLM()
    raw_store = config.get("vector_store")
    if raw_store is None:
        return llm, _MarketplaceReadOnlyKBService(), None
    if not isinstance(raw_store, Mapping):
        raise ValueError("config.vector_store must be a mapping")
    enabled = raw_store.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("config.vector_store.enabled must be a boolean")
    if not enabled:
        return llm, _MarketplaceReadOnlyKBService(), None

    store_config = _validate_config_section(
        raw_store,
        section="vector_store",
        allowed=_QDRANT_CONFIG_KEYS | {"enabled"},
        required=_QDRANT_CONFIG_KEYS,
    )
    raw_embedding = config.get("embedding")
    if not isinstance(raw_embedding, Mapping):
        raise ValueError("config.embedding must be a mapping")
    embedding_config = _validate_config_section(
        raw_embedding,
        section="embedding",
        allowed=_EMBEDDING_CONFIG_KEYS,
        required=_EMBEDDING_CONFIG_KEYS,
    )
    if int(store_config["vector_size"]) != int(embedding_config["dimensions"]):
        raise ValueError(
            "config.vector_store.vector_size must match config.embedding.dimensions"
        )

    embedding_api_key = provider.require(_EMBEDDING_API_KEY_SECRET)
    embedding_config["api_key"] = embedding_api_key
    store_config.update(
        {
            "embedding_base_url": embedding_config["base_url"],
            "embedding_api_key": embedding_api_key,
            "embedding_model": embedding_config["model"],
            "embedding_api_version": embedding_config["api_version"],
            "embedding_provider": embedding_config["provider"],
        }
    )

    runtime = AsyncServiceRuntime()
    try:
        qdrant = QdrantVectorStore(store_config, runtime=runtime)
        embedding = AzureEmbedding(embedding_config, runtime=runtime)
        kb = KBService(
            HotelKBQdrantAdapter(qdrant),
            embedding,
            top_k=int(config.get("kb_top_k", 5)),
        )
    except BaseException:
        # provision_secrets() runs inside the Marketplace runner's event-loop
        # thread. Close on a separate temporary loop instead of nesting
        # asyncio.run() in that running loop.
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(asyncio.run, runtime.aclose(timeout=5.0)).result(
                timeout=6.0
            )
        raise
    return llm, kb, runtime


class MarketplaceGraph(LodgingConciergeGraph):
    """Marketplace-only boundary hardening and atomic dependency wiring."""

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: Any | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Marketplace conversation history is untrusted free text and is not
        # covered by this agent's S-2 input gate. Rebuild adapter-owned context
        # from scratch so present and future runner keys cannot enter State.
        del input_context
        environment_provider = _AllowlistedEnvironmentProvider()
        adapted_context = _marketplace_input_context(environment_provider)
        return cast(
            dict[str, Any],
            super().invoke(
                user_input,
                session_id=session_id,
                ctx=ctx,
                input_context=adapted_context,
            ),
        )

    def provision_secrets(self, provider: SecretProvider) -> None:
        # Ignore the runner's dotenv-capable provider. A single-agent Pod may
        # read only this adapter's explicitly declared process environment.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _REQUIRED_SECRET_KEYS:
            environment_provider.require(key)
        _marketplace_input_context(environment_provider)

        # Build the complete pair first. Publishing only after every constructor
        # succeeds preserves the HTTP adapter's {LLM, KB} all-or-nothing invariant.
        llm, kb, runtime = _build_marketplace_dependencies(
            environment_provider, self.config
        )
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)
        set_default_llm(llm)
        set_default_kb_service(kb)
        self._marketplace_service_runtime = runtime


def main() -> None:
    config = load_agent_config(Path(__file__).resolve().parent)
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=config,
    )


if __name__ == "__main__":
    main()
