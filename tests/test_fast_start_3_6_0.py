"""v3.6.0 — fast, non-blocking startup.

The serial per-backend network I/O that hung Claude Desktop's Extensions panel
(discovery, self-update, GET /tools + GET /health per backend, all BEFORE
mcp.run()) moved off the critical path. These tests pin the new machinery:

  * a disk catalogue cache round-trips and is versioned;
  * cache load populates backend.tools (url-mismatch invalidates an entry);
  * cold-start /tools fetch runs in parallel and honours the sap bundled
    fallback;
  * _build_merged_registry(fetch_missing=False) never touches the network;
  * _startup_warmup runs every step best-effort and never raises;
  * _start_background_warmup respects the PUNCH_SHIM_WARMUP kill-switch.
"""
from __future__ import annotations

import importlib

import pytest


def _shim():
    return importlib.import_module("shim_server")


def _mk(shim, name, url="http://mcp.example.com:3000", auth="negotiate", key=""):
    return shim.Backend(name=name, url=url, header="X-Punch-Auth", key=key, auth=auth)


# ------------------------------------------------------------- disk cache


def test_catalogue_cache_roundtrip(tmp_path, monkeypatch):
    shim = _shim()
    monkeypatch.setattr(shim, "_catalogue_cache_path", lambda: tmp_path / "cat.json")
    b = _mk(shim, "rd")
    b.tools = [{"name": "windchill_list_contexts", "inputSchema": {}}]
    shim._write_catalogue_cache([b])

    data = shim._read_catalogue_cache()
    assert data["cache_version"] == shim._CATALOGUE_CACHE_VERSION
    assert data["backends"]["rd"]["url"] == b.url
    assert data["backends"]["rd"]["tools"][0]["name"] == "windchill_list_contexts"


def test_cache_read_rejects_wrong_version(tmp_path, monkeypatch):
    shim = _shim()
    p = tmp_path / "cat.json"
    monkeypatch.setattr(shim, "_catalogue_cache_path", lambda: p)
    import json
    p.write_text(json.dumps({"cache_version": 999, "backends": {}}), encoding="utf-8")
    assert shim._read_catalogue_cache() is None


def test_cache_read_absent_is_none(tmp_path, monkeypatch):
    shim = _shim()
    monkeypatch.setattr(shim, "_catalogue_cache_path", lambda: tmp_path / "nope.json")
    assert shim._read_catalogue_cache() is None


def test_write_omits_empty_tool_backends(tmp_path, monkeypatch):
    """A backend that transiently fetched nothing must not overwrite a good
    cached catalogue with an empty set."""
    shim = _shim()
    monkeypatch.setattr(shim, "_catalogue_cache_path", lambda: tmp_path / "cat.json")
    full = _mk(shim, "rd"); full.tools = [{"name": "t"}]
    empty = _mk(shim, "zabbix"); empty.tools = []
    shim._write_catalogue_cache([full, empty])
    data = shim._read_catalogue_cache()
    assert set(data["backends"].keys()) == {"rd"}


def test_load_catalogue_into_backends_populates(monkeypatch):
    shim = _shim()
    b = _mk(shim, "rd")
    assert b.tools == []
    monkeypatch.setattr(shim, "_read_catalogue_cache", lambda: {
        "cache_version": shim._CATALOGUE_CACHE_VERSION,
        "backends": {"rd": {"url": b.url, "tools": [{"name": "t1"}]}},
    })
    n = shim._load_catalogue_into_backends([b])
    assert n == 1
    assert b.tools == [{"name": "t1"}]


def test_load_catalogue_skips_url_mismatch(monkeypatch):
    """A backend whose URL moved must NOT be served the old url's catalogue."""
    shim = _shim()
    b = _mk(shim, "rd", url="http://newhost:3010")
    monkeypatch.setattr(shim, "_read_catalogue_cache", lambda: {
        "cache_version": shim._CATALOGUE_CACHE_VERSION,
        "backends": {"rd": {"url": "http://oldhost:3010", "tools": [{"name": "t"}]}},
    })
    assert shim._load_catalogue_into_backends([b]) == 0
    assert b.tools == []


def test_load_catalogue_skips_backend_with_tools(monkeypatch):
    shim = _shim()
    b = _mk(shim, "rd"); b.tools = [{"name": "already"}]
    monkeypatch.setattr(shim, "_read_catalogue_cache", lambda: {
        "cache_version": shim._CATALOGUE_CACHE_VERSION,
        "backends": {"rd": {"url": b.url, "tools": [{"name": "cached"}]}},
    })
    assert shim._load_catalogue_into_backends([b]) == 0
    assert b.tools == [{"name": "already"}]     # not clobbered


