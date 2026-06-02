"""v3.4.0 — call-time backend attribution (`_shim_served_by`).

The shim federates several backends (sap=:3000, rd=:3010) behind one
connector. Nothing in a tool-call's *response* used to say which backend
served it, so when asked "which MCP did you use?" the client guessed
(once wrongly claiming pa_v2 for a Windchill/rd task). v3.4.0 stamps an
additive, namespaced `_shim_served_by` on every dict response at the
single `_enrich_response` choke point, plus on the hand-built transport-
error envelopes where the backend is known.

Hard requirement: strictly additive / non-breaking on the SHARED path
(this also touches sap responses). Dicts only; namespaced; nothing
removed or renamed; idempotent.

Companion unit + integration coverage:
  * `_enrich_response` — the pure stamping function (new `backend` kwarg).
  * `_call_remote`     — proves the wiring actually passes `backend=`.
"""
from __future__ import annotations

import importlib
import json

import httpx


def _import_shim():
    """Import shim_server lazily so the conftest isolation env is in place
    before its module-level side effects run (see test_hot_reload.py)."""
    return importlib.import_module("shim_server")


def _make_backend(shim, name):
    return shim.Backend(
        name=name,
        url="http://shim-test.invalid:1",
        header="X-Punch-Auth",
        key="K-padded-to-pass-the-placeholder-guard-and-min-length",
    )


# --------------------------------------------------------------------- _enrich_response (unit)


def test_dict_response_is_stamped_with_backend_name():
    shim = _import_shim()
    rd = _make_backend(shim, "rd")

    out = shim._enrich_response({"rows": [{"a": 1}]}, http_status=200, backend=rd)

    assert out["_shim_served_by"] == "rd"
    # additive — the original data is untouched
    assert out["rows"] == [{"a": 1}]


def test_each_backend_stamps_its_own_name():
    """Multi-backend correctness: a payload enriched for each backend
    carries that backend's own name, never the other's."""
    shim = _import_shim()
    rd = _make_backend(shim, "rd")
    sap = _make_backend(shim, "sap")

    rd_out = shim._enrich_response({"x": 1}, http_status=200, backend=rd)
    sap_out = shim._enrich_response({"y": 2}, http_status=200, backend=sap)

    assert rd_out["_shim_served_by"] == "rd"
    assert sap_out["_shim_served_by"] == "sap"


def test_non_dict_payload_is_returned_untouched():
    """Lists / bare strings carry no stamp and are not wrapped — same
    shape in, same shape out (the raw-text limitation is accepted)."""
    shim = _import_shim()
    rd = _make_backend(shim, "rd")

    a_list = [1, 2, 3]
    a_str = "just text"
    assert shim._enrich_response(a_list, http_status=200, backend=rd) is a_list
    assert shim._enrich_response(a_str, http_status=200, backend=rd) == a_str


def test_shim_note_and_served_by_coexist_on_403():
    """A 403 still gets `_shim_note`, and now ALSO `_shim_served_by`."""
    shim = _import_shim()
    sap = _make_backend(shim, "sap")

    out = shim._enrich_response({"error": True}, http_status=403, backend=sap)

    assert "_shim_note" in out          # existing behavior preserved
    assert out["_shim_served_by"] == "sap"


def test_served_by_is_idempotent():
    """Re-enriching an already-stamped payload is a no-op (no duplication,
    no value change)."""
    shim = _import_shim()
    rd = _make_backend(shim, "rd")

    payload = {"rows": []}
    once = shim._enrich_response(payload, http_status=200, backend=rd)
    twice = shim._enrich_response(once, http_status=200, backend=rd)

    assert twice["_shim_served_by"] == "rd"
    assert twice is once


def test_backend_own_served_by_wins_via_setdefault():
    """If a backend ever returns its own `_shim_served_by`, the shim must
    not clobber it (setdefault, not assignment)."""
    shim = _import_shim()
    rd = _make_backend(shim, "rd")

    out = shim._enrich_response(
        {"_shim_served_by": "backend-supplied"}, http_status=200, backend=rd)

    assert out["_shim_served_by"] == "backend-supplied"


def test_no_backend_kwarg_preserves_old_behavior():
    """Called without a backend (e.g. an old call site), nothing is
    stamped — the v3.3.0 behavior is unchanged."""
    shim = _import_shim()

    out = shim._enrich_response({"rows": [{"a": 1}]}, http_status=200)

    assert "_shim_served_by" not in out


# --------------------------------------------------------------------- _call_remote (integration)


