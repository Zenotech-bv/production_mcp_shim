# Code & Doc Audit — production_mcp_shim (2026-06-05)

Auditor: independent code/doc review. Scope: every first-party `.py` + `.md`
(excluding `__pycache__`, `.venv`, `.git`, `logs/*`, `*.bak`). Read in full:
`shim_server.py` (2238 lines), `README.md`, `installer/bundled-README.md`,
`2026-06-02-shim-backend-attribution-spec.md`, `generate_bundled_tools.py`,
all 11 test modules, both installer `.ps1.tmpl`, all three `operational/`
scripts, `manifest.json`, the two `manifest-*description*.txt`,
`shim.env.example`, `.gitattributes`, `.gitignore`.

---

## 1. Overview

**What it is.** A single-file Python stdio MCP shim (`shim_server.py`,
`_SHIM_VERSION = "3.4.3"`) that runs on each user's laptop and is consumed by
Cowork / Claude Desktop / Claude Code via the local stdio-MCP plugin
mechanism. It does **not** talk to SAP. It *federates* N backend HTTP MCP
servers (today: `sap` = pa_v2 on `:3000`, `zabbix` on `:3002`, optionally
`rd`) behind one connector: at startup it fetches each backend's `/tools`,
merges them into one registry (bare name when unique, `B_T` prefix on
collision), and registers each as a FastMCP tool whose forwarder POSTs
`/tools/<name>` to the owning backend. It manages credentials/topology via
`backends.json` (hot-reloaded on mtime change), auto-refreshes the catalogue
on backend `/health` deltas, and can self-update from GitHub.

**The three `shim_*` tools the audit prompt names:**

- `shim_info` (l.2078) — read-only self-report: version, Python runtime,
  per-backend `{url, auth, header, configured, registered_tool_count}`, env
  diagnostics (secrets masked). No backend I/O.
- `shim_access` (l.2176) — combines per-backend connectivity probes
  (`_probe_backend`) with the server's `pa_whoami` access profile to
  distinguish "not connected" from "connected but unauthorised".
- `shim_reload` (l.2026) — re-fetches every backend's `/tools`, diffs,
  register/unregisters in place, and emits `tools/list_changed`.

**Overall health.** The code is careful and well-commented, with a genuinely
good hermetic test suite (auth modes, hot-reload, discovery, enrichment,
v1-nuke). Auth handling is sound: Kerberos/SPNEGO via `pyspnego` is the human
default (no key on disk), x-punch-auth keys are placeholder-guarded and never
logged. The most serious findings are an **operational recovery script that
deletes the wrong path** (so it silently does nothing), a **dangling
`shim_diagnostics` reference** in a user-facing error, and a **README line
that contradicts the "v1 host is DEAD" ground truth**. No secrets are
committed; the committed `.mcpb` binaries and large `tools.json`/`uv.lock` are
deliberate release artifacts, not scratch.

---

## 2. Stale docs FIXED in place

### F1 — `README.md` line 9–11: v1 host described as "reachable too" (contradicts ground truth that it is DEAD)

Original text said the v1 server at `mcp.punchpowertrain.com` "is reachable
too but on its way out — keep an explicit `"url"` in your `backends.json` if
you need it." Per GROUND TRUTH (2026-06-05) `mcp.punchpowertrain.com` is DEAD
(no SPN, no alias), and the code itself treats it as a defunct host to be
auto-archived (`_V1_HOSTS`, `_looks_v1_backend`, l.642–652; the v1-nuke logic
l.796–811). Telling users they can still point at it is wrong and actively
harmful. **Fixed** to state v1 is retired and that a `backends.json` pointing
at it is auto-archived + re-seeded to the canonical v2 host. (This is the
evergreen "What this is" section, not a dated record.)

---

## 3. Suspected-stale docs (NOT edited — flagged for owner)

- **S1 — `README.md` line 22 "One file, ~1200 lines."** The file is now 2238
  lines. Minor, but the "~1200 lines" claim is also repeated in the module
  docstring's spirit. Left unedited because it is cosmetic and the exact
  intended number is a judgement call; suggest "~2200 lines" or dropping the
  count. (Suspected stale.)

