"""Release-channel consistency (v3.6.3).

Every laptop's auto-update reads manifest.json from main, downloads
shim_server.py from main (source_url) and refuses it unless its sha256 equals
manifest.sha256. v3.6.2 (36acf63) bumped manifest.version by hand but kept
3.6.1's sha256, so from ~2026-10-07 every laptop logged
`auto_update_fail sha256_mismatch` and no shim fix reached the fleet.

These tests fail on any commit where shim_server.py and manifest.json disagree.
The fix is never a hand edit: bump _SHIM_VERSION, then run
`operational\\publish-shim-to-github.ps1` (or its -DryRun, which builds the
.mcpb and syncs manifest.json without committing).
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIX = r"run operational\publish-shim-to-github.ps1 (-DryRun builds + syncs without committing)"


def _shim_bytes() -> bytes:
    return (REPO / "shim_server.py").read_bytes()


def _manifest() -> dict:
    return json.loads((REPO / "manifest.json").read_text(encoding="utf-8"))


def _shim_version() -> str:
    m = re.search(r'(?m)^_SHIM_VERSION\s*=\s*"(\d+\.\d+\.\d+)"', _shim_bytes().decode("utf-8"))
    assert m, "shim_server.py has no parseable _SHIM_VERSION"
    return m.group(1)


def test_manifest_sha256_matches_shim_source():
    assert _manifest()["sha256"] == hashlib.sha256(_shim_bytes()).hexdigest(), (
        f"manifest.json sha256 is not sha256(shim_server.py): every laptop will "
        f"refuse the update (sha256_mismatch). {FIX}")


def test_manifest_size_matches_shim_source():
    assert _manifest()["size_bytes"] == len(_shim_bytes()), FIX


def test_manifest_version_matches_shim_version():
    assert _manifest()["version"] == _shim_version(), FIX


def test_release_mcpb_carries_this_shim_source():
    """A fresh install (the .mcpb) must run the same shim auto-update serves."""
    mcpb = REPO / "releases" / f"punch-analytics-{_shim_version()}.mcpb"
    assert mcpb.exists(), f"{mcpb.name} missing. {FIX}"
    with zipfile.ZipFile(mcpb) as z:
        packed = z.read("server/shim_server.py")
    assert packed == _shim_bytes(), f"{mcpb.name} holds a different shim_server.py. {FIX}"
