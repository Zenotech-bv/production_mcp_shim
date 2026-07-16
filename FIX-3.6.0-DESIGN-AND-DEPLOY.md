# Shim v3.6.0 — design & deploy runbook

**Status: code complete, 167 tests green, NOT deployed.** Two reliability fixes,
both driven by the rd 2026-07-15 OIDC outage post-mortem
(`C:\claude\R&D-MCP\rd-mcp\deploy\OIDC-POSTMORTEM-AND-RETRY-PLAN.md`):

1. **Request-time auth self-heal** — make OIDC at least as reliable as Kerberos.
2. **Fast, non-blocking startup** — kill the import-time serial network that hung
   Claude Desktop's Extensions panel for ~20 s.

All changes are in `shim_server.py`; `_SHIM_VERSION` is bumped `3.5.3 → 3.6.0`.

---

## Fix 1 — request-time auth self-heal (OIDC ≥ Kerberos)

**The gap it closes.** The old `OidcAuth` attached the Bearer, yielded once, and
never handled a 401; `_call_remote` turned any ≥400 into a terminal envelope. So a
token the *server* rejected (the rd audience mismatch) hard-broke the call with no
retry and no fallback — Kerberos, by contrast, re-handshakes its own 401 and just
works. Two layers now bring OIDC up to parity:

**Layer 1 — OIDC self-heals like Kerberos (`OidcAuth.auth_flow`).** On a 401 it
force-refreshes the token (new `_oidc_acquire_token(..., force_refresh=True)`,
bypassing the valid-cache fast path) and retries **once** over OIDC — mirroring
`NegotiateAuth`'s 401 continuation leg. This recovers a stale / early-expired /
clock-skewed token *without leaving OIDC*. Gives up cleanly if the refresh fails
or returns the same token.

**Layer 2 — cross-scheme fallback (`_call_remote`).** If the backend is on OIDC
(`effective_auth=="oidc"` with a resolved `_oidc_upn`) and, even after Layer 1,
the server **still** 401s — or the token can't be minted at all this call — the
*same* call is retried over Kerberos via a new `http_client(force_auth="negotiate")`,
and the backend sticks to Kerberos for the session (`_fell_back=True`; the startup
pre-flight / `shim_reload` re-probe can flip it back). This converts the entire
"held token the server rejects" failure class from a hard outage into a
transparent degrade to the always-working path.

**Deliberate non-actions:** `403` is never retried (it's an authorization
decision the same identity would hit over Kerberos too — retrying would mask it);
a non-OIDC backend (key/`x-punch-auth`) never triggers the fallback (a 401 there
is a real credential problem). The sticky flip is GIL-atomic and lock-free, matching
`http_client`'s existing lock-free reads.

**Net:** OIDC now (a) self-recovers transient rejections on its own, and (b) can
never hard-break a user the way rd did — worst case it silently uses Kerberos.
Steady-state OIDC is also *faster* than Kerberos (a cached Bearer, no per-call SSPI
round-trip).

Touched: `_oidc_acquire_token` (`force_refresh`), `OidcAuth.auth_flow`,
`Backend.http_client` (`force_auth`), `_call_remote` (`_post_once` + fallback).

## Fix 2 — fast, non-blocking startup

**The gap it closes.** Everything below ran **serially at module import, before
`mcp.run()`**, so `initialize` couldn't be answered until all of it finished:
supervisor discovery (3 s), `_maybe_self_update` (GitHub manifest 10 s + source
30 s), a `GET /tools` per backend (5 s each), and a `GET /health` seed per backend
(5 s each). On a slow/handshaking backend that summed to the ~18–21 s stalls the
connector log showed — and it blocked *every* backend's tools, not just the slow one.

**The new critical path (import):**
1. `_load_catalogue_into_backends` — populate tools from a **disk catalogue cache**
   (`%LOCALAPPDATA%\PunchAnalytics\catalogue_cache.json`, url-validated, versioned).
2. Only if a configured backend still has no tools → `_fetch_all_tools_parallel`
   (cold start: all backends fetched **concurrently**, bounded by one timeout).
3. `_build_merged_registry(fetch_missing=False)` — build with **zero** serial network.
4. `_write_catalogue_cache` — refresh the cache.

**Moved to a background daemon (`_startup_warmup`, started in `__main__` right
before `mcp.run()`):** supervisor discovery and self-update + manifest heal.
Kill-switch: `PUNCH_SHIM_WARMUP=0`. The already-non-blocking OIDC directive
pre-flight was left exactly where it was.

