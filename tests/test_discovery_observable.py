"""Discovery must never fail silently, and shim_reload must be able to repair a short backend list.

THE INCIDENT, 2026-09-29. The shim came up with 2 backends where the supervisor publishes 5. `soc`, `rd`
and `tutorials` registered ZERO tools, so soc_*, windchill_*, polarion_*, svn_* and rd_* were all
absent from the session. Three Claude Desktop restarts were spent on it.

What made it expensive was not the fault itself but that it was UNDIAGNOSABLE. Measurement ruled out the
timeout (discovery answered in 0.38-0.98 s, five of five, all five backends), the env var (unset in
Machine, User and Process), late logging (handlers are up 1,300 lines earlier) and the code's position
(both GitHub main and the installed file call it at import). Every exit from _fetch_discovery logs
EXCEPT the empty-URL one -- and the log held nothing at all, so the cause could not be established.

And shim_reload, the tool whose whole purpose is avoiding a restart, could not help: it re-fetches the
catalogue of the backends already loaded, so a short LIST was beyond it. It answered "No changes".
"""
from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _shim(monkeypatch, *, discovery_url: str | None = None):
    """A freshly imported shim. `discovery_url` of "" is the empty-URL case."""
    if discovery_url is not None:
        monkeypatch.setenv("PUNCH_SHIM_DISCOVERY_URL", discovery_url)
    sys.modules.pop("shim_server", None)
    return importlib.import_module("shim_server")


# --- fix 1: every outcome says something -------------------------------------------------------------

def test_an_empty_discovery_url_is_logged_not_silent(monkeypatch, caplog):
    """THE ONE SILENT EXIT. Discovery disabled looked exactly like discovery having run and found
    nothing, and the two need completely different fixes."""
    shim = _shim(monkeypatch, discovery_url="")
    monkeypatch.setattr(shim, "_DISCOVERY_URL", "")
    with caplog.at_level(logging.WARNING):
        assert shim._fetch_discovery() is None
    assert any("discovery_disabled" in r.getMessage() for r in caplog.records), \
        "an empty URL must say so; three restarts were spent on this silence"


def test_discovery_being_unavailable_is_logged(monkeypatch, caplog):
    """Best-effort is right; silent is not. When the supervisor cannot be reached the shim runs on its
    local list, and the session is missing whole backends -- which somebody has to be told."""
    shim = _shim(monkeypatch, discovery_url="")
    monkeypatch.setattr(shim, "_fetch_discovery", lambda: None)
    before = [shim.Backend(name="sap", url="http://x", header="H", key="k", auth="negotiate")]
    with caplog.at_level(logging.WARNING):
        after = shim._discover_backends(before)
    assert [b.name for b in after] == ["sap"], "best effort: the local list survives"
    assert any("discovery_unavailable" in r.getMessage() for r in caplog.records)


def test_discovery_adding_nothing_is_logged_too(monkeypatch, caplog):
    """"It ran and added nothing" and "it never ran" produced the same silence."""
    shim = _shim(monkeypatch, discovery_url="")
    monkeypatch.setattr(shim, "_fetch_discovery",
                        lambda: {"backends": [{"name": "sap", "url": "http://x", "auth": "negotiate"}]})
    before = [shim.Backend(name="sap", url="http://x", header="H", key="k", auth="negotiate")]
    with caplog.at_level(logging.INFO):
        after = shim._discover_backends(before)
    assert len(after) == 1
    assert any("discovery_added_nothing" in r.getMessage() for r in caplog.records)


def test_a_discovered_backend_is_still_added_and_logged(monkeypatch, caplog):
    """The happy path, so the tests above cannot pass merely because nothing works."""
    shim = _shim(monkeypatch, discovery_url="")
    monkeypatch.setattr(shim, "_fetch_discovery", lambda: {"backends": [
        {"name": "sap", "url": "http://x", "auth": "negotiate"},
        {"name": "soc", "url": "http://ai:3020", "auth": "negotiate"},
    ]})
    before = [shim.Backend(name="sap", url="http://x", header="H", key="k", auth="negotiate")]
    with caplog.at_level(logging.INFO):
        after = shim._discover_backends(before)
    assert sorted(b.name for b in after) == ["sap", "soc"]
    assert any("discovery_merged" in r.getMessage() for r in caplog.records)


# --- fix 2: a reload can repair a short list ---------------------------------------------------------

def test_reload_runs_discovery_so_a_missing_backend_can_come_back(monkeypatch):
    """The gap that made the incident unrecoverable without a restart. shim_reload only re-fetched the
    catalogue of the backends already loaded, so with 2 of 5 present it answered "No changes" -- and the
    one remaining remedy was the restart this tool exists to avoid."""
    shim = _shim(monkeypatch, discovery_url="")
    monkeypatch.setattr(shim, "_BACKENDS",
                        [shim.Backend(name="sap", url="http://x", header="H", key="k", auth="negotiate")])
    monkeypatch.setattr(shim, "_fetch_discovery", lambda: {"backends": [
        {"name": "sap", "url": "http://x", "auth": "negotiate"},
        {"name": "soc", "url": "http://ai:3020", "auth": "negotiate"},
        {"name": "rd", "url": "http://ai:3010", "auth": "negotiate"},
    ]})
    reloaded = {}
    monkeypatch.setattr(shim, "_apply_catalogue_reload",
                        lambda: reloaded.setdefault("r", {"added": [], "removed": [], "changed": [],
                                                          "moved": False, "total_registered": 0,
                                                          "fetch_failures": [], "register_failures": []}))
    monkeypatch.setattr(shim, "_apply_auth_directives_async", lambda *a, **k: None)

    import asyncio

    class _Session:
        async def send_tool_list_changed(self):
            pass

    class _Ctx:
        session = _Session()

    asyncio.run(shim.shim_reload(_Ctx()))
    assert sorted(b.name for b in shim._BACKENDS) == ["rd", "sap", "soc"], \
        "a reload must be able to bring a missing backend back"


def test_reload_resolves_auth_for_a_backend_that_has_just_appeared(monkeypatch):
    """A backend that appears mid-reload has no auth directive yet. Fetching its catalogue first would
    send /tools out on the wrong scheme, and the reload would report it as a failure instead of adding
    its tools -- which is how the soc backend's 401s read in the log."""
    shim = _shim(monkeypatch, discovery_url="")
    monkeypatch.setattr(shim, "_BACKENDS", [])
    monkeypatch.setattr(shim, "_fetch_discovery",
                        lambda: {"backends": [{"name": "soc", "url": "http://ai:3020", "auth": "negotiate"}]})
    order = []
    monkeypatch.setattr(shim, "_apply_auth_directives_async",
                        lambda *a, **k: order.append("auth"))
    monkeypatch.setattr(shim, "_apply_catalogue_reload",
                        lambda: (order.append("catalogue"),
                                 {"added": [], "removed": [], "changed": [], "moved": False,
                                  "total_registered": 0, "fetch_failures": [],
                                  "register_failures": []})[1])

    import asyncio

    class _Ctx:
        class session:
            @staticmethod
            async def send_tool_list_changed():
                pass

    asyncio.run(shim.shim_reload(_Ctx()))
    # The FIRST two, in that order. A third "auth" follows: shim_reload has always re-probed the OIDC
    # directives after the catalogue reload, and that is unchanged. What matters is that a backend which
    # has just appeared gets its directive BEFORE its /tools call, not that nothing else happens.
    assert order[:2] == ["auth", "catalogue"], f"auth must be resolved before /tools is called, got {order}"
