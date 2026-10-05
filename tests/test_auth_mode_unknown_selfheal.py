"""v3.6.2 - an unanswered /auth/mode is UNKNOWN, not a Kerberos directive.

WHY: on 2026-09-14 the Zabbix adapter's event loop was blocked by a 90s extract.
The shim restarted mid-extract, its 5s /auth/mode probe timed out, and
_query_auth_mode returned "kerberos" for "no answer". The backend was pinned to
Kerberos for the whole session with fell_back=False, so shim_info looked like a
deliberate decision. Kerberos to that adapter cannot complete, so every call
401'd until someone ran shim_reload by hand. It happened three times that day.

The fix keeps confirm-before-switch:
  - no answer   -> "unknown": stay on negotiate, but record it;
  - a Kerberos 401 on an "unknown" backend re-asks /auth/mode, and only a
    CONFIRMED "oidc" (plus a silent token) moves it to OIDC, retrying once;
  - a real answer ("kerberos", or any HTTP status) is still a decision.
"""
from __future__ import annotations

import asyncio
import importlib
import json
from unittest.mock import MagicMock, patch

import httpx


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


def _negotiate_backend(shim, *, unknown):
    b = shim.Backend(name="zabbix", url="http://mcp.example.com:3002",
                     header="X-Punch-Auth", key="", auth="negotiate")
    b._directive_unknown = unknown
    return b


def _register(monkeypatch, shim, backend, registered, original):
    monkeypatch.setitem(shim._NAME_TO_BACKEND, registered, (backend, original))


def _must_not_query(*a, **k):
    raise AssertionError("/auth/mode must not be re-asked here")


# --------------------------------------------------------- _query_auth_mode


def test_query_auth_mode_no_answer_is_unknown(monkeypatch):
    shim = _shim()

    def _refused(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(httpx, "get", _refused)
    assert shim._query_auth_mode("http://x:3002", "a@p.com") == "unknown"

    def _slow(*a, **k):
        raise httpx.ReadTimeout("busy server")
    monkeypatch.setattr(httpx, "get", _slow)
    assert shim._query_auth_mode("http://x:3002", "a@p.com") == "unknown"


def test_query_auth_mode_http_answers_are_still_decisions(monkeypatch):
    shim = _shim()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, text="kerberos"))
    assert shim._query_auth_mode("http://x:3002", "a@p.com") == "kerberos"
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(500, text="oops"))
    assert shim._query_auth_mode("http://x:3002", "a@p.com") == "kerberos"
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, text="oidc"))
    assert shim._query_auth_mode("http://x:3002", "a@p.com") == "oidc"


# --------------------------------------------------- startup directive pass


def test_apply_directives_records_unknown(monkeypatch):
    shim = _shim()
    b = _negotiate_backend(shim, unknown=False)
    monkeypatch.setattr(shim, "_local_windows_upn", lambda: "a@p.com")
    monkeypatch.setattr(shim, "_query_auth_mode", lambda url, upn: "unknown")
    out = shim._apply_auth_directives([b])
    assert out[0].effective_auth == "negotiate"   # confirm-before-switch kept
    assert out[0]._directive_unknown is True
    assert out[0]._fell_back is False


def test_apply_directives_explicit_kerberos_is_not_unknown(monkeypatch):
    shim = _shim()
    b = _negotiate_backend(shim, unknown=True)     # stale flag from a prior pass
    monkeypatch.setattr(shim, "_local_windows_upn", lambda: "a@p.com")
    monkeypatch.setattr(shim, "_query_auth_mode", lambda url, upn: "kerberos")
    out = shim._apply_auth_directives([b])
    assert out[0].effective_auth == "negotiate"
    assert out[0]._directive_unknown is False     # a real answer clears it


def test_reprobe_that_gets_oidc_clears_unknown(monkeypatch):
    shim = _shim()
    b = _negotiate_backend(shim, unknown=False)
    monkeypatch.setattr(shim, "_local_windows_upn", lambda: "a@p.com")
    monkeypatch.setattr(shim, "_query_auth_mode", lambda url, upn: "unknown")
    shim._apply_auth_directives([b])
    assert b._directive_unknown is True
    monkeypatch.setattr(shim, "_query_auth_mode", lambda url, upn: "oidc")
    monkeypatch.setattr(shim, "_oidc_acquire_token", lambda upn, *, allow_interactive: "AT")
    shim._apply_auth_directives([b])
    assert b.effective_auth == "oidc"
    assert b._directive_unknown is False


# ------------------------------------------------ request-time self-heal


def test_kerberos_401_on_unknown_backend_heals_to_oidc_and_retries(monkeypatch):
    """The 2026-09-14 failure, end to end: Kerberos 401s, the server now answers
    "oidc", the backend switches and the SAME call succeeds over OIDC."""
    shim = _shim()
    _quiet(monkeypatch, shim)
    b = _negotiate_backend(shim, unknown=True)
    monkeypatch.setattr(shim, "_local_windows_upn", lambda: "matt.stevens@punchpowertrain.com")
    monkeypatch.setattr(shim, "_query_auth_mode", lambda url, upn: "oidc")
    seen_interactive = []

    def _token(upn, *, allow_interactive):
        seen_interactive.append(allow_interactive)
        return "AT"
    monkeypatch.setattr(shim, "_oidc_acquire_token", _token)
    calls = []
    monkeypatch.setattr(b, "http_client", _factory({
        None:   _FakeResp(401, {"error": True, "message": "Negotiate token rejected"}),
        "oidc": _FakeResp(200, {"result": {"rows": [1, 2]}}),
    }, calls))
    _register(monkeypatch, shim, b, "zb_host_list", "zb_host_list")

    out = json.loads(shim._call_remote("zb_host_list", {}))

    assert out["rows"] == [1, 2]
    assert out["_shim_served_by"] == "zabbix"
    assert calls == [None, "oidc"]                 # one Kerberos try, one OIDC retry
    assert b.effective_auth == "oidc"
    assert b._oidc_upn == "matt.stevens@punchpowertrain.com"
    assert b._directive_unknown is False
    assert b._fell_back is False
    assert seen_interactive == [False]             # never pops a browser mid-call


