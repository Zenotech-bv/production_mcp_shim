"""
Punch Analytics — Cowork stdio shim. (Canonical source for auto-update.)

This runs on each user's laptop and is consumed by Cowork / Claude Desktop /
Claude Code via their local stdio MCP plugin mechanism. It does NOT connect
to SAP. Every tool call is forwarded as a POST to the matching backend
MCP server.

v2.2.4 — multi-backend federation.

Pre-v2.2.4 the shim talked to a single backend (the SAP Analytics MCP
server) via two env vars: PUNCH_SAP_URL + PUNCH_SAP_KEY. v2.2.4 adds
support for federating across N backend MCP servers (SAP, Tempo, Jira,
Zabbix, Darwinbox, Windchill, Polarion, SVN, ...).

## Config resolution

The shim looks for a ``backends.json`` file in this order:

   1. ``$PUNCH_BACKENDS_FILE`` env var, if set.
   2. ``%APPDATA%\\Punch\\backends.json`` on Windows.
   3. ``~/.config/punch/backends.json`` on Linux/Mac.

If found, the file's ``backends`` list defines the federation. Each
backend has its own URL + auth header + key. See
``shim.env.example`` (or the README) for the schema.

If NOT found, the shim falls back to single-backend mode using the
legacy ``PUNCH_SAP_URL`` + ``PUNCH_SAP_KEY`` env vars. Existing v2.2.3
installs upgrade in place without config edits.

## Tool naming (the prefix-on-collision rule)

For every tool ``T`` from backend ``B``:

  - Always register ``B.T`` (the explicit / collision-safe name).
  - ALSO register bare ``T`` IFF no other backend offers ``T``. If two
    backends both offer the same tool name, the bare alias is dropped
    and the shim logs a warning at startup. Existing prompts that
    reference unprefixed tool names keep working in the common
    no-collision case.

The collision-only-strips-bare rule means single-backend deployments
behave exactly as before — every tool keeps its original name.

## Soft-fail on backend unreachable

A backend that's unreachable at startup (DNS failure, connection
refused, /tools timeout) does NOT prevent the shim from starting. The
shim logs a warning, registers zero tools from that backend, and
continues. The shim is healthy as long as ONE backend responds.

## Config file schema

```jsonc
{
  "backends": [
    {
      "name":   "sap",
      "url":    "http://ai.punchpowertrain.com:3000",
      "auth":   "negotiate"
      // v3.0.0 default for humans. No "header" or "key" needed — the
      // shim sends Authorization: Negotiate <token> built from the
      // calling user's Windows logon ticket via SSPI/pyspnego.
    },
    {
      "name":   "supervisor-webhook",
      "url":    "http://ai.punchpowertrain.com:3000",
      "auth":   "x-punch-auth",
      "header": "X-Punch-Auth",
      "key":    "<service-account key>"
    }
  ],
  "primary": "sap"
}
```

The optional ``auth`` field selects the auth mode per backend:

  - ``"negotiate"`` — Kerberos/SPNEGO via Windows SSPI; no key on disk.
    Requires a domain-joined laptop and a server-side SPN for the
    backend's hostname.
  - ``"x-punch-auth"`` — legacy API-key path; the key lives in ``"key"``.

If ``"auth"`` is omitted, the shim defaults to ``"x-punch-auth"`` so
existing pre-v3.0.0 config files keep working unchanged.

The optional ``primary`` field names which backend's URL is used for
auto-update fallback (when GitHub is unreachable). Defaults to the
first entry.

## Legacy env vars (still supported)

    PUNCH_SAP_URL         — default http://ai.punchpowertrain.com:3000
    PUNCH_SAP_KEY         — per-user API key issued by the SAP admin
    PUNCH_SAP_TIMEOUT     — read-timeout seconds, default 300
    PUNCH_SAP_VERIFY_TLS  — "false" to skip TLS verification (internal CA)
    PUNCH_SHIM_AUTO_UPDATE — truthy ("1", "true", "yes", "on", case-insensitive)
                              to enable startup self-update from the canonical
                              GitHub repo + server fallback (v2.2.8 broadened
                              the accepted values from "1"-only)
    PUNCH_SHIM_DEBUG      — "1" to enable DEBUG-level logging

New in v2.2.4:

    PUNCH_BACKENDS_FILE   — explicit path to backends.json (overrides default)

New in v2.3.1:

    PUNCH_SHIM_RELOAD_INTERVAL_S — how often (seconds) the shim re-stat's
                              backends.json to pick up a key rotation.
                              Default 2. The check is a single cheap stat
                              call; only an actual mtime delta triggers
                              the JSON re-read + reconcile. Lowering this
                              shortens the rotation window further; raising
                              it eliminates the per-call stat at the cost
                              of slower rotations.
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import base64
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generator
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import Context, FastMCP

# ---------------------------------------------------------------------------
# Shim self-version. Bump alongside MCPB version (manifest.json::version).
# The server's /shim/manifest.json compares against this to decide whether
# to vend an update.
# ---------------------------------------------------------------------------

# v3.4.0 — call-time backend attribution: every dict response (and the
# known-backend transport-error envelopes) is stamped `_shim_served_by`
# so the client reports which federated backend served a call instead of
# guessing. Strictly additive; see _enrich_response.
# v3.4.1 — refresh the bundled SAP offline-fallback tools.json to the live
# pa_v2 catalogue (was a frozen 104-tool V1 snapshot from the 2.4.1 baseline,
# which the v2 backend doesn't serve). build-mcpb.ps1 now copies the repo's
# tools.json into the .mcpb so a fresh install's offline fallback is current,
# not reliant on auto-refresh. No shim logic change.
# v3.4.2 — refresh the connector-directory manifest metadata (description,
# long_description, and the declared `tools` array) to the current pa_v2
# catalogue. Prior 3.x builds only bumped manifest.version, so the directory
# showed the v2.4.1 blurb + 92 V1 tool names. build-mcpb.ps1 now reads the
# description from manifest-{description,long-description}.txt and syncs the
# tools array from the bundled tools.json. No shim logic change.
# v3.4.3 — slow-connect-on-update fix. Each update extracts the .mcpb to a NEW
# dir, so `uv run` rebuilt the Python env from scratch (download CPython +
# ~33 wheels incl. pywin32 9MB) before the shim could start — minutes on a
# corporate network, because the MS Store sandbox doesn't persist uv's default
# cache. Fix: pin UV_PROJECT_ENVIRONMENT / UV_CACHE_DIR / UV_PYTHON_INSTALL_DIR
# to ${HOME}/.punch-shim/* in the manifest env so the venv + cache + interpreter
# PERSIST across updates (update -> reuse, not rebuild), and ship a uv.lock so
# `uv run` skips re-resolution. First install still pays once; updates after are
# seconds. No shim logic change.
# v3.4.4 — one-click install fix. Claude Desktop 1.12603.x stopped recognising
# the legacy DXT manifest key: with `dxt_version` present (and no
# `manifest_version`), the installer can't identify the server block ("not a
# Node.js server or a Python server or no entry point specified" -> falls back
# to basic execution) and the packed-.mcpb one-click install no longer
# completes — only a hand-extracted copy ran. build-mcpb.ps1 now emits the
# current MCPB key `manifest_version: "0.3"` instead of `dxt_version: "0.1"`,
# which every build had silently inherited from the 2.4.1 baseline. No shim
# logic change.
# v3.4.6 — manifest self-heal (delivers the v3.4.3 fix to already-installed
# laptops). The v3.4.3 persistent-uv-env fix lives entirely in the packaged
# manifest.json (server.mcp_config.env -> UV_* under ${HOME}/.punch-shim/*,
# injected by build-mcpb.ps1). But _maybe_self_update only ever rewrites THIS
# file (shim_server.py); it never touches manifest.json. So a laptop that
# auto-updated the shim source past v3.4.3 kept its pre-fix packaged manifest
# and still paid the cold `uv run` re-provision (download CPython + ~33 wheels,
# minutes on a corporate network) on EVERY launch. _heal_manifest_uv_env closes
# that distribution gap through the one artifact that does reach laptops hands-
# off (this file): on startup it injects the UV_* keys into the packaged
# manifest if they're absent. The client re-reads the manifest on its next
# launch, so the env converges with no per-laptop action. Best-effort +
# idempotent; never blocks startup. Logic change, but no behaviour change for
# an already-fixed (v3.4.3+) manifest — it's a no-op there.
_SHIM_VERSION = "3.5.3"
# v3.5.2 — OIDC directive pre-flight (the /auth/mode probe, token refresh, and
#          up-to-120s first-time interactive sign-in) moved OFF the startup path
#          onto a background daemon thread. shim_ready is no longer gated on
#          network round-trips; a backend keeps its working negotiate credential
#          until the directive resolves, then flips to oidc in place. See
#          _apply_auth_directives_async.
# v3.5.1 — auto-update no longer re-execs in-session (os.execv breaks Claude
#          Desktop's stdio pipe on Windows and mangles the spaced install
#          path). Updates now stage to disk and apply on the next launch.
#          See _maybe_self_update.


# ---------------------------------------------------------------------------
# Env / dotenv
# ---------------------------------------------------------------------------

# Look for shim.env next to this file, then fall back to process env.
_SHIM_ENV = Path(__file__).parent / "shim.env"
if _SHIM_ENV.exists():
    load_dotenv(_SHIM_ENV, override=False)
load_dotenv()  # also pick up a .env if the user keeps one

# Legacy single-backend env vars — used as fallback when backends.json
# is absent. Also used at module load before per-backend config is
# resolved (e.g. shim_start log before we know which backend is primary).
PUNCH_SAP_URL = os.getenv("PUNCH_SAP_URL", "http://ai.punchpowertrain.com:3000").rstrip("/")
PUNCH_SAP_KEY = os.getenv("PUNCH_SAP_KEY", "").strip()

# v2.2.7 — placeholder-key guard. The shim's module-level _load_backends()
# call (line ~559) runs against the REAL %APPDATA%\Punch\backends.json
# path whenever this module is imported. If pytest imports the shim with
# `PUNCH_SAP_KEY="test-fixture-key"` already set (which the test files
# in tests/test_v2_2_4_multi_backend.py + tests/test_v2_2_5_backends_seed.py
# do via os.environ.setdefault at module top-level), the auto-seed wrote
# that placeholder string into a real user's APPDATA, where it stayed
# undetected across multiple shim upgrades because the auto-seed's
# `if cfg_path.exists(): return False` guard refused to overwrite.
#
# v2.2.7 detects placeholder-shaped values and refuses to USE them as
# auth -- both at env-load time AND at backends.json load time. The
# net effect: a test-fixture-contaminated user falls through to the
# env-var fallback (which, in Claude Desktop with a properly-filled
# user_config, has the real key) instead of sending a known-bad
# placeholder on every request.
#
# Detection: regex matches obvious test-fixture shapes (case-insensitive).
# Also rejects suspiciously short keys -- real keys from
# `secrets.token_urlsafe(32)` are 43 chars.
_PLACEHOLDER_KEY_PATTERNS = (
    re.compile(r"^test[-_]?(fixture|key|placeholder)", re.IGNORECASE),
    re.compile(r"^(replace|insert|paste|your[-_]?key)", re.IGNORECASE),
    re.compile(r"^(xxx|placeholder|api[-_]?key|secret)$", re.IGNORECASE),
)
_MIN_REAL_KEY_LEN = 16  # real keys are 43+ chars; 16 is a generous floor

# v3.0.7 — strict-shape real-key predicate. Used at seed time
# (`_maybe_seed_backends_file`) to only flip to the x-punch-auth seed
# branch when PUNCH_SAP_KEY plausibly IS a key (not install-dialog
# garbage). v3.0.7 also wired this into a load-time auto-archive of
# stale-xpunch backends; that was rolled back in v3.0.8 because it
# couldn't catch wrong-host cases (e.g. an install with auth=x-punch-auth
# pointed at the zabbix backend, off the canonical-Kerberos-host list)
# without false-positives, and the deterministic `reset-punch.cmd`
# covers every broken-state shape uniformly. Keep this predicate; it
# remains the seed-time gate.
_REAL_KEY_RE = re.compile(r"^[A-Za-z0-9_+=/-]{16,128}$")


def _looks_like_real_key(key: str) -> bool:
    """True if `key` plausibly came from a real key generator (token_urlsafe
    / hex / b64). False for install-dialog garbage. Permissive at the
    boundaries to avoid false-positives on legitimate service-account keys."""
    return bool(key) and bool(_REAL_KEY_RE.match(key))


def _looks_like_placeholder(key: str) -> bool:
    if not key:
        return False
    if len(key) < _MIN_REAL_KEY_LEN:
        return True
    for pat in _PLACEHOLDER_KEY_PATTERNS:
        if pat.match(key):
            return True
    # v3.0.7 — anything failing the real-key shape check is a placeholder.
    # This catches install-dialog typos that look like words ("kerberos",
    # "mypassword") with no special chars but happen to be ≥16 chars.
    if not _looks_like_real_key(key):
        return True
    return False


# Apply guard to the legacy env-var fallback. If PUNCH_SAP_KEY is a
# placeholder, treat it as if unset so the shim doesn't send it AND
# the auto-seed doesn't write it to a fresh backends.json.
if PUNCH_SAP_KEY and _looks_like_placeholder(PUNCH_SAP_KEY):
    sys.stderr.write(
        f"[shim v{_SHIM_VERSION}] WARNING: PUNCH_SAP_KEY env var looks "
        f"like a placeholder ({PUNCH_SAP_KEY[:8]}...); treating as unset. "
        f"Check your Claude Desktop user_config / shim.env.\n"
    )
    PUNCH_SAP_KEY = ""

PUNCH_SAP_TIMEOUT = float(os.getenv("PUNCH_SAP_TIMEOUT", "300"))
_verify_tls = os.getenv("PUNCH_SAP_VERIFY_TLS", "true").lower() != "false"
# v2.2.8: accept any of the standard truthy values. Pre-v2.2.8 this only
# matched "1"; "true" / "yes" / "on" silently failed even though those
# are what the MCPB user_config field defaults to in Claude Desktop's
# UI. Comparison is case-insensitive after a strip().
_AUTO_UPDATE = os.getenv("PUNCH_SHIM_AUTO_UPDATE", "0").strip().lower() in (
    "1", "true", "yes", "on", "enable", "enabled",
)
_DEBUG = os.getenv("PUNCH_SHIM_DEBUG", "0").strip() == "1"


# ---------------------------------------------------------------------------
# Logging (preserved verbatim from v2.2.2 — multi-backend only changes
# the call-site fields, not the sink config)
# ---------------------------------------------------------------------------


def _resolve_log_dir() -> Path:
    """Resolve the per-platform writable log directory."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            d = Path(local) / "PunchAnalytics"
            try:
                d.mkdir(parents=True, exist_ok=True)
                return d
            except OSError:
                pass
        return Path(tempfile.gettempdir()) / "PunchAnalytics"
    home = Path.home()
    d = home / ".local" / "share" / "punch-analytics"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return Path(tempfile.gettempdir()) / "punch-analytics"


