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
        assert set(b.keys()) >= {"name", "url", "auth", "effective_auth",
                                  "oidc_upn", "fell_back", "configured",
                                  "registered_tool_count"}
        assert b["auth"] in shim._VALID_AUTH_MODES
        assert b["effective_auth"] in shim._VALID_AUTH_MODES
        assert isinstance(b["fell_back"], bool)
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


def test_shim_info_includes_process_env_diagnostics():
    """v3.0.3 — shim_info reports the env vars the shim's process sees so
    the operator can compare against what their shell sees. Catches the
    'Claude Desktop launched the shim with a different APPDATA' class
    of mystery in one tool call."""
    shim = _import_shim()
    payload = _call_shim_info(shim)
    assert "process_env" in payload
    env = payload["process_env"]
    # The set of keys is the contract — change carefully.
    expected_keys = {
        "APPDATA", "LOCALAPPDATA", "USERPROFILE",
        "PUNCH_BACKENDS_FILE", "PUNCH_SAP_URL", "PUNCH_SAP_KEY",
        "PUNCH_SHIM_AUTO_UPDATE",
    }
    assert set(env.keys()) == expected_keys, (
        f"process_env key set drift: got {set(env.keys())}, expected {expected_keys}"
    )


def test_shim_info_redacts_punch_sap_key_credential(monkeypatch):
    """v3.0.3 — the credential MUST be reported as <set>/<unset>, never
    echoed verbatim. Echoing it would leak a service-account key into
    whatever chat the shim_info tool was called from. Belt-and-braces:
    if someone later adds the raw value 'because it's useful for
    debugging', this test fails loudly."""
    shim = _import_shim()
    secret = "do-not-leak-this-key-padded-to-look-real-xxxx"
    monkeypatch.setenv("PUNCH_SAP_KEY", secret)

    payload = _call_shim_info(shim)
    env = payload["process_env"]
    assert env["PUNCH_SAP_KEY"] == "<set>", (
        f"PUNCH_SAP_KEY must be redacted, got {env['PUNCH_SAP_KEY']!r}"
    )
    # Whole-payload sweep: the literal secret must not appear anywhere.
    import json as _json
    raw = _json.dumps(payload)
    assert secret not in raw, (
        f"the literal PUNCH_SAP_KEY value leaked somewhere in the payload"
    )


def test_shim_info_punch_sap_key_unset_when_no_env(monkeypatch):
    shim = _import_shim()
    monkeypatch.delenv("PUNCH_SAP_KEY", raising=False)
    payload = _call_shim_info(shim)
    assert payload["process_env"]["PUNCH_SAP_KEY"] == "<unset>"


def test_shim_info_reports_backends_file_exists_bool():
    """v3.0.3 — explicit boolean alongside the resolved path, so the
    operator doesn't have to do their own Test-Path. The path can
    resolve fine but point at a directory that doesn't exist (which
    is exactly the bug that motivated v3.0.3)."""
    shim = _import_shim()
    payload = _call_shim_info(shim)
    assert "backends_file_exists" in payload
    assert isinstance(payload["backends_file_exists"], bool)


def test_shim_info_includes_cwd_and_script_dir():
    shim = _import_shim()
    payload = _call_shim_info(shim)
    assert payload["cwd"]
    assert payload["script_dir"]
    assert payload["executable"]
    # script_dir should match the shim_server.py's own directory
    from pathlib import Path
    expected_script_dir = str(Path(shim.__file__).parent)
    assert payload["script_dir"] == expected_script_dir


