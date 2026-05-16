"""v2.3.1 — backends.json hot-reload (credential rotation).

Pre-2.3.1 a key rotation on the server forced a Claude Desktop restart
on the user's laptop before the new key took effect. v2.3.1 watches
the mtime and re-applies key/url/header on existing Backend objects so
the next request after a rotation already carries the new key.

Companion to v2.3.0's `shim_reload` MCP tool — that one handles tool-
list changes, this one handles credentials. These tests cover the
credential side only.

  * `_reconcile_backends` — the pure mutation function. Key rotation,
    URL change, header change, additions/removals are surfaced in the
    summary; empty-key in the new config is treated as "no change"
    (defends against half-written files mid-edit).

  * `_maybe_reload_backends` — the throttled mtime-aware orchestrator.
    A real file rewrite is detected and reconciled. A malformed JSON
    write is logged and skipped without advancing the tracked mtime
    (so the next throttle window retries).
"""
from __future__ import annotations

import importlib
import json
import os
import time


# --------------------------------------------------------------------- _reconcile_backends


def _import_shim():
    """Import shim_server lazily — it has heavy module-level side effects
    (logging setup, backends load, FastMCP registration). Called per
    test so the conftest env vars are in place."""
    return importlib.import_module("shim_server")


def _make_backend(shim, *, name, url, header, key):
    return shim.Backend(name=name, url=url, header=header, key=key)


def test_reconcile_rotates_key_in_place():
    shim = _import_shim()
    existing = [_make_backend(shim, name="sap",
                              url="http://x:1", header="X-A",
                              key="OLD-key-padded-to-pass-placeholder-guard")]
    new      = [_make_backend(shim, name="sap",
                              url="http://x:1", header="X-A",
                              key="NEW-key-padded-to-pass-placeholder-guard")]

    summary = shim._reconcile_backends(existing, new)

    assert existing[0].key == "NEW-key-padded-to-pass-placeholder-guard"
    assert summary["rotated_keys"] == ["sap"]
    assert summary["url_changes"] == []
    assert summary["header_changes"] == []
    assert summary["added"] == []
    assert summary["removed"] == []


def test_reconcile_changes_url_and_header():
    shim = _import_shim()
    existing = [_make_backend(shim, name="sap",
                              url="http://old:1", header="X-Old",
                              key="K-padded-to-pass-placeholder-guard")]
    new      = [_make_backend(shim, name="sap",
                              url="http://new:2", header="X-New",
                              key="K-padded-to-pass-placeholder-guard")]

    summary = shim._reconcile_backends(existing, new)

    assert existing[0].url == "http://new:2"
    assert existing[0].header == "X-New"
    assert summary["url_changes"] == ["sap"]
    assert summary["header_changes"] == ["sap"]
    assert summary["rotated_keys"] == []   # key didn't change


def test_reconcile_empty_new_key_is_treated_as_no_change():
    """Defensive: a backends.json mid-edit could briefly carry key="" before
    the real value lands. We must NOT clear the in-memory key on that
    transient state — the next reload window picks up the real value."""
    shim = _import_shim()
    existing = [_make_backend(shim, name="sap",
                              url="http://x:1", header="X-A",
                              key="GOOD-key-padded-to-pass-placeholder-guard")]
    new      = [_make_backend(shim, name="sap",
                              url="http://x:1", header="X-A",
                              key="")]   # empty: half-written file

    summary = shim._reconcile_backends(existing, new)

    assert existing[0].key == "GOOD-key-padded-to-pass-placeholder-guard"
    assert summary["rotated_keys"] == []


def test_reconcile_surfaces_added_and_removed_without_mutating_them():
    """Adds + removes are flagged for the caller to log a structural-
    change warning, but `existing` itself isn't appended to or trimmed —
    FastMCP needs `shim_reload` (or a restart) to register/unregister."""
    shim = _import_shim()
    existing = [_make_backend(shim, name="sap",
                              url="http://x:1", header="X-A",
                              key="K-padded-to-pass-placeholder-guard"),
                _make_backend(shim, name="zabbix",
                              url="http://x:2", header="X-A",
                              key="K-padded-to-pass-placeholder-guard")]
    new      = [_make_backend(shim, name="sap",      # zabbix removed
                              url="http://x:1", header="X-A",
                              key="K-padded-to-pass-placeholder-guard"),
                _make_backend(shim, name="jira",     # jira added
                              url="http://x:3", header="X-A",
                              key="K-padded-to-pass-placeholder-guard")]

    summary = shim._reconcile_backends(existing, new)

    assert summary["added"]   == ["jira"]
    assert summary["removed"] == ["zabbix"]
    # The `existing` list itself was NOT mutated by add/remove.
    assert [b.name for b in existing] == ["sap", "zabbix"]


