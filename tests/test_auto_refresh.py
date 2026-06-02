"""Tests for v3.3.0 tool-catalogue auto-refresh.

The shim probes each backend's /health (version + tool count) on a throttle and,
on a delta, re-fetches + rebuilds the registry — so a backend deploy that gains
or drops tools self-heals without a manual shim_reload. These tests exercise the
probe parsing, the trigger logic (seed / delta / unchanged / throttle /
unreachable), and that the shim_reload refactor still works.
"""
from __future__ import annotations

import asyncio
import importlib
import json

import httpx


def _import_shim():
    return importlib.import_module("shim_server")


def _fake_http_client(payload=None, *, status=200, raise_exc=None):
    """Return a `lambda **kw: <ctx-manager client>` to monkeypatch
    backend.http_client, mirroring the pattern in test_shim_access.py."""
    class _Resp:
        is_success = (status == 200)
        def json(self):
            if payload is None:
                raise ValueError("no json body")
            return payload

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw):
            if raise_exc:
                raise raise_exc
            return _Resp()

    return lambda **kw: _Client()


# ---------------------------------------------------------- _probe_catalogue_stamp

def test_probe_stamp_parses_health(monkeypatch):
    shim = _import_shim()
    b = shim._BACKENDS[0]
    monkeypatch.setattr(b, "http_client",
                        _fake_http_client({"version": "0.1.15", "tools": 182}))
    assert shim._probe_catalogue_stamp(b) == ("0.1.15", 182)


def test_probe_stamp_unreachable_returns_none(monkeypatch):
    shim = _import_shim()
    b = shim._BACKENDS[0]
    monkeypatch.setattr(b, "http_client",
                        _fake_http_client(raise_exc=httpx.ConnectError("down")))
    assert shim._probe_catalogue_stamp(b) is None


def test_probe_stamp_non_2xx_returns_none(monkeypatch):
    shim = _import_shim()
    b = shim._BACKENDS[0]
    monkeypatch.setattr(b, "http_client",
                        _fake_http_client({"version": "x"}, status=503))
    assert shim._probe_catalogue_stamp(b) is None


def test_probe_stamp_non_pa_v2_payload_returns_none(monkeypatch):
    """A backend whose /health has neither version nor tools (not a pa_v2
    health shape) yields no signal -> never auto-refreshes that backend."""
    shim = _import_shim()
    b = shim._BACKENDS[0]
    monkeypatch.setattr(b, "http_client",
                        _fake_http_client({"status": "ok"}))
    assert shim._probe_catalogue_stamp(b) is None


# ---------------------------------------------------------- _maybe_refresh_catalogue

def _reset(shim):
    shim._CATALOGUE_STAMPS.clear()
    shim._LAST_CATALOGUE_PROBE_MONO = 0.0


def _stub_apply(shim, monkeypatch, calls, *, moved=True):
    def _apply():
        calls.append(1)
        return {"added": ["pa_x"] if moved else [], "removed": [],
                "changed": [], "total": 182, "fetch_failures": [],
                "register_failures": [], "moved": moved}
    monkeypatch.setattr(shim, "_apply_catalogue_reload", _apply)


def test_maybe_refresh_unknown_baseline_reloads_and_seeds(monkeypatch):
    """No baseline (e.g. startup probe failed) -> reconcile reload, seed stamp."""
    shim = _import_shim()
    _reset(shim)
    monkeypatch.setattr(shim, "_CATALOGUE_PROBE_THROTTLE_S", 0.0)
    b = shim._BACKENDS[0]
    monkeypatch.setattr(b, "http_client",
                        _fake_http_client({"version": "1.0", "tools": 10}))
    calls = []
    _stub_apply(shim, monkeypatch, calls)
    shim._maybe_refresh_catalogue()
    assert calls == [1]
    assert shim._CATALOGUE_STAMPS[b.name] == ("1.0", 10)


def test_maybe_refresh_no_reload_when_unchanged(monkeypatch):
    shim = _import_shim()
    _reset(shim)
    monkeypatch.setattr(shim, "_CATALOGUE_PROBE_THROTTLE_S", 0.0)
    b = shim._BACKENDS[0]
    shim._CATALOGUE_STAMPS[b.name] = ("1.0", 10)
    monkeypatch.setattr(b, "http_client",
                        _fake_http_client({"version": "1.0", "tools": 10}))
    calls = []
    _stub_apply(shim, monkeypatch, calls)
    assert shim._maybe_refresh_catalogue() is None
    assert calls == []  # unchanged -> no reload


