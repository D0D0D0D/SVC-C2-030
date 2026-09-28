"""AgentCore Platform v1.0 — SVC-C2-030 HTTP entry point.

Adapters only — no business logic here. Two guest/operator surfaces with
separated trust levels (docs/02_design.md §KB 投入・更新の口):

  - POST /invoke  — guest question answering       (VERIFIED_EXTERNAL)
  - POST /ingest  — KB bulk load / upsert / delete (INTERNAL, operators only)

Entry authentication is implemented by entry_adapter.auth:
  - /invoke: a verified middleware trust (request.state.trust_level) is used
    as-is; otherwise INVOKE_AUTH_TOKEN Bearer auth promotes ANONYMOUS →
    VERIFIED_EXTERNAL (_resolve_trust raises 401 on token mismatch); an
    unauthenticated caller (no verified trust AND no token promotion) is
    rejected with 401. The old fallback that defaulted such callers to the
    middleware ANONYMOUS stamp was left to the S-1 gate downstream; the entry
    now fails closed at the adapter via the shared auth core.
  - /ingest: operator-only, requires INTERNAL trust — a separate, stricter lane
    (_require_internal). Left as-is: INVOKE_AUTH_TOKEN only promotes to
    VERIFIED_EXTERNAL, never INTERNAL, so the ingest lane is not weakened.

The LLM / KB service are provided via injection seams
(src/services/llm_client.set_default_llm, src/services/service.set_default_kb_service)
wired at startup; tests inject fakes instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import yaml  # type: ignore[import-untyped]
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from shared.services.embedding.azure_embedding import AzureEmbedding
from shared.services.runtime import AsyncServiceRuntime
from shared.services.vector_store.qdrant_vector_store import QdrantVectorStore

from src.entry_adapter.auth import _require_verified_trust, _resolve_trust
from src.graph.graph import LodgingConciergeGraph
from src.services.hotel_kb_qdrant_adapter import HotelKBQdrantAdapter
from src.services.kb_service import KBService
from src.services.llm_client import set_default_llm
from src.services.service import get_kb_service
from src.services.service import set_default_kb_service

_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
_VECTOR_STORE_CONFIG_KEY = "vector_store"
_EMBEDDING_CONFIG_KEY = "embedding"
_EMBEDDING_API_KEY_SECRET = "HOTEL_KB_EMBEDDING_API_KEY"
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
_service_runtime: AsyncServiceRuntime | None = None


def _load_runtime_config(path: Path = _RUNTIME_CONFIG_PATH) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError("config/config.yaml must contain a mapping")
    return loaded


agent = LodgingConciergeGraph(config=_load_runtime_config())
agent.compile()
_secret_provider = secrets_factory(namespace="svc", agent_name="svc_c2_030")
agent.provision_secrets(_secret_provider)


def _build_anthropic_client(config: Mapping[str, Any]) -> Any:
    # Keep the optional dependency lazy so disabled/offline deployments can be
    # imported without the anthropic extra. The deployment wheel includes it.
    from shared.services.llm.anthropic_client import AnthropicClient

    return AnthropicClient(dict(config))


async def _wire_dependencies(*, config: Mapping[str, Any], secret_provider: Any) -> None:
    """Atomically install the real LLM and KB seams.

    Missing or explicitly disabled Qdrant configuration retains the documented
    offline fallback. Once enabled, malformed settings, missing credentials, or
    connection failures abort startup; an enabled-but-broken KB is not treated
    as an intentionally unprovisioned deployment.
    """
    global _service_runtime

    await _shutdown_dependencies()
    raw_store = config.get(_VECTOR_STORE_CONFIG_KEY)
    if raw_store is None:
        return
    if not isinstance(raw_store, Mapping):
        raise ValueError(f"config.{_VECTOR_STORE_CONFIG_KEY} must be a mapping")
    enabled = raw_store.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"config.{_VECTOR_STORE_CONFIG_KEY}.enabled must be a boolean")
    if not enabled:
        return

    store_config = _validate_config_section(
        raw_store,
        section=_VECTOR_STORE_CONFIG_KEY,
        allowed=_QDRANT_CONFIG_KEYS | {"enabled"},
        required=_QDRANT_CONFIG_KEYS,
    )
    raw_embedding = config.get(_EMBEDDING_CONFIG_KEY)
    if not isinstance(raw_embedding, Mapping):
        raise ValueError(f"config.{_EMBEDDING_CONFIG_KEY} must be a mapping")
    embedding_config = _validate_config_section(
        raw_embedding,
        section=_EMBEDDING_CONFIG_KEY,
        allowed=_EMBEDDING_CONFIG_KEYS,
        required=_EMBEDDING_CONFIG_KEYS,
    )
    if int(store_config["vector_size"]) != int(embedding_config["dimensions"]):
        raise ValueError("config.vector_store.vector_size must match config.embedding.dimensions")

    raw_llm = config.get("llm", {})
    if not isinstance(raw_llm, Mapping):
        raise ValueError("config.llm must be a mapping")

    with bound_secrets(secret_provider):
        embedding_api_key = secret_provider.require(_EMBEDDING_API_KEY_SECRET)
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
        # Construct every dependency before publishing either process-wide seam.
        # A failure at any point therefore leaves {LLM, KB} as {none, none}.
        llm = _build_anthropic_client(raw_llm)
        qdrant = QdrantVectorStore(store_config, runtime=runtime)
        embedding = AzureEmbedding(embedding_config, runtime=runtime)
        kb = KBService(
            HotelKBQdrantAdapter(qdrant),
            embedding,
            top_k=int(config.get("kb_top_k", 5)),
        )
    except BaseException:
        await runtime.aclose(timeout=5.0)
        raise

    set_default_llm(llm)
    set_default_kb_service(kb)
    _service_runtime = runtime


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


async def _shutdown_dependencies() -> None:
    global _service_runtime

    runtime, _service_runtime = _service_runtime, None
    set_default_llm(None)
    set_default_kb_service(None)
    if runtime is not None:
        await runtime.aclose(timeout=5.0)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    # Modern FastAPI startup (replaces deprecated on_event("startup")).
    runtime_config = cast(
        Mapping[str, Any], getattr(application.state, "service_config", agent.config)
    )
    secret_provider = getattr(application.state, "secret_provider", _secret_provider)
    await _wire_dependencies(config=runtime_config, secret_provider=secret_provider)
    try:
        yield
    finally:
        await _shutdown_dependencies()


app = FastAPI(
    title="SVC-C2-030 Lodging Guest Multilingual Concierge Agent",
    lifespan=lifespan,
)


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    property_id: str = ""


class IngestDoc(BaseModel):
    source_id: str
    lang: str = ""
    text: str


class IngestRequest(BaseModel):
    property_id: str
    doc_type: str
    docs: list[IngestDoc] = []
    deletes: list[str] = []


def _require_internal(request: Request) -> None:
    """/ingest is operator-only: require INTERNAL trust level (fail-closed)."""
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    if trust != TrustLevel.INTERNAL:
        raise HTTPException(status_code=403, detail="ingest requires INTERNAL trust level")


def _ctx(request: Request, session_id: str) -> InvocationContext:
    """Build the InvocationContext AFTER entry authentication (guest /invoke lane).

    Scope note: the entry_adapter part also exposes a tenant/org scope contract,
    but this template has NO request-scope model. Per-property KB isolation is
    keyed by the explicit `property_id` in the request body (validated below),
    NOT by a request tenant/org scope key — there is no code path that reads
    request tenant/org scope. Enforcing authenticate_scope() would gate on a
    scope nothing consumes (STG-default theater; a "default" fallback would
    silently match everything), so only the auth core (_resolve_trust /
    _require_verified_trust) is wired here.
    """
    trust, token_authenticated = _resolve_trust(request)
    # Unauthenticated = trust is still ANONYMOUS after resolution and no token
    # promotion. Test the trust VALUE, not attribute presence — a middleware
    # that explicitly stamps ANONYMOUS must still be treated as unauthenticated
    # (→ 401), not silently accepted as "verified because the attr exists".
    if trust is TrustLevel.ANONYMOUS and not token_authenticated:
        raise HTTPException(status_code=401, detail="Authentication is required.")
    _require_verified_trust(trust)
    return InvocationContext(
        session_id=session_id or str(uuid4()),
        caller_trust_level=trust,
        caller_id=getattr(request.state, "caller_id", ""),
    )


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    # property_id is required for per-property KB namespacing. Without it the
    # cross-property leak guard cannot apply, so reject rather than risk serving
    # another hotel's KB (fail-closed tenant isolation).
    if not req.property_id:
        raise HTTPException(status_code=400, detail="property_id is required")
    ctx = _ctx(request, req.session_id)
    with bound_secrets(agent._secrets_provider):
        return agent.invoke(
            req.input, ctx=ctx, input_context={"property_id": req.property_id}
        )


@app.post("/ingest")
async def ingest(req: IngestRequest, request: Request):
    """Operator KB ingest/upsert + delete. INTERNAL only.

    Reads degrade gracefully (empty KB → escalation) but WRITES must not: if the
    KB is unwired the degraded fallback raises, and we surface that as 503 so the
    operator sees the ingest failed rather than a false 200 that dropped the docs.
    """
    _require_internal(request)
    with bound_secrets(agent._secrets_provider):
        kb = get_kb_service()
        try:
            upserted = kb.upsert(
                req.property_id, req.doc_type, [d.model_dump() for d in req.docs]
            )
            deleted = kb.delete(req.property_id, req.doc_type, req.deletes)
        except RuntimeError as exc:
            # KB not provisioned — ingest cannot persist. Fail loud (503), never
            # a silent 200 that loses operator-submitted documents.
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"upserted": upserted, "deleted": deleted}


@app.get("/health")
def health():
    return {"status": "ok", "agent": "svc_c2_030"}