# --------------------------------------------------------------------- _maybe_reload_backends


def _set_file_mtime(path, when):
    """Set both atime + mtime so the stat-based change detection sees
    `when` as the new mtime."""
    os.utime(str(path), (when, when))


def test_maybe_reload_picks_up_a_key_rotation(backends_cfg):
    """End-to-end: rewrite backends.json with a new key, call
    _maybe_reload_backends, verify the in-memory backend's key changed."""
    shim = _import_shim()

    sap = next(b for b in shim._BACKENDS if b.name == "test_backend")
    assert sap.key.startswith("INITIAL-KEY")

    # Reset the throttle counter so this synthetic call isn't skipped.
    shim._LAST_RELOAD_CHECK_MONO = 0.0

    rotated = "ROTATED-key-padded-to-pass-the-placeholder-guard-and-min-len"
    backends_cfg.write_text(json.dumps({
        "backends": [{
            "name":   "test_backend",
            "url":    "http://shim-test.invalid:1",
            "header": "X-Punch-Auth",
            "key":    rotated,
        }],
        "primary": "test_backend",
    }), encoding="utf-8")
    _set_file_mtime(backends_cfg, time.time() + 5)   # forward, definitively newer

    shim._maybe_reload_backends()

    sap = next(b for b in shim._BACKENDS if b.name == "test_backend")
    assert sap.key == rotated, "hot-reload didn't rotate the in-memory key"


def test_maybe_reload_skips_when_mtime_unchanged(backends_cfg):
    """No mtime change → no reload work. We assert by mutating the file's
    bytes WITHOUT touching mtime: the in-memory key should stay on the
    pre-existing value."""
    shim = _import_shim()
    shim._LAST_RELOAD_CHECK_MONO = 0.0

    sap_before = next(b for b in shim._BACKENDS if b.name == "test_backend").key

    backends_cfg.write_text(json.dumps({
        "backends": [{
            "name":   "test_backend",
            "url":    "http://shim-test.invalid:1",
            "header": "X-Punch-Auth",
            "key":    "WOULD-CHANGE-IF-RELOADED-padded-to-pass-placeholder",
        }],
        "primary": "test_backend",
    }), encoding="utf-8")
    _set_file_mtime(backends_cfg, shim._BACKENDS_FILE_MTIME)   # pin

    shim._maybe_reload_backends()

    sap_after = next(b for b in shim._BACKENDS if b.name == "test_backend").key
    assert sap_after == sap_before, "reload fired despite unchanged mtime"


def test_maybe_reload_swallows_malformed_json(backends_cfg):
    """A half-written file (parse error) must not crash the call AND must
    not advance the tracked mtime — so the next throttle window retries."""
    shim = _import_shim()
    shim._LAST_RELOAD_CHECK_MONO = 0.0
    pre_mtime = shim._BACKENDS_FILE_MTIME

    backends_cfg.write_text("{ broken json", encoding="utf-8")
    _set_file_mtime(backends_cfg, time.time() + 5)

    shim._maybe_reload_backends()   # MUST NOT raise

    assert shim._BACKENDS_FILE_MTIME == pre_mtime, \
        "tracker advanced past a malformed file — would skip the recovery write"


def test_call_remote_triggers_reload_before_lookup(backends_cfg, monkeypatch):
    """Smoke: _call_remote calls _maybe_reload_backends before doing
    anything else. Patched so we observe the call without making a real
    HTTP request."""
    shim = _import_shim()
    called: list[bool] = []
    monkeypatch.setattr(shim, "_maybe_reload_backends",
                        lambda: called.append(True))
    shim._call_remote("definitely_not_a_real_tool", {})
    assert called == [True]