def _setup_logging() -> logging.Logger:
    log_dir = _resolve_log_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log_dir = Path(tempfile.gettempdir())

    logger = logging.getLogger("punch_shim")
    logger.setLevel(logging.DEBUG if _DEBUG else logging.INFO)
    for h in list(logger.handlers):
        logger.removeHandler(h)

    class _JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            payload = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
                "level": record.levelname,
                "event": record.msg,
            }
            extra = getattr(record, "extra_fields", None)
            if isinstance(extra, dict):
                for k, v in extra.items():
                    if "auth" in k.lower() or "key" in k.lower() or "token" in k.lower():
                        continue
                    payload[k] = v
            return json.dumps(payload, default=str)

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "shim.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(_JsonFormatter())
        logger.addHandler(file_handler)
    except OSError:
        pass

    class _StderrFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            extra = getattr(record, "extra_fields", None) or {}
            tag = "ERROR" if record.levelno >= logging.ERROR else (
                "WARN" if record.levelno >= logging.WARNING else "INFO"
            )
            line = f"[shim {tag}] {record.msg}"
            if extra:
                snippet = " ".join(
                    f"{k}={v}" for k, v in extra.items()
                    if "auth" not in k.lower()
                    and "key" not in k.lower()
                    and "token" not in k.lower()
                )
                line += f" {snippet}"
            return line

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(_StderrFormatter())
    stderr_handler.setLevel(logging.DEBUG if _DEBUG else logging.WARNING)
    logger.addHandler(stderr_handler)
    return logger


_log = _setup_logging()


def _log_event(event: str, level: int = logging.INFO, **fields):
    """Emit a structured event. `fields` carries arbitrary kwargs."""
    _log.log(level, event, extra={"extra_fields": fields})


# ---------------------------------------------------------------------------
# v2.2.4 — backends config loader
# ---------------------------------------------------------------------------


_VALID_AUTH_MODES = ("x-punch-auth", "negotiate", "oidc")


class NegotiateAuth(httpx.Auth):
    """SPNEGO/Kerberos `Authorization: Negotiate` for httpx, via pyspnego + SSPI.

    Mints a fresh Negotiate token from the calling user's Windows logon
    ticket on every request — matches the shim's max_keepalive_connections=0
    stance (no socket pooling, no auth context reuse). The two-step path
    handles the rare case where the server responds 401 with a continuation
    token (NTLM-fallback or multi-leg Kerberos)."""

    def __init__(self, hostname: str):
        self._hostname = hostname

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        import spnego  # local import: only loaded when a negotiate backend is used
        ctx = spnego.client(hostname=self._hostname, service="HTTP", protocol="negotiate")
        out_token = ctx.step()
        if out_token:
            request.headers["Authorization"] = f"Negotiate {base64.b64encode(out_token).decode('ascii')}"
        response = yield request
        # Continuation path. Only act on a 401 carrying a Negotiate
        # challenge with an embedded token; otherwise we're done.
        while response.status_code == 401:
            challenge = response.headers.get("WWW-Authenticate", "")
            scheme, _, b64_in = challenge.partition(" ")
            if scheme.lower() != "negotiate" or not b64_in:
                return
            try:
                in_token = base64.b64decode(b64_in)
            except (ValueError, TypeError):
                return
            out_token = ctx.step(in_token)
            if not out_token:
                return
            request.headers["Authorization"] = f"Negotiate {base64.b64encode(out_token).decode('ascii')}"
            response = yield request


_OIDC_TENANT    = "1c517a07-0e28-4923-a932-cace3138e782"
_OIDC_CLIENT_ID = "71b30538-8e08-47c6-ab2e-1ca23ce68488"
_OIDC_SCOPE     = "api://a23d5e91-7dd8-4544-8a04-a471bb731406/access_as_user offline_access openid"
_OIDC_AUTHORIZE = f"https://login.microsoftonline.com/{_OIDC_TENANT}/oauth2/v2.0/authorize"
_OIDC_TOKEN     = f"https://login.microsoftonline.com/{_OIDC_TENANT}/oauth2/v2.0/token"
_OIDC_HTTP_TIMEOUT = 20.0
_OIDC_INTERACTIVE_TIMEOUT = 120.0  # bounded browser sign-in wait


class OidcError(Exception):
    """OIDC token acquisition failed (network, user cancel, bad state, etc.)."""


