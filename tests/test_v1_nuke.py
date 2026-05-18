"""v3.0.5 — stale v1 backends.json archive + re-seed.

v1 (mcp.punchpowertrain.com) was retired at the v0.0.115/116 Kerberos
cutover. Laptops carrying a backends.json that still points at the
v1 host fail every call. v3.0.5 detects that on shim startup,
archives the stale file to a `.v1-archived-<ts>` sidecar (rather
than deleting), and lets `_maybe_seed_backends_file` write a fresh
Kerberos-default config in its place.

These tests exercise:
- the v1-detection predicate (`_looks_v1_backend`)
- the archive helper (`_archive_v1_backends_file`)
- the end-to-end `_load_backends()` flow against a v1-shaped fixture
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path


def _import_shim():
    return importlib.import_module("shim_server")


def _v1_backends_payload() -> dict:
    """The kind of file a pre-cutover laptop would have on disk: v1
    host, x-punch-auth, a key that v2 has never seen."""
    return {
        "backends": [
            {
                "name":   "sap",
                "url":    "http://mcp.punchpowertrain.com",
                "auth":   "x-punch-auth",
                "header": "X-Punch-Auth",
                "key":    "v1-era-key-padded-to-pass-placeholder-guard-xxxx",
            }
        ],
        "primary": "sap",
    }


def test_looks_v1_backend_matches_v1_host():
    shim = _import_shim()
    Backend = shim.Backend
    v1 = Backend(name="sap", url="http://mcp.punchpowertrain.com", header="X-Punch-Auth", key="x" * 32, auth="x-punch-auth")
    v2 = Backend(name="sap", url="http://ai.punchpowertrain.com:3000", header="", key="", auth="negotiate")
    assert shim._looks_v1_backend(v1) is True
    assert shim._looks_v1_backend(v2) is False


def test_looks_v1_backend_handles_garbage_url():
    """A malformed URL must not raise; just return False so we don't
    archive on noise."""
    shim = _import_shim()
    Backend = shim.Backend
    garbage = Backend(name="sap", url="not-a-url", header="X-Punch-Auth", key="x" * 32, auth="x-punch-auth")
    assert shim._looks_v1_backend(garbage) is False


def test_archive_renames_to_sidecar(tmp_path):
    shim = _import_shim()
    cfg = tmp_path / "backends.json"
    cfg.write_text(json.dumps(_v1_backends_payload()), encoding="utf-8")

    archive = shim._archive_v1_backends_file(cfg, reason="test")

    assert archive is not None
    assert not cfg.exists(), "original path must be renamed away"
    assert archive.exists(), "sidecar must exist"
    assert ".v1-archived-" in archive.name
    # Content preserved on the sidecar, so a user could recover.
    recovered = json.loads(archive.read_text(encoding="utf-8"))
    assert recovered == _v1_backends_payload()


def test_load_backends_archives_v1_and_reseeds(tmp_path, monkeypatch):
    """End-to-end: a v1-shaped backends.json on disk -> archived,
    a fresh Kerberos-default backends.json written in its place,
    the returned backend list has no v1 hosts."""
    shim = _import_shim()
    # Force the no-key (Kerberos) re-seed path. The conftest already
    # leaves PUNCH_SAP_KEY empty; double-belt for clarity.
    monkeypatch.setattr(shim, "PUNCH_SAP_KEY", "")

    cfg = tmp_path / "backends.json"
    cfg.write_text(json.dumps(_v1_backends_payload()), encoding="utf-8")
    monkeypatch.setenv("PUNCH_BACKENDS_FILE", str(cfg))

    backends, primary = shim._load_backends()

    # Re-seeded file in place.
    assert cfg.exists()
    fresh = json.loads(cfg.read_text(encoding="utf-8"))
    for b in fresh["backends"]:
        assert b.get("auth") == "negotiate", f"reseed must use negotiate, got {b!r}"

    # Archive sidecar exists alongside.
    sidecars = list(tmp_path.glob("backends.json.v1-archived-*"))
    assert len(sidecars) == 1, f"expected exactly one archive sidecar, found {sidecars}"

    # Returned backends contain no v1 hosts.
    assert backends, "must return at least one backend"
    assert not any(shim._looks_v1_backend(b) for b in backends), (
        f"returned backends must not include any v1 host: {[b.url for b in backends]}"
    )


def test_load_backends_leaves_clean_v2_file_untouched(tmp_path, monkeypatch):
    """A backends.json that already targets v2 must not be archived
    or rewritten — the heuristic only fires on v1 hosts."""
    shim = _import_shim()
    monkeypatch.setattr(shim, "PUNCH_SAP_KEY", "")

    v2_payload = {
        "backends": [
            {"name": "sap",    "url": "http://ai.punchpowertrain.com:3000", "auth": "negotiate"},
            {"name": "zabbix", "url": "http://ai.punchpowertrain.com:3002", "auth": "negotiate"},
        ],
        "primary": "sap",
    }
    cfg = tmp_path / "backends.json"
    cfg.write_text(json.dumps(v2_payload), encoding="utf-8")
    monkeypatch.setenv("PUNCH_BACKENDS_FILE", str(cfg))

    original_mtime = cfg.stat().st_mtime

    backends, primary = shim._load_backends()

    assert cfg.exists()
    # No archive sidecar — file untouched.
    assert not list(tmp_path.glob("backends.json.v1-archived-*"))
    # Returned set matches what we wrote.
    assert {b.name for b in backends} == {"sap", "zabbix"}
