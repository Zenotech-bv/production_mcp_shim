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

```json
{
  "backends": [
    {
      "name":   "sap",
      "url":    "http://mcp.punchpowertrain.com:3000",
      "header": "X-Punch-Auth",
      "key":    "..."
    },
    {
      "name":   "tempo",
      "url":    "http://tempo-mcp.punchpowertrain.com:3000",
      "header": "X-Punch-Auth",
      "key":    "..."
    }
  ],
  "primary": "sap"
}
```

The optional ``primary`` field names which backend's URL is used for
auto-update fallback (when GitHub is unreachable). Defaults to the
first entry.

## Legacy env vars (still supported)

    PUNCH_SAP_URL         — default http://mcp.punchpowertrain.com:3000
    PUNCH_SAP_KEY         — per-user API key issued by the SAP admin
    PUNCH_SAP_TIMEOUT     — read-timeout seconds, default 300
    PUNCH_SAP_VERIFY_TLS  — "false" to skip TLS verification (internal CA)
    PUNCH_SHIM_AUTO_UPDATE — "1" to enable startup self-update from server
    PUNCH_SHIM_DEBUG      — "1" to enable DEBUG-level logging

New in v2.2.4:

    PUNCH_BACKENDS_FILE   — explicit path to backends.json (overrides default)
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
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Shim self-version. Bump alongside MCPB version (manifest.json::version).
# The server's /shim/manifest.json compares against this to decide whether
# to vend an update.
# ---------------------------------------------------------------------------

_SHIM_VERSION = "2.2.6"


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
PUNCH_SAP_URL = os.getenv("PUNCH_SAP_URL", "http://mcp.punchpowertrain.com:3000").rstrip("/")
PUNCH_SAP_KEY = os.getenv("PUNCH_SAP_KEY", "").strip()
PUNCH_SAP_TIMEOUT = float(os.getenv("PUNCH_SAP_TIMEOUT", "300"))
_verify_tls = os.getenv("PUNCH_SAP_VERIFY_TLS", "true").lower() != "false"
_AUTO_UPDATE = os.getenv("PUNCH_SHIM_AUTO_UPDATE", "0").strip() == "1"
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


@dataclass
class Backend:
    """One backend MCP server. Each backend has its own URL + auth +
    fetched tool set."""
    name: str
    url: str
    header: str
    key: str
    # Filled in at fetch time; empty until _load_tools_multi runs.
    tools: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self.url = self.url.rstrip("/")
        # Defensive: a backend with no auth key means we can't talk
        # to it. Don't crash here; the fetch step will skip it and
        # log a warning.
        self.key = self.key.strip() if isinstance(self.key, str) else ""

    @property
    def is_configured(self) -> bool:
        return bool(self.url and self.key and self.header)

    def http_client(self, *, read_timeout: float | None = None) -> httpx.Client:
        """Build a fresh httpx.Client for this backend.

        v2.2.2 stale-socket defenses preserved:
          - max_keepalive_connections=0 (no socket pooling across calls)
          - Connection: close header (server closes after response)
          - connect=5.0s (fail fast on unreachable backend)
        """
        rt = read_timeout if read_timeout is not None else PUNCH_SAP_TIMEOUT
        timeouts = httpx.Timeout(connect=5.0, read=rt, write=10.0, pool=5.0)
        limits = httpx.Limits(max_keepalive_connections=0, max_connections=10)
        bundled_v = _bundled_server_version() or "unknown"
        return httpx.Client(
            base_url=self.url,
            timeout=timeouts,
            limits=limits,
            verify=_verify_tls,
            headers={
                self.header: self.key,
                "Content-Type": "application/json",
                "Connection": "close",
                "User-Agent": f"punch-shim/{_SHIM_VERSION}",
                "X-Punch-Shim-Backend": self.name,
                "X-Punch-Shim-Bundled-Version": bundled_v,
                "X-Punch-Shim-Version": _SHIM_VERSION,
                "X-Punch-Shim-Pid": str(os.getpid()),
            },
        )


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
        backends.append(Backend(name=name, url=url, header=header, key=key))

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
            "url":    "http://mcp.punchpowertrain.com:3000",
            "header": "X-Punch-Auth",
        },
        {
            "name":   "zabbix",
            "url":    "http://mcp.punchpowertrain.com:3002",
            "header": "X-Punch-Auth",
        },
    ],
    "primary": "sap",
}