def test_maybe_refresh_reloads_on_version_delta(monkeypatch):
    shim = _import_shim()
    _reset(shim)
    monkeypatch.setattr(shim, "_CATALOGUE_PROBE_THROTTLE_S", 0.0)
    b = shim._BACKENDS[0]
    shim._CATALOGUE_STAMPS[b.name] = ("0.1.12", 164)
    monkeypatch.setattr(b, "http_client",
                        _fake_http_client({"version": "0.1.15", "tools": 182}))
    calls = []
    _stub_apply(shim, monkeypatch, calls)
    result = shim._maybe_refresh_catalogue()
    assert calls == [1]
    assert result is not None and result["moved"] is True
    assert shim._CATALOGUE_STAMPS[b.name] == ("0.1.15", 182)


def test_maybe_refresh_throttled(monkeypatch):
    """Within the throttle window the probe is skipped entirely (no HTTP, no
    reload), even when a delta would otherwise be found."""
    import time
    shim = _import_shim()
    _reset(shim)
    b = shim._BACKENDS[0]
    shim._CATALOGUE_STAMPS[b.name] = ("0.1.12", 164)
    shim._LAST_CATALOGUE_PROBE_MONO = time.monotonic()  # just probed
    monkeypatch.setattr(shim, "_CATALOGUE_PROBE_THROTTLE_S", 999.0)
    monkeypatch.setattr(b, "http_client",
                        _fake_http_client({"version": "9.9.9", "tools": 999}))
    calls = []
    _stub_apply(shim, monkeypatch, calls)
    assert shim._maybe_refresh_catalogue() is None
    assert calls == []


def test_maybe_refresh_skips_unreachable_backend(monkeypatch):
    """A failed probe is skipped: no reload, and the working catalogue stamp is
    left untouched (never blow away a good catalogue on a transient)."""
    shim = _import_shim()
    _reset(shim)
    monkeypatch.setattr(shim, "_CATALOGUE_PROBE_THROTTLE_S", 0.0)
    b = shim._BACKENDS[0]
    shim._CATALOGUE_STAMPS[b.name] = ("0.1.15", 182)
    monkeypatch.setattr(b, "http_client",
                        _fake_http_client(raise_exc=httpx.ConnectError("down")))
    calls = []
    _stub_apply(shim, monkeypatch, calls)
    assert shim._maybe_refresh_catalogue() is None
    assert calls == []
    assert shim._CATALOGUE_STAMPS[b.name] == ("0.1.15", 182)


# ---------------------------------------------------------- shim_reload refactor

def test_shim_reload_uses_apply_and_notifies(monkeypatch):
    """shim_reload delegates to _apply_catalogue_reload and, when the catalogue
    moved, pushes tools/list_changed and reports the diff."""
    shim = _import_shim()
    monkeypatch.setattr(shim, "_apply_catalogue_reload",
                        lambda: {"added": ["pa_x"], "removed": [], "changed": [],
                                 "total": 5, "fetch_failures": [],
                                 "register_failures": [], "moved": True})
    sent = []

    class _Sess:
        async def send_tool_list_changed(self): sent.append(1)

    class _Ctx:
        session = _Sess()

    data = json.loads(asyncio.run(shim.shim_reload(_Ctx())))
    assert data["reloaded"] is True
    assert data["added"] == ["pa_x"]
    assert data["total_registered"] == 5
    assert sent == [1]  # notified because the catalogue moved


def test_shim_reload_no_notify_when_unchanged(monkeypatch):
    shim = _import_shim()
    monkeypatch.setattr(shim, "_apply_catalogue_reload",
                        lambda: {"added": [], "removed": [], "changed": [],
                                 "total": 5, "fetch_failures": [],
                                 "register_failures": [], "moved": False})
    sent = []

    class _Sess:
        async def send_tool_list_changed(self): sent.append(1)

    class _Ctx:
        session = _Sess()

    data = json.loads(asyncio.run(shim.shim_reload(_Ctx())))
    assert data["reloaded"] is True
    assert sent == []  # no change -> no notification
