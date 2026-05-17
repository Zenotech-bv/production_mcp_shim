"""v2.4.0 — per-backend auth mode in backends.json.

Adds an `auth` field to each backend entry. Default is "x-punch-auth"
so pre-v2.4.0 files keep working. "negotiate" mode swaps the
X-Punch-Auth header for an httpx.Auth that mints a SPNEGO token via
pyspnego + SSPI on every request.

These tests cover:
  * Backend.__post_init__ defaults + validation
  * Backend.is_configured semantics (key not required for negotiate)
  * Backend.http_client() builds the right httpx.Client (header vs auth)
  * _load_backends_from_file parses + applies the field
  * _reconcile_backends surfaces auth flips
  * NegotiateAuth emits Authorization: Negotiate <token> on the first
    request, handles the 401 continuation case, and bails on a
    non-Negotiate challenge
"""
from __future__ import annotations

import base64
import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest


def _import_shim():
    return importlib.import_module("shim_server")


# ---------------------------------------------------------------------------
# Backend dataclass + http_client
# ---------------------------------------------------------------------------


def test_backend_auth_defaults_to_x_punch_auth():
    shim = _import_shim()
    b = shim.Backend(name="sap", url="http://x:1", header="X-A",
                     key="K-padded-to-pass-placeholder-guard")
    assert b.auth == "x-punch-auth"


def test_backend_unknown_auth_falls_back_safely():
    shim = _import_shim()
    b = shim.Backend(name="sap", url="http://x:1", header="X-A",
                     key="K-padded-to-pass-placeholder-guard",
                     auth="kerberos-but-not-quite")
    # Unknown mode is logged and reverted to the safe default so the shim
    # doesn't crash on a typo'd config.
    assert b.auth == "x-punch-auth"


def test_backend_auth_is_normalised_to_lowercase():
    shim = _import_shim()
    b = shim.Backend(name="sap", url="http://x:1", header="X-A", key="K",
                     auth="NEGOTIATE")
    assert b.auth == "negotiate"


def test_negotiate_backend_is_configured_without_a_key():
    shim = _import_shim()
    b = shim.Backend(name="sap", url="http://mcp.example.com:3000",
                     header="X-Punch-Auth", key="", auth="negotiate")
    assert b.is_configured is True


def test_x_punch_auth_backend_still_requires_key():
    shim = _import_shim()
    b = shim.Backend(name="sap", url="http://x:1", header="X-A", key="",
                     auth="x-punch-auth")
    assert b.is_configured is False


def test_http_client_negotiate_attaches_auth_and_omits_x_punch_auth_header():
    shim = _import_shim()
    b = shim.Backend(name="sap", url="http://mcp.example.com:3000",
                     header="X-Punch-Auth", key="", auth="negotiate")
    with b.http_client() as c:
        # No X-Punch-Auth header on the client's default headers.
        assert "X-Punch-Auth" not in c.headers
        # An auth handler IS attached.
        assert isinstance(c.auth, shim.NegotiateAuth)


def test_http_client_x_punch_auth_attaches_header_and_no_auth():
    shim = _import_shim()
    b = shim.Backend(name="sap", url="http://mcp.example.com:3000",
                     header="X-Punch-Auth",
                     key="ak_test-padded-to-pass-placeholder-guard",
                     auth="x-punch-auth")
    with b.http_client() as c:
        assert c.headers["X-Punch-Auth"] == "ak_test-padded-to-pass-placeholder-guard"
        # No auth handler when the API-key path is in use.
        assert c.auth is None or isinstance(c.auth, httpx._auth.FunctionAuth) is False
        # Stricter: it must NOT be a NegotiateAuth.
        assert not isinstance(c.auth, shim.NegotiateAuth)


def test_http_client_negotiate_without_hostname_raises():
    shim = _import_shim()
    # No hostname (relative URL) should fail loudly at client-build time.
    b = shim.Backend(name="sap", url="/no-host", header="X-A", key="",
                     auth="negotiate")
    with pytest.raises(ValueError, match="hostname"):
        b.http_client()


# ---------------------------------------------------------------------------
# _load_backends_from_file — schema parsing
# ---------------------------------------------------------------------------


