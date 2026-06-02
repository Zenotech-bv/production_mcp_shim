# Shim update — call-time backend attribution (`_shim_served_by`)

**Date:** 2026-06-02
**Target version:** `_SHIM_VERSION` 3.3.0 → **3.4.0**
**Status:** Design approved, ready to implement. Self-contained — no prior session context needed.

---

## TL;DR

The Punch shim federates several backend MCP servers (`sap` = Punch Analytics / "pa_v2" at `:3000`, `rd` = R&D MCP at `:3010`) into one connector. When asked "which MCP are you using?", Claude **guesses** — it once wrongly claimed `pa_v2` for a Windchill (rd) task. Root cause: **nothing in a tool-call's *response* says which backend served it.** The `[backend]` prefix already on every tool *description* only helps at selection time, not when Claude narrates after a call.

**Fix:** stamp every dict response with an additive, namespaced key `"_shim_served_by": "<backend.name>"`, injected at the single existing response choke point. Strictly additive and non-breaking (it touches SAP responses too).

---

## Target file (IMPORTANT)

Edit **only**:

```
C:\claude\production_mcp_shim\shim_server.py   (its own git repo; the copy auto-update pulls from GitHub: Zenotech-bv/production_mcp_shim)
```

**Do NOT edit** the stale copies (they are not deployed and will overwrite/confuse):
- `C:\claude\legacy\production_mcp_shim\shim_server.py` (v3.2.2)
- `C:\claude\legacy\punch-analytics-mcp-server\server\shim_canonical\shim_server.py` (v2.3.0)
- `C:\claude\legacy\punch-analytics-mcp-server\server\shim_server.py`
- `C:\claude\legacy\mcpb-shim\shim_server.py`

(Older READMEs claim the canonical source lives in the `punch-analytics-mcp-server` monorepo. That is out of date — the standalone `C:\claude\production_mcp_shim` repo at v3.3.0 is current. Confirm with `git -C C:\claude\production_mcp_shim log -1` and the `_SHIM_VERSION` constant before starting.)

---

## Background (grounded in the current code)

- **Federation + tool naming** (`shim_server.py` module docstring, ~line 32): each backend tool `T` registers as `B_T` (underscore prefix) **and** as a bare alias `T` when no other backend offers that name. rd/sap/pa names don't collide, so bare aliases always exist and Claude tends to call those.
- **Description prefix already exists** (v2.2.9, ~lines 1910–1918): every registered tool's description is prefixed `f"[{backend.name}] {upstream_desc}"`. This is *selection-time* origin only.
- **`shim_info` tool** (~line 2039) already reports full backend topology (name/url/auth/tool count). Good for diagnosis, but not auto-surfaced per call.
- **`_enrich_response()`** (~line 1679) is already the single choke point applied to **every** response and already injects a namespaced `_shim_note` into dict payloads — the exact pattern this change extends.

So the gap is precisely **call-time** attribution.

---

## The change

### 1. Extend `_enrich_response` to stamp the serving backend

Current (`shim_server.py` ~1679):

```python
def _enrich_response(payload: Any, *, http_status: int) -> Any:
    """Add a deterministic `_shim_note` ..."""
    if not isinstance(payload, dict) or "_shim_note" in payload:
        return payload
    # ... 403 / zero-row logic ...
    return payload
```

Suggested replacement (adds `backend` kwarg; stamps `_shim_served_by` on all dict paths, idempotently, **before** the early-return note logic):

```python
def _enrich_response(payload: Any, *, http_status: int, backend: "Backend | None" = None) -> Any:
    """Add a deterministic `_shim_note` to access-denial / zero-row results,
    and stamp `_shim_served_by` so the client can tell which federated
    backend produced this result. Both are namespaced `_shim_*` and additive;
    every other shape is returned untouched. Mutates and returns dicts.
    """
    if not isinstance(payload, dict):
        return payload
    # Additive, idempotent attribution — which backend served this call.
    if backend is not None:
        payload.setdefault("_shim_served_by", backend.name)  # "rd" / "sap"
    if "_shim_note" in payload:
        return payload
    # ... existing 403 / zero-row logic unchanged ...
    return payload
```

> Key shape: a plain string (`"rd"` / `"sap"` — the `backends.json` `name`). No human-label mapping and no `backends.json` changes (out of scope; see below).

### 2. Pass `backend` at both existing call sites in `_call_remote`

`backend` is already in scope (assigned `backend, original_name = entry`, ~line 1741).

- **HTTP-error path (~1814):**
  ```python
  return json.dumps(
      _enrich_response(r.json(), http_status=r.status_code, backend=backend),
      indent=2, default=str)
  ```
- **Success path (~1831):**
  ```python
  result = envelope.get("result", envelope)
  return json.dumps(
      _enrich_response(result, http_status=r.status_code, backend=backend),
      indent=2, default=str)
  ```

