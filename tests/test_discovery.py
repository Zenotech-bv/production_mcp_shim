"""Tests for shim backend discovery (SHIM-1)."""
from __future__ import annotations

import importlib
from unittest.mock import patch


def _shim():
    return importlib.import_module("shim_server")


def _backend(shim, name, url, auth="negotiate"):
    return shim.Backend(name=name, url=url, header="X-Punch-Auth",
                        key="", auth=auth)


def test_discover_appends_new_backend():
    """A discovered backend with a new name is appended."""
    shim = _shim()
    local = [_backend(shim, "sap", "http://x:3000")]
    payload = {"version": 1, "backends": [
        {"name": "sap",    "url": "http://x:3000", "auth": "negotiate"},
        {"name": "newsvc", "url": "http://x:3009", "auth": "negotiate"},
    ]}
    with patch.object(shim, "_fetch_discovery", return_value=payload):
        merged = shim._discover_backends(local)
    assert [b.name for b in merged] == ["sap", "newsvc"]
    newsvc = next(b for b in merged if b.name == "newsvc")
    assert newsvc.url == "http://x:3009"
    assert newsvc.auth == "negotiate"


def test_discover_name_collision_keeps_local():
    """A discovered backend whose name is already loaded is skipped — the
    local entry is kept verbatim, never overridden."""
    shim = _shim()
    local = [_backend(shim, "sap", "http://LOCAL:3000", auth="negotiate")]
    payload = {"version": 1, "backends": [
        {"name": "sap", "url": "http://DISCOVERED:3000", "auth": "x-punch-auth"},
    ]}
    with patch.object(shim, "_fetch_discovery", return_value=payload):
        merged = shim._discover_backends(local)
    assert len(merged) == 1
    assert merged[0].url == "http://LOCAL:3000"
    assert merged[0].auth == "negotiate"


def test_discover_supervisor_down_returns_local_unchanged():
    """_fetch_discovery returning None (unreachable/disabled) -> the local
    backend set is returned unchanged."""
    shim = _shim()
    local = [_backend(shim, "sap", "http://x:3000")]
    with patch.object(shim, "_fetch_discovery", return_value=None):
        merged = shim._discover_backends(local)
    assert merged == local


def test_discover_skips_entries_missing_fields():
    """Discovered entries missing name or url are skipped; valid ones in
    the same payload still merge."""
    shim = _shim()
    local = [_backend(shim, "sap", "http://x:3000")]
    payload = {"version": 1, "backends": [
        {"name": "",      "url": "http://x:1",    "auth": "negotiate"},
        {"name": "nourl",                          "auth": "negotiate"},
        {"name": "good",  "url": "http://x:3009", "auth": "negotiate"},
    ]}
    with patch.object(shim, "_fetch_discovery", return_value=payload):
        merged = shim._discover_backends(local)
    assert [b.name for b in merged] == ["sap", "good"]


def test_discover_non_list_backends_returns_local_unchanged():
    """A payload whose 'backends' is not a list -> local set unchanged."""
    shim = _shim()
    local = [_backend(shim, "sap", "http://x:3000")]
    with patch.object(shim, "_fetch_discovery", return_value={"version": 1}):
        merged = shim._discover_backends(local)
    assert merged == local


def test_fetch_discovery_unreachable_returns_none():
    """_fetch_discovery against a host that won't resolve -> None, no raise."""
    shim = _shim()
    with patch.object(shim, "_DISCOVERY_URL", "http://shim-disco.invalid:1/x"):
        assert shim._fetch_discovery() is None
