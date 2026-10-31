"""Two backends on the same URL are one backend (v3.6.4).

Discovery advertises pa_v2 as ``sap``; a backends.json that already lists the
same ``:3000`` URL as ``pa_v2`` kept both, because every merge keyed on the
backend NAME. Each copy returned the identical ~240-tool catalogue, so the
registry saw every tool name twice and registered both prefixed forms:
``registered_count 485, collision_count 239`` in a live shim.log.

The fix: a backend whose normalised URL is already loaded is skipped (one log
line). Name-collision handling for DIFFERENT URLs is unchanged.
"""
from __future__ import annotations

import importlib
import json
from unittest.mock import patch


def _shim():
    return importlib.import_module("shim_server")


def _backend(shim, name, url, tools=None):
    b = shim.Backend(name=name, url=url, header="X-Punch-Auth", key="",
                     auth="negotiate")
    if tools is not None:
        b.tools = tools
    return b


def _tools(*names):
    return [{"name": n, "description": "", "inputSchema": {}} for n in names]


def _skips(log_mock):
    """The backend_duplicate_url_skipped events from a patched _log_event."""
    return [c.kwargs for c in log_mock.call_args_list
            if c.args and c.args[0] == "backend_duplicate_url_skipped"]


# -- _normalise_backend_url --------------------------------------------------

def test_normalise_is_case_insensitive_on_host_and_scheme():
    n = _shim()._normalise_backend_url
    assert n("HTTP://AI.PunchPowertrain.com:3000") == n("http://ai.punchpowertrain.com:3000")


def test_normalise_ignores_trailing_slash():
    n = _shim()._normalise_backend_url
    assert n("http://x:3000/") == n("http://x:3000")
    assert n("http://x:3000/mcp/") == n("http://x:3000/mcp")


def test_normalise_keeps_port_and_path_distinct():
    n = _shim()._normalise_backend_url
    assert n("http://x:3000") != n("http://x:3002")
    assert n("http://x:3000/a") != n("http://x:3000/b")
    assert n("http://x:3000") != n("https://x:3000")


def test_normalise_keeps_path_case():
    """Only the host is case-insensitive; a URL path is case-sensitive."""
    n = _shim()._normalise_backend_url
    assert n("http://x:3000/Mcp") != n("http://x:3000/mcp")


# -- discovery ----------------------------------------------------------------

def test_discover_skips_backend_on_already_loaded_url():
    """The live defect: local ``pa_v2`` on :3000, discovery advertises ``sap``
    on the same :3000. Only the local one survives; one line is logged."""
    shim = _shim()
    local = [_backend(shim, "pa_v2", "http://ai.punchpowertrain.com:3000")]
    payload = {"version": 1, "backends": [
        {"name": "sap",    "url": "http://AI.punchpowertrain.com:3000/", "auth": "negotiate"},
        {"name": "newsvc", "url": "http://ai.punchpowertrain.com:3009",  "auth": "negotiate"},
    ]}
    with patch.object(shim, "_fetch_discovery", return_value=payload), \
            patch.object(shim, "_log_event") as log:
        merged = shim._discover_backends(local)
    assert [b.name for b in merged] == ["pa_v2", "newsvc"]
    skipped = _skips(log)
    assert len(skipped) == 1
    assert skipped[0]["backend"] == "sap"
    assert skipped[0]["duplicate_of"] == "pa_v2"


def test_discover_skips_two_discovered_entries_on_same_url():
    shim = _shim()
    payload = {"version": 1, "backends": [
        {"name": "a", "url": "http://x:3009", "auth": "negotiate"},
        {"name": "b", "url": "http://x:3009/", "auth": "negotiate"},
    ]}
    with patch.object(shim, "_fetch_discovery", return_value=payload):
        merged = shim._discover_backends([])
    assert [b.name for b in merged] == ["a"]


# -- backends.json --------------------------------------------------------------

def test_file_load_skips_second_entry_on_same_url(tmp_path):
    """The shim's own docstring example puts ``sap`` and ``supervisor-webhook``
    on one URL. The first entry wins; the second is skipped and logged."""
    shim = _shim()
    p = tmp_path / "backends.json"
    p.write_text(json.dumps({
        "backends": [
            {"name": "sap", "url": "http://ai.punchpowertrain.com:3000", "auth": "negotiate"},
            {"name": "supervisor-webhook", "url": "http://ai.punchpowertrain.com:3000/",
             "auth": "negotiate"},
            {"name": "zabbix", "url": "http://ai.punchpowertrain.com:3002", "auth": "negotiate"},
        ],
        "primary": "sap",
    }), encoding="utf-8")
    with patch.object(shim, "_log_event") as log:
        backends, primary = shim._load_backends_from_file(p)
    assert [b.name for b in backends] == ["sap", "zabbix"]
    assert primary == "sap"
    skipped = _skips(log)
    assert len(skipped) == 1
    assert skipped[0]["backend"] == "supervisor-webhook"
    assert skipped[0]["duplicate_of"] == "sap"


def test_file_load_primary_naming_a_skipped_entry_falls_back_to_first(tmp_path):
    """``primary`` naming the skipped duplicate falls back to the first backend,
    as for any unknown primary. The survivor has the same URL, so auto-update's
    server fallback still reaches the same host."""
    shim = _shim()
    p = tmp_path / "backends.json"
    p.write_text(json.dumps({
        "backends": [
            {"name": "pa_v2", "url": "http://x:3000", "auth": "negotiate"},
            {"name": "sap",   "url": "http://x:3000", "auth": "negotiate"},
        ],
        "primary": "sap",
    }), encoding="utf-8")
    backends, primary = shim._load_backends_from_file(p)
    assert [b.name for b in backends] == ["pa_v2"]
    assert primary == "pa_v2"


# -- registry ---------------------------------------------------------------------

def test_registry_after_dedupe_has_each_tool_once():
    """End to end: file backend + discovery on the same URL -> every tool is
    registered once, under its bare name, with no collisions."""
    shim = _shim()
    names = ("pa_table_list", "pa_whoami", "pa_dso")
    local = [_backend(shim, "pa_v2", "http://x:3000", tools=_tools(*names))]
    payload = {"version": 1, "backends": [
        {"name": "sap", "url": "http://x:3000", "auth": "negotiate"},
    ]}
    with patch.object(shim, "_fetch_discovery", return_value=payload):
        merged = shim._discover_backends(local)
    name_to_backend, registrations = shim._build_merged_registry(merged, fetch_missing=False)
    assert sorted(r[0] for r in registrations) == sorted(names)
    assert set(name_to_backend) == set(names)


def test_registry_name_collision_on_different_urls_still_prefixes():
    """Unchanged behaviour: the same tool name on two DIFFERENT URLs drops the
    bare alias and registers both prefixed forms."""
    shim = _shim()
    backends = [
        _backend(shim, "sap",    "http://x:3000", tools=_tools("ping")),
        _backend(shim, "zabbix", "http://x:3002", tools=_tools("ping")),
    ]
    name_to_backend, registrations = shim._build_merged_registry(backends, fetch_missing=False)
    assert sorted(r[0] for r in registrations) == ["sap_ping", "zabbix_ping"]
    assert "ping" not in name_to_backend