def test_load_catalogue_rejects_malformed_tool_elements(monkeypatch):
    """REGRESSION GUARD (review finding #2): a cache whose tools list contains a
    non-dict element (truncated write that still parses to e.g. [null]) must be
    DROPPED for that backend, not partially loaded — otherwise the import-time
    build would crash and brick every launch."""
    shim = _shim()
    b = _mk(shim, "rd")
    monkeypatch.setattr(shim, "_read_catalogue_cache", lambda: {
        "cache_version": shim._CATALOGUE_CACHE_VERSION,
        "backends": {"rd": {"url": b.url, "tools": [{"name": "ok"}, None, "junk"]}},
    })
    assert shim._load_catalogue_into_backends([b]) == 0
    assert b.tools == []


def test_build_merged_registry_skips_non_dict_tools():
    """REGRESSION GUARD (review finding #2): a malformed tool entry (None, str)
    from ANY source must be skipped, never crash the build (which runs at
    import)."""
    shim = _shim()
    b = _mk(shim, "rd")
    b.tools = [{"name": "good"}, None, "junk", {"name": "good2"}, {"noname": 1}]
    n2b, regs = shim._build_merged_registry([b], fetch_missing=False)
    names = sorted(n for n, _t, _bk in regs)
    assert names == ["good", "good2"]     # only the well-formed dicts with a name


# ------------------------------------------------------- parallel fetch


def test_fetch_all_tools_parallel_populates(monkeypatch):
    shim = _shim()
    rd = _mk(shim, "rd")
    zbx = _mk(shim, "zabbix", url="http://mcp.example.com:3002")

    def _fake_fetch(b):
        return [{"name": f"{b.name}_tool"}]
    monkeypatch.setattr(shim, "_fetch_tools_for_backend", _fake_fetch)

    shim._fetch_all_tools_parallel([rd, zbx])
    assert rd.tools == [{"name": "rd_tool"}]
    assert zbx.tools == [{"name": "zabbix_tool"}]


def test_fetch_all_tools_parallel_uses_sap_bundled_fallback(monkeypatch):
    shim = _shim()
    sap = _mk(shim, "sap")
    monkeypatch.setattr(shim, "_fetch_tools_for_backend", lambda b: None)  # live fails
    monkeypatch.setattr(shim, "_load_bundled_sap_fallback",
                        lambda: [{"name": "bundled_sap_tool"}])
    shim._fetch_all_tools_parallel([sap])
    assert sap.tools == [{"name": "bundled_sap_tool"}]


def test_fetch_all_tools_parallel_skips_configured_with_tools_and_unconfigured(monkeypatch):
    shim = _shim()
    has_tools = _mk(shim, "rd"); has_tools.tools = [{"name": "keep"}]
    unconfigured = _mk(shim, "svc", auth="x-punch-auth", key="")  # no key -> not configured
    assert not unconfigured.is_configured
    fetched = []
    monkeypatch.setattr(shim, "_fetch_tools_for_backend",
                        lambda b: fetched.append(b.name) or [{"name": "x"}])
    shim._fetch_all_tools_parallel([has_tools, unconfigured])
    assert fetched == []                     # neither was a fetch target
    assert has_tools.tools == [{"name": "keep"}]


# --------------------------------------------- build_merged_registry no-net


def test_build_merged_registry_no_fetch_when_disabled(monkeypatch):
    shim = _shim()
    b = _mk(shim, "rd")
    assert b.tools == []
    monkeypatch.setattr(shim, "_fetch_tools_for_backend",
                        lambda b: (_ for _ in ()).throw(AssertionError("must not fetch")))
    n2b, regs = shim._build_merged_registry([b], fetch_missing=False)
    assert regs == []                        # empty tools, no network, no registrations


# ------------------------------------------------- import ordering (critical)


