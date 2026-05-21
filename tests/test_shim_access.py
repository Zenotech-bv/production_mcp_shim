"""Tests for shim_access, _probe_backend, and _enrich_response."""
from __future__ import annotations

import importlib
import json
from unittest.mock import MagicMock

import httpx


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
