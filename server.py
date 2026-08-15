#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.0,<3", "httpx>=0.27"]
# ///
"""edgecenter_mcp — an MCP server for the EdgeCenter Cloud API.

Exposes an EdgeCenter account to MCP clients: a fleet-wide inventory of
baremetal and virtual instances across every region, their networking, VNC
console links, cloud tasks, rental prices, power control, and a generic
escape hatch for any endpoint the named tools do not cover.

Configuration, highest precedence first:
  1. environment: EDGECENTER_API_TOKEN, EDGECENTER_PROJECT_ID, EDGECENTER_CLIENT_ID,
     EDGECENTER_BASE_URL, EDGECENTER_MCP_READONLY
  2. ~/.config/edgecenter_mcp/config.json (chmod 600)

Mutating calls (power actions and POST/PUT/PATCH/DELETE through api_request)
require an explicit confirm=True, and are refused outright when the server runs
with EDGECENTER_MCP_READONLY=1.

Every endpoint used here was verified against a live account; see README.md for
the API map, including the paths that do not exist despite being documented.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Literal

import httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

__version__ = "1.0.0"

DEFAULT_CONFIG_PATHS = (
    "~/.config/edgecenter_mcp/config.json",
    "~/.config/edgecenter-mcp/config.json",  # legacy layout
)


def _config_path() -> Path:
    override = os.environ.get("EDGECENTER_MCP_CONFIG")
    if override:
        return Path(override).expanduser()
    for candidate in DEFAULT_CONFIG_PATHS:
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
    return Path(DEFAULT_CONFIG_PATHS[0]).expanduser()


CONFIG_PATH = _config_path()

DEFAULT_BASE_URL = "https://api.edgecenter.ru"
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
INVENTORY_TTL = 60.0  # seconds
REGIONS_TTL = 3600.0  # seconds
MAX_OUTPUT_CHARS = 40000

# Established by probing routes with a nonexistent UUID: the API exposes
# POST /cloud/v1/instances/{project}/{region}/{id}/{action}.
# There are no reboot_hard / rebuild / rescue / resize routes.
INSTANCE_ACTIONS = {
    "start": "power on",
    "stop": "graceful shutdown",
    "reboot": "soft reboot",
    "powercycle": "hard power cycle",
    "suspend": "suspend",
    "resume": "resume",
}


class ECError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def _int_env(name: str) -> int | None:
    """Read an integer environment variable, failing loudly on garbage."""
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ECError(f"{name} must be an integer, got {raw!r}") from exc


def _load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    if CONFIG_PATH.is_file():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except (OSError, ValueError) as exc:  # a broken config must not kill the server
            cfg = {"_config_error": f"{CONFIG_PATH}: {exc}"}

    env = os.environ
    if env.get("EDGECENTER_API_TOKEN"):
        cfg["api_token"] = env["EDGECENTER_API_TOKEN"]
    if _int_env("EDGECENTER_PROJECT_ID") is not None:
        cfg["project_id"] = _int_env("EDGECENTER_PROJECT_ID")
    if _int_env("EDGECENTER_CLIENT_ID") is not None:
        cfg["client_id"] = _int_env("EDGECENTER_CLIENT_ID")
    if env.get("EDGECENTER_BASE_URL"):
        cfg["base_url"] = env["EDGECENTER_BASE_URL"]
    if env.get("EDGECENTER_MCP_READONLY", "").lower() in ("1", "true", "yes"):
        cfg["readonly"] = True
    return cfg


CONFIG = _load_config()
BASE_URL = CONFIG.get("base_url") or DEFAULT_BASE_URL
READONLY = bool(CONFIG.get("readonly"))


def _auth_header(token: str) -> str:
    """A browser JWT authenticates as Bearer, a permanent API token as APIKey."""
    if token.startswith("ey") and token.count(".") == 2:
        return f"Bearer {token}"
    return f"APIKey {token}"


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #
_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None:
            token = CONFIG.get("api_token")
            if not token:
                raise ECError(
                    "No API token. Set EDGECENTER_API_TOKEN, or write "
                    f'{{"api_token": "..."}} to {CONFIG_PATH} and chmod 600 it.'
                )
            _client = httpx.AsyncClient(
                base_url=BASE_URL,
                timeout=httpx.Timeout(60.0, connect=15.0),
                headers={
                    "Authorization": _auth_header(token),
                    "Accept": "application/json",
                    "User-Agent": f"edgecenter_mcp/{__version__}",
                },
                follow_redirects=True,
            )
        return _client


async def api(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
) -> Any:
    """Issue one API request and return parsed JSON (or {'_text': ...})."""
    client = await _get_client()
    method = method.upper()
    if not path.startswith("/"):
        path = "/" + path

    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            resp = await client.request(method, path, params=params, json=json_body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            if attempt == 0:
                await asyncio.sleep(1.5)
                continue
            raise ECError(f"{method} {path}: network unreachable — {exc}") from exc

        if resp.status_code == 401:
            raise ECError(
                f"401 Unauthorized on {method} {path}. The token is revoked or expired. "
                "A browser JWT lasts about a day; issue a permanent API token in the "
                "EdgeCenter panel (Profile → API tokens) instead."
            )
        if resp.status_code >= 500 and attempt == 0:
            await asyncio.sleep(1.5)
            continue
        if resp.status_code >= 400:
            body = resp.text.strip()
            if not body:
                body = "(empty body — this usually means the route does not exist)"
            raise ECError(f"{resp.status_code} on {method} {path}: {body[:800]}")

        if not resp.content:
            return {"_status": resp.status_code, "_empty": True}
        try:
            return resp.json()
        except ValueError:
            return {"_status": resp.status_code, "_text": resp.text[:4000]}

    raise ECError(f"{method} {path}: request failed — {last_exc}")


# --------------------------------------------------------------------------- #
# caches: regions and inventory
# --------------------------------------------------------------------------- #
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str, ttl: float) -> Any | None:
    hit = _cache.get(key)
    if hit and (time.monotonic() - hit[0]) < ttl:
        return hit[1]
    return None


def _cache_put(key: str, value: Any) -> Any:
    _cache[key] = (time.monotonic(), value)
    return value


def _cache_invalidate() -> None:
    """Drop everything a mutation can invalidate: the inventory and floating IP maps."""
    for key in [k for k in _cache if k == "inventory" or k.startswith("fips:")]:
        del _cache[key]


async def _project_id() -> int:
    if CONFIG.get("project_id"):
        return int(CONFIG["project_id"])
    cached = _cache_get("project_id", REGIONS_TTL)
    if cached:
        return cached
    data = await api("GET", "/cloud/v1/projects")
    results = data.get("results") or []
    if not results:
        raise ECError("This account has no cloud project.")
    return _cache_put("project_id", int(results[0]["id"]))


async def _regions(refresh: bool = False) -> list[dict[str, Any]]:
    if not refresh:
        cached = _cache_get("regions", REGIONS_TTL)
        if cached is not None:
            return cached
    data = await api("GET", "/cloud/v1/regions")
    regions = [
        {
            "id": r["id"],
            "name": r.get("display_name") or r.get("keystone_name"),
            "country": r.get("country"),
            "state": r.get("state"),
            "has_baremetal": r.get("has_baremetal"),
            "has_kvm": r.get("has_kvm"),
        }
        for r in data.get("results", [])
    ]
    regions.sort(key=lambda r: r["id"])
    return _cache_put("regions", regions)


def _ips(instance: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for addrs in (instance.get("addresses") or {}).values():
        for a in addrs or []:
            addr = a.get("addr")
            if addr and addr not in out:
                out.append(addr)
    return out


def _normalize(inst: dict[str, Any], region: dict[str, Any], kind: str) -> dict[str, Any]:
    meta = inst.get("metadata") or {}
    flavor = inst.get("flavor") or {}
    return {
        "name": inst.get("instance_name"),
        "id": inst.get("instance_id"),
        "kind": kind,
        "status": inst.get("status"),
        "vm_state": inst.get("vm_state"),
        "task_state": inst.get("task_state"),
        "region_id": region["id"],
        "region": region["name"],
        "flavor": flavor.get("flavor_name"),
        "vcpus": flavor.get("vcpus"),
        "ram_mb": flavor.get("ram"),
        "hardware": (flavor.get("hardware_description") or {}).get("cpu"),
        "os": " ".join(x for x in (meta.get("os_distro"), meta.get("os_version")) if x) or None,
        "ips": _ips(inst),
        "created": inst.get("instance_created"),
        "keypair": inst.get("keypair_name"),
    }


async def _inventory(refresh: bool = False) -> list[dict[str, Any]]:
    """Every instance (baremetal + virtual) across all regions, fetched in parallel."""
    if not refresh:
        cached = _cache_get("inventory", INVENTORY_TTL)
        if cached is not None:
            return cached

    project = await _project_id()
    regions = await _regions()

    async def fetch(region: dict[str, Any], endpoint: str, kind: str) -> list[dict[str, Any]]:
        try:
            data = await api(
                "GET",
                f"/cloud/v1/{endpoint}/{project}/{region['id']}",
                params={"limit": 1000},
            )
        except ECError:
            return []
        return [_normalize(i, region, kind) for i in data.get("results", [])]

    jobs = []
    for region in regions:
        if region.get("has_baremetal"):
            jobs.append(fetch(region, "bminstances", "baremetal"))
        if region.get("has_kvm"):
            jobs.append(fetch(region, "instances", "virtual"))

    chunks = await asyncio.gather(*jobs)
    # a baremetal node can surface in both endpoints — collapse on id
    seen: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        for s in chunk:
            if s["id"] not in seen or s["kind"] == "baremetal":
                seen[s["id"]] = s
    result = sorted(seen.values(), key=lambda s: (s["region_id"], s["name"] or ""))
    return _cache_put("inventory", result)


async def _fips_by_fixed(region_id: int) -> dict[str, str]:
    """{private fixed IP -> public floating IP} for one region."""
    key = f"fips:{region_id}"
    cached = _cache_get(key, INVENTORY_TTL)
    if cached is not None:
        return cached
    project = await _project_id()
    try:
        data = await api("GET", f"/cloud/v1/floatingips/{project}/{region_id}")
    except ECError:
        return {}
    mapping = {
        f["fixed_ip_address"]: f["floating_ip_address"]
        for f in data.get("results", [])
        if f.get("fixed_ip_address") and f.get("floating_ip_address")
    }
    return _cache_put(key, mapping)


async def _node_fixed_ips(node: dict[str, Any]) -> list[str]:
    """A node's own addresses, including those on VLAN sub-ports."""
    project = await _project_id()
    try:
        data = await api(
            "GET",
            f"/cloud/v1/instances/{project}/{node['region_id']}/{node['id']}/interfaces",
        )
    except ECError:
        return []
    ips: list[str] = []
    for iface in data.get("results", []):
        for port in [iface, *(iface.get("sub_ports") or [])]:
            for assignment in port.get("ip_assignments") or []:
                addr = assignment.get("ip_address")
                if addr and addr not in ips:
                    ips.append(addr)
    return ips


