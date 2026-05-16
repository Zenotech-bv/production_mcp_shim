"""Test fixtures for the shim.

Importing shim_server runs side effects: it reads backends.json (or
falls back to env vars), tries to fetch /tools from each backend, and
registers tools with FastMCP. To keep tests fast and hermetic we point
the shim at a throwaway backends.json BEFORE importing it, and use a
URL that won't resolve so the /tools fetch soft-fails (the shim is
explicitly designed to start with zero registered tools when every
backend is unreachable, see the "Soft-fail on backend unreachable"
section in shim_server.py).
"""
from __future__ import annotations

import json
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_shim_env(tmp_path_factory):
    """Run BEFORE shim_server is imported by any test. Points the shim
    at a session-scoped temp backends.json with one no-op backend so
    importing the module:
      - skips the legacy PUNCH_SAP_KEY env-var fallback
      - skips the auto-seed (file already exists)
      - tries to fetch /tools from a host that doesn't resolve, soft-
        fails, and registers zero tools (the supported empty-shim case)
    """
    tmp_dir = tmp_path_factory.mktemp("shim-test")
    cfg = tmp_dir / "backends.json"
    cfg.write_text(json.dumps({
        "backends": [{
            "name":   "test_backend",
            "url":    "http://shim-test.invalid:1",
            "header": "X-Punch-Auth",
            "key":    "INITIAL-KEY-must-be-long-enough-to-pass-placeholder-guard",
        }],
        "primary": "test_backend",
    }), encoding="utf-8")

    # Set BEFORE shim_server is imported by any test module.
    os.environ["PUNCH_BACKENDS_FILE"]          = str(cfg)
    os.environ["PUNCH_SAP_KEY"]                = ""    # don't pick up a real one
    os.environ["PUNCH_SHIM_AUTO_UPDATE"]       = "0"   # never self-update during tests
    os.environ["PUNCH_SHIM_RELOAD_INTERVAL_S"] = "0"   # disable throttle for tests
    os.environ.setdefault("PUNCH_SAP_TOOLS_TIMEOUT", "1")  # fast soft-fail on /tools

    yield cfg

    for k in ("PUNCH_BACKENDS_FILE", "PUNCH_SHIM_RELOAD_INTERVAL_S"):
        os.environ.pop(k, None)


@pytest.fixture
def backends_cfg(_isolate_shim_env):
    """Per-test handle to the isolated backends.json — tests rewrite this
    file to simulate a key rotation, then call _maybe_reload_backends."""
    return _isolate_shim_env
