"""v3.0.2 — _maybe_seed_backends_file branches on PUNCH_SAP_KEY presence.

Pre-v3.0.2 the function bailed entirely when PUNCH_SAP_KEY was unset, so
a fresh Kerberos install (API key field left blank in the install
dialog, no env var, no prior backends.json) ended up with zero
registered tools. v3.0.2 always seeds — Kerberos template when no key,
x-punch-auth-flavoured template when there is one.

The conftest sets PUNCH_SAP_KEY="" at session scope so the bare-module
state matches the Kerberos default. Each test monkeypatches the
module-level PUNCH_SAP_KEY when it needs the other branch.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path


def _import_shim():
    return importlib.import_module("shim_server")


def test_seed_writes_kerberos_template_when_no_key(tmp_path, monkeypatch):
    """No PUNCH_SAP_KEY -> every backend seeded with auth=negotiate, no key."""
    shim = _import_shim()
    monkeypatch.setattr(shim, "PUNCH_SAP_KEY", "")

    cfg = tmp_path / "subdir" / "backends.json"
    wrote = shim._maybe_seed_backends_file(cfg)

    assert wrote is True
    assert cfg.exists()
    seed = json.loads(cfg.read_text(encoding="utf-8"))
    # All entries are negotiate mode, no key, no header (these are the
    # X-Punch-Auth-era fields and don't belong on a negotiate entry).
    assert seed["backends"], "seed must have at least one backend"
    for b in seed["backends"]:
        assert b.get("auth") == "negotiate", (
            f"backend {b['name']!r}: expected auth=negotiate, got {b.get('auth')!r}"
        )
        assert "key" not in b, (
            f"backend {b['name']!r}: Kerberos seed must not embed any key"
        )


def test_seed_writes_x_punch_auth_template_when_key_present(tmp_path, monkeypatch):
    """PUNCH_SAP_KEY set -> every backend seeded with auth=x-punch-auth + key."""
    shim = _import_shim()
    test_key = "ak_test_padded_to_pass_placeholder_guard_xxxx"
    monkeypatch.setattr(shim, "PUNCH_SAP_KEY", test_key)

    cfg = tmp_path / "backends.json"
    wrote = shim._maybe_seed_backends_file(cfg)

    assert wrote is True
    seed = json.loads(cfg.read_text(encoding="utf-8"))
    for b in seed["backends"]:
        assert b["auth"] == "x-punch-auth"
        assert b["header"] == "X-Punch-Auth"
        assert b["key"] == test_key


def test_seed_does_not_overwrite_existing_file(tmp_path, monkeypatch):
    """User authority always wins — an existing backends.json is never
    clobbered, regardless of which branch would otherwise fire."""
    shim = _import_shim()
    monkeypatch.setattr(shim, "PUNCH_SAP_KEY", "")   # Kerberos branch

    cfg = tmp_path / "backends.json"
    custom = {"backends": [{"name": "custom", "url": "http://x:1", "auth": "negotiate"}],
              "primary": "custom"}
    cfg.write_text(json.dumps(custom), encoding="utf-8")

    wrote = shim._maybe_seed_backends_file(cfg)

    assert wrote is False
    # File contents identical
    on_disk = json.loads(cfg.read_text(encoding="utf-8"))
    assert on_disk == custom


def test_seed_kerberos_template_uses_ai_punchpowertrain_default(tmp_path, monkeypatch):
    """v3.0.2 sanity: the URL baked into the no-key seed is ai., not the v1
    hostname. Catches the auto-seed -> _DEFAULT_BACKENDS_TEMPLATE
    consistency that v3.0.0 also flipped."""
    shim = _import_shim()
    monkeypatch.setattr(shim, "PUNCH_SAP_KEY", "")

    cfg = tmp_path / "backends.json"
    shim._maybe_seed_backends_file(cfg)

    seed = json.loads(cfg.read_text(encoding="utf-8"))
    for b in seed["backends"]:
        assert "ai.punchpowertrain.com" in b["url"], (
            f"backend {b['name']!r}: URL {b['url']!r} should reference v2 hostname"
        )
        assert "mcp.punchpowertrain.com" not in b["url"], (
            f"backend {b['name']!r}: URL must not reference v1 hostname"
        )
