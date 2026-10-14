# Changelog

All notable changes to this project are listed here, newest first, in the plain style of Keep a Changelog.

## 1.2.0 (2026-10-14)

Follows the Gorelo Public API description of 2026-10-14 (103 operations, contract 1b1c0cec0e9a), which adds custom fields to
clients and devices.

### Added

- Custom fields on clients. `update_client` takes `custom_properties`, a list of `{"id": ..., "value": ...}`, to set custom
  fields, and `clear_custom_property_ids` to clear them. A field is named by its id, which comes from `CustomProperties` in
  `get_client`. A value is text whatever the type of the field, and it cannot be empty: the tool cannot save an empty value, and
  an entry without a value is refused with a message that points to clearing. Clearing a field removes its saved value, so the
  field shows its default again (which may itself be empty), and a required field cannot be cleared to nothing. An id may appear
  only once, and the fields you do not name keep their values.
  Because Gorelo's rules for this update are published but have not been tested live, the tool guards it. It reads the client
  before the write and again after it, and refuses an id that the client does not have, so such an id is never sent to Gorelo;
  if the first read is unusable, nothing is written. Once Gorelo has accepted the write, the second read and its checks always
  run, whatever the write's own answer looks like, and that second read is also the record the tool returns: every field you
  sent must show (a saved field as set, with a text value, and a cleared one as not set) and every other custom field of the
  client must be unchanged; otherwise the tool reports the write as unconfirmed. A field that changed without being sent is
  listed with its previous state. If the second read itself fails or cannot be used, the report lists the state of every custom
  field from before the write, so that nothing is lost for good if Gorelo replaced the whole list; so does a write that times
  out, loses its connection or gets a server error, because it may have been applied (a refusal stays a plain refusal). Claude
  is told to compare that list with the client, to tell you and to ask before putting anything back, and the error spells out
  one call that puts the fields back together with what the update meant to set. Those previous values are shown to Claude but
  kept out of the server's log. The tool does not compare the values you sent, because Gorelo shows display text and may
  reformat a number or a date; so Claude is told to compare each returned value with the one it sent and to tell you about a
  difference. When Gorelo refuses a custom field entry, the error names the parameter, the item and the custom field id,
  followed by Gorelo's own name for the problem.
- `get_client` and `get_agent` return `CustomProperties`: every custom field defined for clients, or for devices, with its id,
  name, type and value, and `IsSet`, which says whether a value is saved on that record (when it is not, the value shown is the
  default of the field, if it has one).
- An optional live test area for custom fields in the test harness (`--with-custom-properties`). It is off by default and works
  on the test client only. It runs only while no custom field of that client holds a value, saves a value in one unset text
  custom field, reads it back, clears it and reads it back again, and after every step compares all custom fields with its
  first read. It also checks that the tool refuses an id that is not a client custom field before it changes anything. If a run
  stops half way, the cleanup clears the field, but only while it still shows the run's own text and no other custom field of
  the client holds a value; otherwise it sends nothing and reports the field as left over. Other custom fields that read
  differently after the clear are reported first, by their ids, with the advice to check them in the Gorelo app. Because every
  field is unset while the area runs, it cannot tell whether Gorelo updates only the field it is given or replaces the whole
  list.

### Changed

- The texts of `list_clients` and `list_agents` say that their rows leave custom fields out; `get_client` and `get_agent` have them.
- The server is checked against the API description of 2026-10-14 (contract 1b1c0cec0e9a, 288 schemas).
- Project and task status ids are checked against their own scales before anything is sent (a task status is 1 to 4, a project
  status 1 to 5), on writes and in the status filters of the lists, so a project status such as Closed can no longer reach a
  task. The message gives the scale.
- Version 1.2.0. No tool was added: the registry still holds 92 tools (61 with the default toolsets).

### Fixed

- `create_task_approval` no longer says that creating a task approval emails nobody. Gorelo documents that only for ticket
  approvals, so Claude now tells the user that the approvers may be notified before it creates one.
- In the test harness, the dry listing of the `projects` area (`--only projects`) was one request short. It now shows the request
  for the client locations that the area makes.

### Notes

- Custom fields follow Gorelo's published description only and have not been tested against the live API yet. Not known yet:
  whether Gorelo applies the custom field list as the description says (an entry changes only its own field), how it checks a
  value for each field type, which value formats it accepts, and which name it gives to a custom field error. What it answers
  for an id that is not a client custom field does not matter to the tool, which never sends one. That is why the tool reads
  the client before and after it writes, checks every custom field on the second read, and confirms a write from the `IsSet`
  flag and the text value that read shows, without comparing values itself. The optional live test area does not settle the
  first point either: it runs only while no custom field holds a value, so an update that replaced the list would look the
  same in it as one that did not.
- Custom fields can be set only on a client that exists: Gorelo's create-client call has none. There is no call to update a
  device, so the custom fields of a device can be read but not changed.
- Gorelo's description of `POST /v1/projects` no longer mentions `GroupIds`. `create_project` sends only `GroupId`, so nothing
  changed for it.

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
