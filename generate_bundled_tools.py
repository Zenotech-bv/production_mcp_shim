"""Regenerate the shim's bundled offline-fallback tools.json from the live
pa_v2 catalogue, using the SAME builder GET /tools uses
(server.dispatcher.list_tools_for_user) so the snapshot is byte-identical in
shape to what a live fetch would return. The previous bundle held 104 stale V1
tool names (get_vendor_aging, ...) which the v2 backend does not even serve.

Run from the punch-analytics venv:
  cd C:\\claude\\punch-analytics
  ./.venv/Scripts/python.exe C:\\claude\\production_mcp_shim\\generate_bundled_tools.py
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, r"C:\claude\punch-analytics")
import server
from server.dispatcher import list_tools_for_user

OUT = Path(r"C:\claude\production_mcp_shim\tools.json")

full = list_tools_for_user(full_access=True)
non = list_tools_for_user(full_access=False)
print(f"pa_v2 version: {server.__version__}")
print(f"tools: full_access=True -> {len(full)}  |  full_access=False -> {len(non)}")

# Bundle the representative non-admin set (most users; the live fetch corrects
# per-user once online). Keep only the wire keys the shim reads: name /
# description / inputSchema (matches the prior bundle's shape).
tools = []
for d in non:
    row = d.model_dump(by_alias=True)
    tools.append({
        "name":        row["name"],
        "description": row["description"],
        "inputSchema": row["inputSchema"],
    })

snapshot = {
    "tools":          tools,
    "count":          len(tools),
    "server_version": server.__version__,
    "generated_from": (
        "pa_v2 dispatcher.list_tools_for_user(full_access=False) — "
        f"offline-fallback snapshot @ v{server.__version__}"
    ),
}
OUT.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"wrote {OUT}  ({len(tools)} tools, {OUT.stat().st_size} bytes)")
# sanity: the 4 R5 tools present, no v1 names
names = {t["name"] for t in tools}
for t in ("pa_extract_bsis", "pa_extract_bsas", "pa_gl_open_items", "pa_gl_account_balance"):
    print(f"  {t}: {'OK' if t in names else 'MISSING'}")
print(f"  legacy get_vendor_aging present (should be False): {'get_vendor_aging' in names}")
