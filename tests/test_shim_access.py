"""Tests for shim_access, _probe_backend, and _enrich_response."""
from __future__ import annotations

import importlib
import json
from unittest.mock import MagicMock


def _import_shim():
    return importlib.import_module("shim_server")


def test_probe_backend_unreachable_returns_not_reachable():
    shim = _import_shim()
    # The conftest's single backend points at a host that doesn't resolve.
    backend = shim._BACKENDS[0]
    result = shim._probe_backend(backend)
    assert result["backend"] == backend.name
    assert result["url"] == backend.url
    assert result["reachable"] is False
    assert result["auth_ok"] is False


def test_probe_backend_ok(monkeypatch):
    shim = _import_shim()
    backend = shim._BACKENDS[0]

    class _Resp:
        status_code = 200
        def __init__(self): self.is_success = True

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw): return _Resp()

    monkeypatch.setattr(backend, "http_client", lambda **kw: _Client())
    result = shim._probe_backend(backend)
    assert result["reachable"] is True
    assert result["auth_ok"] is True


def test_probe_backend_auth_failed(monkeypatch):
    shim = _import_shim()
    backend = shim._BACKENDS[0]

    class _Resp:
        status_code = 401
        def __init__(self): self.is_success = False

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw): return _Resp()

    monkeypatch.setattr(backend, "http_client", lambda **kw: _Client())
    result = shim._probe_backend(backend)
    assert result["reachable"] is True
    assert result["auth_ok"] is False


def _call_shim_access(shim):
    import asyncio
    return json.loads(asyncio.run(shim.shim_access(MagicMock())))


def test_shim_access_pa_whoami_missing_degrades_gracefully():
    """With pa_whoami unregistered (old/unreachable pa_v2), account is null
    and account_error explains why — shim_access still returns cleanly."""
    shim = _import_shim()
    payload = _call_shim_access(shim)
    assert payload["shim_version"] == shim._SHIM_VERSION
    assert isinstance(payload["connectivity"], list)
    assert payload["account"] is None
    assert "pa_whoami" in payload["account_error"]
    assert "Access profile unavailable" in payload["summary"]


def test_shim_access_includes_account_when_pa_whoami_present(monkeypatch):
    """When pa_whoami is reachable, its profile lands under 'account'."""
    shim = _import_shim()
    fake_profile = {"identity": {"username": "matt_stevens"},
                    "systems": [], "summary": "SAP: full access."}
    monkeypatch.setitem(shim._NAME_TO_BACKEND, "pa_whoami",
                        (shim._BACKENDS[0], "pa_whoami"))
    monkeypatch.setattr(shim, "_call_remote",
                        lambda name, kw: json.dumps(fake_profile))
    payload = _call_shim_access(shim)
    assert payload["account"] == fake_profile
    assert "SAP: full access." in payload["summary"]


def test_shim_access_survives_internal_failure(monkeypatch):
    """A blown-up internal state still yields JSON carrying the version."""
    shim = _import_shim()
    monkeypatch.setattr(shim, "_BACKENDS", None)  # iterating None raises
    payload = _call_shim_access(shim)
    assert payload["shim_version"] == shim._SHIM_VERSION
    assert "error_type" in payload


def test_shim_access_pa_whoami_error_envelope_becomes_account_error(monkeypatch):
    """pa_whoami registered but returning an error envelope -> account_error,
    account stays None. Covers the 'connected but the call failed' branch."""
    shim = _import_shim()
    monkeypatch.setitem(shim._NAME_TO_BACKEND, "pa_whoami",
                        (shim._BACKENDS[0], "pa_whoami"))
    monkeypatch.setattr(shim, "_call_remote",
                        lambda name, kw: json.dumps(
                            {"error": True, "error_type": "Unreachable",
                             "message": "Cannot reach backend"}))
    payload = _call_shim_access(shim)
    assert payload["account"] is None
    assert payload["account_error"] == "Cannot reach backend"
