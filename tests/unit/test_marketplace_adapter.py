"""Marketplace entry adapter and Azure LLM boundary tests."""

from __future__ import annotations

from typing import Any

import pytest
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import MissingSecret, NullProvider
from framework.secrets.context import bound_secrets

import cli
from src.services.azure_llm import AzureLLM
from src.services.kb_service import KBService
from src.services.llm_client import get_llm, set_default_llm
from src.services.service import get_kb_service, set_default_kb_service
from tests.conftest import FakeEmbedding, FakeLLM, FakeVectorStore, kb_hit


def _set_required_environment(monkeypatch, suffix: str = "one") -> dict[str, str]:
    values = {key: f"test-{suffix}-{key.lower()}" for key in cli._REQUIRED_SECRET_KEYS}
    # The startup gate requires a bare Azure resource endpoint, so
    # this value cannot be a placeholder like the other secrets.
    values["AZURE_OPENAI_ENDPOINT"] = f"https://{suffix}.services.ai.azure.example"
    values[cli._PROPERTY_ID_ENV] = f"hotel-{suffix}"
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def _contains(value: Any, needle: str) -> bool:
    if isinstance(value, dict):
        return any(
            _contains(key, needle) or _contains(item, needle)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains(item, needle) for item in value)
    return needle in value if isinstance(value, str) else False


@pytest.fixture(autouse=True)
def _clear_dependency_seams():
    set_default_llm(None)
    set_default_kb_service(None)
    yield
    set_default_llm(None)
    set_default_kb_service(None)


def test_marketplace_context_is_rebuilt_and_runner_history_never_enters_state_or_output(
    monkeypatch,
):
    values = _set_required_environment(monkeypatch)
    sentinel = "UNMASKED-CONVERSATION-PII-030"
    set_default_llm(FakeLLM())
    set_default_kb_service(
        KBService(FakeVectorStore([kb_hit(property_id="hotel-one")]), FakeEmbedding())
    )
    agent = cli.MarketplaceGraph()
    agent.compile()
    original_invoke = agent._compiled.invoke
    captured = {}

    def capture_state(initial_state, config=None):
        captured["initial"] = initial_state
        final_state = original_invoke(initial_state, config=config)
        captured["final"] = final_state
        return final_state

    agent._compiled.invoke = capture_state
    result = agent.invoke(
        "What time is check-in?",
        ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        input_context={
            "conversation_history": [{"role": "user", "content": sentinel}],
            "property_id": "runner-property-must-not-win",
            "future_runner_key": sentinel,
        },
    )

    assert captured["initial"]["input_context"] == {
        "property_id": values[cli._PROPERTY_ID_ENV]
    }
    assert not _contains(captured["initial"], sentinel)
    assert not _contains(captured["final"], sentinel)
    assert not _contains(result, sentinel)
    assert result["status"] == "success"


def test_property_id_is_resolved_fresh_from_environment(monkeypatch):
    _set_required_environment(monkeypatch)
    provider = cli._AllowlistedEnvironmentProvider()

    assert cli._marketplace_input_context(provider) == {"property_id": "hotel-one"}
    monkeypatch.setenv(cli._PROPERTY_ID_ENV, "hotel-two")
    assert cli._marketplace_input_context(provider) == {"property_id": "hotel-two"}


def test_secret_provider_resolves_only_allowlisted_process_environment(monkeypatch):
    values = _set_required_environment(monkeypatch)
    monkeypatch.setenv("UNRELATED_PLATFORM_SECRET", "must-not-be-visible")
    agent = cli.MarketplaceGraph(config={"vector_store": {"enabled": False}})

    agent.provision_secrets(NullProvider())

    provider = agent._secrets_provider
    assert {key: provider.require(key) for key in cli._REQUIRED_SECRET_KEYS} == values
    assert provider.get("UNRELATED_PLATFORM_SECRET") is None
    assert "must-not-be-visible" not in repr(vars(provider))
    assert isinstance(get_llm(), AzureLLM)
    assert isinstance(get_kb_service(), cli._MarketplaceReadOnlyKBService)


@pytest.mark.parametrize("missing_key", cli._REQUIRED_SECRET_KEYS)
def test_missing_required_value_fails_during_startup_before_invoke(
    monkeypatch, missing_key
):
    _set_required_environment(monkeypatch)
    monkeypatch.delenv(missing_key)
    agent = cli.MarketplaceGraph(config={"vector_store": {"enabled": False}})
    invoked = False

    def invoke(*args, **kwargs):
        nonlocal invoked
        invoked = True

    agent.invoke = invoke
    with pytest.raises(MissingSecret, match=missing_key):
        agent.provision_secrets(NullProvider())

    assert not invoked


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    _set_required_environment(monkeypatch)
    agent = cli.MarketplaceGraph(config={"vector_store": {"enabled": False}})
    invoked = False
    def invoke(*args, **kwargs):
        nonlocal invoked
        invoked = True
    agent.invoke = invoke
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "gate-test-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gate-test-deployment")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        agent.provision_secrets(NullProvider())
    assert not invoked