def test_shim_info_reports_effective_auth_and_oidc_upn_when_cut_over():
    """v3.5.3 — the OIDC cutover flips a backend's effective_auth to "oidc"
    at startup while `auth` stays the static "negotiate" default. shim_info
    MUST surface effective_auth (what the shim actually sends) + the resolved
    oidc_upn, so a chat can answer "am I on OIDC?" correctly. Before this,
    shim_info showed only `auth` (negotiate) and misled users mid-cutover."""
    shim = _import_shim()
    from unittest.mock import patch

    b = shim.Backend(name="pa_v2", url="http://mcp.example.com:3000",
                     header="X-Punch-Auth", key="", auth="negotiate")
    b.effective_auth = "oidc"
    b._oidc_upn = "matt.stevens@punchpowertrain.com"

    with patch.object(shim, "_BACKENDS", new=[b]):
        payload = _call_shim_info(shim)

    entry = next(x for x in payload["backends"] if x["name"] == "pa_v2")
    # `auth` is the static config default — unchanged, back-compatible.
    assert entry["auth"] == "negotiate"
    # `effective_auth` is the truth about what the shim dispatches on now.
    assert entry["effective_auth"] == "oidc"
    # oidc_upn names the OIDC identity, only when actually on oidc.
    assert entry["oidc_upn"] == "matt.stevens@punchpowertrain.com"
    # Not a fallback — genuinely cut over.
    assert entry["fell_back"] is False


def test_shim_info_effective_auth_mirrors_http_client_downgrade():
    """effective_auth=="oidc" with an EMPTY _oidc_upn actually dispatches
    Negotiate at request time (see http_client's mode downgrade). shim_info
    MUST report "negotiate" for that state, not "oidc" — otherwise it just
    re-creates the original mislead with the sign flipped. Reachable via a
    backends.json entry with auth="oidc" whose UPN the negotiate-only directive
    pass never resolves."""
    shim = _import_shim()
    from unittest.mock import patch

    b = shim.Backend(name="pa_v2", url="http://mcp.example.com:3000",
                     header="X-Punch-Auth", key="", auth="negotiate")
    b.effective_auth = "oidc"
    b._oidc_upn = ""   # no resolved identity -> http_client sends Negotiate

    with patch.object(shim, "_BACKENDS", new=[b]):
        payload = _call_shim_info(shim)

    entry = next(x for x in payload["backends"] if x["name"] == "pa_v2")
    assert entry["effective_auth"] == "negotiate"
    assert entry["oidc_upn"] is None


def test_shim_info_surfaces_fell_back():
    """A backend directed to OIDC that fell back to Kerberos this session
    (_fell_back=True) must be distinguishable from a never-directed negotiate
    backend — that's the 'why am I NOT on OIDC?' question shim_info exists to
    answer, one layer deeper than 'am I on OIDC?'."""
    shim = _import_shim()
    from unittest.mock import patch

    b = shim.Backend(name="pa_v2", url="http://mcp.example.com:3000",
                     header="X-Punch-Auth", key="", auth="negotiate")
    b._fell_back = True

    with patch.object(shim, "_BACKENDS", new=[b]):
        payload = _call_shim_info(shim)

    entry = next(x for x in payload["backends"] if x["name"] == "pa_v2")
    assert entry["fell_back"] is True
    assert entry["effective_auth"] == "negotiate"


def test_shim_info_reports_auth_directive_pending_bool():
    """Top-level flag: True while the startup OIDC preflight is still resolving,
    so an early caller gets 'not yet determined' instead of a definitive-looking
    'negotiate'."""
    shim = _import_shim()
    payload = _call_shim_info(shim)
    assert "auth_directive_pending" in payload
    assert isinstance(payload["auth_directive_pending"], bool)


def test_shim_info_oidc_upn_none_for_negotiate_backend():
    """A backend NOT on oidc reports effective_auth=='negotiate' and
    oidc_upn is None (never surface a stale/empty UPN as if it mattered)."""
    shim = _import_shim()
    from unittest.mock import patch

    b = shim.Backend(name="pa_v2", url="http://mcp.example.com:3000",
                     header="X-Punch-Auth", key="", auth="negotiate")
    # effective_auth defaults to auth ("negotiate") via __post_init__.

    with patch.object(shim, "_BACKENDS", new=[b]):
        payload = _call_shim_info(shim)

    entry = next(x for x in payload["backends"] if x["name"] == "pa_v2")
    assert entry["effective_auth"] == "negotiate"
    assert entry["oidc_upn"] is None


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
