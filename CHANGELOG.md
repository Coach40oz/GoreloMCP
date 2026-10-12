# Changelog

All notable changes to this project are listed here, newest first, in the plain style of Keep a Changelog.

## 1.1.0 (2026-10-12)

Follows the Gorelo Public API description of 2026-10-12 (103 operations).

### Added

- Custom assets: `list_custom_asset_hardware_types`, `create_custom_asset` (name, client, location
  and hardware type; optional description, contact, warranty end date, IPv4 address and serial
  number) and `update_custom_asset` (partial update; `clear_fields` clears description, contact,
  warranty date, IP address or serial number). They are in the `core` toolset.
- The registry now holds 92 tools (84 regular, 8 gated). The default toolsets give 61 tools, 67 with
  `GORELO_ENABLE_DESTRUCTIVE`.
- An optional live test area for custom assets in the test harness.

### Changed

- `create_ticket` refuses `send_created_email=true` or `is_unread=true` together with `created_on`
  or `closed_on`, because Gorelo ignores both on a backdated record.
- Tool texts say that backdated tickets, comments, projects and tasks send no emails or
  notifications, and that contact lists can include temporary contacts (no client, status 3
  Temporary).
- `scripts/spec_diff.py` also lists changes in descriptions.
- An API key that is allowed to write custom assets needs the `Assets` write scope.

### Security

- `DELETE /v1/api-keys/{apiKeyId}` can never be called, joining `POST /v1/api-keys`: the server
  neither creates nor revokes API keys. Listing API keys is not exposed either.

### Notes

- Custom assets cannot be deleted through the server, and Gorelo has no read of a single custom
  asset, so writes are read back through the list. A PATCH cannot move a custom asset to another
  client (Gorelo answers 400).
- Gorelo now rejects fictional phone numbers in area code 555 (for example 5555550142 with region
  US) as "Phone number is not valid for the specified country code". Use a real area code.
- Gorelo's release notes of 2026-10-09 say creating payments is not available while Xero or
  QuickBooks is connected. The payments operations stay unexposed.

## 1.0.0 (2026-10-08)

First public release.

- Tools for tickets (comments, side conversations, approvals, attachments), clients, locations,
  contacts, agent assets, time entries, invoices, items, taxes, contracts, alerts and uptime checks,
  plus optional projects and forms toolsets.
- Delete, void and approved-invoice tools are off by default and always need `confirm=true`.
- Built-in OAuth 2.1 sign-in with one password you choose; no outside login service.
- A daily watcher that checks Gorelo's changelog and API description for changes.
- Hardened systemd units and a Cloudflare Tunnel setup, so no inbound port is open.
- Guides for install, operations and troubleshooting, a tool catalog and design notes.
