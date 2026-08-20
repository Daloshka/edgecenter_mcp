# Changelog

## 1.1.0

- New optional tool `dedicated_servers()`: inventory from DCImanager
  (nx-dci.edgecenter.ru), the separate ISPsystem panel for bare dedicated
  hardware that the Cloud API cannot see. Needs its own credentials
  (`dci_username`/`dci_password` or `DCIMANAGER_USERNAME`/
  `DCIMANAGER_PASSWORD`); returns an explanatory message instead of an error
  when unconfigured, so every existing tool is unaffected.
- Session-based auth against the panel (`func=auth`), 5-minute inventory
  cache — hardware inventory changes slowly.
- `whoami()` now reports `dcimanager_configured`.

## 1.0.0

First public release.

- 14 read-only tools: `whoami`, `regions`, `servers`, `costs`, `fleet_health`,
  `server`, `console`, `metrics`, `tasks`, `task`, `network`, `ssh_keys`,
  `images`, `api_tokens`.
- 2 gated tools: `server_action` for instance power control and `api_request`
  as a generic escape hatch. Both require `confirm=True`, and both are refused
  outright under `EDGECENTER_MCP_READONLY=1`.
- Fleet inventory is fetched from every region in parallel and cached briefly;
  a node resolves by name, UUID prefix, or any address it owns — public,
  floating or private.
- Instance prices come from `/cloud/v1/price_info`, which is the only source of
  baremetal pricing in this API.
- Authentication scheme is detected from the token itself: `APIKey` for a
  permanent token, `Bearer` for a browser JWT.
- Configuration from `~/.config/edgecenter_mcp/config.json`, overridable by
  environment variables; the older `edgecenter-mcp` directory is still read.