def _pkce_pair() -> tuple[str, str]:
    import secrets, hashlib, base64  # lazy: OIDC-only
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _loopback_serve_once(port_holder: dict, timeout: float) -> dict:
    """Bind an http.server on 127.0.0.1:0, put the chosen port in port_holder,
    serve exactly ONE request, and return the parsed query {code,state} (or
    {error:...}). Lazy-imports http.server. 127.0.0.1 only; shut down after one hit."""
    import http.server, urllib.parse  # lazy: OIDC-only
    result: dict = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.urlparse(self.path).query
            result.update({k: v[0] for k, v in urllib.parse.parse_qs(q).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>Punch sign-in complete. You can close this tab.</body></html>")

        def log_message(self, *a):  # silence
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port_holder["port"] = srv.server_address[1]
    srv.timeout = timeout
    srv.handle_request()  # exactly one; blocks up to `timeout`
    srv.server_close()
    return result


def _loopback_receive_code(*, state: str, timeout: float, login_hint: str = "") -> tuple[str, str, str, str]:
    """Open the browser to the authorize URL and receive the code on a loopback
    listener. Returns (code, redirect_uri, code_verifier, nonce). Raises OidcError
    on timeout / user cancel / state mismatch. Binds 127.0.0.1 on an ephemeral port
    (via _loopback_serve_once, in a daemon thread) and serves exactly one request."""
    import webbrowser, secrets, threading, time  # lazy: OIDC-only
    from urllib.parse import urlencode           # lazy: OIDC-only

    verifier, challenge = _pkce_pair()
    nonce = secrets.token_urlsafe(24)
    holder: dict = {}     # the server thread sets holder["port"] once bound
    captured: dict = {}   # the single callback's {code,state,error}

    def _serve():
        captured.update(_loopback_serve_once(holder, timeout))

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    for _ in range(200):          # wait up to ~2s for the ephemeral port to bind
        if "port" in holder or not t.is_alive():
            # Either the real listener bound (holder["port"] set), or the
            # server thread already finished — e.g. a test double for
            # _loopback_serve_once that returns immediately without ever
            # touching port_holder. Don't treat the latter as a bind failure;
            # let the state/code checks below produce the right error.
            break
        time.sleep(0.01)
    if "port" not in holder and t.is_alive():
        raise OidcError("loopback listener did not bind")
    redirect_uri = f"http://localhost:{holder['port']}" if "port" in holder else ""

    params = {
        "client_id": _OIDC_CLIENT_ID, "response_type": "code",
        "redirect_uri": redirect_uri, "scope": _OIDC_SCOPE,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "state": state, "nonce": nonce,
    }
    if login_hint:
        params["login_hint"] = login_hint
    webbrowser.open(_OIDC_AUTHORIZE + "?" + urlencode(params))

    t.join(timeout + 5)
    if not captured:
        raise OidcError("sign-in timed out or was cancelled")
    if captured.get("error"):
        raise OidcError(f"authorize error: {captured.get('error')}")
    if not secrets.compare_digest(captured.get("state") or "", state):
        raise OidcError("state mismatch on loopback callback (rejected)")
    code = captured.get("code")
    if not code:
        raise OidcError("no code on loopback callback")
    return code, redirect_uri, verifier, nonce


def _exchange_code(code: str, verifier: str, redirect_uri: str) -> dict:
    data = {
        "grant_type": "authorization_code", "client_id": _OIDC_CLIENT_ID,
        "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier,
        "scope": _OIDC_SCOPE,
    }
    try:
        r = httpx.post(_OIDC_TOKEN, data=data, timeout=_OIDC_HTTP_TIMEOUT)
    except httpx.RequestError as e:
        # DNS/VPN/timeout etc. — a network failure, not an HTTP error response.
        # Must still surface as OidcError so callers (and _apply_auth_directives'
        # narrower `except OidcError`, pre-Fix-1b) can catch it uniformly.
        raise OidcError(f"token endpoint unreachable: {e}") from e
    if r.status_code != 200:
        raise OidcError(f"token exchange failed: {r.status_code} {r.text[:200]}")
    data_out = r.json()
    if not data_out.get("access_token"):
        raise OidcError(f"token response missing access_token: {r.text[:200]}")
    return data_out


def _verify_id_token_nonce(toks: dict, expected_nonce: str) -> None:
    """Bind the id_token (if present) to THIS request via its nonce claim
    (token-replay/injection defense, design §2/§10). Signature isn't verified
    client-side — the token came directly from Entra over TLS in the code
    exchange, and the access token is pa_v2's actual trust anchor. If no
    id_token is present, skip — don't fail (scope doesn't guarantee one)."""
    id_token = toks.get("id_token")
    if not id_token:
        return
    import jwt  # lazy: OIDC-only
    import secrets  # lazy: OIDC-only (constant-time compare)
    claims = jwt.decode(id_token, options={"verify_signature": False,
                                            "verify_aud": False,
                                            "verify_exp": False})
    if not secrets.compare_digest(claims.get("nonce") or "", expected_nonce):
        raise OidcError("id_token nonce mismatch")


def _refresh_token(refresh_token: str) -> dict:
    data = {
        "grant_type": "refresh_token", "client_id": _OIDC_CLIENT_ID,
        "refresh_token": refresh_token, "scope": _OIDC_SCOPE,
    }
    try:
        r = httpx.post(_OIDC_TOKEN, data=data, timeout=_OIDC_HTTP_TIMEOUT)
    except httpx.RequestError as e:
        raise OidcError(f"token endpoint unreachable: {e}") from e
    if r.status_code != 200:
        raise OidcError(f"refresh failed: {r.status_code} {r.text[:200]}")
    data_out = r.json()
    if not data_out.get("access_token"):
        raise OidcError(f"token response missing access_token: {r.text[:200]}")
    return data_out


def _oidc_cache_dir():
    d = _resolve_log_dir() / "oidc"   # %LOCALAPPDATA%\PunchAnalytics\oidc (test-isolated)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _oidc_cache_path(upn: str):
    import hashlib  # lazy
    tag = hashlib.sha256(upn.lower().encode("utf-8")).hexdigest()[:16]
    return _oidc_cache_dir() / f"tok_{tag}.bin"


def _dpapi_available() -> bool:
    try:
        import win32crypt  # noqa: F401  (lazy)
        return True
    except Exception:
        return False


def _dpapi_protect(raw: bytes) -> bytes:
    import win32crypt  # lazy
    # user-scoped DPAPI (no LOCALMACHINE flag): file-copy/backup theft yields ciphertext.
    return win32crypt.CryptProtectData(raw, "punch-oidc", None, None, None, 0)


def _dpapi_unprotect(blob: bytes) -> bytes:
    import win32crypt  # lazy
    _desc, out = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
    return out


def _token_cache_write(upn: str, tokens: dict) -> None:
    import json, base64  # lazy: both are cheap stdlib, but kept out of module load with the rest of OIDC
    payload = json.dumps(tokens).encode("utf-8")
    path = _oidc_cache_path(upn)
    try:
        if _dpapi_available():
            path.write_bytes(_dpapi_protect(payload))
        else:
            # plaintext fallback — still per-Windows-user isolated under LOCALAPPDATA.
            path.write_bytes(b"PLAIN:" + base64.b64encode(payload))
        try:
            import os as _os
            _os.chmod(path, 0o600)   # on Windows this only clears the read-only attribute bit,
            # not owner/other perms; real isolation is NTFS ACLs inherited from the
            # per-user %LOCALAPPDATA% dir, plus DPAPI. Harmless best-effort elsewhere.
        except OSError:
            pass
    except OSError as e:
        _log_event("oidc_cache_write_failed", level=logging.WARNING, error=str(e))


def _token_cache_read(upn: str) -> dict | None:
    import json, base64  # lazy
    path = _oidc_cache_path(upn)
    try:
        blob = path.read_bytes()
    except OSError:
        return None
    try:
        if blob.startswith(b"PLAIN:"):
            raw = base64.b64decode(blob[len(b"PLAIN:"):])
        else:
            raw = _dpapi_unprotect(blob)
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        _log_event("oidc_cache_read_failed", level=logging.WARNING, error=str(e))
        return None


def _oidc_acquire_token(upn: str, *, allow_interactive: bool) -> str:
    import time as _t, secrets  # lazy
    cached = _token_cache_read(upn)
    if cached:
        if cached.get("access_token") and float(cached.get("expires_at", 0)) - 60 > _t.time():
            return cached["access_token"]
        if cached.get("refresh_token"):
            try:
                toks = _refresh_token(cached["refresh_token"])
                _persist_tokens(upn, toks)
                return toks["access_token"]
            except OidcError as e:
                _log_event("oidc_refresh_failed", level=logging.WARNING, error=str(e))
                # fall through to interactive (if allowed)
    if not allow_interactive:
        raise OidcError("no usable OIDC token and interactive sign-in not allowed here")
    state = secrets.token_urlsafe(24)
    code, redirect_uri, verifier, nonce = _loopback_receive_code(
        state=state, timeout=_OIDC_INTERACTIVE_TIMEOUT, login_hint=upn)
    toks = _exchange_code(code, verifier, redirect_uri)
    _verify_id_token_nonce(toks, nonce)
    _persist_tokens(upn, toks)
    return toks["access_token"]


def _persist_tokens(upn: str, toks: dict) -> None:
    import time as _t  # lazy
    try:
        exp = int(toks.get("expires_in", 3600))
    except (ValueError, TypeError):
        exp = 3600
    rec = {
        "access_token": toks.get("access_token"),
        "refresh_token": toks.get("refresh_token"),
        "expires_at": _t.time() + exp,
    }
    _token_cache_write(upn, rec)


def _local_windows_upn() -> str | None:
    """Resolve the logged-in Windows UPN offline (no DC/Kerberos). Tries
    pywin32 GetUserNameEx(NameUserPrincipal), then `whoami /upn`, then env."""
    # 1) pywin32
    try:
        import win32api  # lazy
        NameUserPrincipal = 8
        upn = win32api.GetUserNameEx(NameUserPrincipal)
        if upn and "@" in upn:
            return upn.strip().lower()
    except Exception:
        pass
    # 2) whoami /upn
    try:
        import subprocess  # lazy
        out = subprocess.run(["whoami", "/upn"], capture_output=True, text=True, timeout=5)
        cand = out.stdout.strip()
        if cand and "@" in cand:
            return cand.lower()
    except Exception:
        pass
    # 3) env
    user = os.environ.get("USERNAME", "").strip()
    dom = os.environ.get("USERDNSDOMAIN", "").strip()
    if user and dom:
        return f"{user}@{dom}".lower()
    return None


def _query_auth_mode(base_url: str, upn: str) -> str:
    """Ask the server which auth this user should use. Kerberos-independent
    (unauthenticated GET). Returns 'oidc' ONLY on an explicit 200 'oidc';
    everything else (non-200, non-'oidc' body, network error) -> 'kerberos'."""
    try:
        r = httpx.get(base_url.rstrip("/") + "/auth/mode",
                      params={"upn": upn}, timeout=5.0)
        if r.status_code == 200 and r.text.strip().lower() == "oidc":
            return "oidc"
    except Exception as e:
        _log_event("auth_mode_query_failed", level=logging.WARNING, error=str(e))
    return "kerberos"


class OidcAuth(httpx.Auth):
    """Entra OIDC Bearer for httpx. Attaches `Authorization: Bearer <token>`,
    acquiring/refreshing via the module-level _oidc_acquire_token (Tasks 2-3).
    Mirrors NegotiateAuth; all OIDC-only imports are lazy in the token helpers."""

    def __init__(self, upn: str):
        self._upn = upn

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        token = _oidc_acquire_token(self._upn, allow_interactive=False)
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


@dataclass
class Backend:
    """One backend MCP server. Each backend has its own URL + auth +
    fetched tool set."""
    name: str
    url: str
    header: str
    key: str
    # v3.0.0 — per-backend auth mode. "x-punch-auth" (default; sends the
    # header+key) or "negotiate" (Kerberos via the user's Windows logon
    # ticket, no key on disk).
    auth: str = "x-punch-auth"
    # Filled in at fetch time; empty until _load_tools_multi runs.
    tools: list[dict] = field(default_factory=list)
    # Runtime-computed effective auth mode. Defaults to `auth`; the startup
    # decision function (_apply_auth_directives) may raise it to "oidc" for a
    # human backend the server directs + we can acquire a token for. backends.json
    # is NEVER edited — this is the override channel.
    effective_auth: str = ""
    _oidc_upn: str = ""   # the local UPN the decision function resolved (Task 4)
    _fell_back: bool = False   # directed oidc but fell back to negotiate this session

    def __post_init__(self):
        self.url = self.url.rstrip("/")
        # Defensive: a backend with no auth key means we can't talk
        # to it. Don't crash here; the fetch step will skip it and
        # log a warning.
        self.key = self.key.strip() if isinstance(self.key, str) else ""
        self.auth = self.auth.strip().lower() if isinstance(self.auth, str) else "x-punch-auth"
        if self.auth not in _VALID_AUTH_MODES:
            # Unknown mode -> log+default. Same posture as a missing key:
            # don't crash the shim, just mark this backend unusable.
            _log_event("backend_unknown_auth_mode", level=logging.WARNING,
                       backend=self.name, auth=self.auth,
                       valid_modes=list(_VALID_AUTH_MODES))
            self.auth = "negotiate"   # safe human default (a human row has no key)
        if not self.effective_auth:
            self.effective_auth = self.auth

    @property
    def is_configured(self) -> bool:
        if self.auth in ("negotiate", "oidc"):
            # No key required — the Windows logon ticket / OIDC token is the credential.
            return bool(self.url)
        return bool(self.url and self.key and self.header)

    def http_client(self, *, read_timeout: float | None = None) -> httpx.Client:
        """Build a fresh httpx.Client for this backend.

        v2.2.2 stale-socket defenses preserved:
          - max_keepalive_connections=0 (no socket pooling across calls)
          - Connection: close header (server closes after response)
          - connect=5.0s (fail fast on unreachable backend)

        v3.0.0 — when self.auth == "negotiate", attaches a NegotiateAuth
        and omits the X-Punch-Auth header. Otherwise (default
        "x-punch-auth") behaves exactly as before.

        Slice B — dispatches on `effective_auth` (falls back to `auth` if
        unset) so a startup decision function can raise a human backend to
        "oidc" without editing backends.json. "oidc" attaches an OidcAuth
        and, like "negotiate", omits the X-Punch-Auth header.
        """
        rt = read_timeout if read_timeout is not None else PUNCH_SAP_TIMEOUT
        timeouts = httpx.Timeout(connect=5.0, read=rt, write=10.0, pool=5.0)
        limits = httpx.Limits(max_keepalive_connections=0, max_connections=10)
        bundled_v = _bundled_server_version() or "unknown"
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Connection": "close",
            "User-Agent": f"punch-shim/{_SHIM_VERSION}",
            "X-Punch-Shim-Backend": self.name,
            "X-Punch-Shim-Bundled-Version": bundled_v,
            "X-Punch-Shim-Version": _SHIM_VERSION,
            "X-Punch-Shim-Pid": str(os.getpid()),
            "X-Punch-Shim-Auto-Update": "1" if _AUTO_UPDATE else "0",
        }
        auth: httpx.Auth | None = None
        mode = self.effective_auth or self.auth
        # v3.5.2 - snapshot _oidc_upn alongside the mode. The background directive
        # pre-flight (another thread) can flip effective_auth and clear/set
        # _oidc_upn between our reads; if we observe mode "oidc" without a
        # published upn (mid-resolution, or a reprobe reset), use the working
        # negotiate path rather than build an unusable OidcAuth("").
        oidc_upn = self._oidc_upn
        if mode == "oidc" and not oidc_upn:
            mode = "negotiate"
        if mode == "negotiate":
            host = urlparse(self.url).hostname
            if not host:
                raise ValueError(
                    f"backend {self.name!r}: auth=negotiate requires a "
                    f"URL with a hostname; got {self.url!r}"
                )
            auth = NegotiateAuth(host)
        elif mode == "oidc":
            auth = OidcAuth(oidc_upn)
        else:
            headers[self.header] = self.key
        if self._fell_back:
            headers["X-Punch-Auth-Fallback"] = "oidc->negotiate"
        return httpx.Client(
            base_url=self.url,
            timeout=timeouts,
            limits=limits,
            verify=_verify_tls,
            auth=auth,
            headers=headers,
        )


_AUTH_DIRECTIVE_LOCK = threading.Lock()


def _apply_auth_directives(backends: list[Backend], *, block: bool = True,
                           allow_interactive: bool = True) -> list[Backend]:
    """Confirm-before-switch: for each backend the server directs to OIDC AND
    for which we can (silently, else once-interactively) acquire a token, set
    effective_auth='oidc'. Otherwise stay 'negotiate' (never override a working
    Kerberos backend with an unconfirmed directive).

    v3.5.2 - serialised by _AUTH_DIRECTIVE_LOCK so the background startup pass
    (_apply_auth_directives_async) and a concurrent shim_reload reprobe never
    interleave writes to the same Backend objects. block=False (shim_reload)
    skips the reprobe when the startup pass still holds the lock instead of
    waiting on it - that wait could be an up-to-120s interactive sign-in.
    """
    if not _AUTH_DIRECTIVE_LOCK.acquire(blocking=block):
        _log_event("oidc_preflight_busy", level=logging.INFO)
        return backends
    try:
        upn = _local_windows_upn()
        if not upn:
            _log_event("oidc_no_local_upn", level=logging.INFO)
            return backends
        for b in backends:
            # Only humans (negotiate) are candidates; x-punch-auth service backends stay put.
            if b.auth != "negotiate":
                continue
            # Reset to base state EVERY pass (this runs again on shim_reload, over
            # the SAME Backend objects). Without this, a directive that reverts
            # oidc->kerberos would leave a stale effective_auth="oidc" (or a stale
            # _fell_back=True) from a prior pass, since the `directive != "oidc"`
            # continue below never reaches the assignment that would clear it.
            b.effective_auth = b.auth   # negotiate
            b._oidc_upn = ""
            b._fell_back = False
            directive = _query_auth_mode(b.url, upn)
            if directive != "oidc":
                continue
            try:
                # Silent first; interactive allowed once (first activation). Bounded.
                # Broad except: this runs on a daemon thread at startup and must
                # NEVER crash the shim, no matter what the OIDC path raises.
                _oidc_acquire_token(upn, allow_interactive=allow_interactive)
                # Publish _oidc_upn BEFORE flipping effective_auth. A request on
                # another thread reads effective_auth (http_client picks the auth
                # mode) and then _oidc_upn (OidcAuth). Writing the upn first means
                # a reader that sees "oidc" always sees a non-empty upn, never
                # OidcAuth(""); effective_auth is the last write = the publish.
                b._oidc_upn = upn
                b._fell_back = False
                b.effective_auth = "oidc"
                _log_event("oidc_cutover_confirmed", backend=b.name, upn=upn)
            except Exception as e:
                b.effective_auth = "negotiate"     # fail-safe: stay on Kerberos this session
                b._fell_back = True
                _log_event("oidc_fallback_to_negotiate", level=logging.WARNING,
                           backend=b.name, upn=upn, error=str(e))
        return backends
    finally:
        _AUTH_DIRECTIVE_LOCK.release()


def _apply_auth_directives_async(backends: list[Backend], *, block: bool = True,
                                 allow_interactive: bool = True) -> threading.Thread | None:
    """Run _apply_auth_directives on a daemon thread so shim STARTUP never blocks
    on the /auth/mode probe, the OIDC token refresh, or (first activation) the
    up-to-120s interactive browser sign-in.

    Backends keep their configured `auth` (negotiate - a working credential)
    until the directive resolves in the background, then flip to 'oidc' in place;
    a request arriving during the window uses the working negotiate path. Returns
    the started Thread (for callers/tests that want to join), or None when there
    is no negotiate backend to resolve - so a service-only shim skips even the
    whoami/UPN lookup.
    """
    if not any(b.auth == "negotiate" for b in backends):
        return None

    def _run() -> None:
        try:
            _apply_auth_directives(backends, block=block, allow_interactive=allow_interactive)
        except Exception as e:   # a daemon thread must never die noisily
            _log_event("oidc_preflight_thread_error", level=logging.WARNING, error=str(e))

    t = threading.Thread(target=_run, name="punch-oidc-preflight", daemon=True)
    t.start()
    return t


def _resolve_backends_path() -> Path:
    """Pick the backends.json path:
       1. $PUNCH_BACKENDS_FILE if set
       2. %APPDATA%\\Punch\\backends.json on Windows
       3. ~/.config/punch/backends.json elsewhere
    """
    explicit = os.getenv("PUNCH_BACKENDS_FILE", "").strip()
    if explicit:
        return Path(explicit)
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Punch" / "backends.json"
        return Path.home() / "AppData" / "Roaming" / "Punch" / "backends.json"
    return Path.home() / ".config" / "punch" / "backends.json"


def _load_backends_from_file(path: Path) -> tuple[list[Backend], str | None]:
    """Parse backends.json. Returns (list_of_backends, primary_name).
    Logs+raises on malformed files (caller should soft-fail to env-var
    fallback)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log_event("backends_file_parse_error", level=logging.ERROR,
                   path=str(path), error=f"{type(e).__name__}: {e}")
        raise
    if not isinstance(data, dict):
        raise ValueError(f"backends.json must be a JSON object, got {type(data).__name__}")
    raw_backends = data.get("backends")
    if not isinstance(raw_backends, list) or not raw_backends:
        raise ValueError("backends.json must have a non-empty 'backends' array")

    backends: list[Backend] = []
    seen_names: set[str] = set()
    for i, entry in enumerate(raw_backends):
        if not isinstance(entry, dict):
            _log_event("backends_entry_skipped", level=logging.WARNING,
                       index=i, reason="not_a_dict")
            continue
        name = (entry.get("name") or "").strip()
        url = (entry.get("url") or "").strip()
        header = (entry.get("header") or "X-Punch-Auth").strip()
        key = (entry.get("key") or "").strip()
        # v3.0.0 -- per-backend auth mode. Default is x-punch-auth so
        # existing pre-v3.0.0 backends.json files keep working unchanged.
        auth = (entry.get("auth") or "x-punch-auth").strip().lower()
        # v2.2.7 -- placeholder-key guard at file-load time. Skip for
        # negotiate backends since they have no key field to scrutinise.
        if auth != "negotiate" and key and _looks_like_placeholder(key):
            _log_event(
                "backends_entry_placeholder_key",
                level=logging.WARNING,
                index=i, backend=name, key_preview=key[:8] + "...",
                path=str(path),
                note=(
                    "key in backends.json looks like a placeholder -- "
                    "treating as unset. If this is intentional, rename "
                    "the placeholder; otherwise delete the file and let "
                    "the auto-seed recreate it from a real PUNCH_SAP_KEY."
                ),
            )
            key = ""
        if not name:
            _log_event("backends_entry_skipped", level=logging.WARNING,
                       index=i, reason="missing_name")
            continue
        if not url:
            _log_event("backends_entry_skipped", level=logging.WARNING,
                       index=i, name=name, reason="missing_url")
            continue
        if name in seen_names:
            _log_event("backends_entry_skipped", level=logging.WARNING,
                       index=i, name=name, reason="duplicate_name")
            continue
        seen_names.add(name)
        backends.append(Backend(name=name, url=url, header=header, key=key, auth=auth))

    primary = data.get("primary")
    if isinstance(primary, str) and primary in seen_names:
        primary_name = primary
    elif backends:
        primary_name = backends[0].name
    else:
        primary_name = None
    return backends, primary_name


# v2.2.5 — zero-touch backends.json bootstrap.
#
# Background. v2.2.4 introduced the multi-backend federator and the
# backends.json config file, but laptops needed an operator to drop
# that file by hand at `%APPDATA%\Punch\backends.json` before Zabbix
# (or any non-SAP backend) would light up. For Punch's deployment
# shape -- one organisation, one PUNCH_SAP_KEY per user, one set of
# canonical backend URLs -- the file's contents are deterministic.
# v2.2.5 seeds it automatically on first startup when:
#
#   * The resolved path doesn't already have a file (user authority
#     ALWAYS wins; we never clobber an existing config).
#   * PUNCH_SAP_KEY is set (so we have something to populate the
#     `key` field with for every backend).
#
# The seed populates each backend with the user's existing
# PUNCH_SAP_KEY. Per-backend keys can be filled in later by the
# operator -- the SAP server and Zabbix adapter currently share
# `users.json` so a single key works for both, but the structure
# is per-backend so the day a backend gets its own auth surface,
# only that backend's entry needs an edit.
#
# New backends ship via a new shim version (the canonical source is
# auto-updated from GitHub when PUNCH_SHIM_AUTO_UPDATE=1). The
# default template below is the source of truth for the Punch
# Powertrain deployment; multi-tenant deployments should fork.
_DEFAULT_BACKENDS_TEMPLATE: dict = {
    "backends": [
        {
            "name":   "sap",
            "url":    "http://ai.punchpowertrain.com:3000",
            "auth":   "negotiate",
        },
        {
            "name":   "zabbix",
            "url":    "http://ai.punchpowertrain.com:3002",
            "auth":   "negotiate",
        },
        {
            "name":   "rd",
            "url":    "http://ai.punchpowertrain.com:3010",
            "auth":   "negotiate",
        },
        {
            "name":   "tutorials",
            "url":    "http://ai.punchpowertrain.com:3015",
            "auth":   "negotiate",
        },
    ],
    "primary": "sap",
}

# v3.4.5 — backends that honour the X-Punch-Auth header (resolved against
# pa_users). Only these get converted to x-punch-auth + the env-var key in the
# service-identity seed branch. rd is Kerberos-only (no X-Punch-Auth path) and
# tutorials is no-auth, so both stay auth=negotiate regardless of PUNCH_SAP_KEY.
_XPUNCH_AUTH_CAPABLE = frozenset({"sap", "zabbix"})


# v3.0.5 — v1 was retired at the v0.0.115/116 Kerberos cutover. Laptops
# carrying a backends.json that still points at the v1 host will fail
# on every call (URL unreachable or — if v1 is still up — pointing at
# the wrong cluster entirely). Detect that shape on shim startup,
# archive the stale file (so a user can recover if we're wrong),
# and let the existing auto-seed write a fresh Kerberos-default
# backends.json. Hostnames here are case-folded before comparison.
_V1_HOSTS = frozenset({
    "mcp.punchpowertrain.com",
})

def _looks_v1_backend(backend: Backend) -> bool:
    """Return True if this backend's URL host is a known v1 host."""
    try:
        host = (urlparse(backend.url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    return host in _V1_HOSTS


def _archive_v1_backends_file(cfg_path: Path, reason: str) -> Path | None:
    """Rename cfg_path to a timestamped .v1-archived-<ts> sidecar.

    Returns the archive path on success, None on failure. Failures
    are non-fatal — caller logs and falls through to the env-var
    fallback path. We rename rather than delete so a user / admin
    can recover if the heuristic was wrong.
    """
    ts = time.strftime("%Y%m%d-%H%M%S")
    archive = cfg_path.with_name(cfg_path.name + f".v1-archived-{ts}")
    try:
        cfg_path.rename(archive)
    except OSError as e:
        _log_event(
            "backends_v1_archive_failed",
            level=logging.WARNING,
            path=str(cfg_path),
            archive=str(archive),
            reason=reason,
            error=f"{type(e).__name__}: {e}",
        )
        return None
    _log_event(
        "backends_v1_archived",
        level=logging.WARNING,
        path=str(cfg_path),
        archive=str(archive),
        reason=reason,
        note=(
            "Stale v1 backends.json detected on shim startup. File renamed "
            "to the .v1-archived sidecar above so it can be recovered if "
            "this was a false positive. The shim will now seed a fresh "
            "Kerberos-default backends.json in its place. v1 was retired "
            "at the v0.0.115/116 cutover; v2 (ai.punchpowertrain.com) is "
            "the only valid target now."
        ),
    )
    return archive


def _maybe_seed_backends_file(cfg_path: Path) -> bool:
    """If cfg_path doesn't exist, write a default backends.json.

    Two seed modes, branching on whether PUNCH_SAP_KEY is set:

    * **Kerberos default** (no PUNCH_SAP_KEY env var): write the template
      verbatim — every backend is `auth=negotiate`, no key on disk. Works
      out of the box on a domain-joined laptop. This is the path a fresh
      v3.0+ install with the API-key dialog field left blank lands on.

    * **Service-identity fallback** (PUNCH_SAP_KEY is set): override the
      template to `auth=x-punch-auth` and inject the env-var key into
      every backend entry. Used by laptops whose only setup mode is the
      env var (e.g. an installer .cmd that bakes the key in).

    Returns True if a file was written, False otherwise. Failures are
    swallowed -- a malformed cwd or read-only filesystem must not
    prevent the shim from starting; the env-var fallback path covers
    that case.
    """
    if cfg_path.exists():
        return False

    if PUNCH_SAP_KEY:
        # Service-identity: bake the env-var key into the X-Punch-Auth-capable
        # backends (sap, zabbix). rd / tutorials stay negotiate as in the
        # template — stamping the SAP key onto rd via x-punch-auth would break
        # it (rd is Kerberos-only), and tutorials ignores auth entirely.
        seed: dict = {
            "backends": [
                {
                    "name":   entry["name"],
                    "url":    entry["url"],
                    "auth":   "x-punch-auth",
                    "header": "X-Punch-Auth",
                    "key":    PUNCH_SAP_KEY,
                }
                if entry["name"] in _XPUNCH_AUTH_CAPABLE
                else dict(entry)
                for entry in _DEFAULT_BACKENDS_TEMPLATE["backends"]
            ],
            "primary": _DEFAULT_BACKENDS_TEMPLATE["primary"],
        }
        seed_note = (
            "first-run auto-create; PUNCH_SAP_KEY env var present so the "
            "X-Punch-Auth-capable backends (sap, zabbix) were seeded with "
            "auth=x-punch-auth and that key; rd / tutorials stay negotiate. "
            "Edit the file to issue per-backend keys."
        )
    else:
        # Kerberos default: template as-is, auth=negotiate, no key. Pre-
        # v3.0 the absence of PUNCH_SAP_KEY would bail out here entirely
        # and leave the user with zero backends; v3.0+ writes the
        # Kerberos seed so the laptop is usable on first launch.
        seed = {
            "backends": [dict(entry) for entry in _DEFAULT_BACKENDS_TEMPLATE["backends"]],
            "primary":  _DEFAULT_BACKENDS_TEMPLATE["primary"],
        }
        seed_note = (
            "first-run auto-create; no PUNCH_SAP_KEY env var, so every "
            "backend was seeded with auth=negotiate (Kerberos / SPNEGO "
            "via the calling user's Windows logon ticket). Domain-joined "
            "laptop + server-side SPN required."
        )

    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(seed, indent=2), encoding="utf-8")
    except OSError as e:
        _log_event(
            "backends_file_seed_failed",
            level=logging.WARNING,
            path=str(cfg_path),
            error=f"{type(e).__name__}: {e}",
        )
        return False
    _log_event(
        "backends_file_seeded",
        path=str(cfg_path),
        backend_count=len(seed["backends"]),
        backend_names=[b["name"] for b in seed["backends"]],
        primary=seed["primary"],
        mode=("x-punch-auth" if PUNCH_SAP_KEY else "negotiate"),
        note=seed_note,
    )
    return True


def _load_backends() -> tuple[list[Backend], Backend | None]:
    """Resolve the backend list. Falls back to single-backend env vars
    when backends.json is missing. Returns (backends, primary).

    v2.2.5 -- if the resolved path doesn't exist, attempt a one-time
    auto-create from the baked-in template before falling through to
    the legacy env-var path. See `_maybe_seed_backends_file` for the
    conditions under which the seed actually writes.
    """
    cfg_path = _resolve_backends_path()
    # v2.2.5: zero-touch first-run seed
    _maybe_seed_backends_file(cfg_path)
    if cfg_path.exists():
        try:
            backends, primary_name = _load_backends_from_file(cfg_path)
            # v3.0.5: detect a stale v1-era backends.json. v1 is gone
            # post-Kerberos cutover; carrying an old config will fail
            # every call. Archive + re-seed + reload.
            if backends and any(_looks_v1_backend(b) for b in backends):
                stale_hosts = sorted({
                    urlparse(b.url).hostname or "?" for b in backends
                    if _looks_v1_backend(b)
                })
                _log_event(
                    "backends_v1_detected",
                    level=logging.WARNING,
                    path=str(cfg_path),
                    v1_hosts=stale_hosts,
                    backend_count=len(backends),
                )
                archived = _archive_v1_backends_file(cfg_path, reason="v1_host_in_url")
                if archived is not None:
                    _maybe_seed_backends_file(cfg_path)
                    backends, primary_name = _load_backends_from_file(cfg_path)

            # v3.0.7 added a load-time auto-archive of stale-xpunch
            # backends here; rolled back in v3.0.8. The heuristic couldn't
            # catch wrong-host installs (e.g. zabbix-port 3002 stuck in
            # x-punch-auth mode) without widening the host set to a point
            # where false-positives became likely. Deterministic recovery
            # via `operational/reset-punch.cmd` covers every broken-state
            # shape uniformly; v3.0.5's v1-nuke above stays because it
            # targets a specific defunct host, not a key-shape guess.
            if backends:
                primary = next(
                    (b for b in backends if b.name == primary_name),
                    backends[0],
                )
                _log_event(
                    "backends_loaded",
                    source="config_file",
                    path=str(cfg_path),
                    backend_count=len(backends),
                    backend_names=[b.name for b in backends],
                    primary=primary.name,
                )
                return backends, primary
        except Exception as e:
            _log_event(
                "backends_load_fallback_to_env",
                level=logging.WARNING,
                reason=f"{type(e).__name__}: {e}",
            )

    # Fallback: single backend from PUNCH_SAP_URL / PUNCH_SAP_KEY
    if PUNCH_SAP_KEY:
        b = Backend(
            name="sap",
            url=PUNCH_SAP_URL,
            header="X-Punch-Auth",
            key=PUNCH_SAP_KEY,
        )
        _log_event(
            "backends_loaded",
            source="env_var_fallback",
            backend_count=1,
            backend_names=["sap"],
            primary="sap",
        )
        return [b], b

    # No config and no env var — no backends. The shim will start but
    # have zero tools registered. Logged as ERROR so the user notices.
    _log_event(
        "backends_loaded",
        level=logging.ERROR,
        source="none",
        backend_count=0,
        reason="no_backends_file_and_no_PUNCH_SAP_KEY_env_var",
    )
    return [], None


# ---------------------------------------------------------------------------
# v2.3.1 — backends.json hot-reload (credential rotation, no Desktop restart)
#
# Companion to the v2.3.0 `shim_reload` MCP tool, which manually re-fetches
# the tool catalogue from each backend. This piece handles the OTHER
# rotation surface — credentials. Pre-2.3.1 a `pa_admin rotate <user>` on
# the server forced the user to restart Claude Desktop before the shim
# picked up the new key. The dashboard rotates in seconds; the multi-
# minute Desktop restart was the slow link in the chain.
#
# Strategy: watch backends.json's mtime. On every tool call (with a 2-
# second throttle) re-stat the file; if mtime changed, re-read + apply
# key/url/header in place on the existing Backend objects. Backend
# .http_client() reads self.key fresh on every build, so the next request
# already carries the new credential. No restart, no `shim_reload` call
# from the user.
#
# What's hot-applied: KEY (the rotation case), URL, HEADER on a backend
# whose name was already known at startup.
#
# What still needs Desktop restart OR `shim_reload`: adding a backend,
# removing one, renaming one. (The first two also require an updated
# tool registry; `shim_reload` covers the gain/lose-tools case.) Logged
# at WARN.
#
# Defenses against transient half-written files:
#   * JSONDecodeError → log + skip; do NOT advance the tracked mtime so
#     the next throttle window picks up the eventual valid write.
#   * empty key in the new file → treated as "no change" (don't blow
#     away a good in-memory key on a transient empty during edit).
#   * placeholder-key guard from v2.2.7 still applies on the load path.
#
# Throttle: PUNCH_SHIM_RELOAD_INTERVAL_S (default 2). Single stat call;
# only a real mtime delta triggers the JSON re-read.

import threading

_BACKENDS_FILE_MTIME: float = 0.0
_BACKENDS_RELOAD_LOCK = threading.Lock()
_BACKENDS_RELOAD_THROTTLE_S = float(os.getenv("PUNCH_SHIM_RELOAD_INTERVAL_S", "2"))
_LAST_RELOAD_CHECK_MONO: float = 0.0


def _reconcile_backends(existing: list[Backend], new: list[Backend]) -> dict:
    """Pure: apply key/url/header from `new` onto matching entries in
    `existing` IN PLACE. Returns a summary of what changed. Backends in
    `new` whose name doesn't appear in `existing` are NOT added (FastMCP
    needs a `shim_reload` or restart for that); they show up under
    'added' so the caller can log a structural-change warning. Same for
    'removed'."""
    existing_by_name = {b.name: b for b in existing}
    new_by_name      = {b.name: b for b in new}

    rotated_keys:   list[str] = []
    url_changes:    list[str] = []
    header_changes: list[str] = []
    auth_changes:   list[str] = []

    for name, new_b in new_by_name.items():
        cur = existing_by_name.get(name)
        if cur is None:
            continue
        # An empty key in the new file is treated as "no change" rather
        # than a deliberate revoke — the shim has no other auth path,
        # and a half-written file mid-edit could briefly carry "" before
        # the real key lands. _maybe_reload_backends will retry on the
        # next mtime tick if the file is still mid-edit.
        if cur.key != new_b.key and new_b.key:
            cur.key = new_b.key
            rotated_keys.append(name)
        if cur.url != new_b.url and new_b.url:
            cur.url = new_b.url
            url_changes.append(name)
        if cur.header != new_b.header and new_b.header:
            cur.header = new_b.header
            header_changes.append(name)
        # v3.0.0 — auth-mode flip (Kerberos cutover). Apply on any change;
        # unlike key, an explicit different value is always meaningful.
        if cur.auth != new_b.auth:
            cur.auth = new_b.auth
            auth_changes.append(name)

    return {
        "added":          sorted(set(new_by_name) - set(existing_by_name)),
        "removed":        sorted(set(existing_by_name) - set(new_by_name)),
        "rotated_keys":   rotated_keys,
        "url_changes":    url_changes,
        "header_changes": header_changes,
        "auth_changes":   auth_changes,
    }


def _maybe_reload_backends() -> None:
    """Cheap mtime check. If backends.json has changed, reload + reconcile
    in-place onto the module-level `_BACKENDS`. Throttled to one check
    per PUNCH_SHIM_RELOAD_INTERVAL_S to keep the stat call out of every
    tool invocation's hot path. Safe to call from any thread."""
    global _LAST_RELOAD_CHECK_MONO, _BACKENDS_FILE_MTIME

    now = time.monotonic()
    if now - _LAST_RELOAD_CHECK_MONO < _BACKENDS_RELOAD_THROTTLE_S:
        return
    _LAST_RELOAD_CHECK_MONO = now

    cfg_path = _resolve_backends_path()
    if not cfg_path.exists():
        return

    try:
        mtime = cfg_path.stat().st_mtime
    except OSError:
        return
    if mtime == _BACKENDS_FILE_MTIME:
        return  # no change

    with _BACKENDS_RELOAD_LOCK:
        # Re-check inside the lock so racing threads don't double-load.
        if mtime == _BACKENDS_FILE_MTIME:
            return
        try:
            new_backends, _new_primary = _load_backends_from_file(cfg_path)
        except Exception as e:
            # Half-written file mid-edit, malformed JSON, etc. Don't
            # advance _BACKENDS_FILE_MTIME so the NEXT throttle window
            # retries (the throttle counter advanced — we won't busy-loop).
            _log_event(
                "backends_hot_reload_failed",
                level=logging.WARNING,
                error=f"{type(e).__name__}: {e}",
                note="will retry on next throttle window",
            )
            return

        summary = _reconcile_backends(_BACKENDS, new_backends)
        _BACKENDS_FILE_MTIME = mtime

        if (summary["rotated_keys"] or summary["url_changes"]
                or summary["header_changes"] or summary["auth_changes"]):
            _log_event(
                "backends_hot_reloaded",
                mtime=mtime,
                key_rotations=summary["rotated_keys"],
                url_changes=summary["url_changes"],
                header_changes=summary["header_changes"],
                note="changes applied in-place; the next request uses new credentials",
            )
        if summary["added"] or summary["removed"]:
            _log_event(
                "backends_structural_change_detected",
                level=logging.WARNING,
                added=summary["added"],
                removed=summary["removed"],
                note=("structural changes (add/remove backend) are NOT hot-applied — "
                      "call shim_reload to re-fetch tool catalogues, or restart "
                      "Claude Desktop. Only key/url/header on existing backends "
                      "are hot-applied here."),
            )


# ---------------------------------------------------------------------------
# Bundled tools.json reader (kept from v2.2.2 — last-known-good fallback
# for the SAP backend specifically when offline)
# ---------------------------------------------------------------------------


def _bundled_server_version() -> str | None:
    """Read the highest 'Schema version: vX.Y.Z' stamp from the bundled
    tools.json. Used by the User-Agent header so the server can spot
    stale MCPB installs from telemetry."""
    bundled_path = Path(__file__).parent / "tools.json"
    if not bundled_path.exists():
        return None
    try:
        data = json.loads(bundled_path.read_text())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    import re as _re
    pat = _re.compile(r"Schema version: v(\d+\.\d+\.\d+)")
    versions: list[tuple[int, ...]] = []
    for tool in data.get("tools") or []:
        desc = tool.get("description") or ""
        for m in pat.finditer(desc):
            try:
                versions.append(tuple(int(p) for p in m.group(1).split(".")))
            except Exception:
                continue
    if not versions:
        return None
    return ".".join(str(p) for p in max(versions))


# ---------------------------------------------------------------------------
# Resolve backends + primary at module load
# ---------------------------------------------------------------------------

# --- SHIM-1: backend discovery -------------------------------------------
# At startup the shim asks the supervisor for the canonical backend list,
# so a NEW backend needs no shim release. Discovery is additive-only (a
# name already loaded locally always wins) and strictly best-effort (any
# failure -> the shim runs on its local backends exactly as before). Set
# PUNCH_SHIM_DISCOVERY_URL="" to disable it (the test suite does this so
# importing the module makes no network call).
_DISCOVERY_URL = os.environ.get(
    "PUNCH_SHIM_DISCOVERY_URL",
    "http://ai.punchpowertrain.com:3030/api/discover/backends",
)
_DISCOVERY_TIMEOUT_S = 3.0


def _fetch_discovery() -> dict | None:
    """GET the supervisor's discovery endpoint. Returns the parsed JSON
    object, or None on any failure -- disabled (empty URL), unreachable,
    timeout, non-200, or a non-JSON / non-object body. Never raises."""
    if not _DISCOVERY_URL:
        return None
    try:
        with httpx.Client(timeout=_DISCOVERY_TIMEOUT_S) as c:
            r = c.get(_DISCOVERY_URL, headers={"Connection": "close"})
        if r.status_code != 200:
            _log_event("discovery_skipped", level=logging.INFO,
                       reason=f"http_{r.status_code}", url=_DISCOVERY_URL)
            return None
        payload = r.json()
        return payload if isinstance(payload, dict) else None
    except Exception as e:
        _log_event("discovery_skipped", level=logging.INFO,
                   reason=f"{type(e).__name__}: {e}", url=_DISCOVERY_URL)
        return None


def _discover_backends(backends: list[Backend]) -> list[Backend]:
    """Merge supervisor-discovered backends into `backends`, additive-only.

    Appends any discovered backend whose name is not already present; a
    name already loaded locally is never overridden. Best-effort: when
    discovery is unavailable (`_fetch_discovery` -> None) or the payload
    is malformed, `backends` is returned unchanged. Discovery never blocks
    startup and never shrinks the backend set."""
    payload = _fetch_discovery()
    if payload is None:
        return backends
    discovered = payload.get("backends")
    if not isinstance(discovered, list):
        _log_event("discovery_skipped", level=logging.WARNING,
                   reason="no_backends_array", url=_DISCOVERY_URL)
        return backends

    known = {b.name for b in backends}
    result = list(backends)
    added: list[str] = []
    for entry in discovered:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        url = (entry.get("url") or "").strip()
        auth = (entry.get("auth") or "x-punch-auth").strip().lower()
        if not name or not url:
            _log_event("discovery_entry_skipped", level=logging.WARNING,
                       reason="missing_name_or_url")
            continue
        if name in known:
            continue                       # additive-only: local always wins
        known.add(name)
        result.append(Backend(name=name, url=url, header="X-Punch-Auth",
                              key="", auth=auth))
        added.append(name)
    if added:
        _log_event("discovery_merged", added=added, total_backends=len(result))
    return result


_BACKENDS, _PRIMARY = _load_backends()
_BACKENDS = _discover_backends(_BACKENDS)
# v3.5.2 - resolve OIDC directives on a background daemon thread so startup
# reports ready immediately. Backends keep their working negotiate credential
# until the directive resolves, then flip to oidc in place. The thread is kept
# in a module global so it is not garbage-collected mid-resolution.
_AUTH_PREFLIGHT_THREAD = _apply_auth_directives_async(_BACKENDS)


# v2.3.1: prime the hot-reload mtime tracker so the FIRST throttle window
# after startup doesn't spuriously decide the file changed (which would
# cost a no-op reload on the first tool call).
def _init_backends_file_mtime() -> None:
    global _BACKENDS_FILE_MTIME
    cfg_path = _resolve_backends_path()
    try:
        if cfg_path.exists():
            _BACKENDS_FILE_MTIME = cfg_path.stat().st_mtime
    except OSError:
        pass


_init_backends_file_mtime()


_log_event(
    "shim_start",
    shim_version=_SHIM_VERSION,
    pid=os.getpid(),
    backend_count=len(_BACKENDS),
    backend_names=[b.name for b in _BACKENDS],
    primary=_PRIMARY.name if _PRIMARY else None,
    auto_update=_AUTO_UPDATE,
    debug=_DEBUG,
)


# ---------------------------------------------------------------------------
# v2.2.2 / v2.2.4 — Auto-update
#
# Same source priority as v2.2.3:
#   1. GitHub raw (HTTPS, public).
#   2. Server fallback — uses the PRIMARY backend (typically SAP).
#
# Multi-backend doesn't change the update logic; the shim binary itself
# is one file regardless of how many backends it talks to.
# ---------------------------------------------------------------------------

_GITHUB_RAW_BASE = os.getenv(
    "PUNCH_SHIM_UPDATE_URL_BASE",
    "https://raw.githubusercontent.com/Zenotech-bv/production_mcp_shim/main",
).rstrip("/")


def _semver_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in v.split("."))
    except Exception:
        return (0,)


def _fetch_update_source(prefer_github: bool = True) -> tuple[dict, bytes, str] | None:
    """Fetch (manifest, source_bytes, source_label) from GitHub or the
    primary backend's /shim/* fallback. Returns None on all-source
    failure."""
    short_timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
    longer_timeout = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)
    limits = httpx.Limits(max_keepalive_connections=0, max_connections=10)

    sources: list[tuple[str, str, str, dict | None]] = []
    if prefer_github:
        sources.append((
            "github",
            f"{_GITHUB_RAW_BASE}/manifest.json",
            f"{_GITHUB_RAW_BASE}/shim_server.py",
            None,
        ))
    if _PRIMARY and _PRIMARY.is_configured:
        sources.append((
            f"server[{_PRIMARY.name}]",
            f"{_PRIMARY.url}/shim/manifest.json",
            f"{_PRIMARY.url}/shim/shim_server.py",
            {_PRIMARY.header: _PRIMARY.key},
        ))

    for label, manifest_url, source_url, extra_headers in sources:
        try:
            client_kwargs: dict[str, Any] = {
                "timeout": short_timeout, "limits": limits,
                "verify": _verify_tls,
            }
            if extra_headers:
                client_kwargs["headers"] = extra_headers
            with httpx.Client(**client_kwargs) as c:
                m = c.get(manifest_url)
            if not m.is_success:
                _log_event("auto_update_skip", level=logging.DEBUG,
                           source=label, reason="manifest_status",
                           status=m.status_code)
                continue
            manifest = m.json()
        except Exception as e:
            _log_event("auto_update_skip", level=logging.DEBUG,
                       source=label, reason="manifest_fetch_failed",
                       error=f"{type(e).__name__}: {e}")
            continue
        if not isinstance(manifest, dict):
            continue
        try:
            client_kwargs["timeout"] = longer_timeout
            with httpx.Client(**client_kwargs) as c:
                s = c.get(source_url)
            if not s.is_success:
                _log_event("auto_update_fail", level=logging.WARNING,
                           source=label, reason="source_status",
                           status=s.status_code)
                continue
            return manifest, s.content, label
        except Exception as e:
            _log_event("auto_update_fail", level=logging.WARNING,
                       source=label, reason="source_fetch_failed",
                       error=f"{type(e).__name__}: {e}")
            continue
    return None