def test_kerberos_401_on_decided_backend_is_not_healed(monkeypatch):
    """Kerberos by DECISION (explicit answer): a 401 is a real auth failure.
    Do not re-ask, do not retry."""
    shim = _shim()
    _quiet(monkeypatch, shim)
    b = _negotiate_backend(shim, unknown=False)
    monkeypatch.setattr(shim, "_query_auth_mode", _must_not_query)
    calls = []
    monkeypatch.setattr(b, "http_client", _factory({
        None: _FakeResp(401, {"error": True}),
    }, calls))
    _register(monkeypatch, shim, b, "zb_host_list", "zb_host_list")

    out = json.loads(shim._call_remote("zb_host_list", {}))
    assert out["_shim_served_by"] == "zabbix"
    assert calls == [None]
    assert b.effective_auth == "negotiate"


def test_heal_still_no_answer_keeps_flag_and_does_not_retry(monkeypatch):
    shim = _shim()
    _quiet(monkeypatch, shim)
    b = _negotiate_backend(shim, unknown=True)
    monkeypatch.setattr(shim, "_local_windows_upn", lambda: "a@p.com")
    monkeypatch.setattr(shim, "_query_auth_mode", lambda url, upn: "unknown")
    calls = []
    monkeypatch.setattr(b, "http_client", _factory({
        None: _FakeResp(401, {"error": True}),
    }, calls))
    _register(monkeypatch, shim, b, "zb_host_list", "zb_host_list")

    shim._call_remote("zb_host_list", {})
    assert calls == [None]
    assert b.effective_auth == "negotiate"
    assert b._directive_unknown is True            # a later 401 may re-ask


def test_heal_confirmed_kerberos_clears_flag_and_does_not_retry(monkeypatch):
    shim = _shim()
    _quiet(monkeypatch, shim)
    b = _negotiate_backend(shim, unknown=True)
    monkeypatch.setattr(shim, "_local_windows_upn", lambda: "a@p.com")
    monkeypatch.setattr(shim, "_query_auth_mode", lambda url, upn: "kerberos")
    calls = []
    monkeypatch.setattr(b, "http_client", _factory({
        None: _FakeResp(401, {"error": True}),
    }, calls))
    _register(monkeypatch, shim, b, "zb_host_list", "zb_host_list")

    shim._call_remote("zb_host_list", {})
    assert calls == [None]
    assert b.effective_auth == "negotiate"
    assert b._directive_unknown is False


def test_heal_oidc_confirmed_but_no_token_stays_negotiate(monkeypatch):
    shim = _shim()
    _quiet(monkeypatch, shim)
    b = _negotiate_backend(shim, unknown=True)
    monkeypatch.setattr(shim, "_local_windows_upn", lambda: "a@p.com")
    monkeypatch.setattr(shim, "_query_auth_mode", lambda url, upn: "oidc")

    def _no_token(upn, *, allow_interactive):
        raise shim.OidcError("no cached token, interactive disallowed")
    monkeypatch.setattr(shim, "_oidc_acquire_token", _no_token)
    calls = []
    monkeypatch.setattr(b, "http_client", _factory({
        None: _FakeResp(401, {"error": True}),
    }, calls))
    _register(monkeypatch, shim, b, "zb_host_list", "zb_host_list")

    shim._call_remote("zb_host_list", {})
    assert calls == [None]
    assert b.effective_auth == "negotiate"
    assert b._fell_back is True                    # same state the startup pass records
    assert b._directive_unknown is False


def test_403_on_unknown_backend_is_not_healed(monkeypatch):
    """403 is authorization. Re-asking which auth to use cannot fix it."""
    shim = _shim()
    _quiet(monkeypatch, shim)
    b = _negotiate_backend(shim, unknown=True)
    monkeypatch.setattr(shim, "_query_auth_mode", _must_not_query)
    calls = []
    monkeypatch.setattr(b, "http_client", _factory({
        None: _FakeResp(403, {"error": True}),
    }, calls))
    _register(monkeypatch, shim, b, "zb_host_list", "zb_host_list")

    shim._call_remote("zb_host_list", {})
    assert calls == [None]
    assert b._directive_unknown is True


def test_heal_does_not_wait_on_a_busy_directive_lock(monkeypatch):
    """The startup pass may hold the lock through an interactive sign-in.
    The request-time heal must skip rather than block the tool call."""
    shim = _shim()
    b = _negotiate_backend(shim, unknown=True)
    monkeypatch.setattr(shim, "_query_auth_mode", _must_not_query)
    assert shim._AUTH_DIRECTIVE_LOCK.acquire(blocking=False)
    try:
        assert shim._heal_unknown_directive(b) is False
    finally:
        shim._AUTH_DIRECTIVE_LOCK.release()
    assert b._directive_unknown is True


# ------------------------------------------------------------------ shim_info


def test_shim_info_surfaces_auth_directive_unknown():
    shim = _shim()
    b = _negotiate_backend(shim, unknown=True)
    with patch.object(shim, "_BACKENDS", new=[b]):
        payload = json.loads(asyncio.run(shim.shim_info(MagicMock())))
    entry = next(x for x in payload["backends"] if x["name"] == "zabbix")
    assert entry["auth_directive_unknown"] is True
    assert entry["effective_auth"] == "negotiate"
    assert entry["fell_back"] is False
