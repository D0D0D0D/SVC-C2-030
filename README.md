# SVC-C2-030 — Lodging Guest Multilingual Concierge Agent

> **Category**: Cat 2 (domain workflow)
> **Industry**: SVC

## Overview

This agent answers lodging guests' questions in Japanese, English, Chinese and Korean, grounded
strictly on the property's own knowledge base: facility facts, the hot-spring water analysis
certificate (温泉分析書), and any cultural material the operator chooses to supply. When a
question falls outside that knowledge base it hands the guest to the front desk rather than
guessing.

It suits a property that wants round-the-clock first-line answers without putting a language
model's invention in front of a guest: an unattended night desk, a busy check-in window, a
multilingual clientele asking the same twenty questions in four languages.

The division of labour between the language model and the deterministic layer is the point of
the design. The model phrases and localises the answer; it never supplies the facts:

- **No knowledge-base hit, no answer.** If retrieval returns nothing usable, the run escalates to
  the front desk instead of generating text. Fabrication is prevented by refusing to generate,
  not by asking the model to be careful.
- **Complaints, bookings and legal matters are routed out by intent, not by tone.** They are
  never handled in-chat.
- **Health questions go through a disclaimer gate.** An answer touching hot-spring
  contraindications must retain its medical-disclaimer marker; the output gate rejects the
  response if the marker was dropped. It is general information, never a diagnosis.
- **Ungated text is never published.** The framework's default would fall back to the raw inner
  answer when the output gate raised, or when the localisation came back empty — precisely the
  two cases where the text must be withheld. This template overrides that: on a non-success run
  `output` is `null`, and the status carries the failure.
- **Knowledge bases are per property.** `property_id` is required on every question; a request
  without one is rejected rather than risking another property's knowledge base.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime and a secret provider;
- a Qdrant vector store and an Azure embedding deployment, configured under `vector_store` and
  `embedding` in `config/config.yaml`. The embedding credential comes from the secret provider as
  `HOTEL_KB_EMBEDDING_API_KEY` and is deliberately absent from the file;
- an Anthropic API configuration under `config.llm`, from which the adapter builds the client;
- caller authentication in front of the entry adapter, including a way to establish `INTERNAL`
  trust for the operator ingest lane.

Runtime parameters in `config/config.yaml`:

| Key | Meaning |
|---|---|
| `max_retry` (`3`) | Retry budget |
| `timeout_seconds` (`30`) | Per-invocation timeout |
| `kb_top_k` (`5`) | Knowledge-base chunks retrieved per question |
| `history_max_turns` (`8`) | Multi-turn context window, personal data removed |
| `vector_store.*` | Qdrant connection, collection, vector size and limits. `enabled: false` keeps the documented offline degradation |
| `embedding.*` | Azure embedding endpoint, model, dimensions and retry policy |

`vector_store.vector_size` and `embedding.dimensions` must match; start-up refuses a
configuration where they do not. Once `vector_store.enabled` is true, every field in both
sections is required — a malformed section, a missing credential or an unreachable store aborts
start-up rather than quietly serving an empty knowledge base.

Knowledge-base document types are `facility_facts` (required), `onsen_analysis` and `culture`
(optional). Each document is `{source_id, lang, text}`.

The agent does not make or change reservations, give medical or legal advice, or answer anything
outside the supplied knowledge base.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent is designed to run on the platform, which supplies the entry adapter, the authentication middleware and the secret provider. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. The `anthropic` and `platform-rag` extras are both required. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run on AGENTIC STAR. The standalone server in `src/api/server.py`
does start without it, and with `vector_store.enabled: false` — the shipped default — it runs the
documented offline path: retrieval returns nothing, so every question escalates to the front
desk. Reads degrade that way on purpose; writes do not. An `/ingest` call against an unwired
knowledge base returns **503** rather than a false 200 that would silently drop the operator's
documents. Enabling the store without valid configuration or credentials aborts start-up instead.
The test suite runs without a platform connection.