def _maybe_self_update() -> None:
    """Check for an updated shim and apply if newer. Best-effort."""
    if not _AUTO_UPDATE:
        return
    fetched = _fetch_update_source(prefer_github=True)
    if fetched is None:
        _log_event("auto_update_skip", level=logging.DEBUG,
                   reason="all_sources_failed")
        return

    manifest, new_source, source_label = fetched
    server_version = str(manifest.get("version") or "")
    expected_sha256 = str(manifest.get("sha256") or "").lower()
    if not server_version or not expected_sha256:
        _log_event("auto_update_skip", source=source_label,
                   reason="manifest_missing_fields",
                   manifest_keys=list(manifest.keys()))
        return

    if _semver_tuple(server_version) <= _semver_tuple(_SHIM_VERSION):
        _log_event("auto_update_uptodate", level=logging.DEBUG,
                   shim_version=_SHIM_VERSION, server_version=server_version,
                   source=source_label)
        return

    _log_event("auto_update_available", shim_version=_SHIM_VERSION,
               server_version=server_version, source=source_label)

    actual_sha256 = hashlib.sha256(new_source).hexdigest().lower()
    if actual_sha256 != expected_sha256:
        _log_event("auto_update_fail", level=logging.ERROR,
                   source=source_label, reason="sha256_mismatch",
                   expected=expected_sha256, actual=actual_sha256)
        return

    if len(new_source) < 1024:
        _log_event("auto_update_fail", level=logging.ERROR,
                   source=source_label, reason="source_too_small",
                   size_bytes=len(new_source))
        return

    self_path = Path(__file__).resolve()
    backup_path = self_path.with_suffix(self_path.suffix + ".bak")
    try:
        if self_path.exists():
            shutil.copy2(self_path, backup_path)
        new_path = self_path.with_suffix(self_path.suffix + ".new")
        new_path.write_bytes(new_source)
        os.replace(new_path, self_path)
    except OSError as e:
        _log_event("auto_update_fail", level=logging.ERROR,
                   source=source_label, reason="filesystem_error",
                   error=f"{type(e).__name__}: {e}")
        return

    # v3.5.1: DO NOT re-exec in-session. os.execv on Windows spawns a NEW
    # process instead of replacing in place, which (a) drops the stdio pipe
    # Claude Desktop launched us with, and (b) mangles the space in the
    # "Claude Extensions" install path — python then receives a truncated
    # path and exits "early". Either way the client sees the server
    # disconnect on the very launch that pulls the update. Instead we STAGE:
    # the new bytes are already on disk (os.replace above); this session keeps
    # running the current code, and the new version is picked up on the next
    # natural Claude Desktop launch (whose up-to-date check then no-ops).
    _log_event("auto_update_staged", from_version=_SHIM_VERSION,
               to_version=server_version, source=source_label,
               backup_path=str(backup_path),
               note="applies on next launch (no in-session relaunch)")


