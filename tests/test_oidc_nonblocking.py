"""v3.5.2: the OIDC directive pre-flight must NOT block shim startup.

Before v3.5.2 the module ran `_apply_auth_directives(_BACKENDS)` synchronously
at import, so `shim_ready` was gated on a /auth/mode probe (5s each backend), an
OIDC token refresh (up to 20s), and — the first time — an interactive browser
sign-in (up to 120s). These tests pin the new contract:

  * `_apply_auth_directives_async` returns immediately (a started daemon thread),
    leaving backends on their working `negotiate` credential until the directive
    resolves in the background, then flipping them to `oidc` in place.
  * With no negotiate backend there is nothing to resolve, so it does no work at
    all (not even the whoami/UPN lookup) and returns None.
  * A concurrent `shim_reload` reprobe (`block=False`) skips rather than waiting
    on the startup thread's directive lock.
"""
from __future__ import annotations

import importlib
import threading


def _shim():
    return importlib.import_module("shim_server")


def _mk_negotiate_backend(shim):
    return shim.Backend(name="sap", url="http://mcp.example.com:3000",
                        header="X-Punch-Auth", key="", auth="negotiate")


def test_async_launcher_does_not_block_and_applies_eventually(monkeypatch):
    shim = _shim()
    b = _mk_negotiate_backend(shim)
    gate = threading.Event()

    monkeypatch.setattr(shim, "_local_windows_upn", lambda: "a@p.com")
    monkeypatch.setattr(shim, "_query_auth_mode", lambda url, upn: "oidc")

    def _slow_acquire(upn, *, allow_interactive):
        # Stand in for the up-to-120s interactive sign-in / token round-trip.
        gate.wait(timeout=5)
        return "AT"
    monkeypatch.setattr(shim, "_oidc_acquire_token", _slow_acquire)

    t = shim._apply_auth_directives_async([b])
    # Returned a live thread WITHOUT waiting on the slow acquire:
    assert isinstance(t, threading.Thread)
    assert t.is_alive()
    # Startup is not gated on the pre-flight: the backend still carries its
    # working negotiate credential.
    assert b.effective_auth == "negotiate"

    # Let the pre-flight finish; the switch to OIDC lands in the background.
    gate.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert b.effective_auth == "oidc"
    assert b._oidc_upn == "a@p.com"
    assert b._fell_back is False


def test_async_launcher_is_noop_without_a_negotiate_backend(monkeypatch):
    shim = _shim()
    b = shim.Backend(name="svc", url="http://x:3000", header="X-Punch-Auth",
                     key="k-long-enough-to-clear-the-placeholder-guard-xxxx",
                     auth="x-punch-auth")
    # No negotiate backend -> must not even resolve the local UPN (no whoami).
    monkeypatch.setattr(shim, "_local_windows_upn",
                        lambda: (_ for _ in ()).throw(AssertionError("must not resolve UPN")))
    t = shim._apply_auth_directives_async([b])
    assert t is None
    assert b.effective_auth == "x-punch-auth"


def test_reload_reprobe_skips_when_preflight_still_holds_lock(monkeypatch):
    shim = _shim()
    b = _mk_negotiate_backend(shim)
    # Simulate the startup pre-flight thread still holding the directive lock.
    assert shim._AUTH_DIRECTIVE_LOCK.acquire(blocking=False)
    try:
        monkeypatch.setattr(shim, "_local_windows_upn",
                            lambda: (_ for _ in ()).throw(AssertionError("must not run while locked")))
        out = shim._apply_auth_directives([b], block=False)   # must not block, must not run
        assert out[0].effective_auth == "negotiate"           # unchanged; reprobe skipped
    finally:
        shim._AUTH_DIRECTIVE_LOCK.release()


def test_http_client_oidc_without_upn_falls_back_to_negotiate(monkeypatch):
    """F1 regression: a request that observes effective_auth 'oidc' but an empty
    _oidc_upn (a torn read while a reprobe reset is clearing state on another
    thread) must build a NegotiateAuth, NEVER an OidcAuth("") — which would raise
    inside auth_flow and surface as a spurious transport error instead of using
    the working negotiate path."""
    shim = _shim()
    b = _mk_negotiate_backend(shim)
    b.effective_auth = "oidc"
    b._oidc_upn = ""   # the torn-read / mid-reset state
    monkeypatch.setattr(shim, "OidcAuth",
                        lambda upn: (_ for _ in ()).throw(
                            AssertionError(f"OidcAuth built with empty upn: {upn!r}")))
    # Must not raise: http_client downgrades to the real NegotiateAuth.
    with b.http_client() as c:
        assert isinstance(c.auth, shim.NegotiateAuth)


def test_reprobe_never_requests_interactive_signin(monkeypatch):
    """F2 regression: the shim_reload reprobe path (allow_interactive=False) must
    never trigger an interactive browser sign-in. If no token can be acquired
    silently it falls back to the working negotiate path instead of popping a
    browser / blocking the conversation."""
    shim = _shim()
    b = _mk_negotiate_backend(shim)
    seen = {}
    monkeypatch.setattr(shim, "_local_windows_upn", lambda: "a@p.com")
    monkeypatch.setattr(shim, "_query_auth_mode", lambda url, upn: "oidc")

    def _capture(upn, *, allow_interactive):
        seen["interactive"] = allow_interactive
        if not allow_interactive:
            raise shim.OidcError("no cached token; interactive sign-in disallowed")
        return "AT"
    monkeypatch.setattr(shim, "_oidc_acquire_token", _capture)

    out = shim._apply_auth_directives([b], allow_interactive=False)
    assert seen["interactive"] is False
    assert out[0].effective_auth == "negotiate"
    assert out[0]._fell_back is True
