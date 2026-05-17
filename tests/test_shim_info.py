"""v3.0.0 — shim_info MCP tool.

Pure read-only introspection: returns the shim's version, runtime
fingerprint, and per-backend topology to the calling Claude session.
No backend probes, no side effects. The companion to shim_reload (which
mutates registry state); shim_info just looks.

The tests exercise the function's data shape and key contents — the
MCP-tool wrapping is exercised at startup by the import-time
`mcp.tool()(shim_info)` decorator (covered by the conftest's import).
"""
from __future__ import annotations

import importlib
import json
from unittest.mock import MagicMock


def _import_shim():
    return importlib.import_module("shim_server")


def _call_shim_info(shim):
    """Helper: run the async shim_info() to completion and return the
    parsed payload. ctx is a no-op MagicMock since shim_info doesn't
    actually use it."""
    import asyncio
    out = asyncio.run(shim.shim_info(MagicMock()))
    return json.loads(out)


def test_shim_info_returns_current_version():
    shim = _import_shim()
    payload = _call_shim_info(shim)
    assert payload["shim_version"] == shim._SHIM_VERSION
    # Version is a real semver string, not the placeholder.
    parts = payload["shim_version"].split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_shim_info_includes_python_and_pid():
    shim = _import_shim()
    payload = _call_shim_info(shim)
    assert "python" in payload
    # Python version string of the form X.Y.Z
    parts = payload["python"].split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)
    assert isinstance(payload["pid"], int)
    assert payload["pid"] > 0
    assert payload["platform"]


def test_shim_info_lists_backends_with_auth():
    shim = _import_shim()
    payload = _call_shim_info(shim)
    assert "backends" in payload
    assert isinstance(payload["backends"], list)
    assert payload["backend_count"] == len(payload["backends"])
    # Each backend entry has the required keys; auth is one of the valid values
    for b in payload["backends"]:
        assert set(b.keys()) >= {"name", "url", "auth", "configured",
                                  "registered_tool_count"}
        assert b["auth"] in shim._VALID_AUTH_MODES
        assert isinstance(b["registered_tool_count"], int)
        assert b["registered_tool_count"] >= 0


def test_shim_info_header_field_omitted_for_negotiate_backends():
    """A negotiate backend has no relevant header field (the Authorization
    header is added by NegotiateAuth, not via backends.json). shim_info
    should report header=None for negotiate, the configured header for key."""
    shim = _import_shim()
    payload = _call_shim_info(shim)
    for b in payload["backends"]:
        if b["auth"] == "negotiate":
            assert b["header"] is None, (
                f"backend {b['name']}: negotiate-mode should report header=None, got {b['header']!r}"
            )
        elif b["auth"] == "x-punch-auth":
            # x-punch-auth always has a header (defaults to 'X-Punch-Auth')
            assert b["header"], f"backend {b['name']}: x-punch-auth must surface its header name"


def test_shim_info_reports_backends_file_path():
    shim = _import_shim()
    payload = _call_shim_info(shim)
    assert payload["backends_file"]
    # On Windows the test conftest sets PUNCH_BACKENDS_FILE to a tmp path,
    # so the field should reflect that, not the default APPDATA path.
    assert "shim-test" in payload["backends_file"] or "Punch" in payload["backends_file"]


def test_shim_info_reports_auto_update_state():
    shim = _import_shim()
    payload = _call_shim_info(shim)
    # conftest sets PUNCH_SHIM_AUTO_UPDATE=0 so this should be False
    assert payload["auto_update_enabled"] is False


def test_shim_info_survives_introspection_failure_gracefully():
    """If iterating _BACKENDS / _REGISTRATIONS raises (corrupted state),
    shim_info should still return a JSON payload with at least the
    version, never bubble the exception up to the MCP layer."""
    shim = _import_shim()
    import asyncio
    from unittest.mock import patch

    # Force the registry iteration to blow up
    with patch.object(shim, "_BACKENDS", new=None):  # iterating None raises TypeError
        out = asyncio.run(shim.shim_info(MagicMock()))

    payload = json.loads(out)
    # The error path must still carry the shim_version + an error_type
    assert payload["shim_version"] == shim._SHIM_VERSION
    assert "error_type" in payload