# ---------------------------------------------------------------------------
# v3.4.6 — manifest self-heal (see the version comment near _SHIM_VERSION).
# Carries the v3.4.3 persistent-uv-env fix to laptops that auto-updated
# shim_server.py but kept a pre-fix packaged manifest.
# ---------------------------------------------------------------------------

# The exact keys + literal ${HOME} values build-mcpb.ps1 writes, so a healed
# manifest is byte-for-byte what a fresh build would have produced. Claude
# Desktop expands ${HOME} when it reads the manifest env at launch.
_UV_PERSIST_ENV = {
    "UV_PROJECT_ENVIRONMENT": "${HOME}/.punch-shim/venv",
    "UV_CACHE_DIR":           "${HOME}/.punch-shim/uv-cache",
    "UV_PYTHON_INSTALL_DIR":  "${HOME}/.punch-shim/uv-python",
}


def _packaged_manifest_path() -> Path:
    """Path to the .mcpb-packaged manifest.json — the extension root, one
    level up from this server/ dir. This is NOT the source repo's
    update-descriptor manifest.json (version/sha256/source_url) that sits
    beside shim_server.py; _heal_manifest_uv_env's shape guard refuses to
    touch that one."""
    return Path(__file__).resolve().parent.parent / "manifest.json"


def _heal_manifest_uv_env(manifest_path: Path) -> bool:
    """Ensure the packaged manifest's server.mcp_config.env carries the
    UV_* persistence keys (the v3.4.3 fix). Returns True iff the file was
    modified.

    Idempotent: a no-op once all three keys are present, so steady-state
    launches never rewrite the file. Best-effort: any missing-file,
    wrong-shape, unreadable, or unwritable condition returns False without
    raising — startup must never depend on this succeeding.

    Shape guard: only a .mcpb-packaged manifest has server.mcp_config. The
    source repo's update-descriptor manifest.json does not, so a dev/test
    import that happens to resolve to it is left untouched.
    """
    try:
        if not manifest_path.exists():
            return False
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    server = data.get("server")
    if not isinstance(server, dict):
        return False
    mcp_config = server.get("mcp_config")
    if not isinstance(mcp_config, dict):
        return False
    env = mcp_config.get("env")
    if not isinstance(env, dict):
        env = {}
    missing = {k: v for k, v in _UV_PERSIST_ENV.items() if k not in env}
    if not missing:
        return False
    env.update(missing)
    mcp_config["env"] = env
    try:
        backup = manifest_path.with_suffix(manifest_path.suffix + ".prepatch.bak")
        if not backup.exists():
            shutil.copy2(manifest_path, backup)
        manifest_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        _log_event("manifest_uv_env_heal_failed", level=logging.WARNING,
                   path=str(manifest_path), error=f"{type(e).__name__}: {e}")
        return False
    _log_event(
        "manifest_uv_env_healed",
        path=str(manifest_path),
        injected=sorted(missing.keys()),
        note=("packaged manifest lacked the v3.4.3 persistent-uv-env keys; "
              "injected so the next client launch reuses ~/.punch-shim instead "
              "of re-provisioning the uv env from cold each start"),
    )
    return True


