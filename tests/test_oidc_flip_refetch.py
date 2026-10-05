"""v3.6.2 — re-fetch /tools after the background OIDC directive flips a backend.

Seen 2026-10-05: at startup the shim fetched render-adapter's GET /tools with the
static negotiate auth BEFORE the background /auth/mode pass flipped that backend
to OIDC. The backend accepts only OIDC / X-Punch-Auth, so it answered 401 and the
shim registered 0 tools for it. The flip then happened, but the catalogue watcher
only re-fetches on a /health (version, tool_count) delta, and /health had not
changed — so the 0 stuck until a manual shim_reload. rd and soc showed the same.

The fix: a backend the directive newly flips to "oidc" is queued, and the next
_maybe_refresh_catalogue (on the event-loop thread, so the FastMCP registry is
never mutated from the OIDC daemon thread) re-fetches exactly those backends.
"""
from __future__ import annotations

import importlib


def _shim():
    return importlib.import_module("shim_server")


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self.is_success = (status == 200)
        self._payload = payload

    def json(self):
        return self._payload


def _routing_client(backend, tools):
    """A fake http_client that answers like render-adapter does: /tools is 401
    unless the backend's effective_auth is "oidc"; /health is open and constant."""
    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def get(self, path, **kw):
            if path == "/health":
                return _Resp(200, {"version": "1.0", "tools": len(tools)})
            if path == "/tools":
                if backend.effective_auth != "oidc":
                    return _Resp(401, {"detail": "unauthorized"})
                return _Resp(200, {"tools": tools, "server_version": "1.0"})
            return _Resp(404, {})

    return lambda **kw: _Client()


def _isolate(shim, monkeypatch, backend):
    monkeypatch.setattr(shim, "_BACKENDS", [backend])
    monkeypatch.setattr(shim, "_REGISTRATIONS", [])
    monkeypatch.setattr(shim, "_NAME_TO_BACKEND", {})
    monkeypatch.setattr(shim, "_CATALOGUE_STAMPS", {})
    monkeypatch.setattr(shim, "_AUTH_FLIP_PENDING", set(), raising=False)
    monkeypatch.setattr(shim, "_write_catalogue_cache", lambda backends: None)
    registered = []
    monkeypatch.setattr(shim, "_register_one",
                        lambda name, tool, b: registered.append(name))
    monkeypatch.setattr(shim, "_local_windows_upn", lambda: "a@p.com")
    monkeypatch.setattr(shim, "_query_auth_mode", lambda url, upn: "oidc")
    monkeypatch.setattr(shim, "_oidc_acquire_token",
                        lambda upn, *, allow_interactive: "AT")
    return registered


def test_oidc_flip_refetches_backend_that_401d_at_startup(monkeypatch):
    shim = _shim()
    b = shim.Backend(name="render", url="http://render.invalid:3005",
                     header="X-Punch-Auth", key="", auth="negotiate")
    tools = [{"name": "render_status", "description": "d",
              "inputSchema": {"type": "object", "properties": {}}}]
    monkeypatch.setattr(b, "http_client", _routing_client(b, tools))
    registered = _isolate(shim, monkeypatch, b)

    # Startup order that broke: the /tools fetch on negotiate -> 401 -> nothing.
    assert shim._fetch_tools_for_backend(b) is None
    # The first tool call reconciles the unseeded /health baseline. Still on
    # negotiate, so this re-fetch also 401s, and the stamp is now seeded.
    monkeypatch.setattr(shim, "_LAST_CATALOGUE_PROBE_MONO", 0.0)
    monkeypatch.setattr(shim, "_CATALOGUE_PROBE_THROTTLE_S", 0.0)
    shim._maybe_refresh_catalogue()
    assert b.tools == [] and registered == []

    # The background OIDC pass now flips the backend.
    shim._apply_auth_directives([b])
    assert b.effective_auth == "oidc"

    # The next tool call — even inside the /health throttle window, with an
    # unchanged /health stamp — must re-fetch the flipped backend.
    monkeypatch.setattr(shim, "_CATALOGUE_PROBE_THROTTLE_S", 999.0)
    import time
    monkeypatch.setattr(shim, "_LAST_CATALOGUE_PROBE_MONO", time.monotonic())
    result = shim._maybe_refresh_catalogue()

    assert result is not None
    assert [t["name"] for t in b.tools] == ["render_status"]
    assert registered, "the flipped backend's tools were never registered"
    # One-shot: the queue is drained, so the next call does not re-fetch again.
    assert shim._maybe_refresh_catalogue() is None


def test_reprobe_that_keeps_oidc_does_not_queue_a_refetch(monkeypatch):
    """shim_reload re-runs the directive over the same Backend objects. A backend
    that was already on OIDC must not be queued again, or every shim_reload would
    cost a second /tools round trip."""
    shim = _shim()
    b = shim.Backend(name="render", url="http://render.invalid:3005",
                     header="X-Punch-Auth", key="", auth="negotiate")
    _isolate(shim, monkeypatch, b)

    shim._apply_auth_directives([b])
    assert shim._AUTH_FLIP_PENDING == {"render"}
    shim._AUTH_FLIP_PENDING.clear()

    shim._apply_auth_directives([b])          # reprobe, still oidc
    assert shim._AUTH_FLIP_PENDING == set()


def test_flip_refetch_touches_only_flipped_backends(monkeypatch):
    """sap and zabbix work today. Healing render must not re-fetch them."""
    shim = _shim()
    render = shim.Backend(name="render", url="http://render.invalid:3005",
                          header="X-Punch-Auth", key="", auth="negotiate")
    sap = shim.Backend(name="sap", url="http://sap.invalid:3000",
                       header="X-Punch-Auth", key="k" * 40, auth="x-punch-auth")
    sap.tools = [{"name": "pa_x", "description": "d",
                  "inputSchema": {"type": "object", "properties": {}}}]
    _isolate(shim, monkeypatch, render)
    monkeypatch.setattr(shim, "_BACKENDS", [render, sap])

    fetched = []

    def _fetch(backend):
        fetched.append(backend.name)
        return [{"name": f"{backend.name}_t", "description": "d",
                 "inputSchema": {"type": "object", "properties": {}}}]
    monkeypatch.setattr(shim, "_fetch_tools_for_backend", _fetch)

    shim._apply_auth_directives([render, sap])
    import time
    monkeypatch.setattr(shim, "_CATALOGUE_PROBE_THROTTLE_S", 999.0)
    monkeypatch.setattr(shim, "_LAST_CATALOGUE_PROBE_MONO", time.monotonic())
    shim._maybe_refresh_catalogue()

    assert fetched == ["render"]
    assert [t["name"] for t in sap.tools] == ["pa_x"]
