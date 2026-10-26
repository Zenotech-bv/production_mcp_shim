"""Tests for v3.6.3 tool-list change notifications (backlog CAT-2).

Up to v3.6.2 the shim told every client at `initialize` that its tool list never
changes (`tools.listChanged: false`, the mcp SDK default), and the auto-refresh
(_maybe_refresh_catalogue) rebuilt the registry without telling the client. Only
a manual `shim_reload` sent `notifications/tools/list_changed`. A client may
ignore a change signal from a server that said it would never send one, so a
pa_v2 deploy's new tools could stay invisible.

These tests pin both halves of the fix:
  * the real stdio entry point advertises `tools.listChanged: true`;
  * an auto-refresh that moves the catalogue sends exactly ONE
    notifications/tools/list_changed, and a refresh that moves nothing sends none.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import anyio
import httpx
import mcp.types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

SHIM_PATH = Path(__file__).resolve().parent.parent / "shim_server.py"
PROBE_TOOL = "cat2_probe"


def _import_shim():
    return importlib.import_module("shim_server")


# ---------------------------------------------------------- capability

def test_stdio_initialize_advertises_tools_list_changed():
    """Launch shim_server.py exactly as Claude Desktop does (a stdio subprocess
    running __main__) and read the capabilities it answers `initialize` with."""
    env = dict(os.environ)          # the conftest's hermetic env (no network, temp log)
    env["PUNCH_SHIM_WARMUP"] = "0"  # no background self-update / manifest heal
    params = StdioServerParameters(command=sys.executable, args=[str(SHIM_PATH)],
                                   env=env, cwd=str(SHIM_PATH.parent))

    async def _initialize():
        with anyio.fail_after(60):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    return await client.initialize()

    init = anyio.run(_initialize)
    assert init.capabilities.tools is not None
    assert init.capabilities.tools.listChanged is True


# ---------------------------------------------------------- auto-refresh notifies

def _fake_backend_client(stamp):
    """backend.http_client stand-in: GET /health answers `stamp`; the forwarded
    POST fails as unreachable (the refresh runs before it, which is all we need)."""
    class _Resp:
        is_success = True
        def json(self):
            return {"version": stamp[0], "tools": stamp[1]}

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw): return _Resp()
        def post(self, *a, **kw): raise httpx.ConnectError("test: no backend")

    return lambda **kw: _Client()


def _arm_refresh(shim, monkeypatch, calls, *, moved):
    """Make the next tool call run a refresh whose result has `moved`."""
    b = shim._BACKENDS[0]
    monkeypatch.setattr(shim, "_CATALOGUE_PROBE_THROTTLE_S", 0.0)
    monkeypatch.setattr(shim, "_LAST_CATALOGUE_PROBE_MONO", 0.0)
    shim._CATALOGUE_STAMPS.clear()       # unknown baseline -> the probe reloads
    monkeypatch.setattr(b, "http_client", _fake_backend_client(("9.9.9", 999)))

    def _apply(only=None):
        calls.append(only)
        return {"added": ["pa_new"] if moved else [], "removed": [], "changed": [],
                "total": 1, "fetch_failures": [], "register_failures": [],
                "moved": moved}
    monkeypatch.setattr(shim, "_apply_catalogue_reload", _apply)


def _register_probe(shim, monkeypatch):
    """Register one forwarded tool, the kind of call that runs the auto-refresh."""
    b = shim._BACKENDS[0]
    shim._register_one(PROBE_TOOL, {"name": PROBE_TOOL, "description": "probe",
                                    "inputSchema": {"type": "object", "properties": {}}}, b)
    monkeypatch.setitem(shim._NAME_TO_BACKEND, PROBE_TOOL, (b, PROBE_TOOL))


def _call_probe_and_collect(shim) -> list:
    """Call the probe tool over a real in-memory MCP session; return every
    notifications/tools/list_changed the client received."""
    seen: list = []
    arrived = anyio.Event()

    async def on_message(message):
        if (isinstance(message, types.ServerNotification)
                and isinstance(message.root, types.ToolListChangedNotification)):
            seen.append(message.root)
            arrived.set()

    async def _session():
        async with create_connected_server_and_client_session(
                shim.mcp, message_handler=on_message) as client:
            await client.call_tool(PROBE_TOOL, {})
            with anyio.move_on_after(2):  # the notification may trail the result
                await arrived.wait()
            await client.send_ping()      # barrier: anything already sent has landed

    try:
        anyio.run(_session)
    finally:
        shim.mcp.remove_tool(PROBE_TOOL)
    return seen


def test_auto_refresh_delta_sends_one_list_changed(monkeypatch):
    shim = _import_shim()
    calls: list = []
    _arm_refresh(shim, monkeypatch, calls, moved=True)
    _register_probe(shim, monkeypatch)
    seen = _call_probe_and_collect(shim)
    assert calls == [None]  # the auto-refresh ran once, for every backend
    assert len(seen) == 1


def test_auto_refresh_noop_sends_no_list_changed(monkeypatch):
    shim = _import_shim()
    calls: list = []
    _arm_refresh(shim, monkeypatch, calls, moved=False)
    _register_probe(shim, monkeypatch)
    seen = _call_probe_and_collect(shim)
    assert calls == [None]  # a refresh ran ...
    assert seen == []       # ... but nothing moved, so the client is not told


def test_oidc_flip_refetch_delta_sends_one_list_changed(monkeypatch):
    """The v3.6.2 OIDC-flip re-fetch is a catalogue change too."""
    shim = _import_shim()
    calls: list = []
    _arm_refresh(shim, monkeypatch, calls, moved=True)
    b = shim._BACKENDS[0]
    shim._AUTH_FLIP_PENDING.add(b.name)
    _register_probe(shim, monkeypatch)
    seen = _call_probe_and_collect(shim)
    assert calls == [{b.name}]
    assert len(seen) == 1


def test_tools_list_is_logged_with_count_client_and_pid(caplog):
    """Each tools/list the shim answers is logged, so the shim log shows whether
    a client re-pulled the list after a notifications/tools/list_changed."""
    shim = _import_shim()

    async def _session():
        async with create_connected_server_and_client_session(
                shim.mcp, client_info=types.Implementation(name="cat2-client", version="0")) as client:
            return await client.list_tools()

    with caplog.at_level("INFO", logger="punch_shim"):
        listed = anyio.run(_session)
    served = [r.extra_fields for r in caplog.records if r.msg == "tools_list_served"]
    assert served == [{"tool_count": len(listed.tools), "client": "cat2-client",
                       "pid": os.getpid()}]


def test_refresh_outside_a_request_does_not_raise(monkeypatch):
    """No MCP request in progress (no session, no event loop): the refresh still
    runs and returns its result; the notification is skipped, never raised."""
    shim = _import_shim()
    calls: list = []
    _arm_refresh(shim, monkeypatch, calls, moved=True)
    result = shim._maybe_refresh_catalogue()
    assert calls == [None]
    assert result is not None and result["moved"] is True