_maybe_self_update()
_heal_manifest_uv_env(_packaged_manifest_path())


# ---------------------------------------------------------------------------
# Per-backend tool fetch + merged registry
# ---------------------------------------------------------------------------

_TOOLS_FETCH_TIMEOUT_S = float(os.getenv("PUNCH_SAP_TOOLS_TIMEOUT", "5"))


def _fetch_tools_for_backend(backend: Backend) -> list[dict] | None:
    """GET /tools from one backend. Returns the parsed tools list or
    None on failure. Soft-fail — never raises."""
    if not backend.is_configured:
        _log_event("backend_tools_skipped", level=logging.WARNING,
                   backend=backend.name, reason="not_configured")
        return None
    try:
        with backend.http_client(read_timeout=_TOOLS_FETCH_TIMEOUT_S) as c:
            r = c.get("/tools", timeout=_TOOLS_FETCH_TIMEOUT_S)
        if not r.is_success:
            _log_event("backend_tools_fetch_failed", level=logging.WARNING,
                       backend=backend.name, status=r.status_code)
            return None
        data = r.json()
    except Exception as e:
        _log_event("backend_tools_fetch_exception", level=logging.WARNING,
                   backend=backend.name,
                   error=f"{type(e).__name__}: {e}")
        return None
    if not isinstance(data, dict):
        return None
    tools = data.get("tools")
    if not isinstance(tools, list):
        return None
    sv = data.get("server_version", "?")
    _log_event("backend_tools_loaded",
               backend=backend.name,
               source="live",
               tool_count=len(tools),
               server_version=sv)
    return tools


def _probe_backend(backend: Backend) -> dict:
    """Probe one backend's reachability and auth in a single GET /tools.

    Returns {backend, url, reachable, auth_ok, status}. Never raises:
      - ConnectError / timeout / transport error -> reachable=False
      - HTTP 401                                  -> reachable=True, auth_ok=False
      - any other HTTP status                     -> reachable=True, auth_ok=True
    """
    result: dict = {
        "backend": backend.name, "url": backend.url,
        "configured": backend.is_configured,
        "reachable": False, "auth_ok": False, "status": None,
    }
    if not backend.is_configured:
        return result
    try:
        with backend.http_client(read_timeout=_TOOLS_FETCH_TIMEOUT_S) as c:
            r = c.get("/tools", timeout=_TOOLS_FETCH_TIMEOUT_S)
    except Exception as e:
        _log_event("shim_access_probe_unreachable", level=logging.DEBUG,
                   backend=backend.name, error=f"{type(e).__name__}: {e}")
        return result
    result["reachable"] = True
    result["status"] = r.status_code
    result["auth_ok"] = r.status_code != 401
    return result


def _load_bundled_sap_fallback() -> list[dict]:
    """If the SAP backend is unreachable AND a bundled tools.json
    exists, register the bundled tool set under the SAP backend's name.
    Pre-v2.2.4 single-backend behaviour."""
    bundled_path = Path(__file__).parent / "tools.json"
    if not bundled_path.exists():
        return []
    try:
        data = json.loads(bundled_path.read_text())
    except Exception as e:
        _log_event("bundled_tools_parse_error", level=logging.WARNING,
                   error=f"{type(e).__name__}: {e}")
        return []
    if not isinstance(data, dict):
        return []
    tools = data.get("tools") or []
    if not isinstance(tools, list):
        return []
    bundled_sv = data.get("server_version") or _bundled_server_version() or "?"
    _log_event("backend_tools_loaded",
               backend="sap",
               source="bundled",
               tool_count=len(tools),
               bundled_server_version=bundled_sv,
               level=logging.WARNING)
    return tools


def _build_merged_registry(
    backends: list[Backend],
) -> tuple[dict[str, tuple[Backend, str]], list[tuple[str, dict, Backend]]]:
    """Fetch tools from each backend, build the registered-name map.

    Returns:
        name_to_backend: maps every REGISTERED name (both prefixed
            forms `B.T` and bare aliases `T`) to the backend + the
            ORIGINAL tool name on that backend.
        registrations: ordered list of (registered_name, schema, backend)
            tuples. One entry per name that needs registering with
            FastMCP. Same schema may appear twice for a tool with both
            prefixed + bare-alias forms.
    """
    # 1. Fetch per backend.
    #
    # If a backend already has tools populated (e.g. tests pre-fill
    # the list), skip the network fetch and use what's there. This
    # keeps `_build_merged_registry` unit-testable without monkey-
    # patching the HTTP fetch — every other call path enters with
    # backend.tools=[] and triggers the live fetch as before.
    for backend in backends:
        if not backend.tools:
            backend.tools = _fetch_tools_for_backend(backend) or []
            # SAP-specific bundled fallback for the offline case. Only
            # applies to the backend named "sap" — other backends don't
            # ship a bundled tools.json today.
            if not backend.tools and backend.name == "sap":
                backend.tools = _load_bundled_sap_fallback()

    # 2. Count occurrences of each bare tool name across backends.
    bare_counts: dict[str, int] = defaultdict(int)
    for backend in backends:
        for t in backend.tools:
            name = t.get("name")
            if isinstance(name, str) and name:
                bare_counts[name] += 1

    # 3. Build the merged registry.
    name_to_backend: dict[str, tuple[Backend, str]] = {}
    registrations: list[tuple[str, dict, Backend]] = []
    collisions: dict[str, list[str]] = defaultdict(list)

    for backend in backends:
        for tool in backend.tools:
            orig = tool.get("name")
            if not isinstance(orig, str) or not orig:
                continue

            # v2.2.9: register EITHER the bare name OR the prefixed form,
            # never both. Pre-2.2.9 we ALWAYS registered the prefixed
            # `<backend>_<tool>` AND additionally the bare alias when there
            # was no collision -- which doubled the catalogue: every tool
            # on a single-backend-for-that-name showed up twice (e.g.
            # `pa_extract_lfa1` AND `pa_v2_pa_extract_lfa1`). Now: no
            # collision -> bare only; collision -> prefixed forms only.
            #
            # The prefix separator stays '_' (not the v2.2.4/2.2.5 '.').
            # Claude's API rejects tool names that don't match
            # ``^[a-zA-Z0-9_-]{1,64}$``; the '.' separator triggered
            # ``tools.N.FrontendRemoteMcpToolDefinition.name`` validation
            # errors as soon as any laptop activated a second backend.
            if bare_counts[orig] > 1:
                prefixed = f"{backend.name}_{orig}"
                name_to_backend[prefixed] = (backend, orig)
                registrations.append((prefixed, tool, backend))
                collisions[orig].append(backend.name)
            else:
                name_to_backend[orig] = (backend, orig)
                registrations.append((orig, tool, backend))

    if collisions:
        for name, backend_names in collisions.items():
            _log_event("tool_name_collision",
                       level=logging.WARNING,
                       tool=name,
                       backends=sorted(set(backend_names)),
                       resolution=("dropped bare alias; only prefixed "
                                    "forms registered"))

    _log_event("tools_registry_built",
               registered_count=len(registrations),
               unique_names=len(name_to_backend),
               collision_count=len(collisions))

    return name_to_backend, registrations


_NAME_TO_BACKEND, _REGISTRATIONS = _build_merged_registry(_BACKENDS)


def _reload_registry() -> tuple[
    dict[str, tuple["Backend", str]],
    list[tuple[str, dict, "Backend"]],
    list[str],
]:
    """Re-fetch every configured backend's /tools and rebuild the registry.

    Safety: a backend whose re-fetch fails (None or empty) KEEPS its
    previously-known tools — a transient fetch failure must never silently
    unregister a whole backend's catalogue. Returns
    (name_to_backend, registrations, fetch_failures).
    """
    fetch_failures: list[str] = []
    for backend in _BACKENDS:
        if not backend.is_configured:
            continue
        fresh = _fetch_tools_for_backend(backend)
        if fresh:                          # non-empty list — adopt it
            backend.tools = fresh
        else:                              # None (error) or [] — keep stale
            fetch_failures.append(backend.name)
    # _build_merged_registry skips the network fetch when backend.tools is
    # already populated, so it rebuilds from exactly what we set/kept above.
    name_to_backend, registrations = _build_merged_registry(_BACKENDS)
    return name_to_backend, registrations, fetch_failures


# ---------------------------------------------------------------------------
# v3.3.0 — tool-catalogue auto-refresh
#
# The credential hot-reload (_maybe_reload_backends) deliberately re-applies
# only key/url/header on an existing backend, NOT the tool registry — so a
# backend that GAINS or DROPS tools (a pa_v2 deploy) used to strand the client
# on a stale catalogue until a manual `shim_reload` (and even a Claude Desktop
# *window* restart didn't help, since the shim subprocess kept its cached
# fetch). This watcher closes that gap: it cheaply probes each backend's
# /health (version + tool count) on its own throttle and, on a delta, runs the
# SAME re-fetch+rebuild that shim_reload does. A deploy then self-heals on the
# next tool call — the rebuilt registry is what the next tools/list (a new
# chat / reconnect) returns. It also recovers a shim that soft-failed its
# startup /tools fetch (backend briefly down): the next probe re-fetches.
#
# Notification caveat: the rebuild is synchronous (the FastMCP forwarders are
# sync, ctx-less), so it does NOT push tools/list_changed to the *current*
# conversation — a new chat picks up the fresh catalogue automatically. Manual
# `shim_reload` remains for an instant in-conversation refresh.
# ---------------------------------------------------------------------------

_CATALOGUE_PROBE_THROTTLE_S = float(os.getenv("PUNCH_SHIM_CATALOGUE_PROBE_S", "30"))
_LAST_CATALOGUE_PROBE_MONO  = 0.0
# backend name -> last-seen (version, tool_count) from /health. Seeded at
# startup (below). A delta, or a None baseline (startup probe failed because
# the backend was briefly down), triggers a re-fetch+rebuild.
_CATALOGUE_STAMPS: dict[str, tuple] = {}


