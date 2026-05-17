# Punch Analytics — Claude Desktop Extension

Thin-client MCP shim that bridges Claude Desktop (stdio MCP) to the
internal Punch Analytics HTTP server at
[ai.punchpowertrain.com](http://ai.punchpowertrain.com:3000). One shim,
multiple backends (SAP / Jira / Tempo / Zabbix), federated.

## v3.x — Kerberos / SPNEGO

As of v3.0.0 humans authenticate via their Windows logon ticket. **No
key on disk.** The install dialog's "Punch Analytics API Key" field is
optional — leave it blank if you're a regular user. Service identities
(headless dashboards, webhooks) keep the X-Punch-Auth key path; the
admin will tell you which mode applies.

## How to use

1. Install Claude Desktop: <https://claude.ai/download>
2. Double-click this `.mcpb`. Claude Desktop will ask whether to install
   the Punch Analytics extension. Click Install. In the dialog:
   - **API Key:** leave blank (or paste your service-account key)
   - **Server URL:** accept the default
   - Everything else: accept the default
3. Restart Claude Desktop.
4. Try `Using the pa_ tools, list my SAP company codes` in a Claude chat.

## What's where

| Path | Purpose |
|---|---|
| `%APPDATA%\Punch\backends.json` | Per-laptop backend config. Auto-seeded on first launch with `auth=negotiate`. Edit by hand if you need a custom setup. |
| `%LOCALAPPDATA%\PunchAnalytics\shim.log` | Shim activity log (JSON-lines). The first place to look if tools don't appear. |
| `shim.env.example` (next to `shim_server.py` in this bundle) | Documented env-var overrides. Almost always unneeded. |

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| No `pa_*` tools in Claude Desktop | MCPB not installed, or shim crashed | Read `shim.log` |
| Tools fail with 401 | UPN doesn't match `pa_users.aad_upn` | Admin runs `pa-admin show <you>` |
| Tools fail with NTLM password prompt in browser | Not on the corporate network | Connect via VPN |
| Tools fail with "no pa_users row matches UPN" | Never onboarded | Ask admin to run `pa-admin onboard <you> --upn <your-upn>` |

## Self-report

In Claude Desktop, ask: **"call shim_info"**. You'll get back the shim
version, configured backends, and auth mode for each.

## Auto-update

If `PUNCH_SHIM_AUTO_UPDATE=1` (the default), the shim pulls the latest
`shim_server.py` from
<https://github.com/Zenotech-bv/production_mcp_shim> on every Claude
Desktop bounce. Sha256-verified before the atomic swap; bumped versions
ship without re-installing the `.mcpb`. Dep changes still require a
fresh `.mcpb` (the bundled venv is built from `server/requirements.txt`
at first launch).

## Full documentation

- GitHub: <https://github.com/Zenotech-bv/production_mcp_shim>
- Onboarding runbook (in the punch-analytics repo): `docs/onboarding-kerberos.md`
- Auth design: `docs/auth.md` §13 (Kerberos / SPNEGO cutover)

## License

Proprietary — Punch Powertrain internal use only.