**Why the warmup does NOT refresh the tool registry itself** (this is the crux of
review finding #1): FastMCP runs a sync tool function as a bare `fn(...)` on the
event-loop thread, and `list_tools` there does `list(self._tools.values())`. So
*every* registry mutation (`shim_reload`, the per-call auto-refresh) already
happens on the loop thread, serialized with `list_tools`. A daemon thread
mutating `_tools` would race that iteration
(`RuntimeError: dictionary changed size during iteration`) on exactly the
catalogue-delta case. So the authoritative **live** refresh is left to the
existing **on-loop** path (`_maybe_refresh_catalogue`): the stamp baseline is
left unseeded at startup, so the first tool call reconciles the registry safely
on the loop thread and a new chat then shows the fresh catalogue — the same
self-heal that already handles a mid-session backend deploy. Startup stays fast;
freshness is one tool-call away, never at the cost of a cross-thread registry
race.

**Measured (3 unreachable backends, worst case for blocking I/O):**

| Start | Blocking network at import | Total import |
|---|---|---|
| **Warm** (cache present) | **none** | 2.2 s (pure CPU: deps + tool registration — the unchanged floor) |
| **Cold** (no cache) | ~5 s (parallel, = one connect timeout) | 8.6 s |
| *Old code (same 3 backends)* | *~15 s serial /tools + /health + up to 40 s self-update/discovery* | *20 s+* |

Warm start — every launch after the first — now adds **no** blocking network;
the Extensions-panel stall is eliminated. First-ever run (no cache) is bounded by
one parallel fetch, then warm forever.

Touched: new `_catalogue_cache_path/_write_catalogue_cache/_read_catalogue_cache/
_load_catalogue_into_backends/_fetch_all_tools_parallel`, `_build_merged_registry`
(`fetch_missing`), the import orchestration, `_apply_catalogue_reload` (cache
write), `_startup_warmup`/`_start_background_warmup`, `__main__`.

---

## Adversarial review (done)

An independent read against FastMCP's internals found three issues, all fixed:
- **#1 (high)** the warmup daemon must not mutate the tool registry off the event
  loop — removed the warmup reconcile (see above); guarded by
  `test_startup_warmup_never_touches_the_tool_registry`.
- **#2 (med)** a malformed catalogue-cache element could crash import and brick
  every launch — `_build_merged_registry` and `_load_catalogue_into_backends` now
  skip/drop non-dict entries, the import build is wrapped to degrade to an empty
  registry and drop the poisoned cache; guarded by two new tests.
- **#3 (med)** `shim_reload` now holds `_BACKENDS_RELOAD_LOCK` like the other
  reconcile callers.
The self-heal and fast-start logic itself was found correct (401-only fallback,
403/non-OIDC no-fallback, single retry, `_shim_served_by` on every path,
httpx-protocol-correct second yield, no cache poisoning on transient failure).

## Test coverage

`171 passed` (142 pre-existing regression + 29 new). Run:
```powershell
cd C:\claude\production_mcp_shim
& C:\python312\python.exe -m pytest -q
```
New: `tests\test_auth_self_heal_3_6_0.py` (OIDC 401 refresh-retry; cross-scheme
fallback; 403 no-fallback; non-OIDC no-retry; `force_auth`; sticky flip) and
`tests\test_fast_start_3_6_0.py` (cache round-trip/versioning/url-invalidation;
parallel fetch + sap bundled fallback; `fetch_missing=False` no-net; warmup
best-effort + kill-switch). Import-safety guard still green: no `win32crypt`/`msal`
at module load.

---

## Deploy runbook (tomorrow — nothing here is done yet)

**Recommended order: deploy this shim to matt's laptop FIRST, then run the rd OIDC
retry.** Fix 1 is the durable safety net the post-mortem called for — with it live,
even a wrong rd audience self-heals to Kerberos instead of hard-breaking.

### 1. Build the .mcpb (on the server, PowerShell)
```powershell
cd C:\claude\production_mcp_shim
pwsh operational\build-mcpb.ps1        # reads _SHIM_VERSION=3.6.0; bumps both manifests + sha256
```
Produces `releases\punch-analytics-3.6.0.mcpb` and syncs the top-level
`manifest.json` (version + sha256 + size) for auto-update. (Optionally
`-DryRun` first to build into `%TEMP%` without touching `releases/`.)

### 2. Install on matt's laptop (manual — auto-update is wedged through the proxy)
Because the shim's GitHub auto-update currently fails on this network
(`sha256_mismatch`/`429`), push v3.6.0 by hand: copy `punch-analytics-3.6.0.mcpb`
to the laptop and double-click to install (Claude Desktop → reinstall the Punch
connector), then fully quit + relaunch Desktop. Confirm with `shim_info`:
`shim_version 3.6.0`, 4 backends.

### 3. Smoke-test on Kerberos (before any OIDC change)
With rd still Kerberos-only, exercise a few Windchill/Polarion/SVN tools — they
must work exactly as before (Fix 1 is dormant unless a backend is on OIDC; Fix 2
only changes *when* the catalogue loads). Watch that the Extensions panel opens
fast.

### 4. Then run the rd OIDC retry (separate runbook)
Follow `OIDC-POSTMORTEM-AND-RETRY-PLAN.md`. With v3.6.0 on matt's laptop, a 401
from a mis-configured rd audience now **self-heals to Kerberos** (visible as
`_fell_back=true` in `shim_info` + `oidc_request_fallback_to_negotiate` in
shim.log) instead of breaking his tools — so "option B" from the post-mortem is
satisfied and the retry is materially safer.

### 5. Fleet rollout (later, not tomorrow)
Distribute `punch-analytics-3.6.0.mcpb` to the ~18 users (or fix the GitHub
auto-update path so the staged update applies on next launch). Every user gets
the fast startup immediately; OIDC users get the self-heal.

### Rollback
Reinstall the previous `releases\punch-analytics-3.5.3.mcpb`. No server-side or
data changes are involved — this is a client-only shim swap.

## New env knobs (all optional, safe defaults)
- `PUNCH_SHIM_WARMUP` (default on) — set falsey to disable the background warmup
  (stay on exactly the import-time cached/parallel catalogue). Ops escape hatch.
- `PUNCH_SHIM_WARMUP_DELAY_S` (default 0.75) — delay before the warmup's first
  reconcile.

## Notes / residual (non-blocking)
- The disk cache is best-effort and self-correcting: a url change invalidates an
  entry; an empty fetch never overwrites a good cached set; the background reconcile
  is authoritative each launch.
- The 2.2 s warm floor is CPU (dependency import + FastMCP tool registration),
  unchanged from before and unrelated to the panel-hang; not worth touching.
- Independent of this fix: the shim's GitHub auto-update still fails through the
  corporate proxy. v3.6.0 doesn't fix that (it just stops it blocking startup);
  manual `.mcpb` distribution remains the reliable channel.
