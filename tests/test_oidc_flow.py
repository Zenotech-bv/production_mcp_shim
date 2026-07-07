from __future__ import annotations

import base64
import hashlib
import importlib
from unittest.mock import MagicMock, patch

import httpx
import pytest


def _shim():
    return importlib.import_module("shim_server")


def test_pkce_pair_is_valid_s256():
    shim = _shim()
    verifier, challenge = shim._pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_exchange_code_posts_no_secret_and_returns_tokens():
    shim = _shim()
    captured = {}

    def _fake_post(url, data=None, timeout=None, **kw):
        captured["url"] = url
        captured["data"] = data
        return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT",
                                         "expires_in": 3600, "id_token": "idt"})

    with patch("httpx.post", _fake_post):
        toks = shim._exchange_code("CODE", "VERIFIER", "http://localhost:5000")
    assert toks["access_token"] == "AT"
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["client_id"] == shim._OIDC_CLIENT_ID
    assert captured["data"]["code_verifier"] == "VERIFIER"
    assert "client_secret" not in captured["data"]     # public client, no secret


def test_exchange_code_raises_on_error_response():
    shim = _shim()
    with patch("httpx.post", lambda *a, **k: httpx.Response(400, json={"error": "invalid_grant"})):
        with pytest.raises(shim.OidcError):
            shim._exchange_code("bad", "v", "http://localhost:5000")


def test_exchange_code_network_error_raises_oidc_error():
    """Fix 1a: a network failure (DNS/VPN/timeout -> httpx.RequestError, NOT
    an HTTP error response) must still surface as OidcError, not escape as
    the raw httpx.ConnectError. _exchange_code's contract is 'raises
    OidcError' unconditionally."""
    shim = _shim()

    def _boom(*a, **k):
        raise httpx.ConnectError("down")

    with patch("httpx.post", _boom):
        with pytest.raises(shim.OidcError):
            shim._exchange_code("CODE", "VERIFIER", "http://localhost:5000")


def test_refresh_token_network_error_raises_oidc_error():
    """Same as above, for _refresh_token."""
    shim = _shim()

    def _boom(*a, **k):
        raise httpx.ConnectError("down")

    with patch("httpx.post", _boom):
        with pytest.raises(shim.OidcError):
            shim._refresh_token("RT")


def test_loopback_rejects_state_mismatch():
    """A callback whose state doesn't match must be rejected (CSRF guard)."""
    shim = _shim()
    # Simulate the server having received a callback with the WRONG state by
    # patching the internal single-shot receiver to return a mismatched state.
    with patch.object(shim, "_loopback_serve_once",
                      return_value={"code": "C", "state": "WRONG"}):
        with pytest.raises(shim.OidcError, match="state"):
            shim._loopback_receive_code(state="EXPECTED", timeout=1.0)


def test_refresh_token_posts_refresh_grant():
    shim = _shim()
    captured = {}

    def _fake_post(url, data=None, timeout=None, **kw):
        captured["data"] = data
        return httpx.Response(200, json={"access_token": "AT2", "refresh_token": "RT2",
                                         "expires_in": 3600})

    with patch("httpx.post", _fake_post):
        toks = shim._refresh_token("RT")
    assert toks["access_token"] == "AT2"
    assert captured["data"]["grant_type"] == "refresh_token"
    assert captured["data"]["refresh_token"] == "RT"
    assert "client_secret" not in captured["data"]
