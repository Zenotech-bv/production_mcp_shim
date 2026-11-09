"""v3.6.7 — no bundled offline tool fallback.

Up to v3.6.4, when the `sap` backend's /tools failed and there was no
catalogue cache, the shim registered the tool names from a `tools.json`
beside shim_server.py. That file was a frozen snapshot (104 V1 `get_*` /
`jira_*` / `tempo_*` names on an old install, a 224-tool pa_v2 0.1.66 list in
the repo), and auto-update never replaced it. `_write_catalogue_cache` then
persisted those phantom names, and Claude Desktop never re-pulls a tool list,
so a user could be stuck with tools that fail with UnknownTool.

Now an unreachable `sap` registers 0 tools; the next tool call's auto-refresh
fills them from live. These tests plant a phantom `tools.json` beside the shim
(as an old .mcpb install still has) and prove the shim ignores it.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

SHIM_PATH = Path(__file__).resolve().parent.parent / "shim_server.py"
PHANTOM = ["get_vendor_aging", "jira_search_issues", "tempo_list_worklogs"]
# Same shape as the conftest key: passes the shim's placeholder-key guard.
KEY = "INITIAL-KEY-must-be-long-enough-to-pass-placeholder-guard"


def _import_shim():
    return importlib.import_module("shim_server")


def _plant_phantom_tools_json(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tools.json").write_text(json.dumps({
        "tools": [{"name": n, "description": "Schema version: v0.0.1",
                   "inputSchema": {"type": "object", "properties": {}}}
                  for n in PHANTOM],
        "count": len(PHANTOM),
        "server_version": "0.0.1",
    }), encoding="utf-8")


def _sap(shim):
    return shim.Backend(name="sap", url="http://shim-test.invalid:1",
                        header="X-Punch-Auth", key=KEY)


# ---------------------------------------------------------- real cold start

def test_cold_start_unreachable_sap_registers_and_caches_no_bundled_names(tmp_path):
    """Import a copy of shim_server.py the way a laptop runs it (an installed
    server/ dir that still holds an old tools.json), with `sap` unreachable and
    no catalogue cache. The import-time registry build must register no
    bundled name and must not write one to catalogue_cache.json."""
    server_dir = tmp_path / "server"
    _plant_phantom_tools_json(server_dir)
    shutil.copy2(SHIM_PATH, server_dir / "shim_server.py")

    cfg = tmp_path / "backends.json"
    cfg.write_text(json.dumps({
        "backends": [{
            "name":   "sap",
            "url":    "http://shim-test.invalid:1",
            "header": "X-Punch-Auth",
            "key":    KEY,
        }],
        "primary": "sap",
    }), encoding="utf-8")

    env = dict(os.environ)
    env.update({
        "PUNCH_BACKENDS_FILE":     str(cfg),
        "PUNCH_SAP_KEY":           "",
        "PUNCH_SHIM_AUTO_UPDATE":  "0",
        "PUNCH_SHIM_DISCOVERY_URL": "",
        "PUNCH_SAP_TOOLS_TIMEOUT": "1",
        "PUNCH_SHIM_WARMUP":       "0",
        "LOCALAPPDATA":            str(tmp_path / "appdata"),  # Windows log/cache dir
        "HOME":                    str(tmp_path / "home"),     # POSIX log/cache dir
    })
    probe = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(server_dir)!r})\n"
        "import shim_server as s\n"
        "print(json.dumps({\n"
        "    'registered': [n for n, _t, _b in s._REGISTRATIONS],\n"
        "    'sap_configured': [b.is_configured for b in s._BACKENDS if b.name == 'sap'],\n"
        "    'sap_tools': [t.get('name') for b in s._BACKENDS if b.name == 'sap' for t in b.tools],\n"
        "    'cache': str(s._catalogue_cache_path()),\n"
        "}))\n"
    )
    proc = subprocess.run([sys.executable, "-c", probe], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["sap_configured"] == [True]  # else the cold fetch never runs
    assert out["sap_tools"] == []
    assert not set(PHANTOM) & set(out["registered"])

    cache = Path(out["cache"])
    assert cache.is_relative_to(tmp_path)  # never the operator's real cache
    if cache.exists():
        text = cache.read_text(encoding="utf-8")
        assert "sap" not in json.loads(text).get("backends", {})
        assert not any(n in text for n in PHANTOM)


# ---------------------------------------------------------- unit level

def test_fetch_all_tools_parallel_unreachable_sap_gets_no_tools(tmp_path, monkeypatch):
    shim = _import_shim()
    _plant_phantom_tools_json(tmp_path)
    monkeypatch.setattr(shim, "__file__", str(tmp_path / "shim_server.py"))
    monkeypatch.setattr(shim, "_fetch_tools_for_backend", lambda b: None)  # live fails
    sap = _sap(shim)
    shim._fetch_all_tools_parallel([sap])
    assert sap.tools == []


def test_build_merged_registry_unreachable_sap_registers_nothing(tmp_path, monkeypatch):
    shim = _import_shim()
    _plant_phantom_tools_json(tmp_path)
    monkeypatch.setattr(shim, "__file__", str(tmp_path / "shim_server.py"))
    monkeypatch.setattr(shim, "_fetch_tools_for_backend", lambda b: None)  # live fails
    sap = _sap(shim)
    name_to_backend, registrations = shim._build_merged_registry([sap])
    assert name_to_backend == {}
    assert registrations == []


def test_http_client_sends_no_bundled_version_header(tmp_path, monkeypatch):
    """The X-Punch-Shim-Bundled-Version header came from that tools.json. With
    the file no longer read, the header is gone (no server reads it)."""
    shim = _import_shim()
    _plant_phantom_tools_json(tmp_path)
    monkeypatch.setattr(shim, "__file__", str(tmp_path / "shim_server.py"))
    with _sap(shim).http_client() as c:
        assert "X-Punch-Shim-Bundled-Version" not in c.headers
        assert c.headers["X-Punch-Shim-Version"] == shim._SHIM_VERSION


def test_release_mcpb_ships_no_tools_json_but_keeps_manifest_tools():
    """build-mcpb.ps1 drops the baseline's server/tools.json from the package
    and still fills manifest.tools (connector-directory text) from
    manifest-tools.json."""
    shim = _import_shim()
    repo = SHIM_PATH.parent
    mcpb = repo / "releases" / f"punch-analytics-{shim._SHIM_VERSION}.mcpb"
    with zipfile.ZipFile(mcpb) as z:
        assert "server/tools.json" not in z.namelist()
        packed = json.loads(z.read("manifest.json"))
    snapshot = json.loads((repo / "manifest-tools.json").read_text(encoding="utf-8"))
    assert [t["name"] for t in packed["tools"]] == [t["name"] for t in snapshot["tools"]]
    assert packed["tools"]


def test_empty_sap_fills_from_live_on_the_next_refresh(tmp_path, monkeypatch):
    """The recovery path the fallback used to paper over: a sap backend that
    started with 0 tools is filled by the next catalogue reload once live."""
    shim = _import_shim()
    monkeypatch.setattr(shim, "_catalogue_cache_path", lambda: tmp_path / "cat.json")
    sap = _sap(shim)
    live = [{"name": "pa_whoami_live_probe", "description": "probe",
             "inputSchema": {"type": "object", "properties": {}}}]
    monkeypatch.setattr(shim, "_fetch_tools_for_backend", lambda b: live)
    monkeypatch.setattr(shim, "_BACKENDS", [sap])
    monkeypatch.setattr(shim, "_REGISTRATIONS", [])
    monkeypatch.setattr(shim, "_NAME_TO_BACKEND", {})
    try:
        result = shim._apply_catalogue_reload()
        assert result["moved"] is True
        assert result["added"] == ["pa_whoami_live_probe"]
        assert shim._NAME_TO_BACKEND["pa_whoami_live_probe"] == (sap, "pa_whoami_live_probe")
        cached = json.loads((tmp_path / "cat.json").read_text(encoding="utf-8"))
        assert [t["name"] for t in cached["backends"]["sap"]["tools"]] == ["pa_whoami_live_probe"]
    finally:
        shim.mcp.remove_tool("pa_whoami_live_probe")