def _maybe_seed_backends_file(cfg_path: Path) -> bool:
    """If cfg_path doesn't exist AND PUNCH_SAP_KEY is set, write a
    default backends.json populated with that key for every backend.

    Returns True if a file was written, False otherwise. Failures are
    swallowed -- a malformed cwd or read-only filesystem must not
    prevent the shim from starting; the env-var fallback path covers
    that case.
    """
    if cfg_path.exists():
        return False
    if not PUNCH_SAP_KEY:
        return False
    # Build the seed payload from the template.
    seed: dict = {
        "backends": [
            {**entry, "key": PUNCH_SAP_KEY}
            for entry in _DEFAULT_BACKENDS_TEMPLATE["backends"]
        ],
        "primary": _DEFAULT_BACKENDS_TEMPLATE["primary"],
    }
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
        note=(
            "first-run auto-create; populated all backends with "
            "PUNCH_SAP_KEY. Edit the file to issue per-backend keys."
        ),
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

_BACKENDS, _PRIMARY = _load_backends()


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

    _log_event("auto_update_applied", from_version=_SHIM_VERSION,
               to_version=server_version, source=source_label,
               backup_path=str(backup_path))

    try:
        os.execv(sys.executable, [sys.executable, str(self_path), *sys.argv[1:]])
    except OSError as e:
        _log_event("auto_update_fail", level=logging.ERROR,
                   reason="execv_failed",
                   error=f"{type(e).__name__}: {e}")


_maybe_self_update()


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

            # Always register the prefixed form.
            #
            # v2.2.6 hotfix: prefix separator is '_' not '.'. Claude's
            # API rejects tool names that don't match
            # ``^[a-zA-Z0-9_-]{1,64}$`` -- the '.' separator we used in
            # v2.2.4 and v2.2.5 triggered
            # ``tools.N.FrontendRemoteMcpToolDefinition.name: String
            # should match pattern...`` validation errors as soon as
            # any laptop activated more than one backend. The bug was
            # latent in v2.2.4 (no laptop had a second backend
            # configured) and fired the day v2.2.5's auto-create
            # finally wired up the Zabbix backend on a real laptop.
            prefixed = f"{backend.name}_{orig}"
            name_to_backend[prefixed] = (backend, orig)
            registrations.append((prefixed, tool, backend))

            # Bare alias only when no collision.
            if bare_counts[orig] == 1:
                name_to_backend[orig] = (backend, orig)
                registrations.append((orig, tool, backend))
            else:
                collisions[orig].append(backend.name)

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


# ---------------------------------------------------------------------------
# _call_remote — looks up the backend and forwards
# ---------------------------------------------------------------------------


def _call_remote(registered_name: str, kwargs: dict) -> str:
    """Forward the call to the backend that owns this registered name.
    Returns a JSON string for MCP."""
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
            return json.dumps(r.json(), indent=2, default=str)
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
    return json.dumps(envelope.get("result", envelope), indent=2, default=str)


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
        t = arg_schema.get("type", "string")
        py_type = JSON_SCHEMA_TYPE_TO_PY.get(t, "str")
        default = arg_schema.get("default", None)
        default_repr = "None" if default is None else repr(default)
        params.append(f"{arg_name}: {py_type} = {default_repr}")
    sig = ", ".join(params)

    forwarder = _make_forwarder(registered_name)
    safe_desc = (
        tool.get("description") or ""
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


for _registered_name, _tool, _backend in _REGISTRATIONS:
    _register_one(_registered_name, _tool, _backend)


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
