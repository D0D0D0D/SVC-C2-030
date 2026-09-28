"""Unit tests — SVC-C2-030 API guards (trust separation, fail-closed)."""

from __future__ import annotations

import pytest

from framework.schemas.trust_level import TrustLevel


class _FakeReqState:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeRequest:
    def __init__(self, headers=None, **state):
        self.state = _FakeReqState(**state)
        self.headers = headers or {}


class TestIngestTrustGate:
    def test_ingest_requires_internal(self):
        from fastapi import HTTPException

        from src.api.server import _require_internal

        # No trust stamp ⇒ ANONYMOUS default ⇒ rejected (fail-closed).
        with pytest.raises(HTTPException) as exc:
            _require_internal(_FakeRequest())
        assert exc.value.status_code == 403

    def test_ingest_rejects_verified_external(self):
        from fastapi import HTTPException

        from src.api.server import _require_internal

        with pytest.raises(HTTPException):
            _require_internal(_FakeRequest(trust_level=TrustLevel.VERIFIED_EXTERNAL))

    def test_ingest_allows_internal(self):
        from src.api.server import _require_internal

        # INTERNAL passes (no exception).
        _require_internal(_FakeRequest(trust_level=TrustLevel.INTERNAL))


class TestInvokeRequestModel:
    def test_property_id_defaults_empty(self):
        from src.api.server import InvokeRequest

        req = InvokeRequest(input="hi")
        assert req.property_id == ""  # guard rejects empty at the endpoint


class TestInvokeEntryAuth:
    """/invoke entry-adapter auth core (_ctx) — fail-closed, token-gated."""

    def test_unauthenticated_rejected_401(self, monkeypatch):
        from fastapi import HTTPException

        from src.api.server import _ctx

        # No verified middleware trust AND no INVOKE_AUTH_TOKEN promotion ⇒ 401.
        monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
        with pytest.raises(HTTPException) as exc:
            _ctx(_FakeRequest(), "sess-1")
        assert exc.value.status_code == 401

    def test_explicit_anonymous_stamp_rejected_401(self, monkeypatch):
        from fastapi import HTTPException

        from src.api.server import _ctx

        # Middleware that explicitly stamps ANONYMOUS must still 401 (value test).
        monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
        with pytest.raises(HTTPException) as exc:
            _ctx(_FakeRequest(trust_level=TrustLevel.ANONYMOUS), "sess-2")
        assert exc.value.status_code == 401

    def test_bad_token_rejected_401(self, monkeypatch):
        from fastapi import HTTPException

        from src.api.server import _ctx

        monkeypatch.setenv("INVOKE_AUTH_TOKEN", "s3cret")
        with pytest.raises(HTTPException) as exc:
            _ctx(_FakeRequest(headers={"authorization": "Bearer wrong"}), "sess-3")
        assert exc.value.status_code == 401

    def test_valid_token_promotes_verified_external(self, monkeypatch):
        from src.api.server import _ctx

        monkeypatch.setenv("INVOKE_AUTH_TOKEN", "s3cret")
        ctx = _ctx(_FakeRequest(headers={"authorization": "Bearer s3cret"}), "sess-4")
        assert ctx.caller_trust_level == TrustLevel.VERIFIED_EXTERNAL
        assert ctx.session_id == "sess-4"

    def test_verified_middleware_trust_used_as_is(self):
        from src.api.server import _ctx

        ctx = _ctx(_FakeRequest(trust_level=TrustLevel.VERIFIED_EXTERNAL), "sess-5")
        assert ctx.caller_trust_level == TrustLevel.VERIFIED_EXTERNAL
