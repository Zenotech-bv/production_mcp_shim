"""
SAP Analytics — Cowork stdio shim. (Canonical source for auto-update.)

This runs on each user's laptop and is consumed by Cowork / Claude Desktop /
Claude Code via their local stdio MCP plugin mechanism. It does NOT connect
to SAP. Every tool call is forwarded as a POST to the internal HTTP server
at PUNCH_SAP_URL (default: http://mcp.punchpowertrain.com:3000).

Why a shim, not direct MCP-over-HTTP: Cowork's remote-MCP path requires
public DNS + HTTPS + org-level connector approval. A stdio shim sidesteps
all of that — the laptop's OS resolves the internal hostname, and the only
protocol Cowork sees is local stdio, which it supports out of the box.

This file is the CANONICAL shim source. The server vends it via
GET /shim/shim_server.py for auto-update (when the laptop has
PUNCH_SHIM_AUTO_UPDATE=1 set in shim.env).

Config (laptop-side, via env or shim.env next to this file):
    PUNCH_SAP_URL         — default http://mcp.punchpowertrain.com:3000
    PUNCH_SAP_KEY         — per-user API key issued by the SAP admin
    PUNCH_SAP_TIMEOUT     — read-timeout seconds, default 300
    PUNCH_SAP_VERIFY_TLS  — "false" to skip TLS verification (internal CA)
    PUNCH_SHIM_AUTO_UPDATE — "1" to enable startup self-update from server
    PUNCH_SHIM_DEBUG      — "1" to enable DEBUG-level logging
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Shim self-version. Bump alongside MCPB version (manifest.json::version).
# The server's /shim/manifest.json compares against this to decide whether
# to vend an update.
# ---------------------------------------------------------------------------

_SHIM_VERSION = "2.2.2"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Look for shim.env next to this file, then fall back to process env.
_SHIM_ENV = Path(__file__).parent / "shim.env"
if _SHIM_ENV.exists():
    load_dotenv(_SHIM_ENV, override=False)
load_dotenv()  # also pick up a .env if the user keeps one

PUNCH_SAP_URL = os.getenv("PUNCH_SAP_URL", "http://mcp.punchpowertrain.com:3000").rstrip("/")
PUNCH_SAP_KEY = os.getenv("PUNCH_SAP_KEY", "").strip()
# v2.2.2 — bumped default from 120 → 300 to match MCPB v2.1.1's manifest.
# Composer calls on PPS-class CCs in detailed mode hit ~95s; line_items
# can hit 200s+. 300s gives headroom without hiding real hangs.
PUNCH_SAP_TIMEOUT = float(os.getenv("PUNCH_SAP_TIMEOUT", "300"))
# Optional: allow-insecure-tls for HTTPS with internal CA that isn't imported.
_verify_tls = os.getenv("PUNCH_SAP_VERIFY_TLS", "true").lower() != "false"

# v2.2.2 — auto-update + debug toggles.
_AUTO_UPDATE = os.getenv("PUNCH_SHIM_AUTO_UPDATE", "0").strip() == "1"
_DEBUG = os.getenv("PUNCH_SHIM_DEBUG", "0").strip() == "1"


# ---------------------------------------------------------------------------
# Logging (v2.2.2)
#
# JSON-lines to file at:
#   Windows: %LOCALAPPDATA%\PunchAnalytics\shim.log
#   Mac/Linux: ~/.local/share/punch-analytics/shim.log
# Falls back to %TEMP% / /tmp if the preferred dir can't be created.
# Rotates at 5 MB × 5 backups.
#
# Errors also mirror to stderr as `[shim ERROR] ...` so Claude Desktop's
# MCP Servers panel surfaces failures without opening the file.
#
# Auth keys are NEVER logged. Tool kwargs are logged at DEBUG only.
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
    # Avoid duplicate handlers on auto-update re-exec.
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
                # Defensive: never log auth headers even if a caller fat-fingers.
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
        # Read-only filesystem or similar — keep the shim usable, log
        # only to stderr.
        pass

    # Stderr handler — terse, surfaces in Claude Desktop's MCP panel.
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
    # Stderr only carries WARNING+ unless DEBUG is on, to keep the
    # MCP panel readable.
    stderr_handler.setLevel(logging.DEBUG if _DEBUG else logging.WARNING)
    logger.addHandler(stderr_handler)
    return logger


_log = _setup_logging()


def _log_event(event: str, level: int = logging.INFO, **fields):
    """Emit a structured event. `fields` carries arbitrary kwargs."""
    _log.log(level, event, extra={"extra_fields": fields})


_log_event(
    "shim_start",
    shim_version=_SHIM_VERSION,
    pid=os.getpid(),
    server_url=PUNCH_SAP_URL,
    auto_update=_AUTO_UPDATE,
    debug=_DEBUG,
)


# ---------------------------------------------------------------------------
# httpx client (v2.2.2: stale-socket defenses)
#
# Three stacked defenses against the failure mode that wedged the 11:40
# replay run:
#   1. max_keepalive_connections=0 — httpx never pools sockets across
#      calls. Each call gets a fresh TCP connection.
#   2. Connection: close header — server closes the socket after the
#      response. No half-open sockets to inherit.
#   3. connect=5.0s — TCP+TLS handshake fails fast if the server is
#      unreachable. The user sees a clean error envelope in <5s instead
#      of the 4-minute hang the replay hit.
# ---------------------------------------------------------------------------

_LIMITS = httpx.Limits(max_keepalive_connections=0, max_connections=10)
_TIMEOUTS = httpx.Timeout(
    connect=5.0,
    read=PUNCH_SAP_TIMEOUT,
    write=10.0,
    pool=5.0,
)


def _bundled_server_version() -> str | None:
    """Best-effort read of the bundled tools.json's schema version,
    without committing to using it. Returns None on any failure.

    The on-disk tools.json does NOT carry a top-level `server_version`
    field — that's injected by the server's /tools endpoint at HTTP
    serve time. To get a stable on-disk version-marker, we scan for
    the per-tool ``Schema version: vX.Y.Z`` stamps embedded in
    descriptions (added v0.5.5, bumped each release). Pick the
    highest semantic version found across all tools — that's the
    "this MCPB was packaged from server vX.Y.Z" signal.
    """
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
    highest = max(versions)
    return ".".join(str(p) for p in highest)


def _client() -> httpx.Client:
    """Construct a fresh httpx client per call. v2.2.2 stacks
    no-keepalive + Connection:close + short connect timeout to make
    the stale-socket failure mode unreachable."""
    bundled = _bundled_server_version() or "unknown"
    return httpx.Client(
        base_url=PUNCH_SAP_URL,
        timeout=_TIMEOUTS,
        limits=_LIMITS,
        verify=_verify_tls,
        headers={
            "X-Punch-Auth": PUNCH_SAP_KEY,
            "Content-Type": "application/json",
            "Connection": "close",  # v2.2.2 — server closes after response
            "User-Agent": f"sap-analytics-shim/{_SHIM_VERSION}",
            "X-Punch-Shim-Bundled-Version": bundled,
            "X-Punch-Shim-Version": _SHIM_VERSION,
            "X-Punch-Shim-Pid": str(os.getpid()),
        },
    )


# ---------------------------------------------------------------------------
# v2.2.2 — Auto-update
#
# Opt-in via PUNCH_SHIM_AUTO_UPDATE=1. On startup, the shim asks the
# canonical source (GitHub) for its current version. If newer than
# the running shim, downloads + verifies sha256 + swaps + re-execs.
#
# Source priority:
#   1. PRIMARY: GitHub raw (HTTPS, public, no auth). Default URL
#      points at Zenotech-bv/production_mcp_shim. Override via
#      PUNCH_SHIM_UPDATE_URL_BASE env var if you fork.
#   2. FALLBACK: server's /shim/* endpoints (HTTP, auth-gated).
#      Used when GitHub is unreachable (corp firewall, GitHub
#      outage, raw.githubusercontent.com blocked).
#
# Both endpoints serve the same content. The server's
# `shim_canonical/` mirror is kept in sync with GitHub via the
# operational/publish-shim-to-github.ps1 script.
#
# Trust model: GitHub is publicly auditable + HTTPS. The server
# is auth-gated. Either source is acceptable; we prefer GitHub
# because HTTPS protects against MITM on internal HTTP-only
# deployments.
#
# Safety nets:
#   - Opt-in flag (PUNCH_SHIM_AUTO_UPDATE=1) — default off.
#   - sha256 verification of every downloaded payload.
#   - Backup-and-restore — old shim copied to shim_server.py.bak.
# ---------------------------------------------------------------------------

# Configurable via env var so a fork can point at its own GitHub
# org. Default points at the canonical Zenotech-bv repo. Strip
# trailing slash so the joined URLs are always well-formed.
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
    """Fetch (manifest, source_bytes, source_label) from GitHub or
    the server fallback. Returns None on every failure path so the
    caller skips the update silently.

    Tries GitHub first when prefer_github=True (the default). The
    fallback to the server is automatic: if GitHub returns a non-
    success status OR a transport error fires within the connect
    timeout, we try /shim/manifest.json + /shim/shim_server.py on
    the configured PUNCH_SAP_URL.
    """
    short_timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
    longer_timeout = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)

    sources: list[tuple[str, str, str, dict | None]] = []
    if prefer_github:
        sources.append((
            "github",
            f"{_GITHUB_RAW_BASE}/manifest.json",
            f"{_GITHUB_RAW_BASE}/shim_server.py",
            None,  # no auth headers for GitHub raw
        ))
    if PUNCH_SAP_KEY:
        sources.append((
            "server",
            f"{PUNCH_SAP_URL}/shim/manifest.json",
            f"{PUNCH_SAP_URL}/shim/shim_server.py",
            {"X-Punch-Auth": PUNCH_SAP_KEY},
        ))

    for label, manifest_url, source_url, extra_headers in sources:
        try:
            client_kwargs = {"timeout": short_timeout, "limits": _LIMITS,
                             "verify": _verify_tls}
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
        # Fetch source from same source.
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
    """Check for an updated shim and apply if newer.

    Best-effort — every failure path logs and returns without raising.
    """
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

    # Verify sha256 matches manifest before touching anything.
    actual_sha256 = hashlib.sha256(new_source).hexdigest().lower()
    if actual_sha256 != expected_sha256:
        _log_event("auto_update_fail", level=logging.ERROR,
                   source=source_label, reason="sha256_mismatch",
                   expected=expected_sha256, actual=actual_sha256)
        return

    # Sanity-check the source isn't empty / truncated.
    if len(new_source) < 1024:
        _log_event("auto_update_fail", level=logging.ERROR,
                   source=source_label, reason="source_too_small",
                   size_bytes=len(new_source))
        return

    # Backup current, write new, atomic rename.
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

    _log_event("auto_update_applied", from_version=_SHIM_VERSION,
               to_version=server_version, source=source_label,
               backup_path=str(backup_path))

    # Re-exec self with the same argv. Stdio handles are inherited so
    # Claude Desktop sees an uninterrupted MCP session.
    try:
        os.execv(sys.executable, [sys.executable, str(self_path), *sys.argv[1:]])
    except OSError as e:
        _log_event("auto_update_fail", level=logging.ERROR,
                   reason="execv_failed",
                   error=f"{type(e).__name__}: {e}")
        # Fall through to normal startup — the new code is on disk
        # and the next Claude Desktop bounce will pick it up.


_maybe_self_update()


# ---------------------------------------------------------------------------
# Load tools.json — schema source of truth.
#
# Resolution order (added v0.4.13, "auto-fetch shim"):
#   1. Live server's /tools endpoint, if reachable.
#   2. Bundled tools.json next to this file. Last-known-good fallback.
# ---------------------------------------------------------------------------

_TOOLS_JSON = Path(__file__).parent / "tools.json"
_TOOLS_FETCH_TIMEOUT_S = float(os.getenv("PUNCH_SAP_TOOLS_TIMEOUT", "5"))


def _fetch_tools_from_server() -> dict | None:
    """Try to GET /tools from the live server. Returns the parsed dict or None."""
    if not PUNCH_SAP_KEY:
        return None
    try:
        with _client() as c:
            r = c.get("/tools", timeout=_TOOLS_FETCH_TIMEOUT_S)
        if not r.is_success:
            _log_event(
                "tools_fetch_failed",
                level=logging.WARNING,
                status=r.status_code,
            )
            return None
        data = r.json()
    except Exception as e:
        _log_event(
            "tools_fetch_exception",
            level=logging.WARNING,
            error=f"{type(e).__name__}: {e}",
        )
        return None
    if not isinstance(data, dict):
        return None
    tools = data.get("tools")
    if not isinstance(tools, list) or not tools:
        return None
    return data


def _load_tools() -> dict:
    """Resolve the tools.json the shim will register."""
    fetched = _fetch_tools_from_server()
    if fetched is not None:
        n = len(fetched.get("tools") or [])
        sv = fetched.get("server_version", "?")
        _log_event(
            "tools_loaded",
            source="live",
            tool_count=n,
            server_version=sv,
        )
        bundled_sv = _bundled_server_version()
        if bundled_sv and isinstance(sv, str) and sv != "?" and bundled_sv != sv:
            _log_event(
                "bundled_tools_stale",
                level=logging.INFO,
                bundled_version=bundled_sv,
                live_version=sv,
            )
        return fetched

    if not _TOOLS_JSON.exists():
        _log_event(
            "tools_load_fatal",
            level=logging.ERROR,
            reason="no_live_no_bundled",
            bundled_path=str(_TOOLS_JSON),
        )
        sys.exit(2)
    bundled = json.loads(_TOOLS_JSON.read_text())
    n = len(bundled.get("tools") or [])
    bundled_sv = bundled.get("server_version", "?")
    _log_event(
        "tools_loaded",
        level=logging.WARNING,
        source="bundled",
        tool_count=n,
        bundled_server_version=bundled_sv,
    )
    return bundled


_TOOLS_DATA = _load_tools()
_TOOLS = _TOOLS_DATA.get("tools", [])


def _call_remote(tool_name: str, kwargs: dict) -> str:
    """POST to the internal server. Always returns a JSON string for MCP."""
    if not PUNCH_SAP_KEY:
        _log_event(
            "tool_call_misconfigured",
            level=logging.ERROR,
            tool=tool_name,
        )
        return json.dumps({
            "error": True,
            "error_type": "NotConfigured",
            "message": "PUNCH_SAP_KEY is not set. Edit shim.env with the key your admin gave you.",
        }, indent=2)

    t0 = time.monotonic()
    if _DEBUG:
        # DEBUG only — kwargs may contain free-text but no secrets.
        _log_event("tool_call_start", level=logging.DEBUG,
                   tool=tool_name, arg_keys=list(kwargs.keys()))
    try:
        with _client() as c:
            r = c.post(f"/tools/{tool_name}", json=kwargs)
    except httpx.ConnectError as e:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        _log_event("tool_call_unreachable", level=logging.ERROR,
                   tool=tool_name, elapsed_ms=elapsed_ms,
                   error=f"{type(e).__name__}: {e}")
        return json.dumps({
            "error": True,
            "error_type": "Unreachable",
            "message": f"Cannot reach {PUNCH_SAP_URL} — on VPN? {e}",
        }, indent=2)
    except httpx.TimeoutException as e:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        _log_event("tool_call_timeout", level=logging.ERROR,
                   tool=tool_name, elapsed_ms=elapsed_ms,
                   error=f"{type(e).__name__}")
        return json.dumps({
            "error": True,
            "error_type": "Timeout",
            "message": f"No response from {PUNCH_SAP_URL} in {PUNCH_SAP_TIMEOUT}s",
        }, indent=2)
    except Exception as e:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        _log_event("tool_call_transport_error", level=logging.ERROR,
                   tool=tool_name, elapsed_ms=elapsed_ms,
                   error=f"{type(e).__name__}: {e}")
        return json.dumps({
            "error": True,
            "error_type": "TransportError",
            "message": str(e),
        }, indent=2)

    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
    response_bytes = len(r.content) if hasattr(r, "content") else None

    # HTTP errors — surface the server's JSON body as-is where possible.
    if r.status_code >= 400:
        _log_event("tool_call_http_error",
                   level=logging.WARNING if r.status_code < 500 else logging.ERROR,
                   tool=tool_name, status=r.status_code,
                   elapsed_ms=elapsed_ms, response_bytes=response_bytes)
        try:
            return json.dumps(r.json(), indent=2, default=str)
        except Exception:
            return json.dumps({
                "error": True,
                "error_type": f"HTTP{r.status_code}",
                "message": r.text[:500],
            }, indent=2)

    _log_event("tool_call_ok", tool=tool_name,
               elapsed_ms=elapsed_ms, response_bytes=response_bytes)

    # Happy path — server wraps result in {"correlation_id", "tool", "user", "result"}
    try:
        envelope = r.json()
    except Exception:
        return r.text
    return json.dumps(envelope.get("result", envelope), indent=2, default=str)


# ---------------------------------------------------------------------------
# Register tools dynamically from tools.json
# ---------------------------------------------------------------------------

mcp = FastMCP("Punch Analytics — Punch Powertrain (thin client)")


def _make_forwarder(tool_name: str):
    """Return a closure that forwards kwargs to the internal server."""
    def forward(**kwargs) -> str:
        clean = {k: v for k, v in kwargs.items() if v not in (None, "")}
        return _call_remote(tool_name, clean)
    forward.__name__ = tool_name
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


def _register_from_schema(tool: dict) -> None:
    name = tool["name"]
    schema = tool["inputSchema"]
    props = schema.get("properties", {})

    params = []
    for arg_name, arg_schema in props.items():
        t = arg_schema.get("type", "string")
        py_type = JSON_SCHEMA_TYPE_TO_PY.get(t, "str")
        default = arg_schema.get("default", None)
        if default is None:
            default_repr = "None"
        else:
            default_repr = repr(default)
        params.append(f"{arg_name}: {py_type} = {default_repr}")
    sig = ", ".join(params)

    forwarder = _make_forwarder(name)
    safe_desc = (
        tool.get("description") or ""
    ).replace(chr(34) * 3, "").replace(chr(92), chr(92) * 2).strip()
    src = (
        f"def {name}({sig}) -> str:\n"
        f"    \"\"\"{safe_desc}\"\"\"\n"
        f"    return _forwarder(**{{k: v for k, v in locals().items()}})\n"
    )
    ns: dict = {"_forwarder": forwarder}
    exec(src, ns)
    typed_fn = ns[name]
    mcp.tool()(typed_fn)


for _tool in _TOOLS:
    _register_from_schema(_tool)


if __name__ == "__main__":
    _log_event(
        "shim_ready",
        shim_version=_SHIM_VERSION,
        server_url=PUNCH_SAP_URL,
        tool_count=len(_TOOLS),
    )
    mcp.run(transport="stdio")
