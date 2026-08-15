# edgecenter_mcp

An MCP server for the [EdgeCenter](https://edgecenter.ru) Cloud API. It gives an
MCP client — Claude Code, Claude Desktop, or anything else speaking the protocol —
read access to your EdgeCenter account and gated control over instance power.

Built for people running a fleet: one call lists every baremetal and virtual
instance across all regions with addresses, status and monthly price; another
hands you a VNC console link for a node that stopped answering over SSH.

*[Русская версия](README.ru.md)*

## What you get

- **Fleet inventory in one call** — every instance in every region, with public,
  floating and private addresses resolved and mapped to each other.
- **Rental costs** — per instance and totalled by region, straight from the API.
- **A way back into a dead node** — noVNC console URLs without opening the panel.
- **Cloud task visibility** — what the infrastructure is doing and how it ended.
- **An escape hatch** — `api_request` reaches any endpoint the named tools miss.
- **Guardrails** — power actions demand explicit confirmation, and a read-only
  mode blocks every mutation outright.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (the script declares its own dependencies via
  PEP 723 — no virtualenv to manage)
- An EdgeCenter account with a permanent API token

## Quick start

```bash
git clone https://github.com/Daloshka/edgecenter_mcp.git ~/Tools/edgecenter_mcp

mkdir -p ~/.config/edgecenter_mcp
cp ~/Tools/edgecenter_mcp/config.example.json ~/.config/edgecenter_mcp/config.json
$EDITOR ~/.config/edgecenter_mcp/config.json     # paste your API token
chmod 600 ~/.config/edgecenter_mcp/config.json

# register with Claude Code
claude mcp add edgecenter_mcp -- uv run --script ~/Tools/edgecenter_mcp/server.py
claude mcp get edgecenter_mcp                    # should report: Connected
```

For Claude Desktop, add this to `claude_desktop_config.json` instead:

```json
{
  "mcpServers": {
    "edgecenter_mcp": {
      "command": "uv",
      "args": ["run", "--script", "/absolute/path/to/edgecenter_mcp/server.py"]
    }
  }
}
```

Use an absolute path to `uv` (`which uv`) if your client starts with a minimal
`PATH`.

## Getting an API token

**From the control panel.** Log in to [EdgeCenter](https://edgecenter.ru), open
your profile → **API tokens** → create one. Choose no expiry if you want the
server to keep working unattended.

**Or over the API,** using a browser JWT from DevTools as bootstrap:

```bash
curl -X POST "https://api.edgecenter.ru/iam/clients/<CLIENT_ID>/tokens" \
  -H "Authorization: Bearer <JWT_FROM_DEVTOOLS>" \
  -H "Content-Type: application/json" \
  -d '{"name":"mcp","description":"MCP server","exp_date":null,
       "client_user":{"role":{"id":1,"name":"Administrators"}}}'
```

`exp_date: null` means the token never expires. List your tokens with the
`api_tokens()` tool, and revoke one with
`DELETE /iam/clients/<CLIENT_ID>/tokens/<TOKEN_ID>`.

The server picks the right authentication scheme on its own: a permanent token
goes out as `Authorization: APIKey <token>`, a browser JWT as
`Authorization: Bearer <token>`. Sending a permanent token as `Bearer` fails
with *"jwt decode failed"* — a common hour-waster.

> **Careful with `$` in shell.** EdgeCenter tokens look like `12345$abcdef…`.
> Inside double quotes your shell eats everything after `$` as a variable name,
> so `curl -H "Authorization: APIKey $TOKEN_LITERAL"` silently sends a truncated
> token and you get `401` on everything. Use single quotes, or read the token
> from the config file.

## Configuration

Environment variables win over the config file. The file is looked up at
`$EDGECENTER_MCP_CONFIG`, then `~/.config/edgecenter_mcp/config.json`.

| Config key | Environment | Meaning |
|---|---|---|
| `api_token` | `EDGECENTER_API_TOKEN` | permanent API token or browser JWT — required |
| `base_url` | `EDGECENTER_BASE_URL` | defaults to `https://api.edgecenter.ru` |
| `project_id` | `EDGECENTER_PROJECT_ID` | optional; with several projects the first one is used, so set this to pick another (`whoami()` lists them) |
| `client_id` | `EDGECENTER_CLIENT_ID` | optional; read from `/iam/users/me` when absent |
| `readonly` | `EDGECENTER_MCP_READONLY=1` | refuse every mutating call |

## Tools

Read-only — safe to call at any time:

| Tool | What it does |
|---|---|
| `whoami()` | account, client, enabled services, projects, regions |
| `regions()` | regions with baremetal/KVM capability flags |
| `servers()` | fleet inventory; `with_floating_ips=True` resolves floating and private addresses |
| `costs()` | price per instance and fleet total, grouped by region |
| `fleet_health()` | anything not ACTIVE, nodes stuck in a task state, running tasks |
| `server(query)` | full detail for one node: hardware, interfaces, VLAN sub-ports, price |
| `console(query)` | single-use noVNC console URL |
| `metrics(query)` | CPU/network/disk series (KVM only — baremetal returns nothing) |
| `tasks()` / `task(id)` | cloud task history and one task's outcome |
| `network(region_id)` | networks, subnets, floating IPs, security groups, routers |
| `ssh_keys()` | keypairs, per region or across all of them |
| `images(region_id)` | available OS images, baremetal or virtual |
| `api_tokens()` | issued API tokens and when each was last used |

Mutating — refused unless `confirm=True`, and always refused in read-only mode:

| Tool | What it does |
|---|---|
| `server_action(query, action, confirm)` | `start`, `stop`, `reboot`, `powercycle`, `suspend`, `resume` |
| `api_request(path, method, params, body, confirm)` | any endpoint; `{project}` in the path is substituted |

Every tool that takes a `query` accepts a node's name, its UUID (or a prefix of
it), or any address it owns — public, floating or private. Matching ignores case.

Two parameters recur across tools: `refresh=True` bypasses the cache (the
inventory is held for 60 seconds, the region list for an hour), and `raw=True`
returns unformatted JSON instead of a table — useful when you want to process
the output rather than read it.

## Safety model

- Read-only tools carry `readOnlyHint`, mutating ones `destructiveHint`, so
  clients can present them differently.
- `server_action` and mutating `api_request` calls fail with an explanatory
  error unless `confirm=True` is passed, which keeps a model from rebooting
  production on its own initiative.
- `EDGECENTER_MCP_READONLY=1` refuses mutations even with `confirm=True` —
  worth setting for anything unattended.
- `powercycle` cuts power without a clean shutdown. It exists for nodes that no
  longer respond; the tool description says so, and it is not the default.

## API map

Verified against a live account. Lists come back as `{count, results}`.

```
GET  /iam/users/me · /iam/clients/me · /iam/clients/{client}/tokens
GET  /cloud/v1/projects · /cloud/v1/regions
GET  /cloud/v1/bminstances/{project}/{region}      # baremetal, fully populated
GET  /cloud/v1/instances/{project}/{region}        # KVM instances only
GET  /cloud/v1/instances/{project}/{region}/{id}   # works for baremetal too
GET  …/{id}/interfaces · …/{id}/ports · …/{id}/get_console
GET  /cloud/v1/price_info/{project}/{region}/instances/{id}
POST …/{id}/metrics            {"time_interval": 6, "time_unit": "hour"}
POST …/{id}/{action}           start|stop|reboot|powercycle|suspend|resume
POST …/{id}/attach_interface · …/{id}/detach_interface · …/{id}/put_into_servergroup
GET  /cloud/v1/tasks?project_id=…&region_id=…&limit=… · /cloud/v1/tasks/{id}
GET  /cloud/v1/{networks|subnets|routers|floatingips|securitygroups|keypairs
      |servergroups|volumes|snapshots|images|bmimages|flavors|bmflavors
      |loadbalancers}/{project}/{region}
```

Routes that do **not** exist, despite appearing in documentation or being the
obvious guess:

```
POST /cloud/v1/instances/{p}/{r}/{id}/action        # no "action in the body" scheme
POST /cloud/v1/bminstances/{p}/{r}/{id}/reboot      # bminstances has no actions at all
     …/reboot_hard · …/power-cycle · …/pause · …/rebuild · …/rescue · …/resize
GET  /cloud/v1/quotas* · /cloud/v1/client_quotas/*  # no quota endpoints
GET  /billing/v1/* · /iam/clients/{id}/{balance,invoices}
GET  /cloud/v1/price_info/{p}/{r}/{floatingips|volumes|networks}/{id}
```

Quotas, balance, invoices and discounts are panel-only. `price_info` returns a
list price for an instance, not an invoice.

### Gotchas worth knowing

- **Baremetal is invisible in the `/instances` listing** — it returns `count: 0`
  while `/bminstances` holds the nodes. Yet fetching a single instance and every
  power action go through `/instances/{project}/{region}/{id}` with the same id.
- **Probe a route without triggering it**: POST to a nonexistent UUID. An empty
  `404` means the route does not exist; a JSON `{"exception_class":
  "NotFoundError"}` means it does and only the instance was missing. The action
  list above was mapped this way without rebooting anything.
- **A node can have two public addresses** — the one in its instance record, and
  a floating IP attached to a private VLAN sub-port address. `servers()` shows
  the first; `servers(with_floating_ips=True)` correlates both.
- **`OPTIONS` on `/cloud/*` always answers `204`** with no schema, so it tells
  you nothing about whether a route exists. Under `/iam/*` it returns a full
  field schema.
- **`bmflavors?include_prices=true` returns `null` prices** for baremetal, even
  though virtual `g1-*` flavors carry real ones. Per-instance `price_info` is
  the only source of baremetal pricing.
- **Baremetal metrics are always empty** — EdgeCenter collects them for KVM only.
- **`status: ACTIVE` is the orchestrator's opinion.** A node can be ACTIVE and
  still be unreachable; use it as a hint, not a health check.

## Development

```bash
uv run --script server.py            # start the server on stdio (Ctrl-C to stop)
uv run --script examples/smoke_test.py   # list tools and exercise the read-only ones
uvx ruff check .                     # lint
```

`examples/smoke_test.py` talks to your real account over read-only calls, and
checks that mutating tools refuse to run without confirmation.

## Compatibility

EdgeCenter's API descends from the same codebase as Gcore's, so most paths match
there too. Point `base_url` at another host to try it — everything else is
discovered at runtime.

## License

MIT — see [LICENSE](LICENSE).
