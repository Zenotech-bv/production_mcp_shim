"""Fleet-brick guard: the OIDC-capable shim ships to EVERY laptop via
auto-update, including Kerberos-only ones where win32crypt may not be
importable on a fresh pywin32. win32crypt (and msal, which must never exist)
MUST NOT be imported at shim_server MODULE LOAD — only lazily inside the OIDC
path. Verified in a clean SUBPROCESS so other tests' imports don't pollute
sys.modules (and so re-running module-level side effects is avoided).

(webbrowser / http.server are stdlib — always importable, so leaking them
can't brick anyone; their lazy-ness is a code-review discipline item, not a
brick risk, so they're intentionally not asserted here.)"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys


def _shim_repo_root() -> str:
    shim = importlib.import_module("shim_server")
    return os.path.dirname(os.path.abspath(shim.__file__))


def test_win32crypt_and_msal_not_imported_at_module_load():
    root = _shim_repo_root()
    code = (
        "import sys, shim_server\n"
        "assert 'win32crypt' not in sys.modules, 'win32crypt leaked to module load (fleet-brick risk)'\n"
        "assert 'msal' not in sys.modules, 'msal must never be imported'\n"
        "print('OK')\n"
    )
    env = dict(os.environ)          # inherits conftest's PUNCH_BACKENDS_FILE (throwaway, unreachable)
    env["PUNCH_SHIM_AUTO_UPDATE"] = "0"
    r = subprocess.run([sys.executable, "-c", code], cwd=root,
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "OK" in r.stdout


def test_negotiate_backend_builds_client_without_oidc_imports():
    """A negotiate backend must build its http_client with no OIDC machinery."""
    shim = importlib.import_module("shim_server")
    b = shim.Backend(name="sap", url="http://mcp.example.com:3000",
                     header="X-Punch-Auth", key="", auth="negotiate")
    with b.http_client() as c:
        assert isinstance(c.auth, shim.NegotiateAuth)