def test_discovery_runs_before_registry_build_and_preflight():
    """REGRESSION GUARD — the 2026-07-16 breakage.

    Supervisor discovery DEFINES the backend set: a real laptop's backends.json
    carries only sap + zabbix, and rd (Windchill/Polarion/SVN) + tutorials are
    DISCOVERED. A pre-release v3.6.0 deferred discovery to the background warmup,
    so the registry was built from 2 backends and every discovery-supplied
    backend registered ZERO tools (and the OIDC pre-flight never saw rd).

    This is a static source-ORDER check because the bug is module-level import
    ordering — there is nothing to call, it either runs in the right order at
    import or it doesn't. Exact stripped-line matching so comments can't satisfy it.
    """
    import inspect
    shim = _shim()
    lines = inspect.getsource(shim).splitlines()

    def line_of(stmt):
        for i, l in enumerate(lines):
            if l.strip() == stmt:
                return i
        raise AssertionError(f"module-level statement not found: {stmt!r}")

    disc      = line_of("_BACKENDS = _discover_backends(_BACKENDS)")
    preflight = line_of("_AUTH_PREFLIGHT_THREAD = _apply_auth_directives_async(_BACKENDS)")
    build     = line_of(
        "_NAME_TO_BACKEND, _REGISTRATIONS = _build_merged_registry(_BACKENDS, fetch_missing=False)")

    assert disc < build, (
        "discovery must run BEFORE the registry is built, or discovery-supplied "
        "backends (rd, tutorials) register zero tools")
    assert disc < preflight, (
        "discovery must run BEFORE the OIDC pre-flight, or rd never gets an "
        "auth directive")


# ---------------------------------------------------------- warmup thread


def test_startup_warmup_runs_selfupdate_and_not_discovery(monkeypatch):
    """The warmup carries the expensive, deferrable work (self-update: a GitHub
    fetch, up to ~40s). It must NOT do discovery — that defines the backend set
    and belongs at import (see test_discovery_runs_before_registry_build_*)."""
    shim = _shim()
    monkeypatch.setattr(shim, "_WARMUP_DELAY_S", 0.0)
    seen = {"update": 0}
    monkeypatch.setattr(shim, "_maybe_self_update",
                        lambda: seen.__setitem__("update", 1))
    monkeypatch.setattr(shim, "_heal_manifest_uv_env", lambda p: False)
    monkeypatch.setattr(shim, "_discover_backends",
                        lambda bs: (_ for _ in ()).throw(
                            AssertionError("warmup must NOT run discovery; it belongs at import")))

    shim._startup_warmup()   # must not raise
    assert seen["update"] == 1


def test_startup_warmup_never_touches_the_tool_registry(monkeypatch):
    """REGRESSION GUARD (review finding #1): the warmup runs on a DAEMON thread,
    but sync tool fns / shim_reload mutate FastMCP's _tools dict on the event
    loop thread where list_tools iterates it. So the warmup must NEVER call
    _apply_catalogue_reload / mcp.remove_tool / _register_one — off-loop
    mutation would race list_tools (dictionary-changed-size). The live refresh
    is left to the on-loop _maybe_refresh_catalogue instead."""
    shim = _shim()
    monkeypatch.setattr(shim, "_WARMUP_DELAY_S", 0.0)
    monkeypatch.setattr(shim, "_discover_backends", lambda bs: bs)
    monkeypatch.setattr(shim, "_maybe_self_update", lambda: None)
    monkeypatch.setattr(shim, "_heal_manifest_uv_env", lambda p: False)
    monkeypatch.setattr(shim, "_apply_catalogue_reload",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("warmup must NOT reconcile the registry off-loop")))
    monkeypatch.setattr(shim.mcp, "remove_tool",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("warmup must NOT remove_tool off-loop")))
    shim._startup_warmup()   # must not raise (must not call the guarded funcs)


def test_startup_warmup_survives_a_failing_step(monkeypatch):
    """A raising discovery must not stop self-update, and must never propagate
    out of the warmup."""
    shim = _shim()
    monkeypatch.setattr(shim, "_WARMUP_DELAY_S", 0.0)
    def _boom(bs):
        raise RuntimeError("discovery blew up")
    monkeypatch.setattr(shim, "_discover_backends", _boom)
    later = {"update": 0}
    monkeypatch.setattr(shim, "_maybe_self_update",
                        lambda: later.__setitem__("update", 1))
    monkeypatch.setattr(shim, "_heal_manifest_uv_env", lambda p: False)

    shim._startup_warmup()   # must not raise
    assert later["update"] == 1   # step after the failing one still ran


def test_start_background_warmup_respects_kill_switch(monkeypatch):
    shim = _shim()
    monkeypatch.setattr(shim, "_WARMUP_ENABLED", False)
    assert shim._start_background_warmup() is None


def test_start_background_warmup_starts_daemon_when_enabled(monkeypatch):
    shim = _shim()
    monkeypatch.setattr(shim, "_WARMUP_ENABLED", True)
    ran = {"n": 0}
    monkeypatch.setattr(shim, "_startup_warmup", lambda: ran.__setitem__("n", 1))
    t = shim._start_background_warmup()
    assert t is not None
    t.join(timeout=5)
    assert not t.is_alive()
    assert ran["n"] == 1
