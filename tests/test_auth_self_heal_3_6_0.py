"""v3.6.0 — request-time auth self-heal, so OIDC is at least as reliable as
Kerberos.

Two layers:
  1. OidcAuth.auth_flow mirrors NegotiateAuth's 401 continuation: on a rejected
     Bearer it FORCE-REFRESHES the token (bypassing the cache) and retries once
     over OIDC — recovering a stale / early-expired / clock-skewed token without
     leaving OIDC.
  2. _call_remote cross-falls-back to Kerberos: if a backend on OIDC still 401s
     after that refresh (a genuine audience/config mismatch — the rd 2026-07-15
     outage) OR the token can't be minted at all, the SAME call is retried over
     Negotiate and the backend sticks to Negotiate for the session.

403 (authorization) is never retried; a non-OIDC backend never triggers the
fallback.
"""
from __future__ import annotations

import importlib
import json

import httpx
import pytest


def _shim():
    return importlib.import_module("shim_server")


# --------------------------------------------------------------------- fakes


class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"result": {"ok": True}}
        self.text = json.dumps(self._payload)
        self.content = self.text.encode()

    def json(self):
        return self._payload


class _Client:
    """Context-manager stand-in for backend.http_client()."""
    def __init__(self, resp=None, exc=None):
        self._resp, self._exc = resp, exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, path, json=None):
        if self._exc is not None:
            raise self._exc
        return self._resp


def _factory(by_auth, calls):
    """Return a `lambda **kw: client` whose response depends on force_auth."""
    def make(**kw):
        fa = kw.get("force_auth")
        calls.append(fa)
        spec = by_auth[fa]
        if isinstance(spec, BaseException):
            return _Client(exc=spec)
        return _Client(resp=spec)
    return make


def _quiet(monkeypatch, shim):
    monkeypatch.setattr(shim, "_maybe_reload_backends", lambda: None)
    monkeypatch.setattr(shim, "_maybe_refresh_catalogue", lambda: None)


def _oidc_backend(shim, name="rd"):
    b = shim.Backend(name=name, url="http://mcp.example.com:3010",
                     header="X-Punch-Auth", key="", auth="negotiate")
    b.effective_auth = "oidc"
    b._oidc_upn = "matt.stevens@punchpowertrain.com"
    return b


def _register(monkeypatch, shim, backend, registered, original):
    monkeypatch.setitem(shim._NAME_TO_BACKEND, registered, (backend, original))


# --------------------------------------------------- http_client force_auth


def test_force_auth_negotiate_overrides_oidc(monkeypatch):
    shim = _shim()
    b = _oidc_backend(shim)
    # Without force_auth the OIDC backend builds an OidcAuth...
    with b.http_client() as c:
        assert isinstance(c.auth, shim.OidcAuth)
    # ...but force_auth="negotiate" builds a NegotiateAuth even so.
    with b.http_client(force_auth="negotiate") as c:
        assert isinstance(c.auth, shim.NegotiateAuth)


# --------------------------------------------------- OidcAuth 401 self-heal


def test_oidc_auth_flow_refreshes_and_retries_on_401(monkeypatch):
    shim = _shim()

    def _acq(upn, *, allow_interactive, force_refresh=False):
        return "T2" if force_refresh else "T1"
    monkeypatch.setattr(shim, "_oidc_acquire_token", _acq)

    req = httpx.Request("POST", "http://mcp.example.com:3010/tools/x")
    flow = shim.OidcAuth("u@p").auth_flow(req)
    r1 = next(flow)
    assert r1.headers["Authorization"] == "Bearer T1"
    # Server rejects it -> auth_flow force-refreshes and re-yields with a fresh token.
    r2 = flow.send(_FakeResp(401))
    assert r2.headers["Authorization"] == "Bearer T2"
    with pytest.raises(StopIteration):
        flow.send(_FakeResp(200))


def test_oidc_auth_flow_no_retry_on_success(monkeypatch):
    shim = _shim()
    monkeypatch.setattr(shim, "_oidc_acquire_token",
                        lambda upn, *, allow_interactive, force_refresh=False: "T1")
    req = httpx.Request("POST", "http://mcp.example.com:3010/tools/x")
    flow = shim.OidcAuth("u@p").auth_flow(req)
    next(flow)
    # A 200 ends the flow — no second yield, no refresh.
    with pytest.raises(StopIteration):
        flow.send(_FakeResp(200))


def test_oidc_auth_flow_gives_up_when_refresh_fails(monkeypatch):
    shim = _shim()

    def _acq(upn, *, allow_interactive, force_refresh=False):
        if force_refresh:
            raise shim.OidcError("refresh failed")
        return "T1"
    monkeypatch.setattr(shim, "_oidc_acquire_token", _acq)
    req = httpx.Request("POST", "http://mcp.example.com:3010/tools/x")
    flow = shim.OidcAuth("u@p").auth_flow(req)
    next(flow)
    # 401 + refresh raises -> the original 401 stands, no second yield.
    with pytest.raises(StopIteration):
        flow.send(_FakeResp(401))