- **S2 — `shim.env.example` line 37–41 vs README/manifest on the auto-update
  default.** `shim.env.example` says `PUNCH_SHIM_AUTO_UPDATE` defaults to "1
  (enabled)" and `installer/bundled-README.md` line 51 says "(the default)".
  But the **code** default is OFF: `os.getenv("PUNCH_SHIM_AUTO_UPDATE", "0")`
  (l.272) and `README.md` line 60 explicitly says "Default is off." This is a
  real **doc/doc + doc/code contradiction** (see also C-P2 below). Not edited
  because the *intended* default is a product decision (the manifest/installer
  may set the env var to "1" out of band, making "enabled by default in
  practice" defensible) — needs Matt to confirm which is intended, then align
  all three. (Suspected stale / needs-decision.)

- **S3 — `installer/bundled-README.md` line 6** describes backends as
  "(SAP / Jira / Tempo / Zabbix)". The shipped templates and seed only
  configure `sap` + `zabbix`; Jira/Tempo are pa_v2 *tools under the sap
  backend*, not separate federated backends. Mildly misleading but arguably
  aspirational/illustrative. (Suspected stale.)

- **S4 — `README.md` "How updates ship" §1 (line 33–34)** instructs editing
  the shim in the monorepo at
  `sap-analytics-mcp-server/server/shim_canonical/shim_server.py`. The
  attribution spec (`2026-06-02-...md` line 25–31) explicitly states that path
  is a **stale legacy copy** and the standalone `production_mcp_shim` repo is
  now canonical. The README's own line 13 already says "This repository is the
  canonical source," so §1 internally contradicts it. Strong candidate for a
  fix, but I left it because the exact replacement workflow (do you still
  hand-edit anywhere in the monorepo?) needs owner confirmation. (Suspected
  stale — likely a real bug in the docs.)

- **S5 — `installer/bundled-README.md` line 62** points to an onboarding
  runbook `docs/onboarding-kerberos.md` "in the punch-analytics repo" and
  line 63 to `docs/auth.md §13`. Could not verify those paths from this repo.
  (Suspected stale — unverifiable here.)

---

## 4. Code findings (grouped, P0–P3, file:line)

### P0 — operational breakage

**P0-1 — `operational/reset-punch.cmd` deletes the WRONG `backends.json`
directory; the "deterministic reset" silently no-ops on the real config.**
`reset-punch.cmd:43–44,49–50` delete `…\PunchAnalytics\backends.json`
(directory name `PunchAnalytics`). But the shim's `_resolve_backends_path()`
(`shim_server.py:516`) resolves `%APPDATA%\Punch\backends.json` (directory
`Punch`), and **both** installer templates write `%APPDATA%\Punch\backends.json`
(`punch-analytics-install.ps1.tmpl:73`,
`punch-analytics-install-kerberos.ps1.tmpl:83`). So on a real laptop every
`del` target reports "not present", the broken `…\Punch\backends.json` is left
in place, and the script's whole purpose (kick a bad install back to factory)
fails. Note the **log** paths *are* correct
(`%LOCALAPPDATA%\PunchAnalytics\shim.log` matches `_resolve_log_dir`,
`shim_server.py:289`), which is exactly why the bug is easy to miss — only the
`backends.json` directory name is wrong. Fix: change the four `backends.json`
delete targets from `…\PunchAnalytics\…` to `…\Punch\…` (and the same inside
the two MS-Store-sandbox prefixes, l.49–50). The script is referenced as the
"universal recovery" by both the in-code comments (l.227, l.813–820) and the
seed/guard rationale, so this is load-bearing.

### P1 — correctness / user-facing wrongness

**P1-1 — Dangling `shim_diagnostics` reference in a user-facing error.**
`shim_server.py:1771` — the `UnknownTool` envelope tells the user "Run
`shim_diagnostics` to see the registered list." No tool named
`shim_diagnostics` is registered anywhere (the real tools are `shim_info`,
`shim_access`, `shim_reload`; grep confirms the string appears only at this
one call site). A user/LLM following the instruction calls a non-existent
tool. Fix: point at `shim_info` (which reports `registered_tool_count` per
backend) or `shim_reload`.

**P1-2 — Discovered x-punch-auth backends are merged with an empty key →
permanently `NotConfigured`.** `_discover_backends` (`shim_server.py:1136`)
appends every discovered backend as `Backend(..., header="X-Punch-Auth",
key="", auth=auth)`. For an `auth="negotiate"` discovery this is correct (no
key needed). But for an `auth="x-punch-auth"` discovery (the default applied
at l.1128 when the payload omits `auth`), `is_configured` is `False`
(needs `url AND key AND header`, l.456), so the backend registers zero tools
and every call returns `NotConfigured`. Discovery therefore can only ever
usefully add *negotiate* backends; an x-punch-auth one is silently dead.
Either document that discovery is negotiate-only, or default discovered
backends to `auth="negotiate"`, or have discovery carry no auth and resolve
the key from a known source. Today's deployment is all-Kerberos so this is
latent, but the default at l.1128 makes it a foot-gun.

**P1-3 — `shim.log` masking is substring-based and over-broad / fragile.**
The JSON + stderr formatters drop any field whose key contains `auth`, `key`,
or `token` (`shim_server.py:327`, l.354–356). This both (a) over-redacts —
a benign field like `auth_ok` or `auth_changes` or `unauthorized_count` would
be silently dropped from logs, and indeed `auth_ok` is a real field on
`_probe_backend` results — and (b) is the *only* line of defence; a future
field carrying a secret under a key *not* matching those substrings (e.g.
`credential`, `secret_value` already partially covered, `bearer`) would be
logged verbatim. The masking works for today's fields but is a maintenance
hazard. Consider an explicit allowlist or a dedicated `_SENSITIVE_FIELDS` set.
Flagged, not P0, because no current call site logs a raw secret.

### P2 — inefficiency / consistency

**C-P2 (also S2) — `PUNCH_SHIM_AUTO_UPDATE` default disagreement.** Code
default is `"0"` (off, `shim_server.py:272`; README l.60 agrees). `shim.env.example`
(l.37–41) and `installer/bundled-README.md` (l.51) say the default is "1 /
enabled." Pick one and align. If the manifest sets the env var to `1` for
shipped installs, say so explicitly in `shim.env.example` ("code default off;
the .mcpb ships it on") so the two statements stop contradicting.

**P2-1 — No HTTP connection reuse, by deliberate design — confirm it's still
wanted.** Every backend call builds a fresh `httpx.Client` with
`max_keepalive_connections=0` + `Connection: close`
(`shim_server.py:472`, l.476), and `NegotiateAuth` mints a fresh SPNEGO token
on **every** request (l.396–401, docstring l.387–390). On a stdio shim with
low call volume this is a defensible stale-socket defense, and the comments
justify it. But it means: a fresh TCP connect (5 s connect budget) + a fresh
Kerberos handshake per tool call, plus `_maybe_reload_backends` (a `stat`)
and `_maybe_refresh_catalogue` (throttled `/health` GET) on the hot path of
*every* `_call_remote` (l.1757, l.1761). Worth a note: the catalogue probe at
30 s and reload at 2 s throttles keep this bounded, but the per-call
no-keepalive + per-call token mint is a real latency floor. Not a bug — flag
for awareness; if call volume rises, an auth-context/connection cache keyed on
backend would help.

**P2-2 — `_register_one` builds tool functions via `exec` of a generated
source string.** `shim_server.py:1966–1972` constructs a `def` as text and
`exec`s it to get a typed signature for FastMCP. It is guarded (description is
sanitised l.1955–1957; names are `_`/`-`-normalised; v3.2.3 wraps each
registration in try/except l.1989). It works and is tested, but generating +
`exec`-ing Python source per tool (175+ tools) is harder to reason about than
building a `pydantic`/`inspect.Signature` model directly. The sanitisation
looks adequate for trusted-server schemas, but since the *description* comes
from the upstream server it is an exec'd-string-with-server-controlled-content
surface. Low risk given the server is first-party; flag as a maintainability /
defense-in-depth note.

### P3 — naming / minor / dead code

**P3-1 — Prefix-separator naming drift documented but stale in the module
docstring.** The module docstring (l.32–44) describes the collision rule using
`B.T` (dot separator) and the config schema examples use `.`, but the actual
registration uses `_` (underscore) since v2.2.6/v2.2.9 (l.1495,
comment l.1489–1493, l.1959–1964). The docstring's "Tool naming" section is
internally inconsistent with the code's `f"{backend.name}_{orig}"`. Cosmetic
but confusing to a new reader. (Docstring inside `.py`, so flagged not edited.)

**P3-2 — `_bundled_server_version()` does a second `re` import under an
alias.** `shim_server.py:1049` `import re as _re` despite `re` already imported
at module top (l.127). Harmless, but redundant; use the module-level `re`.

**P3-3 — `_make_forwarder` produces a closure whose `clean` filter drops
falsy-but-meaningful values.** `shim_server.py:1888`:
`{k: v for k, v in kwargs.items() if v not in (None, "")}`. Empty string is
dropped (intended — empties are usually "unset"), but note that a legitimately
empty-string argument can never be sent to a backend. Combined with the
generated forwarder passing **all** `locals()` (l.1969), defaults of `None`
are also filtered. This matches the "omit unset" intent and is tested
indirectly, but it means a tool that semantically distinguishes `""` from
"absent" can't express `""`. Flag as a known limitation.

**P3-4 — `installer/punch-analytics-install.ps1.tmpl` is the pre-Kerberos
(API-key) installer and still embeds a plaintext key.** Not dead per se (it's
the service-identity path), but its header comment (l.2–14) and prompts make
no mention that the Kerberos template is now the default for humans. Both
templates coexist; ensure the renderer picks the Kerberos one for human users.
Naming: the two templates differ only by a `-kerberos` suffix; consider
renaming the legacy one `…-apikey.ps1.tmpl` for clarity. (Flag, not a bug.)

**P3-5 — `reset-punch.cmd:64` points at the wrong GitHub repo.** It tells the
operator to reinstall from `github.com/Zenotech-bv/punch-analytics-shim/releases`,
but the canonical repo everywhere else (manifest `source_url`, README,
bundled-README, `_GITHUB_RAW_BASE` l.1189) is
`Zenotech-bv/production_mcp_shim`. The `punch-analytics-shim` repo name appears
nowhere else and is likely dead/renamed. Fix to `production_mcp_shim`.

**No committed secrets.** The two installer templates carry `{{API_KEY}}` /
`{{UPN}}` placeholders only (rendered server-side). `.gitignore` excludes
`.env`, `shim.env`, `shim.log`, `*.bak`, `*.new`. No real keys found in any
tracked file. The committed `releases/*.mcpb` (24 files), `tools.json` (254 KB),
and `uv.lock` (185 KB) are intentional release/offline-fallback artifacts, not
scratch — `build-mcpb.ps1` and `generate_bundled_tools.py` document their role.

**Duplication note.** `NegotiateAuth` (l.384–418) is a clean, self-contained
SPNEGO httpx.Auth. It is conceptually similar to server-side negotiate
handling in pa_v2, but a *client*-side minter belongs on the shim and is not
duplicated logic to consolidate — different role (token mint vs token
validate). No action.

---

## 5. Implementation plan (phased; S/M/L; deps; needs-Matt)

### Phase A — zero-risk doc/string fixes (do now)
- **A1 (S, done):** README v1-host line — **fixed in §2 (F1).**
- **A2 (S):** Fix `shim_diagnostics` → `shim_info` in the `UnknownTool`
  message (`shim_server.py:1771`). *Code change — out of audit scope (report
  only); flagged for a follow-up edit.*
- **A3 (S):** Fix `reset-punch.cmd:64` repo URL → `production_mcp_shim`.
  *Script change — flagged.*
- Deps: none. needs-Matt: no.

### Phase B — the P0 reset bug (do next, needs verify)
- **B1 (S, code):** In `reset-punch.cmd`, change the four `backends.json`
  delete targets from `…\PunchAnalytics\backends.json` to
  `…\Punch\backends.json` (lines 43, 44, 49, 50). Leave the `shim.log` targets
  as-is (already correct).
- **B2 (S, verify):** Smoke-test on a laptop with a real
  `%APPDATA%\Punch\backends.json` — confirm the script now reports "deleted"
  for it. Per the "smoke-test the full round trip" standard, don't declare
  fixed on a dry read.
- Deps: B1→B2. needs-Matt: yes (run on a real install / he owns the recovery
  flow handed to users).

### Phase C — discovery + auto-update default (decision then code)
- **C1 (S, needs-Matt decision):** Resolve `PUNCH_SHIM_AUTO_UPDATE` default
  (off in code vs "default on" in two docs). Decide the intended product
  behaviour, then align `shim.env.example`, `bundled-README.md`, README, and
  (if needed) the manifest env. (S2/C-P2.)
- **C2 (M):** Decide discovery's auth contract (P1-2). Either default
  discovered backends to `negotiate`, or document discovery as negotiate-only,
  or carry+resolve a key. Add a test covering an x-punch-auth discovered
  backend's `is_configured`.
- Deps: independent. needs-Matt: C1 yes (product call); C2 yes (deployment
  contract).

### Phase D — hardening / maintainability (backlog)
- **D1 (M):** Replace substring-based log masking with an explicit
  sensitive-field allowlist (P1-3); add a test asserting `auth_ok` /
  `auth_changes` survive into logs while `key`/`token` values don't.
- **D2 (L, optional):** Evaluate replacing the `exec`-generated forwarders
  (P2-2) with `inspect.Signature`-built functions; large surface, well-tested
  today, low urgency.
- **D3 (S):** Reconcile the module-docstring `B.T` (dot) naming with the
  actual `B_T` (underscore) implementation (P3-1); drop the redundant
  `import re as _re` (P3-2).
- **D4 (S, doc):** README "How updates ship" §1 monorepo path (S4) and the
  "~1200 lines" count (S1) — update once the canonical-edit workflow is
  confirmed with Matt.
- Deps: none block each other. needs-Matt: D4 (confirm edit workflow); others
  no.

---

*End of audit. Markdown-only changes were made (README F1); no code, scripts,
or git operations were touched.*