def _probe_catalogue_stamp(backend: "Backend") -> tuple | None:
    """Cheap GET /health -> (version, tool_count), or None if the backend has
    no /health, is unconfigured/unreachable, errors, or returns an unexpected
    shape. None means "no usable signal": it never triggers a refresh and never
    disturbs the working catalogue."""
    if not backend.is_configured:
        return None
    try:
        with backend.http_client(read_timeout=_TOOLS_FETCH_TIMEOUT_S) as c:
            r = c.get("/health", timeout=_TOOLS_FETCH_TIMEOUT_S)
        if not getattr(r, "is_success", False):
            return None
        data = r.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    ver, tools = data.get("version"), data.get("tools")
    if ver is None and tools is None:
        return None
    return (ver, tools)


def _apply_catalogue_reload() -> dict:
    """Re-fetch every backend's /tools and update FastMCP registrations in
    place. SYNC — does NOT notify the client. Returns a summary with
    added/removed/changed (sorted), total, fetch_failures, register_failures,
    and `moved` (the catalogue changed). Shared by shim_reload (which adds the
    client notification) and the auto-refresh watcher (which does not)."""
    global _NAME_TO_BACKEND, _REGISTRATIONS
    old_tool_by_name = {name: tool for name, tool, _b in _REGISTRATIONS}

    new_name_to_backend, new_registrations, fetch_failures = _reload_registry()
    new_tool_by_name    = {name: tool for name, tool, _b in new_registrations}
    new_backend_by_name = {name: b for name, _t, b in new_registrations}

    added   = [n for n in new_tool_by_name if n not in old_tool_by_name]
    removed = [n for n in old_tool_by_name if n not in new_tool_by_name]
    changed = [n for n in new_tool_by_name
               if n in old_tool_by_name and new_tool_by_name[n] != old_tool_by_name[n]]

    register_failures: list[str] = []
    for name in removed + changed:
        try:
            mcp.remove_tool(name)
        except Exception as e:
            _log_event("shim_reload_remove_failed", level=logging.WARNING,
                       tool=name, error=f"{type(e).__name__}: {e}")
    for name in added + changed:
        try:
            _register_one(name, new_tool_by_name[name], new_backend_by_name[name])
        except Exception as e:
            register_failures.append(name)
            _log_event("shim_reload_register_failed", level=logging.WARNING,
                       tool=name, error=f"{type(e).__name__}: {e}")

    _NAME_TO_BACKEND = new_name_to_backend
    _REGISTRATIONS   = new_registrations

    return {
        "added": sorted(added),
        "removed": sorted(removed),
        "changed": sorted(changed),
        "total": len(new_registrations),
        "fetch_failures": fetch_failures,
        "register_failures": register_failures,
        "moved": bool(added or removed or changed),
    }


def _maybe_refresh_catalogue() -> dict | None:
    """Throttled tool-catalogue auto-refresh. Probes each backend's /health;
    on a (version, tool_count) delta — or an unknown baseline (startup probe
    failed) — re-fetches + rebuilds the registry via _apply_catalogue_reload.
    Returns the reload summary if a refresh ran, else None. Safe on every tool
    call: throttled, and any probe failure is skipped so the working catalogue
    is never disturbed."""
    global _LAST_CATALOGUE_PROBE_MONO

    now = time.monotonic()
    if now - _LAST_CATALOGUE_PROBE_MONO < _CATALOGUE_PROBE_THROTTLE_S:
        return None
    _LAST_CATALOGUE_PROBE_MONO = now

    needs_reload = False
    for backend in _BACKENDS:
        stamp = _probe_catalogue_stamp(backend)
        if stamp is None:
            continue  # no /health or unreachable -> leave catalogue as-is
        prev = _CATALOGUE_STAMPS.get(backend.name)
        if prev != stamp:
            if prev is not None:
                _log_event("catalogue_change_detected", backend=backend.name,
                           old=list(prev), new=list(stamp))
            _CATALOGUE_STAMPS[backend.name] = stamp
            needs_reload = True

    if not needs_reload:
        return None

    with _BACKENDS_RELOAD_LOCK:
        result = _apply_catalogue_reload()
    _log_event("catalogue_auto_refreshed",
               added=result["added"], removed=result["removed"],
               changed=result["changed"], total=result["total"],
               fetch_failures=result["fetch_failures"],
               register_failures=result["register_failures"])
    return result


# Seed the catalogue stamps so the watcher has a baseline matching what we just
# registered at import. Best-effort: a backend unreachable now leaves its stamp
# unset, and the first successful runtime probe reconciles (re-fetches) then.
for _b in _BACKENDS:
    _seed = _probe_catalogue_stamp(_b)
    if _seed is not None:
        _CATALOGUE_STAMPS[_b.name] = _seed


# ---------------------------------------------------------------------------
# _enrich_response — deterministic failure enrichment
# ---------------------------------------------------------------------------


def _enrich_response(payload: Any, *, http_status: int,
                     backend: "Backend | None" = None) -> Any:
    """Add a deterministic `_shim_note` to a response that is an access
    denial or a zero-row table-handle result, and stamp `_shim_served_by`
    so the client can tell which federated backend produced this result.
    Both keys are namespaced `_shim_*` and strictly additive; every other
    shape is returned untouched. No heuristics — the note never guesses a
    cause; on an empty result it only points at shim_access.

    `payload` is the already-parsed JSON body. `http_status` is the backend's
    HTTP status code. `backend` is the serving backend (when known).
    Mutates and returns `payload` when it is a dict.
    """
    if not isinstance(payload, dict):
        return payload
    # v3.4.0: additive, idempotent call-time attribution. setdefault so a
    # backend that ever returns its own `_shim_served_by` wins.
    if backend is not None:
        payload.setdefault("_shim_served_by", backend.name)  # "rd" / "sap"
    if "_shim_note" in payload:
        return payload
    # Access denial — an explicit 403, or an error envelope naming it.
    if http_status == 403 or payload.get("error_type") == "AccessDenied":
        payload["_shim_note"] = (
            "Your account may lack the access this tool needs. Call "
            "shim_access to see your account's full access profile."
        )
        return payload
    # Zero-row table-handle result — identified by the (str handle, int
    # row_count) pair that only a TableHandleResult carries. A scalar,
    # admin, or error response has no such pair and is left untouched.
    # bool is an int subclass; exclude it so a stray `false` can't misfire.
    row_count = payload.get("row_count")
    if (isinstance(payload.get("handle"), str)
            and isinstance(row_count, int) and not isinstance(row_count, bool)
            and row_count == 0):
        payload["_shim_note"] = (
            "0 rows. If you expected data, run shim_access to check your "
            "account's company-code / project coverage."
        )
    return payload


# ---------------------------------------------------------------------------
# _call_remote — looks up the backend and forwards
# ---------------------------------------------------------------------------


def _call_remote(registered_name: str, kwargs: dict) -> str:
    """Forward the call to the backend that owns this registered name.
    Returns a JSON string for MCP."""
    # v2.3.1: cheap mtime check on backends.json. If the operator
    # rotated the user's key on the server, the next request after
    # the file lands picks up the new key — no Claude Desktop restart.
    _maybe_reload_backends()
    # v3.3.0: throttled /health probe — if a backend's tool set changed (a
    # deploy), re-fetch + rebuild so the fresh catalogue is live for the next
    # tools/list, no manual shim_reload needed.
    _maybe_refresh_catalogue()
    entry = _NAME_TO_BACKEND.get(registered_name)
    if entry is None:
        _log_event("tool_call_unknown", level=logging.ERROR,
                   tool=registered_name)
        return json.dumps({
            "error": True,
            "error_type": "UnknownTool",
            "message": (
                f"No backend registered for tool {registered_name!r}. "
                f"Run shim_diagnostics to see the registered list."
            ),
        }, indent=2)

    backend, original_name = entry
    if not backend.is_configured:
        _log_event("tool_call_misconfigured",
                   level=logging.ERROR,
                   tool=registered_name,
                   backend=backend.name)
        return json.dumps({
            "error": True,
            "error_type": "NotConfigured",
            "message": (
                f"Backend {backend.name!r} is missing url/header/key. "
                f"Edit your backends.json or set PUNCH_SAP_KEY for the "
                f"single-backend fallback."
            ),
            "_shim_served_by": backend.name,
        }, indent=2)

    t0 = time.monotonic()
    if _DEBUG:
        _log_event("tool_call_start", level=logging.DEBUG,
                   tool=registered_name,
                   original=original_name,
                   backend=backend.name,
                   arg_keys=list(kwargs.keys()))
    try:
        with backend.http_client() as c:
            r = c.post(f"/tools/{original_name}", json=kwargs)
    except httpx.ConnectError as e:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        _log_event("tool_call_unreachable", level=logging.ERROR,
                   tool=registered_name, backend=backend.name,
                   elapsed_ms=elapsed_ms,
                   error=f"{type(e).__name__}: {e}")
        return json.dumps({
            "error": True,
            "error_type": "Unreachable",
            "message": f"Cannot reach backend {backend.name!r} at {backend.url} - on VPN? {e}",
            "_shim_served_by": backend.name,
        }, indent=2)
    except httpx.TimeoutException as e:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        _log_event("tool_call_timeout", level=logging.ERROR,
                   tool=registered_name, backend=backend.name,
                   elapsed_ms=elapsed_ms,
                   error=f"{type(e).__name__}")
        return json.dumps({
            "error": True,
            "error_type": "Timeout",
            "message": (
                f"No response from backend {backend.name!r} "
                f"({backend.url}) in {PUNCH_SAP_TIMEOUT}s"
            ),
            "_shim_served_by": backend.name,
        }, indent=2)
    except Exception as e:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        _log_event("tool_call_transport_error", level=logging.ERROR,
                   tool=registered_name, backend=backend.name,
                   elapsed_ms=elapsed_ms,
                   error=f"{type(e).__name__}: {e}")
        return json.dumps({
            "error": True,
            "error_type": "TransportError",
            "message": str(e),
            "_shim_served_by": backend.name,
        }, indent=2)

    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
    response_bytes = len(r.content) if hasattr(r, "content") else None

    if r.status_code >= 400:
        _log_event("tool_call_http_error",
                   level=logging.WARNING if r.status_code < 500 else logging.ERROR,
                   tool=registered_name, backend=backend.name,
                   status=r.status_code,
                   elapsed_ms=elapsed_ms, response_bytes=response_bytes)
        try:
            return json.dumps(
                _enrich_response(r.json(), http_status=r.status_code,
                                 backend=backend),
                indent=2, default=str)
        except Exception:
            return json.dumps({
                "error": True,
                "error_type": f"HTTP{r.status_code}",
                "message": r.text[:500],
            }, indent=2)

    _log_event("tool_call_ok", tool=registered_name, backend=backend.name,
               elapsed_ms=elapsed_ms, response_bytes=response_bytes)

    try:
        envelope = r.json()
    except Exception:
        return r.text
    result = envelope.get("result", envelope)
    return json.dumps(
        _enrich_response(result, http_status=r.status_code, backend=backend),
        indent=2, default=str)


# ---------------------------------------------------------------------------
# FastMCP registration
# ---------------------------------------------------------------------------

mcp = FastMCP("Punch Analytics — Punch Powertrain (federated client)")


def _make_forwarder(registered_name: str):
    """Return a closure that forwards kwargs to the right backend.
    `registered_name` is the name as registered with FastMCP (either
    a prefixed form like `sap.get_aging_summary` or a bare alias)."""
    def forward(**kwargs) -> str:
        clean = {k: v for k, v in kwargs.items() if v not in (None, "")}
        return _call_remote(registered_name, clean)
    forward.__name__ = registered_name.replace(".", "_").replace("-", "_")
    return forward


JSON_SCHEMA_TYPE_TO_PY = {
    "string":  "str",
    "integer": "int",
    "number":  "float",
    "boolean": "bool",
    "array":   "list",
    "object":  "dict",
    "null":    "type(None)",
}


def _register_one(registered_name: str, tool: dict, backend: Backend) -> None:
    """Register a single tool with FastMCP under `registered_name`. The
    schema comes from `tool` (the original /tools entry). The forwarder
    routes to the backend's URL."""
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}

    params = []
    for arg_name, arg_schema in props.items():
        # Pydantic emits `X | None` types as {"anyOf": [{"type": <X>}, {"type":
        # "null"}]} with NO top-level "type" key. The original `.get("type",
        # "string")` fallback silently registered every optional structured
        # argument as a string, which made FastMCP serialise dicts/lists to
        # JSON strings; the server's pydantic validator then rejected them
        # with "Input should be a valid dictionary"/"valid list". This bit
        # `pa_table_filter.where`, `pa_table_aggregate.having`, and
        # `pa_table_aggregate.order_by` — caught in the 2026-05-25 PPS
        # top-50 session findings (Zenotech-bv/punch-analytics
        # docs/test-session-findings-2026-05-25-pps-top50.md).
        t = arg_schema.get("type")
        # v3.2.4 — JSON Schema array-form nullable type: ["string", "null"].
        # Some MCP backends emit this form (the rd MCP server's svn_log /
        # svn_log_for_file tools, as of 2026-05-27) instead of the anyOf
        # form pa_v2's pydantic produces. Normalise to a single non-null
        # type before the dict lookup; without this,
        # JSON_SCHEMA_TYPE_TO_PY.get(t) raised TypeError: unhashable type:
        # 'list' and the registration crashed, taking the whole shim with
        # it on every restart.
        if isinstance(t, list):
            t = next((x for x in t if x != "null"), None)
        if t is None:
            for variant in arg_schema.get("anyOf", []):
                if variant.get("type") != "null":
                    t = variant.get("type")
                    break
        if t is None:
            t = "string"
        py_type = JSON_SCHEMA_TYPE_TO_PY.get(t, "str")
        default = arg_schema.get("default", None)
        default_repr = "None" if default is None else repr(default)
        params.append(f"{arg_name}: {py_type} = {default_repr}")
    sig = ", ".join(params)

    forwarder = _make_forwarder(registered_name)
    # v2.2.9: prefix the description with the backend name so an LLM
    # client can answer "what <backend> tools do I have?" by text-searching
    # descriptions. The federator collapses every backend into one MCP
    # connector, so without this the backend-of-origin is invisible -- a
    # Zabbix tool reads identically to a SAP tool.
    upstream_desc = tool.get("description") or ""
    safe_desc = (
        f"[{backend.name}] {upstream_desc}".strip()
    ).replace(chr(34) * 3, "").replace(chr(92), chr(92) * 2).strip()

    # v2.2.6: registered_name uses '_' as the prefix separator (was '.'
    # in v2.2.4/v2.2.5). Underscores are valid Python identifiers AND
    # valid MCP tool names per Claude's ``^[a-zA-Z0-9_-]{1,64}$`` regex,
    # so no special-case handling is needed any more. The .replace()
    # calls below are kept defensively in case an upstream tool name
    # somehow contains '.' or '-'.
    safe_fn_name = registered_name.replace(".", "_").replace("-", "_")
    src = (
        f"def {safe_fn_name}({sig}) -> str:\n"
        f"    \"\"\"{safe_desc}\"\"\"\n"
        f"    return _forwarder(**{{k: v for k, v in locals().items()}})\n"
    )
    ns: dict = {"_forwarder": forwarder}
    exec(src, ns)
    typed_fn = ns[safe_fn_name]
    # If the registered name differs from the Python-safe form (e.g.
    # an upstream tool name contains '-'), tell FastMCP the desired
    # registered name explicitly.
    if safe_fn_name != registered_name:
        mcp.tool(name=registered_name)(typed_fn)
    else:
        mcp.tool()(typed_fn)


