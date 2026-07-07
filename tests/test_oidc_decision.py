from __future__ import annotations

import importlib
from unittest.mock import patch

import httpx
import pytest


def _shim():
    return importlib.import_module("shim_server")


def _mk_backend(shim, auth="negotiate"):
    return shim.Backend(name="sap", url="http://mcp.example.com:3000",
                        header="X-Punch-Auth", key="", auth=auth)


def test_query_auth_mode_oidc(monkeypatch):
    shim = _shim()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, text="oidc"))
    assert shim._query_auth_mode("http://x:3000", "a@p.com") == "oidc"


def test_query_auth_mode_collapses_non_oidc_to_kerberos(monkeypatch):
    shim = _shim()
    # 200 "kerberos", a 429, a 500, and a network error all -> "kerberos".
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, text="kerberos"))
    assert shim._query_auth_mode("http://x:3000", "a@p.com") == "kerberos"
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(429, text="kerberos"))
    assert shim._query_auth_mode("http://x:3000", "a@p.com") == "kerberos"

    def _boom(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(httpx, "get", _boom)
    assert shim._query_auth_mode("http://x:3000", "a@p.com") == "kerberos"


def test_directive_oidc_and_token_ok_switches_to_oidc(monkeypatch):
    shim = _shim()
    b = _mk_backend(shim)
    monkeypatch.setattr(shim, "_local_windows_upn", lambda: "a@p.com")
    monkeypatch.setattr(shim, "_query_auth_mode", lambda url, upn: "oidc")
    monkeypatch.setattr(shim, "_oidc_acquire_token", lambda upn, *, allow_interactive: "AT")
    out = shim._apply_auth_directives([b])
    assert out[0].effective_auth == "oidc"
    assert out[0]._oidc_upn == "a@p.com"
    assert out[0]._fell_back is False


def test_directive_oidc_but_token_fails_falls_back_to_negotiate(monkeypatch):
    shim = _shim()
    b = _mk_backend(shim)
    monkeypatch.setattr(shim, "_local_windows_upn", lambda: "a@p.com")
    monkeypatch.setattr(shim, "_query_auth_mode", lambda url, upn: "oidc")

    def _fail(upn, *, allow_interactive):
        raise shim.OidcError("browser cancelled")
    monkeypatch.setattr(shim, "_oidc_acquire_token", _fail)
    out = shim._apply_auth_directives([b])
    assert out[0].effective_auth == "negotiate"      # never overrides a working negotiate unconfirmed
    assert out[0]._fell_back is True


def test_directive_unreachable_stays_negotiate(monkeypatch):
    shim = _shim()
    b = _mk_backend(shim)
    monkeypatch.setattr(shim, "_local_windows_upn", lambda: "a@p.com")
    monkeypatch.setattr(shim, "_query_auth_mode", lambda url, upn: "kerberos")
    out = shim._apply_auth_directives([b])
    assert out[0].effective_auth == "negotiate"
    assert out[0]._fell_back is False


def test_no_local_upn_stays_negotiate(monkeypatch):
    shim = _shim()
    b = _mk_backend(shim)
    monkeypatch.setattr(shim, "_local_windows_upn", lambda: None)
    # /auth/mode must not even be queried without a UPN.
    monkeypatch.setattr(shim, "_query_auth_mode",
                        lambda *a: (_ for _ in ()).throw(AssertionError("should not query")))
    out = shim._apply_auth_directives([b])
    assert out[0].effective_auth == "negotiate"


def test_fell_back_backend_sends_fallback_header():
    shim = _shim()
    b = _mk_backend(shim)
    b._fell_back = True
    with b.http_client() as c:
        assert c.headers.get("X-Punch-Auth-Fallback") == "oidc->negotiate"


def test_http_client_always_sends_auto_update_header():
    shim = _shim()
    b = _mk_backend(shim)
    with b.http_client() as c:
        assert c.headers.get("X-Punch-Shim-Auto-Update") in ("0", "1")