async def _attach_fips(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copies of the nodes carrying fixed_ips and floating_ips."""
    region_ids = sorted({n["region_id"] for n in nodes})
    maps = await asyncio.gather(*(_fips_by_fixed(r) for r in region_ids))
    by_region = dict(zip(region_ids, maps, strict=True))
    fixed_lists = await asyncio.gather(*(_node_fixed_ips(n) for n in nodes))

    enriched = []
    for node, fixed in zip(nodes, fixed_lists, strict=True):
        mapping = by_region.get(node["region_id"], {})
        copy = dict(node)
        copy["fixed_ips"] = fixed
        copy["floating_ips"] = [mapping[ip] for ip in fixed if ip in mapping]
        enriched.append(copy)
    return enriched


async def _resolve(query: str, refresh: bool = False) -> dict[str, Any]:
    """Find one node by name, UUID (or its prefix), or any of its IP addresses."""
    nodes = await _inventory(refresh=refresh)
    q = query.strip()
    ql = q.lower()

    exact = [s for s in nodes if (s["name"] or "").lower() == ql or s["id"].lower() == ql]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ECError(
            f'"{q}" is ambiguous: ' + ", ".join(f"{s['name']} ({s['id']})" for s in exact)
        )

    by_ip = [s for s in nodes if q in s["ips"]]
    if len(by_ip) == 1:
        return by_ip[0]

    partial = [
        s for s in nodes if s["id"].lower().startswith(ql) or ql in (s["name"] or "").lower()
    ]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise ECError(
            f'"{q}" is ambiguous, candidates: '
            + ", ".join(f"{s['name']} ({s['id'][:8]}, {s['region']})" for s in partial[:10])
        )

    # no hit on the primary addresses: it may be a floating or private IP
    if q.count(".") == 3 and q.replace(".", "").isdigit():
        for node in await _attach_fips(nodes):
            if q in node["floating_ips"] or q in node["fixed_ips"]:
                return node

    raise ECError(
        f'No node matches "{q}" among {len(nodes)} instances. '
        "Call servers() to list them (name / UUID / IP)."
    )


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #
def _dump(obj: Any, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    if len(text) > limit:
        text = text[:limit] + f"\n… (truncated, {len(text)} characters total)"
    return text


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    head = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()
    sep = "  ".join("-" * w for w in widths)
    body = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip() for row in rows]
    return "\n".join([head, sep, *body])


def _guard_mutation(what: str, confirm: bool) -> None:
    if READONLY:
        raise ECError(
            "Refused: this server runs in read-only mode "
            f"(EDGECENTER_MCP_READONLY=1). Blocked: {what}"
        )
    if not confirm:
        raise ECError(
            f"Confirmation required: {what}. Repeat the call with confirm=True once "
            "the user has agreed to it."
        )


# --------------------------------------------------------------------------- #
# MCP server
# --------------------------------------------------------------------------- #
mcp = MCPServer(
    name="edgecenter_mcp",
    version=__version__,
    instructions=(
        "EdgeCenter Cloud account access: baremetal and virtual instances across "
        "every region, their networking, VNC consoles, cloud tasks and prices.\n"
        "Start with servers() — a fleet-wide inventory with IPs and statuses; every "
        "other tool accepts a name, UUID or IP taken from it.\n"
        "The API does NOT expose quotas, balance or invoices — those live in the web "
        "panel only.\n"
        "Anything not covered by a named tool is reachable through api_request().\n"
        "Every mutation (power actions, POST/DELETE) requires confirm=True and must "
        "be agreed with the user first."
    ),
)

RO = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
RW = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)


@mcp.tool(
    annotations=RO,
    description="Account identity: user, client, enabled services, projects and regions.",
)
async def whoami() -> str:
    user, client, projects, regs = await asyncio.gather(
        api("GET", "/iam/users/me"),
        api("GET", "/iam/clients/me"),
        api("GET", "/cloud/v1/projects"),
        _regions(),
        return_exceptions=True,
    )
    token = CONFIG.get("api_token", "")
    out: dict[str, Any] = {
        "base_url": BASE_URL,
        "auth_scheme": _auth_header(token).split(" ")[0] if token else "no token",
        "readonly_mode": READONLY,
        "config_file": str(CONFIG_PATH),
    }
    if isinstance(user, dict):
        out["user"] = {
            "id": user.get("id"),
            "email": user.get("email"),
            "name": user.get("name"),
            "company": user.get("company"),
            "client_id": user.get("client"),
            "groups": [g.get("name") for g in user.get("groups", [])],
            "two_fa": user.get("two_fa"),
        }
    else:
        out["user_error"] = str(user)
    if isinstance(client, dict):
        out["client"] = {
            "id": client.get("id"),
            "status": client.get("status"),
            "capabilities": client.get("capabilities"),
            "services": {
                k: v.get("status") for k, v in (client.get("serviceStatuses") or {}).items()
            },
        }
    if isinstance(projects, dict):
        out["projects"] = [
            {"id": p["id"], "name": p.get("name"), "state": p.get("state")}
            for p in projects.get("results", [])
        ]
    if isinstance(regs, list):
        out["regions"] = regs
    return _dump(out)


@mcp.tool(annotations=RO, description="List regions with their baremetal/KVM capabilities.")
async def regions(refresh: bool = False) -> str:
    data = await _regions(refresh=refresh)
    rows = [
        [
            str(r["id"]),
            r["name"] or "",
            r["country"] or "",
            r["state"] or "",
            "bm" if r["has_baremetal"] else "",
            "kvm" if r["has_kvm"] else "",
        ]
        for r in data
    ]
    return _table(rows, ["id", "region", "country", "state", "bm", "kvm"])


@mcp.tool(
    annotations=RO,
    description=(
        "Fleet inventory: every baremetal and virtual instance across all regions "
        "with its IPs, status, flavor and OS. Filter by region or by a substring "
        "matching a name, IP or UUID.\n"
        "with_floating_ips=True also resolves each node's floating and private "
        "addresses (costs one extra API call per node)."
    ),
)
async def servers(
    region_id: int | None = None,
    query: str | None = None,
    with_floating_ips: bool = False,
    refresh: bool = False,
    raw: bool = False,
) -> str:
    data = await _inventory(refresh=refresh)
    if region_id is not None:
        data = [s for s in data if s["region_id"] == region_id]
    if query:
        q = query.strip().lower()
        data = [
            s
            for s in data
            if q in (s["name"] or "").lower()
            or q in s["id"].lower()
            or any(q in ip for ip in s["ips"])
        ]
    if not data:
        return "Nothing matched (check the filter, or retry with refresh=True)."
    if with_floating_ips:
        data = await _attach_fips(data)
    if raw:
        return _dump(data)

    headers = ["name", "kind", "region", "ip", "status", "task", "flavor", "os", "id"]
    if with_floating_ips:
        headers.insert(4, "floating")
    rows = []
    for s in data:
        row = [
            s["name"] or "-",
            s["kind"][:2],
            s["region"] or str(s["region_id"]),
            ", ".join(s["ips"]) or "-",
            s["status"] or "-",
            s["task_state"] or "",
            s["flavor"] or "-",
            s["os"] or "-",
            s["id"][:8],
        ]
        if with_floating_ips:
            row.insert(4, ", ".join(s.get("floating_ips") or []) or "-")
        rows.append(row)
    return f"{_table(rows, headers)}\n\ntotal: {len(data)}"


@mcp.tool(
    annotations=RO,
    description=(
        "Rental cost per instance and for the fleet as a whole, broken down by "
        "region. Sourced from /cloud/v1/price_info — this is the instance list "
        "price, not an invoice: the API exposes no billing or balance data."
    ),
)
async def costs(region_id: int | None = None, refresh: bool = False) -> str:
    nodes = await _inventory(refresh=refresh)
    if region_id is not None:
        nodes = [n for n in nodes if n["region_id"] == region_id]
    if not nodes:
        return "No instances found."
    project = await _project_id()

    async def price(node: dict[str, Any]) -> dict[str, Any]:
        try:
            return await api(
                "GET",
                f"/cloud/v1/price_info/{project}/{node['region_id']}/instances/{node['id']}",
            )
        except ECError as exc:
            return {"error": str(exc)[:120]}

    prices = await asyncio.gather(*(price(n) for n in nodes))

    rows, total, by_region, currency = [], 0.0, {}, ""
    for node, p in zip(nodes, prices, strict=True):
        per_month = p.get("price_per_month")
        per_hour = p.get("price_per_hour")
        currency = p.get("currency_code") or currency
        if isinstance(per_month, (int, float)):
            total += per_month
            by_region[node["region"]] = by_region.get(node["region"], 0.0) + per_month
        rows.append(
            [
                node["name"] or "-",
                node["region"] or "-",
                node["flavor"] or "-",
                f"{per_month:,.0f}" if isinstance(per_month, (int, float)) else "—",
                f"{per_hour:.2f}" if isinstance(per_hour, (int, float)) else "—",
            ]
        )
    unit = currency or "?"
    rows.sort(key=lambda r: -float(r[3].replace(",", "")) if r[3] != "—" else 0)
    table = _table(rows, ["name", "region", "flavor", f"{unit}/month", f"{unit}/hour"])
    summary = "\n".join(
        f"  {r:<20} {v:>12,.0f}" for r, v in sorted(by_region.items(), key=lambda x: -x[1])
    )
    return (
        f"{table}\n\nBy region ({unit}/month):\n{summary}\n\n"
        f"TOTAL: {total:,.2f} {unit}/month across {len(nodes)} instances\n\n"
        "Note: list prices for the instances themselves. Actual invoices, account "
        "balance and discounts are not available through the API — only in the "
        "EdgeCenter control panel."
    )


@mcp.tool(
    annotations=RO,
    description=(
        "Fleet health summary: instance counts per region, anything not ACTIVE, "
        "nodes stuck in a task_state, and cloud tasks currently running."
    ),
)
async def fleet_health(refresh: bool = True) -> str:
    project = await _project_id()
    inv, tasks_data = await asyncio.gather(
        _inventory(refresh=refresh),
        api("GET", "/cloud/v1/tasks", params={"project_id": project, "limit": 50}),
        return_exceptions=True,
    )
    if isinstance(inv, BaseException):
        raise inv

    by_region: dict[str, int] = {}
    unhealthy, busy = [], []
    for s in inv:
        key = f"{s['region']} ({s['region_id']})"
        by_region[key] = by_region.get(key, 0) + 1
        if s["status"] != "ACTIVE":
            unhealthy.append(s)
        elif s["task_state"]:
            busy.append(s)

    active_tasks = []
    if isinstance(tasks_data, dict):
        active_tasks = [
            {
                "id": t.get("id"),
                "type": t.get("task_type"),
                "state": t.get("state"),
                "region": t.get("region_id"),
                "created": t.get("created_on"),
            }
            for t in tasks_data.get("results", [])
            if t.get("state") in ("NEW", "RUNNING")
        ]

    return _dump(
        {
            "total_nodes": len(inv),
            "by_region": by_region,
            "unhealthy": [
                {
                    "name": s["name"],
                    "region": s["region"],
                    "status": s["status"],
                    "vm_state": s["vm_state"],
                    "ips": s["ips"],
                }
                for s in unhealthy
            ]
            or "none — every instance is ACTIVE",
            "busy_with_task_state": [
                {
                    "name": s["name"],
                    "region": s["region"],
                    "task_state": s["task_state"],
                    "ips": s["ips"],
                }
                for s in busy
            ]
            or "none",
            "running_cloud_tasks": active_tasks or "none",
            "note": (
                "status and task_state describe the orchestrator's view, not whether "
                "the workload inside the node is alive: an ACTIVE node can still be "
                "unreachable over SSH."
            ),
        }
    )


@mcp.tool(
    annotations=RO,
    description=(
        "Full detail for one instance (by name, UUID or any of its IPs): hardware, "
        "network interfaces including VLAN sub-ports, floating IPs and price."
    ),
)
async def server(query: str, refresh: bool = False) -> str:
    node = await _resolve(query, refresh=refresh)
    project = await _project_id()
    base = f"/cloud/v1/instances/{project}/{node['region_id']}/{node['id']}"
    detail, ifaces, enriched, price = await asyncio.gather(
        api("GET", base),
        api("GET", f"{base}/interfaces"),
        _attach_fips([node]),
        api("GET", f"/cloud/v1/price_info/{project}/{node['region_id']}/instances/{node['id']}"),
        return_exceptions=True,
    )
    if isinstance(enriched, list) and enriched:
        node = enriched[0]
    if isinstance(price, dict):
        node = dict(node)
        node["price"] = {
            "per_month": price.get("price_per_month"),
            "per_hour": price.get("price_per_hour"),
            "currency": price.get("currency_code"),
        }
    out: dict[str, Any] = {"summary": node}
    if isinstance(detail, dict):
        out["detail"] = detail
    else:
        out["detail_error"] = str(detail)
    if isinstance(ifaces, dict):
        out["interfaces"] = ifaces.get("results", ifaces)
    else:
        out["interfaces_error"] = str(ifaces)
    return _dump(out)


@mcp.tool(
    annotations=RO,
    description=(
        "A noVNC console URL for one instance — the way in when a node stops "
        "answering over SSH. The link is single-use and expires quickly."
    ),
)
async def console(query: str) -> str:
    node = await _resolve(query)
    project = await _project_id()
    data = await api(
        "GET",
        f"/cloud/v1/instances/{project}/{node['region_id']}/{node['id']}/get_console",
    )
    rc = data.get("remote_console") or data
    return _dump(
        {
            "server": f"{node['name']} ({node['region']}, {', '.join(node['ips'])})",
            "protocol": rc.get("protocol"),
            "type": rc.get("type"),
            "url": rc.get("url"),
            "note": "Hand this URL to the user to open in a browser; the token expires fast.",
        }
    )


@mcp.tool(
    annotations=RO,
    description=(
        "CPU / network / disk metrics for one instance. Available for KVM "
        "instances; EdgeCenter returns nothing for baremetal nodes."
    ),
)
async def metrics(query: str, time_interval: int = 6, time_unit: str = "hour") -> str:
    node = await _resolve(query)
    project = await _project_id()
    data = await api(
        "POST",
        f"/cloud/v1/instances/{project}/{node['region_id']}/{node['id']}/metrics",
        json_body={"time_interval": time_interval, "time_unit": time_unit},
    )
    if isinstance(data, dict) and not data.get("results"):
        return (
            f"No metrics for {node['name']} ({node['kind']}). For baremetal this is "
            "expected — EdgeCenter only collects metrics for KVM instances; use an "
            "in-node exporter instead."
        )
    return _dump(data)


@mcp.tool(
    annotations=RO,
    description=(
        "Cloud tasks — creation, rebuild, power actions and deletions. Shows what "
        "the infrastructure is doing now and how earlier operations ended."
    ),
)
async def tasks(
    limit: int = 20,
    state: str | None = None,
    region_id: int | None = None,
    active_only: bool = False,
    raw: bool = False,
) -> str:
    project = await _project_id()
    params: dict[str, Any] = {"project_id": project, "limit": max(1, min(limit, 200))}
    if region_id is not None:
        params["region_id"] = region_id
    if state:
        params["state"] = state
    data = await api("GET", "/cloud/v1/tasks", params=params)
    results = data.get("results", [])
    if active_only:
        results = [t for t in results if t.get("state") in ("NEW", "RUNNING")]
    if raw:
        return _dump({"count": data.get("count"), "results": results})
    rows = [
        [
            (t.get("id") or "")[:8],
            t.get("task_type") or "-",
            t.get("state") or "-",
            str(t.get("region_id") or "-"),
            (t.get("created_on") or "")[:19],
            (t.get("finished_on") or "-")[:19],
            (t.get("error") or "")[:40],
        ]
        for t in results
    ]
    if not rows:
        return f"No tasks matched (history holds {data.get('count')})."
    table = _table(rows, ["id", "type", "state", "reg", "created", "finished", "error"])
    return f"{table}\n\nshowing {len(rows)} of {data.get('count')}"


@mcp.tool(annotations=RO, description="One cloud task by UUID, with its result or error.")
async def task(task_id: str) -> str:
    return _dump(await api("GET", f"/cloud/v1/tasks/{task_id}"))


@mcp.tool(
    annotations=RO,
    description=(
        "Networking in one region: networks, subnets, floating IPs, security "
        "groups and routers — how the public addresses map to instances."
    ),
)
async def network(region_id: int, raw: bool = False) -> str:
    project = await _project_id()

    async def grab(kind: str) -> Any:
        try:
            return await api("GET", f"/cloud/v1/{kind}/{project}/{region_id}")
        except ECError as exc:
            return {"error": str(exc)[:200]}

    nets, subnets, fips, sgs, routers = await asyncio.gather(
        grab("networks"),
        grab("subnets"),
        grab("floatingips"),
        grab("securitygroups"),
        grab("routers"),
    )
    if raw:
        return _dump(
            {
                "networks": nets,
                "subnets": subnets,
                "floatingips": fips,
                "securitygroups": sgs,
                "routers": routers,
            }
        )
    return _dump(
        {
            "region_id": region_id,
            "networks": [
                {
                    "id": n.get("id"),
                    "name": n.get("name"),
                    "external": n.get("external"),
                    "mtu": n.get("mtu"),
                }
                for n in nets.get("results", [])
            ],
            "subnets": [
                {
                    "id": s.get("id"),
                    "name": s.get("name"),
                    "cidr": s.get("cidr"),
                    "gateway": s.get("gateway_ip"),
                }
                for s in subnets.get("results", [])
            ],
            "floating_ips": [
                {
                    "ip": f.get("floating_ip_address"),
                    "fixed_ip": f.get("fixed_ip_address"),
                    "status": f.get("status"),
                    "port_id": f.get("port_id"),
                }
                for f in fips.get("results", [])
            ],
            "security_groups": [
                {
                    "id": g.get("id"),
                    "name": g.get("name"),
                    "rules": len(g.get("security_group_rules") or []),
                }
                for g in sgs.get("results", [])
            ],
            "routers": [
                {"id": r.get("id"), "name": r.get("name"), "status": r.get("status")}
                for r in routers.get("results", [])
            ],
        }
    )


@mcp.tool(
    annotations=RO,
    description="SSH keypairs registered in the project, optionally for one region.",
)
async def ssh_keys(region_id: int | None = None) -> str:
    project = await _project_id()
    if region_id is not None:
        return _dump(await api("GET", f"/cloud/v1/keypairs/{project}/{region_id}"))

    regs = await _regions()

    async def grab(rid: int) -> tuple[int, Any]:
        try:
            return rid, await api("GET", f"/cloud/v1/keypairs/{project}/{rid}")
        except ECError as exc:
            return rid, {"error": str(exc)[:120]}

    pairs = await asyncio.gather(*(grab(r["id"]) for r in regs))
    return _dump({str(rid): data.get("results", data) for rid, data in pairs})


@mcp.tool(
    annotations=RO,
    description="OS images available in a region, for baremetal or virtual instances.",
)
async def images(region_id: int, kind: Literal["baremetal", "virtual"] = "baremetal") -> str:
    project = await _project_id()
    endpoint = "bmimages" if kind == "baremetal" else "images"
    data = await api("GET", f"/cloud/v1/{endpoint}/{project}/{region_id}")
    rows = [
        [
            i.get("name") or "-",
            (i.get("id") or "")[:8],
            i.get("os_distro") or "-",
            str(i.get("os_version") or "-"),
            i.get("status") or "-",
        ]
        for i in data.get("results", [])
    ]
    if not rows:
        return f"No {kind} images in region {region_id}."
    return _table(rows, ["name", "id", "distro", "version", "status"]) + f"\n\ntotal: {len(rows)}"


@mcp.tool(
    annotations=RO,
    description="Permanent API tokens issued for this account, with last-used dates.",
)
async def api_tokens() -> str:
    client_id = CONFIG.get("client_id")
    if not client_id:
        user = await api("GET", "/iam/users/me")
        client_id = user.get("client")
    data = await api("GET", f"/iam/clients/{client_id}/tokens")
    items = data if isinstance(data, list) else data.get("results", [])
    return _dump(
        [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "description": t.get("description"),
                "exp_date": t.get("exp_date") or "never expires",
                "expired": t.get("expired"),
                "created": t.get("created"),
                "last_usage": t.get("last_usage"),
            }
            for t in items
        ]
    )


@mcp.tool(
    annotations=RW,
    description=(
        "DANGEROUS: instance power control — start / stop / reboot / powercycle / "
        "suspend / resume. Requires confirm=True. powercycle cuts power without a "
        "clean shutdown and risks filesystem damage; use it only on a node that no "
        "longer responds. Returns a task id — follow it with task()."
    ),
)
async def server_action(
    query: str,
    action: Literal["start", "stop", "reboot", "powercycle", "suspend", "resume"],
    confirm: bool = False,
) -> str:
    if READONLY:  # refuse before spending any API call resolving the node
        _guard_mutation(f'{action} on "{query}"', confirm)
    node = await _resolve(query)
    label = INSTANCE_ACTIONS.get(action, action)
    _guard_mutation(
        f'{action} ({label}) on "{node["name"]}" — {node["region"]}, '
        f"{', '.join(node['ips']) or 'no IP'}, currently {node['status']}",
        confirm,
    )
    project = await _project_id()
    data = await api(
        "POST",
        f"/cloud/v1/instances/{project}/{node['region_id']}/{node['id']}/{action}",
        json_body={},
    )
    _cache_invalidate()
    return _dump(
        {
            "requested": action,
            "server": node["name"],
            "region": node["region"],
            "response": data,
            "next": "Track progress with task(<id>) or tasks(active_only=True).",
        }
    )


@mcp.tool(
    annotations=RW,
    description=(
        "Any request against the EdgeCenter API — the escape hatch for endpoints "
        "the named tools do not cover. GET/HEAD/OPTIONS run immediately; "
        "POST/PUT/PATCH/DELETE require confirm=True. A {project} placeholder in the "
        "path is replaced with the project id.\n"
        "Examples: /cloud/v1/volumes/{project}/10, /cloud/v1/servergroups/{project}/10, "
        "/iam/clients/me, /cloud/v1/loadbalancers/{project}/10."
    ),
)
async def api_request(
    path: str,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    confirm: bool = False,
) -> str:
    method = method.upper()
    if method in MUTATING_METHODS:
        _guard_mutation(
            f"{method} {path} body={json.dumps(body, ensure_ascii=False)[:300]}", confirm
        )
    if "{project}" in path or "{project_id}" in path:
        project = str(await _project_id())
        path = path.replace("{project_id}", project).replace("{project}", project)
    data = await api(method, path, params=params, json_body=body)
    if method in MUTATING_METHODS:
        _cache_invalidate()
    return _dump(data)


if __name__ == "__main__":
    mcp.run("stdio")