class _FakeResp:
    def __init__(self, *, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.content = b"{}"
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeClient:
    """Context-manager stand-in for backend.http_client()."""
    def __init__(self, *, resp=None, raise_exc=None):
        self._resp = resp
        self._raise = raise_exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, path, json=None):
        if self._raise is not None:
            raise self._raise
        return self._resp


def _quiet_side_effects(monkeypatch, shim):
    """_call_remote calls _maybe_reload_backends + _maybe_refresh_catalogue
    first; neutralize both so the test makes no network calls."""
    monkeypatch.setattr(shim, "_maybe_reload_backends", lambda: None)
    monkeypatch.setattr(shim, "_maybe_refresh_catalogue", lambda: None)


def _register(monkeypatch, shim, backend, registered_name, original_name):
    monkeypatch.setitem(
        shim._NAME_TO_BACKEND, registered_name, (backend, original_name))


def test_call_remote_stamps_served_by_on_success(monkeypatch):
    shim = _import_shim()
    _quiet_side_effects(monkeypatch, shim)
    rd = _make_backend(shim, "rd")
    monkeypatch.setattr(rd, "http_client", lambda **kw: _FakeClient(
        resp=_FakeResp(status_code=200, payload={"result": {"rows": [{"a": 1}]}})))
    _register(monkeypatch, shim, rd, "rd_thing", "thing")

    out = json.loads(shim._call_remote("rd_thing", {}))

    assert out["_shim_served_by"] == "rd"
    assert out["rows"] == [{"a": 1}]


def test_call_remote_each_backend_stamped_with_its_own_name(monkeypatch):
    shim = _import_shim()
    _quiet_side_effects(monkeypatch, shim)
    rd = _make_backend(shim, "rd")
    sap = _make_backend(shim, "sap")
    monkeypatch.setattr(rd, "http_client", lambda **kw: _FakeClient(
        resp=_FakeResp(status_code=200, payload={"result": {"v": "rd"}})))
    monkeypatch.setattr(sap, "http_client", lambda **kw: _FakeClient(
        resp=_FakeResp(status_code=200, payload={"result": {"v": "sap"}})))
    _register(monkeypatch, shim, rd, "rd_thing", "thing")
    _register(monkeypatch, shim, sap, "sap_thing", "thing")

    rd_out = json.loads(shim._call_remote("rd_thing", {}))
    sap_out = json.loads(shim._call_remote("sap_thing", {}))

    assert rd_out["_shim_served_by"] == "rd"
    assert sap_out["_shim_served_by"] == "sap"


def test_call_remote_http_error_path_is_stamped(monkeypatch):
    shim = _import_shim()
    _quiet_side_effects(monkeypatch, shim)
    sap = _make_backend(shim, "sap")
    monkeypatch.setattr(sap, "http_client", lambda **kw: _FakeClient(
        resp=_FakeResp(status_code=403, payload={"error": True})))
    _register(monkeypatch, shim, sap, "sap_thing", "thing")

    out = json.loads(shim._call_remote("sap_thing", {}))

    assert out["_shim_served_by"] == "sap"
    assert "_shim_note" in out


def test_call_remote_unreachable_envelope_is_stamped(monkeypatch):
    shim = _import_shim()
    _quiet_side_effects(monkeypatch, shim)
    rd = _make_backend(shim, "rd")
    monkeypatch.setattr(rd, "http_client", lambda **kw: _FakeClient(
        raise_exc=httpx.ConnectError("no route")))
    _register(monkeypatch, shim, rd, "rd_thing", "thing")

    out = json.loads(shim._call_remote("rd_thing", {}))

    assert out["error_type"] == "Unreachable"
    assert out["_shim_served_by"] == "rd"


def test_call_remote_not_configured_envelope_is_stamped(monkeypatch):
    shim = _import_shim()
    _quiet_side_effects(monkeypatch, shim)
    # empty key -> is_configured False -> NotConfigured envelope
    broken = shim.Backend(name="rd", url="http://x:1",
                          header="X-Punch-Auth", key="")
    assert not broken.is_configured
    _register(monkeypatch, shim, broken, "rd_thing", "thing")

    out = json.loads(shim._call_remote("rd_thing", {}))

    assert out["error_type"] == "NotConfigured"
    assert out["_shim_served_by"] == "rd"


def test_call_remote_unknown_tool_is_not_stamped(monkeypatch):
    """UnknownTool fires before a backend is resolved — no backend in
    scope, so it must NOT carry `_shim_served_by`."""
    shim = _import_shim()
    _quiet_side_effects(monkeypatch, shim)

    out = json.loads(shim._call_remote("definitely_not_a_real_tool", {}))

    assert out["error_type"] == "UnknownTool"
    assert "_shim_served_by" not in out