### 3. (Recommended) Stamp the transport-error envelopes too

The hand-built error dicts where the backend is known make "which backend failed?" explicit. Add `"_shim_served_by": backend.name` to the returned dicts in:
- `NotConfigured` (~1747), `Unreachable` (~1773), `Timeout` (~1784), `TransportError` (~1798).

Leave **`UnknownTool` (~1732) unstamped** — it fires before a backend is resolved (no `backend` in scope).

---

## Non-breaking constraints (hard requirements)

This change is on the **shared** path, so it also alters SAP/pa_v2 responses. It must be strictly additive:

1. **Dicts only.** Non-dict payloads (bare strings, lists, the raw `r.text` fallback at ~1830) are returned untouched — same as today. Accepted limitation: raw-text results carry no stamp; the `[sap]`/`[rd]` description prefix still covers those.
2. **Namespaced key.** `_shim_served_by` mirrors the existing `_shim_note` convention; it must not collide with or overwrite backend data. Use `setdefault` so a backend that ever returns its own `_shim_served_by` wins.
3. **Nothing removed or renamed.** SAP responses simply gain one extra key.
4. **Idempotent.** Re-enriching an already-stamped payload is a no-op.

---

## Tests (TDD — hermetic `tests/`)

Follow the existing suite style (e.g. `tests/test_v2_2_4_multi_backend.py`). Add a focused test module, e.g. `tests/test_v3_4_0_served_by.py`:

1. **Dict response is stamped.** Mock a backend returning `{"result": {"rows": [...]}}`; forward a tool call; assert the returned JSON has `_shim_served_by == "<that backend's name>"`.
2. **Multi-backend correctness.** Two backends; a tool from each; assert each result is stamped with its own backend name.
3. **Non-dict untouched.** Backend returns a bare JSON string / list; assert the result is returned unchanged (no wrapping, no stamp).
4. **`_shim_note` still works.** A 403 / zero-row response still gets `_shim_note` AND now also `_shim_served_by`.
5. **Idempotent.** Calling `_enrich_response` twice doesn't duplicate or change the key.

Run: `pytest tests/` (must stay green across the existing suite).

---

## Release steps

1. Bump `_SHIM_VERSION = "3.4.0"`.
2. Add a short changelog/comment header noting v3.4.0 = `_shim_served_by` call-time attribution.
3. **Regenerate `manifest.json`** so its `sha256` equals the sha256 of the new `shim_server.py`. Auto-update verifies sha256 before the atomic swap and will **refuse** a mismatch. (Use the repo's existing publish/manifest tooling if present; otherwise recompute sha256 + size + version + timestamp.)
4. Commit both `shim_server.py` and `manifest.json` together in `C:\claude\production_mcp_shim`.
5. **Operational (human):** push to GitHub `main` (Zenotech-bv/production_mcp_shim). Laptops with `PUNCH_SHIM_AUTO_UPDATE=1` pick it up on the next Claude Desktop bounce (sha256-verified, then `os.execv` relaunch).

> Note: the original author's environment had PowerShell blocked, so the publish `.ps1` could not be run there — the implementing session should run/verify the publish step in its own environment.

---

## Verification (after deploy)

1. `call shim_info` → still returns cleanly; `shim_version` shows `3.4.0`.
2. Call an **rd** tool (e.g. `windchill_list_contexts`) → result JSON contains `"_shim_served_by": "rd"`.
3. Call a **sap** tool → result JSON contains `"_shim_served_by": "sap"`; nothing else in the SAP flow regresses.
4. Ask Claude "which MCP server served that?" → it answers from `_shim_served_by` instead of guessing.

---

## Acceptance criteria

- [ ] `_enrich_response` accepts `backend` and stamps `_shim_served_by` on all dict paths, idempotently.
- [ ] Both `_call_remote` `_enrich_response` call sites pass `backend=backend`.
- [ ] Transport-error envelopes (NotConfigured/Unreachable/Timeout/TransportError) carry `_shim_served_by`; `UnknownTool` does not.
- [ ] Non-dict and raw-text responses are unchanged.
- [ ] New tests pass; full `pytest tests/` stays green.
- [ ] `_SHIM_VERSION == "3.4.0"`; `manifest.json` sha256 matches `shim_server.py`.
- [ ] Edited only `C:\claude\production_mcp_shim\shim_server.py` (no legacy copies).

---

## Out of scope (do NOT do here)

- **Human-friendly labels** (e.g. showing `sap` as "Punch Analytics / pa_v2") via a `backends.json` `label` field — deliberately deferred; this change uses the raw `backend.name`.
- **Stamping non-dict / raw-text** responses (would change response shape — breaking).
- **The "Punch R&D" skill** — separate sub-project, separate spec.