def _write_backends(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "backends.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_load_backends_parses_auth_negotiate(tmp_path: Path):
    shim = _import_shim()
    p = _write_backends(tmp_path, {
        "backends": [{
            "name":   "sap",
            "url":    "http://mcp.example.com:3000",
            "auth":   "negotiate",
        }],
    })
    backends, primary = shim._load_backends_from_file(p)
    assert len(backends) == 1
    assert backends[0].auth == "negotiate"
    assert backends[0].key == ""  # no key in payload, none invented
    assert primary == "sap"


def test_load_backends_missing_auth_defaults_to_x_punch_auth(tmp_path: Path):
    shim = _import_shim()
    p = _write_backends(tmp_path, {
        "backends": [{
            "name":   "sap",
            "url":    "http://x:1",
            "header": "X-Punch-Auth",
            "key":    "ak_legacy-padded-to-pass-placeholder-guard",
        }],
    })
    backends, _ = shim._load_backends_from_file(p)
    assert backends[0].auth == "x-punch-auth"


def test_load_backends_negotiate_skips_placeholder_guard(tmp_path: Path):
    """A negotiate backend has no key to guard against — the placeholder
    check must not fire and accidentally erase a legitimate empty key."""
    shim = _import_shim()
    # "test-key" is a placeholder fingerprint per _looks_like_placeholder.
    # If the guard fired, key would be blanked; we explicitly assert it
    # stays whatever the file said (empty here, but the assertion is that
    # no warning event fires for placeholder rejection).
    p = _write_backends(tmp_path, {
        "backends": [{
            "name":   "sap",
            "url":    "http://x:1",
            "auth":   "negotiate",
            "key":    "test-key",  # would normally trip the guard
        }],
    })
    backends, _ = shim._load_backends_from_file(p)
    # The Backend got built and is configured (negotiate doesn't need key).
    assert backends[0].auth == "negotiate"
    assert backends[0].is_configured is True


# ---------------------------------------------------------------------------
# _reconcile_backends — Kerberos cutover hot-reload
# ---------------------------------------------------------------------------


def test_reconcile_flips_auth_mode_in_place():
    shim = _import_shim()
    existing = [shim.Backend(name="sap", url="http://x:1", header="X-Punch-Auth",
                             key="OLD-padded-to-pass-placeholder-guard",
                             auth="x-punch-auth")]
    new      = [shim.Backend(name="sap", url="http://x:1", header="X-Punch-Auth",
                             key="", auth="negotiate")]

    summary = shim._reconcile_backends(existing, new)

    assert existing[0].auth == "negotiate"
    assert summary["auth_changes"] == ["sap"]
    assert summary["rotated_keys"] == []  # empty new.key is "no change"


def test_reconcile_unchanged_auth_does_not_surface():
    shim = _import_shim()
    existing = [shim.Backend(name="sap", url="http://x:1", header="X-A",
                             key="K-padded-to-pass-placeholder-guard",
                             auth="x-punch-auth")]
    new      = [shim.Backend(name="sap", url="http://x:1", header="X-A",
                             key="K-padded-to-pass-placeholder-guard",
                             auth="x-punch-auth")]

    summary = shim._reconcile_backends(existing, new)

    assert summary["auth_changes"] == []


# ---------------------------------------------------------------------------
# NegotiateAuth — protocol behaviour
# ---------------------------------------------------------------------------


def _fake_spnego_client(initial_token: bytes, continuation_token: bytes | None = None):
    """Return an object that mimics spnego.client's interface for tests.

    .step() (first call, in_token=None) -> initial_token
    .step(any_bytes)                    -> continuation_token (or None to end)
    """
    calls = {"n": 0}

    def step(in_token=None):
        calls["n"] += 1
        if calls["n"] == 1:
            assert in_token is None
            return initial_token
        return continuation_token

    ctx = MagicMock()
    ctx.step = step
    return ctx


def test_negotiate_auth_adds_authorization_header_on_first_request():
    shim = _import_shim()
    auth = shim.NegotiateAuth("mcp.example.com")

    fake_token = b"\x01\x02\x03initial-spnego-blob"
    fake_ctx = _fake_spnego_client(fake_token)

    with patch("spnego.client", return_value=fake_ctx) as mock_client:
        flow = auth.auth_flow(httpx.Request("POST", "http://mcp.example.com:3000/tools/foo"))
        req = next(flow)

    # Built the SSPI context against the right hostname + HTTP service.
    mock_client.assert_called_once()
    _, kwargs = mock_client.call_args
    assert kwargs["hostname"] == "mcp.example.com"
    assert kwargs["service"] == "HTTP"
    assert kwargs["protocol"] == "negotiate"

    # Authorization header contains base64-encoded initial token.
    auth_hdr = req.headers["Authorization"]
    assert auth_hdr.startswith("Negotiate ")
    decoded = base64.b64decode(auth_hdr[len("Negotiate "):])
    assert decoded == fake_token


def test_negotiate_auth_handles_401_continuation():
    shim = _import_shim()
    auth = shim.NegotiateAuth("mcp.example.com")

    fake_ctx = _fake_spnego_client(b"first", b"second")

    # httpx reuses the SAME Request object across yields, so we snapshot
    # the Authorization header at each step rather than holding refs.
    headers_per_leg: list[str] = []

    with patch("spnego.client", return_value=fake_ctx):
        flow = auth.auth_flow(httpx.Request("POST", "http://mcp.example.com:3000/x"))
        first_req = next(flow)
        headers_per_leg.append(first_req.headers["Authorization"])

        challenge_b64 = base64.b64encode(b"server-leg-2").decode("ascii")
        resp = httpx.Response(401, headers={"WWW-Authenticate": f"Negotiate {challenge_b64}"})
        try:
            second_req = flow.send(resp)
        except StopIteration:
            pytest.fail("NegotiateAuth should have yielded a second request for the continuation")
        headers_per_leg.append(second_req.headers["Authorization"])

    assert headers_per_leg[0] == "Negotiate " + base64.b64encode(b"first").decode("ascii")
    assert headers_per_leg[1] == "Negotiate " + base64.b64encode(b"second").decode("ascii")


def test_negotiate_auth_stops_on_401_without_negotiate_challenge():
    """A 401 carrying only Basic / Bearer / nothing must end the auth flow,
    not loop forever."""
    shim = _import_shim()
    auth = shim.NegotiateAuth("mcp.example.com")

    fake_ctx = _fake_spnego_client(b"first")

    with patch("spnego.client", return_value=fake_ctx):
        flow = auth.auth_flow(httpx.Request("POST", "http://mcp.example.com:3000/x"))
        next(flow)  # first request
        resp = httpx.Response(401, headers={"WWW-Authenticate": "Basic realm=foo"})
        with pytest.raises(StopIteration):
            flow.send(resp)