@pytest.mark.parametrize("invalid", ["", " ", " leading", "trailing ", "line\nbreak"])
def test_invalid_marketplace_property_id_fails_during_startup(monkeypatch, invalid):
    _set_required_environment(monkeypatch)
    monkeypatch.setenv(cli._PROPERTY_ID_ENV, invalid)
    agent = cli.MarketplaceGraph(config={"vector_store": {"enabled": False}})

    expected = MissingSecret if invalid == "" else ValueError
    with pytest.raises(expected, match=cli._PROPERTY_ID_ENV):
        agent.provision_secrets(NullProvider())


def test_dependency_construction_failure_does_not_publish_only_the_llm(monkeypatch):
    _set_required_environment(monkeypatch)
    old_llm = FakeLLM()
    old_kb = KBService(FakeVectorStore(), FakeEmbedding())
    set_default_llm(old_llm)
    set_default_kb_service(old_kb)
    closed = []

    class Runtime:
        async def aclose(self, *, timeout=None):
            closed.append(timeout)

    monkeypatch.setattr(cli, "AsyncServiceRuntime", Runtime)
    monkeypatch.setattr(cli, "QdrantVectorStore", lambda _config, *, runtime: object())

    def fail_embedding(_config, *, runtime):
        raise RuntimeError("embedding construction failed")

    monkeypatch.setattr(cli, "AzureEmbedding", fail_embedding)
    config = {
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

    with pytest.raises(RuntimeError, match="embedding construction failed"):
        cli.MarketplaceGraph(config=config).provision_secrets(NullProvider())

    assert get_llm() is old_llm
    assert get_kb_service() is old_kb
    assert closed == [5.0]


def test_marketplace_read_only_posture_rejects_kb_writes():
    kb = cli._MarketplaceReadOnlyKBService()

    with pytest.raises(RuntimeError, match="writes are not available"):
        kb.upsert("hotel-one", "facility_facts", [])
    with pytest.raises(RuntimeError, match="writes are not available"):
        kb.delete("hotel-one", "facility_facts", [])


def test_azure_llm_resolves_fresh_values_without_retaining_them(monkeypatch):
    import shared.services.llm.azure_openai_client as azure_module

    configs = []

    class FakeAzureOpenAIClient:
        def __init__(self, config):
            configs.append(dict(config))

        def complete(self, messages):
            return {"content": "ok", "messages": messages}

        def stream(self, messages):
            yield "ok"

        def bind_tools(self, tools):
            return self

    monkeypatch.setattr(azure_module, "AzureOpenAIClient", FakeAzureOpenAIClient)
    provider = cli._AllowlistedEnvironmentProvider()
    llm = AzureLLM()
    assert vars(llm) == {}

    with bound_secrets(provider):
        first = _set_required_environment(monkeypatch, "first")
        llm.complete([{"role": "user", "content": "first"}])
        assert vars(llm) == {}

        second = _set_required_environment(monkeypatch, "second")
        llm.complete([{"role": "user", "content": "second"}])
        assert vars(llm) == {}

    assert configs == [
        {
            "api_key": first["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": first["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": first["AZURE_OPENAI_DEPLOYMENT"],
        },
        {
            "api_key": second["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": second["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": second["AZURE_OPENAI_DEPLOYMENT"],
        },
    ]


def test_azure_llm_bind_tools_returns_a_new_instance():
    llm = AzureLLM()
    bound = llm.bind_tools([{"name": "lookup"}])

    assert bound is not llm
    assert vars(llm) == {}
    assert vars(bound) == {"_tools": ({"name": "lookup"},)}


def test_main_loads_repo_config_for_marketplace_runner(monkeypatch):
    called = {}

    def fake_runner(agent_cls, **kwargs):
        called["agent_cls"] = agent_cls
        called.update(kwargs)

    monkeypatch.setattr(cli, "run_agent_marketplace", fake_runner)
    cli.main()

    assert called["agent_cls"] is cli.MarketplaceGraph
    assert called["agent_name"] == "SVC-C2-030"
    assert called["namespace"] == "svc"
    assert called["config"]["max_retry"] == 3
    assert called["config"]["vector_store"]["collection_name"] == "svc_c2_030_hotel_kb"