# v3.2.3 — defensive: per-tool try/except so one malformed upstream schema
# can't take down the whole shim during startup. A failed registration
# names itself in shim.log with the exception type + message; the rest of
# the catalogue still registers, so chat stays usable while the offending
# schema is fixed at the source.
_REGISTRATION_FAILURES: list[dict] = []
for _registered_name, _tool, _backend in _REGISTRATIONS:
    try:
        _register_one(_registered_name, _tool, _backend)
    except Exception as _e:
        _REGISTRATION_FAILURES.append({
            "tool": _registered_name,
            "backend": _backend.name,
            "error_type": type(_e).__name__,
            "error": str(_e),
        })
        _log_event(
            "register_one_failed",
            level=logging.ERROR,
            tool=_registered_name,
            backend=_backend.name,
            error_type=type(_e).__name__,
            error=str(_e),
        )

if _REGISTRATION_FAILURES:
    _log_event(
        "registration_failures_summary",
        level=logging.WARNING,
        failure_count=len(_REGISTRATION_FAILURES),
        failures=_REGISTRATION_FAILURES,
    )


# ---------------------------------------------------------------------------
# v2.3.0 — shim_reload: pick up new/changed backend tools without a Claude
# Desktop restart. The shim fetches each backend's /tools once at startup; a
# backend that gains a tool (e.g. a new pa_v2 analytical) was invisible until
# Desktop was fully restarted. shim_reload re-fetches, diffs, register/
# unregisters in place, and emits notifications/tools/list_changed so the
# client re-pulls the catalogue mid-conversation.
# ---------------------------------------------------------------------------

async def shim_reload(ctx: Context) -> str:
    """Re-fetch the tool catalogue from every Punch backend and update this
    shim's registered tools in place — no Claude Desktop restart needed. Call
    this after a backend gains, drops, or changes a tool."""
    try:
        r = _apply_catalogue_reload()

        # v3.5.2 - reprobe OIDC directives OFF the event loop (background daemon)
        # and NON-interactively: a shim_reload must never block the conversation
        # or pop a browser. block=False -> skip (don't wait) if the startup
        # pre-flight still holds the lock; the interactive first-time sign-in
        # happens only at startup. Mutates the _BACKENDS objects in place.
        _apply_auth_directives_async(_BACKENDS, block=False, allow_interactive=False)
        _log_event("oidc_reprobe_scheduled", backend_names=[b.name for b in _BACKENDS])

        if r["moved"]:
            await ctx.session.send_tool_list_changed()

        _log_event("shim_reload_done",
                   added=r["added"], removed=r["removed"],
                   changed=r["changed"], total=r["total"],
                   fetch_failures=r["fetch_failures"],
                   register_failures=r["register_failures"])

        if r["moved"]:
            note = ("Claude Desktop has been notified (tools/list_changed); the "
                    "updated tool list is live in this conversation.")
        else:
            note = "No changes — every backend's catalogue is already current."
        return json.dumps({
            "reloaded": True,
            "added": r["added"],
            "removed": r["removed"],
            "changed": r["changed"],
            "total_registered": r["total"],
            "fetch_failures": r["fetch_failures"],
            "register_failures": r["register_failures"],
            "note": note,
        }, indent=2)
    except Exception as e:
        _log_event("shim_reload_failed", level=logging.ERROR,
                   error=f"{type(e).__name__}: {e}")
        return json.dumps({
            "reloaded": False,
            "error_type": type(e).__name__,
            "message": str(e),
        }, indent=2)


mcp.tool()(shim_reload)


# ---------------------------------------------------------------------------
# v3.0.0 — shim_info: self-report shim version + backend topology from
# inside a Claude session. The shim already stamps X-Punch-Shim-Version
# on every outbound request (servers audit it), but Claude can't ask the
# shim "what version are you" without an MCP-tool path. This closes that
# gap. Pure read-only: returns version + python info + per-backend
# {url, auth, registered_tool_count, reachable}. Safe to call any time.
# ---------------------------------------------------------------------------

async def shim_info(ctx: Context) -> str:
    """Report this shim's version, Python runtime, and per-backend
    configuration to the Claude session. Useful for "what version is my
    shim?" / "which backends are configured?" questions and for
    troubleshooting (e.g. confirming auth=negotiate is set on the SAP
    backend after a backends.json edit).

    Each backend reports BOTH ``auth`` (the static backends.json default)
    and ``effective_auth`` (what the shim actually dispatches on right now).
    The OIDC cutover directive flips ``effective_auth`` to "oidc" at startup
    while ``auth`` stays "negotiate" — so to answer "am I on OIDC?", read
    ``effective_auth``, NOT ``auth``. ``effective_auth`` mirrors the shim's
    real dispatch decision (an "oidc" backend with no resolved UPN reports
    "negotiate", exactly as it would be sent). ``oidc_upn`` names the identity
    the OIDC leg authenticates as (populated only when effective_auth ==
    "oidc"). ``fell_back`` is True when OIDC was directed this session but token
    acquisition failed and the shim fell back to Kerberos — distinct from a
    backend that was never directed to OIDC. The top-level
    ``auth_directive_pending`` is True while the startup OIDC preflight is still
    resolving (an early call may read "negotiate" before OIDC takes).

    No side effects. Doesn't talk to any backend — uses the already-loaded
    in-process registry.
    """
    try:
        backends_payload: list[dict[str, Any]] = []
        for b in _BACKENDS:
            tool_count = sum(1 for _name, _tool, bk in _REGISTRATIONS if bk is b)
            # effective_auth is what the shim dispatches on right now (the OIDC
            # startup directive flips it to "oidc"); `auth` is only the static
            # backends.json default. Reporting both is what lets a chat answer
            # "am I on OIDC?" correctly — shim_info used to show only `auth`
            # (negotiate) and misled users mid-cutover into thinking OIDC
            # hadn't taken.
            #
            # Mirror http_client's dispatch decision EXACTLY (see http_client:
            # `mode = effective_auth or auth; if mode == "oidc" and not
            # _oidc_upn: mode = "negotiate"`). Reporting effective_auth="oidc"
            # for a backend that has no resolved UPN — and therefore actually
            # sends Negotiate — would just re-create the original mislead with
            # the sign flipped. fell_back distinguishes "directed to OIDC but
            # token acquisition failed this session" from "never directed".
            eff = b.effective_auth or b.auth
            if eff == "oidc" and not b._oidc_upn:
                eff = "negotiate"
            backends_payload.append({
                "name":                   b.name,
                "url":                    b.url,
                "auth":                   b.auth,
                "effective_auth":         eff,
                "oidc_upn":               (b._oidc_upn or None) if eff == "oidc" else None,
                "fell_back":              b._fell_back,
                "header":                 b.header if b.auth == "x-punch-auth" else None,
                "configured":             b.is_configured,
                "registered_tool_count":  tool_count,
            })

        # bundled_v: the version of pa_v2 the shim THINKS it's pinned to,
        # via the bundled server-version file. Distinct from the live
        # backend versions (which would require a /health probe and we
        # deliberately don't do that here).
        bundled_v = _bundled_server_version() or None

        # v3.0.3 — process-env diagnostics. Resolves the class of "shim
        # is reading config from somewhere I don't expect" mysteries in
        # one tool call instead of three PowerShell rounds. Sensitive
        # values (PUNCH_SAP_KEY) reported as <set>/<unset>, never echoed
        # verbatim — leaking a service-account key into a chat would be
        # worse than the diagnostic value of showing it.
        env_diagnostics = {
            "APPDATA":              os.environ.get("APPDATA"),
            "LOCALAPPDATA":         os.environ.get("LOCALAPPDATA"),
            "USERPROFILE":          os.environ.get("USERPROFILE"),
            "PUNCH_BACKENDS_FILE":  os.environ.get("PUNCH_BACKENDS_FILE"),
            "PUNCH_SAP_URL":        os.environ.get("PUNCH_SAP_URL"),
            "PUNCH_SAP_KEY":        "<set>" if os.environ.get("PUNCH_SAP_KEY") else "<unset>",
            "PUNCH_SHIM_AUTO_UPDATE": os.environ.get("PUNCH_SHIM_AUTO_UPDATE"),
        }
        backends_file_path = _resolve_backends_path()
        return json.dumps({
            "shim_version":         _SHIM_VERSION,
            "bundled_server":       bundled_v,
            "python":               f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "executable":           sys.executable,
            "platform":             sys.platform,
            "pid":                  os.getpid(),
            "cwd":                  os.getcwd(),
            "script_dir":           str(Path(__file__).parent),
            "backends_file":        str(backends_file_path),
            "backends_file_exists": backends_file_path.exists(),
            "primary_backend":      _PRIMARY.name if _PRIMARY else None,
            "backend_count":        len(_BACKENDS),
            "backends":             backends_payload,
            "auto_update_enabled":  _AUTO_UPDATE,
            # True while the startup OIDC preflight is still resolving on its
            # background thread (first activation can include an interactive
            # browser sign-in). Until it lands, effective_auth reads the static
            # default; this flag says "not yet determined" rather than letting a
            # too-early call read a definitive-looking "negotiate".
            "auth_directive_pending": bool(_AUTH_PREFLIGHT_THREAD and _AUTH_PREFLIGHT_THREAD.is_alive()),
            "process_env":          env_diagnostics,
        }, indent=2)
    except Exception as e:
        _log_event("shim_info_failed", level=logging.ERROR,
                   error=f"{type(e).__name__}: {e}")
        return json.dumps({
            "shim_version":  _SHIM_VERSION,
            "error_type":    type(e).__name__,
            "message":       str(e),
        }, indent=2)


mcp.tool()(shim_info)


# ---------------------------------------------------------------------------
# v3.1.0 — shim_access: report what the calling account can access. Combines
# per-backend connectivity (the shim knows this) with the pa_whoami access
# profile (the server knows this). Answers "what can I access?" and
# disambiguates "connected but unauthorised" from "not connected".
# ---------------------------------------------------------------------------

def _build_access_summary(connectivity: list[dict], account, account_error) -> str:
    """Human-readable connectivity + account summary (one or more sentences)."""
    ok = [c["backend"] for c in connectivity if c["reachable"] and c["auth_ok"]]
    bad = [c["backend"] for c in connectivity if not (c["reachable"] and c["auth_ok"])]
    parts: list[str] = []
    if ok:
        parts.append(f"Connected to {', '.join(ok)}.")
    if bad:
        parts.append(f"NOT connected to {', '.join(bad)}.")
    if isinstance(account, dict) and account.get("summary"):
        parts.append(str(account["summary"]))
    elif account_error:
        parts.append(f"Access profile unavailable: {account_error}")
    return " ".join(parts).strip()


async def shim_access(ctx: Context) -> str:
    """Report what your account can access: per-backend connectivity plus your
    access profile (systems, SAP company codes, Atlassian projects, Zabbix
    scope) from the server's pa_whoami tool. Use this to answer 'what can I
    access?' or to check whether a failure is 'not connected' vs 'connected
    but your account is not authorised'."""
    try:
        connectivity = [_probe_backend(b) for b in _BACKENDS]

        account = None
        account_error: str | None = None
        if "pa_whoami" in _NAME_TO_BACKEND:
            raw = _call_remote("pa_whoami", {})
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                account_error = "pa_whoami returned a non-JSON response"
            else:
                if not isinstance(parsed, dict):
                    account_error = (f"pa_whoami returned an unexpected "
                                     f"{type(parsed).__name__}, not an object")
                elif parsed.get("error"):
                    account_error = str(parsed.get("message") or "pa_whoami failed")
                else:
                    account = parsed
        else:
            account_error = ("pa_whoami is not registered — the pa_v2 server "
                             "may predate this tool. Update pa_v2.")

        return json.dumps({
            "shim_version":  _SHIM_VERSION,
            "connectivity":  connectivity,
            "account":       account,
            "account_error": account_error,
            "summary":       _build_access_summary(connectivity, account, account_error),
        }, indent=2, default=str)
    except Exception as e:
        _log_event("shim_access_failed", level=logging.ERROR,
                   error=f"{type(e).__name__}: {e}")
        return json.dumps({
            "shim_version": _SHIM_VERSION,
            "error_type":   type(e).__name__,
            "message":      str(e),
        }, indent=2)


mcp.tool()(shim_access)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _log_event(
        "shim_ready",
        shim_version=_SHIM_VERSION,
        backend_count=len(_BACKENDS),
        registered_tool_count=len(_REGISTRATIONS),
        unique_name_count=len(_NAME_TO_BACKEND),
    )
    mcp.run(transport="stdio")
