"""v3.4.6 — manifest self-heal for the persistent-uv-env fix (v3.4.3).

The v3.4.3 fix (persistent ~/.punch-shim/* paths for UV_PROJECT_ENVIRONMENT
/ UV_CACHE_DIR / UV_PYTHON_INSTALL_DIR) lives in the .mcpb-packaged
manifest.json's server.mcp_config.env, injected at build time by
operational/build-mcpb.ps1. But the shim auto-updater only rewrites
shim_server.py — never manifest.json — so a laptop that auto-updated the
shim source past v3.4.3 kept its pre-fix packaged manifest and still paid
the cold `uv run` re-provision (minutes) on every launch.

_heal_manifest_uv_env closes that distribution gap through the one artifact
that DOES reach laptops hands-off (shim_server.py): on startup, inject the
UV_* keys into the packaged manifest's env if they're missing. These tests
pin the behaviour: inject-when-missing, idempotent, shape-guarded (never
touch the source repo's update-descriptor manifest), content-preserving,
and best-effort (never raise).
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path


def _import_shim():
    return importlib.import_module("shim_server")


_UV_KEYS = {"UV_PROJECT_ENVIRONMENT", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR"}


def _packaged_manifest(env: dict | None = None) -> dict:
    """A minimal .mcpb-packaged manifest (has server.mcp_config)."""
    mcp_config: dict = {
        "command": "${__dirname}/bin/uv.exe",
        "args": ["run", "--directory", "${__dirname}", "server/shim_server.py"],
    }
    if env is not None:
        mcp_config["env"] = env
    return {
        "version": "3.4.0",
        "server": {"type": "python", "mcp_config": mcp_config},
        "user_config": {"punch_sap_key": {"type": "string"}},
    }


def test_injects_uv_env_when_missing(tmp_path):
    """A pre-fix packaged manifest (env without UV_* keys) gets all three
    persistence keys injected, with the exact ${HOME}/.punch-shim/* values
    build-mcpb.ps1 writes — so a healed manifest matches a freshly-built one."""
    shim = _import_shim()
    cfg = tmp_path / "manifest.json"
    cfg.write_text(json.dumps(_packaged_manifest(env={
        "PUNCH_SAP_URL": "${user_config.punch_sap_url}",
    })), encoding="utf-8")

    changed = shim._heal_manifest_uv_env(cfg)

    assert changed is True
    env = json.loads(cfg.read_text(encoding="utf-8"))["server"]["mcp_config"]["env"]
    assert env["UV_PROJECT_ENVIRONMENT"] == "${HOME}/.punch-shim/venv"
    assert env["UV_CACHE_DIR"] == "${HOME}/.punch-shim/uv-cache"
    assert env["UV_PYTHON_INSTALL_DIR"] == "${HOME}/.punch-shim/uv-python"
    # existing env entries are preserved, not clobbered
    assert env["PUNCH_SAP_URL"] == "${user_config.punch_sap_url}"


def test_idempotent_when_all_keys_present(tmp_path):
    """An already-fixed (v3.4.3+) manifest is a no-op: returns False, leaves
    the file untouched, and writes no backup. This is the steady state on
    every launch after the heal has run once."""
    shim = _import_shim()
    cfg = tmp_path / "manifest.json"
    cfg.write_text(json.dumps(_packaged_manifest(env=dict(shim._UV_PERSIST_ENV))),
                   encoding="utf-8")
    before = cfg.read_text(encoding="utf-8")

    changed = shim._heal_manifest_uv_env(cfg)

    assert changed is False
    assert cfg.read_text(encoding="utf-8") == before
    assert not (tmp_path / "manifest.json.prepatch.bak").exists()


def test_fills_only_missing_keys_preserving_custom_value(tmp_path):
    """If the operator set a custom UV_CACHE_DIR, the heal injects only the
    two absent keys and leaves the custom one untouched."""
    shim = _import_shim()
    cfg = tmp_path / "manifest.json"
    cfg.write_text(json.dumps(_packaged_manifest(env={
        "UV_CACHE_DIR": "D:/custom/uv-cache",
    })), encoding="utf-8")

    changed = shim._heal_manifest_uv_env(cfg)

    assert changed is True
    env = json.loads(cfg.read_text(encoding="utf-8"))["server"]["mcp_config"]["env"]
    assert env["UV_CACHE_DIR"] == "D:/custom/uv-cache"   # not overwritten
    assert env["UV_PROJECT_ENVIRONMENT"] == "${HOME}/.punch-shim/venv"
    assert env["UV_PYTHON_INSTALL_DIR"] == "${HOME}/.punch-shim/uv-python"


def test_ignores_update_descriptor_manifest(tmp_path):
    """The source repo's update-descriptor manifest.json (version/sha256/
    source_url, no server block) must never be rewritten — the shape guard
    bails. Protects dev/test runs where __file__/../manifest.json resolves
    to the descriptor."""
    shim = _import_shim()
    cfg = tmp_path / "manifest.json"
    descriptor = {"version": "3.4.6", "sha256": "deadbeef",
                  "source_url": "https://example/shim_server.py"}
    cfg.write_text(json.dumps(descriptor), encoding="utf-8")

    changed = shim._heal_manifest_uv_env(cfg)

    assert changed is False
    assert json.loads(cfg.read_text(encoding="utf-8")) == descriptor


def test_missing_file_returns_false(tmp_path):
    """No manifest at the path -> best-effort no-op, never raises."""
    shim = _import_shim()
    assert shim._heal_manifest_uv_env(tmp_path / "nope.json") is False


def test_preserves_unrelated_manifest_content(tmp_path):
    """command/args/user_config and other env keys survive the heal."""
    shim = _import_shim()
    cfg = tmp_path / "manifest.json"
    cfg.write_text(json.dumps(_packaged_manifest(env={"PUNCH_SAP_KEY": "${user_config.punch_sap_key}"})),
                   encoding="utf-8")

    shim._heal_manifest_uv_env(cfg)

    m = json.loads(cfg.read_text(encoding="utf-8"))
    assert m["server"]["mcp_config"]["command"] == "${__dirname}/bin/uv.exe"
    assert m["server"]["mcp_config"]["args"][0] == "run"
    assert m["user_config"]["punch_sap_key"]["type"] == "string"
    assert m["server"]["mcp_config"]["env"]["PUNCH_SAP_KEY"] == "${user_config.punch_sap_key}"
