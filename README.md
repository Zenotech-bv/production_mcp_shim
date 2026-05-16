# production_mcp_shim

Canonical source for the Punch Analytics MCP stdio shim.

## What this is

A single-file Python shim that runs on each user's laptop and bridges
stdio-MCP (Cowork / Claude Desktop / Claude Code) to the internal
HTTP MCP server at `http://mcp.punchpowertrain.com:3000`.

This repository is the **canonical source** for the shim. Laptops
with `PUNCH_SHIM_AUTO_UPDATE=1` set in their `shim.env` fetch
`manifest.json` + `shim_server.py` from here on every Claude
Desktop bounce, verify the sha256, and apply if newer.

## Files

| File | Role |
|---|---|
| `shim_server.py` | The canonical shim source. One file, ~1200 lines. |
| `manifest.json` | Version + sha256 + size + release timestamp. The shim reads this first to decide whether an update is available. |
| `tests/` | Hermetic pytest suite. `pytest tests/` to run. |

The `manifest.json` `sha256` field MUST equal the sha256 of
`shim_server.py`. CI / pre-commit hooks should enforce this — if
the two drift, the shim's auto-update step refuses to swap (sha256
verification before `os.replace`).

## How updates ship

1. Edit `shim_server.py` in the main monorepo at
   `sap-analytics-mcp-server/server/shim_canonical/shim_server.py`.
   Bump `_SHIM_VERSION = "X.Y.Z"`.
2. Run `operational/publish-shim-to-github.ps1` from the main repo.
   The script computes the sha256, regenerates `manifest.json`,
   commits both files here, and pushes to `main`.
3. Every laptop with auto-update enabled picks up the new shim
   on the next Claude Desktop bounce.

## Auto-update behaviour

The shim's `_maybe_self_update()` runs on startup if
`PUNCH_SHIM_AUTO_UPDATE=1` is set:

1. Fetch `manifest.json` from this repo (HTTPS, public, no auth).
2. If GitHub is unreachable (firewall, outage), fall back to the
   server's `/shim/manifest.json` endpoint (HTTP, auth-gated).
3. Compare manifest version to the shim's baked-in `_SHIM_VERSION`.
4. If newer, download `shim_server.py`.
5. Verify sha256 against the manifest.
6. Backup the current shim to `shim_server.py.bak`.
7. Atomic file swap.
8. `os.execv` re-launches the shim — Claude Desktop sees an
   uninterrupted MCP session.

## Safety

- **Opt-in**: `PUNCH_SHIM_AUTO_UPDATE=1` is required. Default is off.
- **sha256 verification**: A tampered or truncated download is
  refused before the swap.
- **Backup retained**: `shim_server.py.bak` is kept after each
  successful swap, so manual revert is `mv shim_server.py.bak
  shim_server.py` followed by a Claude Desktop restart.
- **Two trust gradients**: GitHub HTTPS (this repo, primary) +
  internal server X-Punch-Auth (fallback). Either is sufficient.

## Hot-reload of `backends.json` (v2.3.1)

A key rotation on the server (`pa_admin rotate <user>`) used to force a
full Claude Desktop restart on the user's laptop before the shim picked
up the new key. The dashboard rotates in seconds; the multi-minute
restart was the slow link in the auth flow.

Companion to v2.3.0's `shim_reload` MCP tool — that one handles tools-
list changes; this one handles credential rotations. Together they
cover both rotation surfaces without a Desktop restart.

The shim watches `backends.json`'s mtime. On every tool call (with a
2-second throttle) it re-stat's the file; if mtime changed it re-reads,
validates, and reconciles `key` / `url` / `header` onto the existing
`Backend` objects in place. The next request after the rotation
already carries the new key. **No Claude Desktop restart, no manual
`shim_reload` call.**

What's hot-applied: `key`, `url`, `header` on a backend whose `name`
was already known at startup.

What still needs `shim_reload` (added/removed tools) or Claude Desktop
restart (added/removed/renamed backend): structural changes to the
backend list. The shim logs a `backends_structural_change_detected`
WARN event so the operator sees a clear signal.

Tunables:

| env var | default | what it does |
|---|---|---|
| `PUNCH_SHIM_RELOAD_INTERVAL_S` | `2` | seconds between mtime checks. Set to `0` to check on every call (handy for tests). The check is a single stat call; only an actual delta triggers the JSON re-read. |

Atomic-write defenses: a malformed file (mid-edit JSON-decode error)
is logged + skipped without advancing the tracked mtime, so the next
throttle window picks up the eventual valid write. An empty `key` in
the new file is treated as "no change" — protects against a transient
half-written state.

## Override

Fork-friendly: set `PUNCH_SHIM_UPDATE_URL_BASE` in `shim.env` to
point at a fork's raw URL. Default is
`https://raw.githubusercontent.com/Zenotech-bv/production_mcp_shim/main`.

## Audit trail

All shim activity is logged to (Windows)
`%LOCALAPPDATA%\PunchAnalytics\shim.log` or (Mac/Linux)
`~/.local/share/punch-analytics/shim.log`. JSON-lines. Every
auto-update probe + outcome appears as an `auto_update_*` event.
Auth keys are never logged.

## License

Proprietary — internal use only. (Punch Powertrain.)
