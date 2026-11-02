"""A failed discovery must not shrink the backend set: the shim keeps the last good one.

THE INCIDENT, recurring. A laptop's backends.json holds only sap + zabbix; rd, soc and tutorials come
from the supervisor's discovery endpoint at import. When that one GET fails (shim.log `discovery_skipped`
ReadTimeout on 2026-10-23 and 2026-11-02, ConnectError on 2026-10-25), the shim starts with 2 backends and
312 tools instead of 405. Claude Desktop ignores notifications/tools/list_changed (proven with shim 3.6.3),
so the short list stays for the whole Desktop session -- every windchill_*, polarion_*, svn_*, soc_* and
tutorials tool is missing until a full tray Quit.

The fix: every good discovery is saved to disk, and a failed one falls back to that saved set. A good
discovery still replaces the saved set, so a backend the supervisor retires (render, 2026-11-02) goes away.
"""
from __future__ import annotations

import importlib
import json
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

URL = "http://supervisor.example:3030/api/discover/backends"

LIVE = {"version": 1, "backends": [
    {"name": "sap", "url": "http://ai:3000", "auth": "negotiate"},
    {"name": "zabbix", "url": "http://ai:3002", "auth": "negotiate"},
    {"name": "rd", "url": "http://ai:3010", "auth": "negotiate"},
    {"name": "tutorials", "url": "http://ai:3015", "auth": "negotiate"},
    {"name": "soc", "url": "http://ai:3020", "auth": "negotiate"},
]}


def _shim():
    return importlib.import_module("shim_server")


@pytest.fixture
def shim(monkeypatch, tmp_path):
    """The shim with discovery ENABLED and its state directory isolated to this test."""
    s = _shim()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(s, "_DISCOVERY_URL", URL)
    return s


def _laptop(shim):
    """What backends.json gives a real laptop: sap + zabbix only."""
    return [shim.Backend(name="sap", url="http://ai:3000", header="X-Punch-Auth", key="", auth="negotiate"),
            shim.Backend(name="zabbix", url="http://ai:3002", header="X-Punch-Auth", key="", auth="negotiate")]


def _discover(shim, monkeypatch, payload):
    monkeypatch.setattr(shim, "_fetch_discovery", lambda: payload)
    return shim._discover_backends(_laptop(shim))


# --- the fallback ----------------------------------------------------------------------------------

def test_a_failed_discovery_keeps_the_last_good_backend_set(shim, monkeypatch, caplog):
    """THE BUG. Without the fallback this returns sap + zabbix, and the session never gets rd, soc or
    tutorials back."""
    _discover(shim, monkeypatch, LIVE)
    with caplog.at_level(logging.WARNING):
        after = _discover(shim, monkeypatch, None)
    assert [b.name for b in after] == ["sap", "zabbix", "rd", "tutorials", "soc"]
    soc = next(b for b in after if b.name == "soc")
    assert (soc.url, soc.auth, soc.key) == ("http://ai:3020", "negotiate", ""), \
        "a restored backend must be built exactly like a discovered one"
    assert any("discovery_used_saved_set" in r.getMessage() for r in caplog.records), \
        "running on a saved set must be visible in shim.log"


def test_a_malformed_discovery_answer_also_uses_the_saved_set(shim, monkeypatch):
    _discover(shim, monkeypatch, LIVE)
    after = _discover(shim, monkeypatch, {"version": 1, "oops": True})
    assert "soc" in [b.name for b in after]


def test_an_answer_with_no_usable_backend_neither_overwrites_nor_shrinks_the_saved_set(shim, monkeypatch):
    """An empty list from the supervisor is a supervisor fault, not "no backends exist". Saving it would
    wipe the good set and shrink every later start that cannot reach the supervisor."""
    _discover(shim, monkeypatch, LIVE)
    after = _discover(shim, monkeypatch, {"version": 1, "backends": [{"name": "", "url": ""}]})
    assert "soc" in [b.name for b in after]
    after = _discover(shim, monkeypatch, None)
    assert "soc" in [b.name for b in after], "the saved set must still hold soc"


def test_the_local_backend_list_still_wins_over_the_saved_set(shim, monkeypatch):
    """Additive-only, the same rule as live discovery: a name backends.json already has is never
    overridden."""
    _discover(shim, monkeypatch, {"backends": [{"name": "sap", "url": "http://ELSEWHERE:1", "auth": "negotiate"}]})
    after = _discover(shim, monkeypatch, None)
    assert [(b.name, b.url) for b in after] == [("sap", "http://ai:3000"), ("zabbix", "http://ai:3002")]


# --- a good discovery stays authoritative ------------------------------------------------------------

def test_a_good_discovery_replaces_the_saved_set_so_a_retired_backend_goes(shim, monkeypatch):
    """render was retired on 2026-11-02. A saved set that only ever grew would bring it back on every
    start that cannot reach the supervisor."""
    with_render = {"backends": LIVE["backends"] + [{"name": "render", "url": "http://ai:3005", "auth": "negotiate"}]}
    _discover(shim, monkeypatch, with_render)
    _discover(shim, monkeypatch, LIVE)
    after = _discover(shim, monkeypatch, None)
    assert "render" not in [b.name for b in after]