def test_oidc_auth_flow_end_to_end_via_httpx(monkeypatch):
    """Integration: drive OidcAuth through a real httpx.Client + MockTransport.
    Proves the generator's second yield is protocol-correct and the transport
    actually re-sends with the fresh token — OIDC self-healing like Kerberos."""
    shim = _shim()

    def _acq(upn, *, allow_interactive, force_refresh=False):
        return "T2" if force_refresh else "T1"
    monkeypatch.setattr(shim, "_oidc_acquire_token", _acq)

    seen = []

    def handler(request):
        seen.append(request.headers.get("Authorization"))
        if len(seen) == 1:
            return httpx.Response(401)          # reject the first (stale) token
        return httpx.Response(200, json={"result": {"ok": True}})

    client = httpx.Client(transport=httpx.MockTransport(handler),
                          auth=shim.OidcAuth("u@p"), base_url="http://x")
    try:
        resp = client.post("/tools/t", json={})
    finally:
        client.close()
    assert resp.status_code == 200
    assert seen == ["Bearer T1", "Bearer T2"]   # forced-refresh retry re-sent


def test_oidc_auth_flow_no_retry_when_same_token(monkeypatch):
    """If the force-refresh returns the identical token, retrying would just
    401 again — skip the pointless second attempt."""
    shim = _shim()
    monkeypatch.setattr(shim, "_oidc_acquire_token",
                        lambda upn, *, allow_interactive, force_refresh=False: "SAME")
    req = httpx.Request("POST", "http://mcp.example.com:3010/tools/x")
    flow = shim.OidcAuth("u@p").auth_flow(req)
    next(flow)
    with pytest.raises(StopIteration):
        flow.send(_FakeResp(401))


# --------------------------------------------------- _call_remote fallback


def test_call_remote_401_over_oidc_falls_back_to_kerberos(monkeypatch):
    shim = _shim()
    _quiet(monkeypatch, shim)
    b = _oidc_backend(shim)
    calls = []
    monkeypatch.setattr(b, "http_client", _factory({
        None:        _FakeResp(401, {"error": "bad audience"}),
        "negotiate": _FakeResp(200, {"result": {"rows": [1, 2]}}),
    }, calls))
    _register(monkeypatch, shim, b, "rd_thing", "thing")

    out = json.loads(shim._call_remote("rd_thing", {}))

    # The Kerberos retry's success is what the caller sees.
    assert out["rows"] == [1, 2]
    assert out["_shim_served_by"] == "rd"
    # First attempt on OIDC (force_auth None), retry forced onto negotiate.
    assert calls == [None, "negotiate"]
    # Sticky: the backend stays on Kerberos for the session.
    assert b.effective_auth == "negotiate"
    assert b._fell_back is True


def test_call_remote_oidc_acquire_failure_falls_back(monkeypatch):
    shim = _shim()
    _quiet(monkeypatch, shim)
    b = _oidc_backend(shim)
    calls = []
    monkeypatch.setattr(b, "http_client", _factory({
        None:        shim.OidcError("no cached token, interactive disallowed"),
        "negotiate": _FakeResp(200, {"result": {"ok": True}}),
    }, calls))
    _register(monkeypatch, shim, b, "rd_thing", "thing")

    out = json.loads(shim._call_remote("rd_thing", {}))
    assert out["ok"] is True
    assert calls == [None, "negotiate"]
    assert b.effective_auth == "negotiate"
    assert b._fell_back is True


def test_call_remote_403_over_oidc_does_not_fall_back(monkeypatch):
    """403 is authorization, not authentication — the same identity would be
    denied over Kerberos too. Must NOT retry and must NOT flip to negotiate."""
    shim = _shim()
    _quiet(monkeypatch, shim)
    b = _oidc_backend(shim)
    calls = []
    monkeypatch.setattr(b, "http_client", _factory({
        None: _FakeResp(403, {"error": True}),
    }, calls))
    _register(monkeypatch, shim, b, "rd_thing", "thing")

    out = json.loads(shim._call_remote("rd_thing", {}))
    assert "_shim_note" in out            # 403 enrichment preserved
    assert out["_shim_served_by"] == "rd"
    assert calls == [None]                # no retry
    assert b.effective_auth == "oidc"     # unchanged
    assert b._fell_back is False


def test_call_remote_401_on_non_oidc_backend_does_not_retry(monkeypatch):
    """A key/x-punch-auth backend that 401s has a real credential problem; a
    Kerberos retry would be wrong. Return the 401 envelope once."""
    shim = _shim()
    _quiet(monkeypatch, shim)
    b = shim.Backend(name="sap", url="http://mcp.example.com:3000",
                     header="X-Punch-Auth",
                     key="K-padded-to-clear-the-placeholder-guard-xxxxxx")
    assert b.effective_auth == "x-punch-auth"
    calls = []
    monkeypatch.setattr(b, "http_client", _factory({
        None: _FakeResp(401, {"error": True, "error_type": "Unauthorized"}),
    }, calls))
    _register(monkeypatch, shim, b, "sap_thing", "thing")

    out = json.loads(shim._call_remote("sap_thing", {}))
    assert out["_shim_served_by"] == "sap"
    assert calls == [None]                       # exactly one attempt
    assert b.effective_auth == "x-punch-auth"    # unchanged


def test_call_remote_oidc_success_no_fallback(monkeypatch):
    """The happy OIDC path: a 200 first try never triggers the fallback and
    leaves the backend on OIDC."""
    shim = _shim()
    _quiet(monkeypatch, shim)
    b = _oidc_backend(shim)
    calls = []
    monkeypatch.setattr(b, "http_client", _factory({
        None: _FakeResp(200, {"result": {"v": 1}}),
    }, calls))
    _register(monkeypatch, shim, b, "rd_thing", "thing")

    out = json.loads(shim._call_remote("rd_thing", {}))
    assert out["v"] == 1
    assert calls == [None]
    assert b.effective_auth == "oidc"
    assert b._fell_back is False