## Input

There are two lanes.

**`POST /invoke` — the guest lane.** Body: `input` (the question), `session_id`, and
`property_id`, which is **required**. Callers must be authenticated: the entry adapter starts
every request at `ANONYMOUS` unless upstream middleware stamps a verified trust level, and when
`INVOKE_AUTH_TOKEN` is set on the server environment a matching `Authorization: Bearer` header
promotes the caller to `VERIFIED_EXTERNAL`. A caller that is neither is rejected with **401**.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "input": "체크인 몇 시예요?",
    "session_id": "guest-1",
    "property_id": "hotelA"
  }'
```

**`POST /ingest` — the operator lane.** Body: `property_id`, `doc_type`, `docs` (each
`{source_id, lang, text}`) and `deletes`. This lane requires **`INTERNAL`** trust and answers
**403** to anything less; it is not reachable with the guest entry token.

```bash
curl -X POST http://127.0.0.1:8000/ingest \
  -H 'Content-Type: application/json' \
  -d '{
    "property_id": "hotelA",
    "doc_type": "facility_facts",
    "docs": [{"source_id": "checkin", "lang": "en", "text": "Check-in 15:00, check-out 10:00."}]
  }'
```

`/ingest` returns `{"upserted": n, "deleted": n}`.

## Output

The standard AgentCore envelope is preserved. `output` carries the guest-facing answer in the
guest's language — and **only** when the run succeeded and the answer passed the output gate; on
any other outcome it is `null` and `status` carries the failure.

```json
{
  "status": "success",
  "output": "체크인은 15:00부터입니다. 짐은 프런트에서 미리 보관해 드립니다.",
  "session_id": "guest-1"
}
```

An escalated question returns the front-desk hand-off text rather than an invented answer.

Values above are illustrative; real output depends on the knowledge base supplied.

## Security and Limitations

The guest lane requires verified-external trust and the operator ingest lane requires `INTERNAL`,
so a guest token cannot write to the knowledge base. Knowledge-base isolation is keyed by the
explicit `property_id` on each request; there is deliberately no request tenant/organisation
scope, because nothing in the workflow would consume one and a "default" fallback would match
every property. Conversation history carried between turns has personal data removed before it is
retained. The output gate rejects a guest-facing answer that contains personal data, and rejects
a health-adjacent answer that lost its disclaimer.

Known limits:

- **The personal-data detector does not recognise a bare CJK name.** `山田太郎` on its own is not
  detected; a name following an English salutation is. The output gate is therefore fail-closed
  on what it can see, not exhaustive.
- **Localisation failure can return an unlocalised answer.** The exception and
  disclaimer-preservation fallbacks return the grounded answer as a success, and that answer does
  not pin its own language — so a non-Japanese question can come back in English. This is a
  recorded open item, not a fixed behaviour.
- **Prompt wording cannot be regression-tested in CI.** Asserting a prompt string is a constant
  assertion; deleting an instruction leaves the suite green. The CI-side defence is that the
  output gate fails closed and that ungated text is never published.
- **Answer quality is the knowledge base's quality.** The agent adds nothing the operator did not
  supply.

Output is machine-generated and assistive. Hot-spring health responses are general information,
not medical advice or a diagnosis; guests are directed to a doctor or the front desk for health
decisions.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), retrieval-grounded
- Generation mode: `llm`
- HITL: not enabled
- Persistence: a Qdrant collection per deployment; nothing bundled
- External API clients: `AnthropicClient` and `AzureEmbedding` from the framework's shared
  services, constructed by the standalone adapter from configuration

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, without a vector store and without a language model.
Running the agent against a bare `uvicorn src.api.server:app` starts in the offline path, where
every question escalates to the front desk.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the escalation and disclaimer
contracts and the security design. `docs/03_test_spec.md` records the test specification and the
verification results, including a live-model run and a Marketplace connectivity check.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