def test_a_good_discovery_still_wins_over_the_saved_set(shim, monkeypatch):
    _discover(shim, monkeypatch, LIVE)
    moved = {"backends": [{"name": "soc", "url": "http://ai:4020", "auth": "negotiate"}]}
    after = _discover(shim, monkeypatch, moved)
    assert [(b.name, b.url) for b in after if b.name not in ("sap", "zabbix")] == [("soc", "http://ai:4020")]


# --- when the saved set must NOT be used -------------------------------------------------------------

def test_with_no_saved_set_a_failed_discovery_keeps_the_local_list(shim, monkeypatch, caplog):
    """First start on a new version, before any good discovery: exactly the old behaviour, and logged."""
    with caplog.at_level(logging.WARNING):
        after = _discover(shim, monkeypatch, None)
    assert [b.name for b in after] == ["sap", "zabbix"]
    assert any("discovery_unavailable" in r.getMessage() for r in caplog.records)


def test_disabled_discovery_never_uses_the_saved_set(shim, monkeypatch):
    """PUNCH_SHIM_DISCOVERY_URL="" means "backends.json only" -- the test suite and the ops escape hatch
    rely on that."""
    _discover(shim, monkeypatch, LIVE)
    monkeypatch.setattr(shim, "_DISCOVERY_URL", "")
    after = _discover(shim, monkeypatch, None)
    assert [b.name for b in after] == ["sap", "zabbix"]


def test_a_set_saved_from_another_discovery_url_is_not_used(shim, monkeypatch):
    """A set learned from a test supervisor must not leak into a start pointed at the real one."""
    _discover(shim, monkeypatch, LIVE)
    monkeypatch.setattr(shim, "_DISCOVERY_URL", "http://other-supervisor:3030/api/discover/backends")
    after = _discover(shim, monkeypatch, None)
    assert [b.name for b in after] == ["sap", "zabbix"]


@pytest.mark.parametrize("content", ["not json {", "[]", '{"cache_version": 999, "backends": []}',
                                     '{"cache_version": 1, "discovery_url": "%s", "backends": "x"}' % URL,
                                     '{"cache_version": 1, "discovery_url": "%s", '
                                     '"backends": [{"name": 5, "url": ["x"], "auth": null}]}' % URL])
def test_a_damaged_saved_file_is_ignored_and_never_raises(shim, monkeypatch, tmp_path, content):
    """This read runs at import. A raise here would stop the shim from starting at all, on every launch,
    which is far worse than the short list it exists to prevent."""
    path = shim._discovered_backends_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    after = _discover(shim, monkeypatch, None)
    assert [b.name for b in after] == ["sap", "zabbix"]


def test_a_failed_save_never_breaks_discovery(shim, monkeypatch):
    """The save is best effort, like the catalogue cache: a locked or read-only file must not cost the
    live result."""
    def _boom(*a, **k):
        raise PermissionError("locked")
    monkeypatch.setattr(shim.os, "replace", _boom)
    after = _discover(shim, monkeypatch, LIVE)
    assert "soc" in [b.name for b in after]


def test_the_saved_file_lives_beside_the_catalogue_cache(shim):
    assert shim._discovered_backends_path().parent == shim._catalogue_cache_path().parent
    assert shim._discovered_backends_path().name == "discovered_backends.json"


# --- end to end: a fresh import with the supervisor down -----------------------------------------------

def test_a_start_with_the_supervisor_down_registers_the_saved_backends_tools(monkeypatch, tmp_path):
    """The whole path at import, as on a laptop: discovery fails, the saved set restores rd, and the
    catalogue cache gives rd its tools with no network -- so windchill_* is in the very first tools/list,
    the only one Claude Desktop ever reads."""
    rd_url = "http://shim-test.invalid:3"
    state = tmp_path / "PunchAnalytics"
    state.mkdir()
    dead = "http://shim-test.invalid:1/api/discover/backends"
    (state / "discovered_backends.json").write_text(json.dumps({
        "cache_version": 1, "discovery_url": dead, "saved_at": "2026-11-02T12:00:00Z",
        "backends": [{"name": "rd", "url": rd_url, "auth": "negotiate"}],
    }), encoding="utf-8")
    (state / "catalogue_cache.json").write_text(json.dumps({
        "cache_version": 1, "shim_version": "test",
        "backends": {"rd": {"url": rd_url, "tools": [{
            "name": "windchill_list_contexts", "description": "List Windchill contexts.",
            "inputSchema": {"type": "object", "properties": {}, "required": []}}]}},
    }), encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("PUNCH_SHIM_DISCOVERY_URL", dead)
    sys.modules.pop("shim_server", None)
    try:
        shim = importlib.import_module("shim_server")
        assert "rd" in [b.name for b in shim._BACKENDS]
        assert "windchill_list_contexts" in shim._NAME_TO_BACKEND
    finally:
        # The next test must get a shim imported under the suite's own environment again.
        sys.modules.pop("shim_server", None)
