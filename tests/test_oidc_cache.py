from __future__ import annotations

import importlib
import time
from unittest.mock import patch

import pytest


def _shim():
    return importlib.import_module("shim_server")


def test_cache_roundtrip_plaintext_fallback(monkeypatch):
    """With win32crypt unavailable, the cache degrades to plaintext (still
    per-user-isolated under LOCALAPPDATA) and round-trips."""
    shim = _shim()
    # Force the DPAPI import to fail so the plaintext path is exercised.
    monkeypatch.setattr(shim, "_dpapi_available", lambda: False)
    upn = "alice@punchpowertrain.com"
    shim._token_cache_write(upn, {"access_token": "AT", "refresh_token": "RT",
                                  "expires_at": time.time() + 3600})
    got = shim._token_cache_read(upn)
    assert got["access_token"] == "AT"
    assert got["refresh_token"] == "RT"


def test_acquire_uses_valid_cached_access_token(monkeypatch):
    shim = _shim()
    upn = "bob@punchpowertrain.com"
    monkeypatch.setattr(shim, "_token_cache_read",
                        lambda u: {"access_token": "CACHED", "refresh_token": "RT",
                                   "expires_at": time.time() + 600})
    # No network calls should happen.
    with patch("httpx.post", side_effect=AssertionError("must not hit network")):
        tok = shim._oidc_acquire_token(upn, allow_interactive=False)
    assert tok == "CACHED"


def test_acquire_refreshes_when_access_expired(monkeypatch):
    shim = _shim()
    upn = "carol@punchpowertrain.com"
    monkeypatch.setattr(shim, "_token_cache_read",
                        lambda u: {"access_token": "OLD", "refresh_token": "RT",
                                   "expires_at": time.time() - 10})
    monkeypatch.setattr(shim, "_refresh_token",
                        lambda rt: {"access_token": "NEW", "refresh_token": "RT2",
                                    "expires_in": 3600})
    saved = {}
    monkeypatch.setattr(shim, "_token_cache_write", lambda u, t: saved.update(t))
    tok = shim._oidc_acquire_token(upn, allow_interactive=False)
    assert tok == "NEW"
    assert saved["access_token"] == "NEW"


def test_acquire_no_cache_no_interactive_raises(monkeypatch):
    shim = _shim()
    monkeypatch.setattr(shim, "_token_cache_read", lambda u: None)
    with pytest.raises(shim.OidcError):
        shim._oidc_acquire_token("dave@punchpowertrain.com", allow_interactive=False)


def test_acquire_interactive_when_allowed_and_no_cache(monkeypatch):
    shim = _shim()
    monkeypatch.setattr(shim, "_token_cache_read", lambda u: None)
    monkeypatch.setattr(shim, "_loopback_receive_code",
                        lambda *, state, timeout, login_hint="": ("CODE", "http://localhost:5000", "VER", "NONCE"))
    monkeypatch.setattr(shim, "_exchange_code",
                        lambda code, ver, ru: {"access_token": "IAT", "refresh_token": "IRT",
                                               "expires_in": 3600})
    saved = {}
    monkeypatch.setattr(shim, "_token_cache_write", lambda u, t: saved.update(t))
    tok = shim._oidc_acquire_token("eve@punchpowertrain.com", allow_interactive=True)
    assert tok == "IAT"
    assert saved["refresh_token"] == "IRT"


def test_acquire_refresh_fails_then_interactive_succeeds(monkeypatch):
    """Cache has an EXPIRED access token + a refresh_token; the refresh call
    fails (OidcError). Falling through to interactive sign-in must still
    succeed and return the freshly-acquired access token."""
    shim = _shim()
    upn = "frank@punchpowertrain.com"
    monkeypatch.setattr(shim, "_token_cache_read",
                        lambda u: {"access_token": "OLD", "refresh_token": "RT",
                                   "expires_at": time.time() - 10})

    def _fail_refresh(rt):
        raise shim.OidcError("refresh failed: 400 invalid_grant")

    monkeypatch.setattr(shim, "_refresh_token", _fail_refresh)
    monkeypatch.setattr(shim, "_loopback_receive_code",
                        lambda *, state, timeout, login_hint="": ("CODE", "http://localhost:5000", "VER", "NONCE"))
    monkeypatch.setattr(shim, "_exchange_code",
                        lambda code, ver, ru: {"access_token": "IAT2", "refresh_token": "IRT2",
                                               "expires_in": 3600})
    saved = {}
    monkeypatch.setattr(shim, "_token_cache_write", lambda u, t: saved.update(t))
    tok = shim._oidc_acquire_token(upn, allow_interactive=True)
    assert tok == "IAT2"
    assert saved["refresh_token"] == "IRT2"


def test_acquire_refresh_fails_no_interactive_raises(monkeypatch):
    """Same expired-access + refresh-token cache, but refresh fails and
    interactive sign-in is NOT allowed (e.g. a background/non-interactive
    caller). Must raise OidcError -- never fall back to a stale or None
    token."""
    shim = _shim()
    upn = "grace@punchpowertrain.com"
    monkeypatch.setattr(shim, "_token_cache_read",
                        lambda u: {"access_token": "OLD", "refresh_token": "RT",
                                   "expires_at": time.time() - 10})

    def _fail_refresh(rt):
        raise shim.OidcError("refresh failed: 400 invalid_grant")

    monkeypatch.setattr(shim, "_refresh_token", _fail_refresh)
    with pytest.raises(shim.OidcError):
        shim._oidc_acquire_token(upn, allow_interactive=False)


def test_exchange_code_missing_access_token_raises_oidc_error():
    """A 200 response that omits access_token (malformed/hostile IdP
    response) must surface as OidcError, matching the documented
    '_oidc_acquire_token raises OidcError' contract -- not an unhandled
    KeyError escaping the caller's `except OidcError`."""
    import httpx
    shim = _shim()
    with patch("httpx.post", lambda *a, **k: httpx.Response(200, json={"token_type": "Bearer"})):
        with pytest.raises(shim.OidcError, match="access_token"):
            shim._exchange_code("CODE", "VERIFIER", "http://localhost:5000")
