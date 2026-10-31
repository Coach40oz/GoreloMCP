# Gated tools: what is blocked and how to turn it on

Some tools can delete data, void an invoice, push an invoice to your accounting system or make a
document readable outside Gorelo. This server ships with those tools switched off. This page lists them, says what each one
does, and shows how to switch on the ones you want.

## How the gate works

A gated tool has to pass three locks before it does anything:

1. **The switch.** `GORELO_ENABLE_DESTRUCTIVE` in `.env` must switch it on. While it is off, the tool is not registered at all:
   Claude does not see it and cannot call it.
2. **Its toolset.** The toolset the tool belongs to must be on in `GORELO_TOOLSETS` (see the table below).
3. **`confirm=true` on every call.** A call without it is refused before anything is sent to Gorelo, and Claude is told to ask
   you first.

Your Gorelo API key must also have the matching scope (a delete needs the delete scope of that area), or Gorelo answers 403.

## The gated tools (15)

| Toolset | Tool | What it does | Can it be undone? |
|---|---|---|---|
| `billing` | `approve_invoice` | Approves a Draft or Submitted invoice. Gorelo pushes it to your connected accounting system at once. | No. It can only be voided (`delete_invoice`). |
| `billing` | `create_approved_invoice` | Creates an invoice that is already Approved, so it is pushed to your accounting system at once; it may email the addresses you give it. | No. It can only be voided. |
| `billing` | `delete_invoice` | Deletes a Draft invoice, or voids an Approved one (it stays listed as Void). | No. |
| `billing` | `delete_item` | Deletes a catalog item. | No. |
| `billing` | `delete_product_charge` | Removes a product charged to a ticket or task. | Charge it again with `create_product_charge`. |
| `core` | `delete_asset_type` | Deletes a custom asset type (Gorelo's own types cannot be deleted). | No. |
| `core` | `delete_client_location` | Deactivates a client location (Gorelo refuses the default, billing or only location, and one still in use). | Not through this server. |
| `core` | `delete_client_domain` | Deactivates a client's web domain, so inbound email from it is no longer matched to the client. | Add it again with `create_client_domain`. |
| `tickets` | `delete_ticket_comment` | Deletes a private ticket comment (public ones are refused). | It can be recovered in the Gorelo app. |
| `time` | `delete_time_entry` | Deletes an open time entry (approved, invoiced and closed-ticket entries are refused). | No. |
| `uptime` | `delete_uptime_check` | Deletes an uptime check, which stops monitoring its target. | No. |
| `projects` | `delete_project_comment` | Deletes a private project or task comment. | It can be recovered in the Gorelo app. |
| `projects` | `delete_project_task` | Deletes a project task. | It can be recovered in the Gorelo app. |
| `documents` | `share_document` | Turns on a document's public link (anyone with the link can read it) or publishes it to the client portal. | Turn it off with `unshare_document`. |
| `documents` | `remove_document_related_item` | Removes a link between a document and another record (neither record is deleted). | Link them again with `add_document_related_items`. |

[tool-catalog.md](tool-catalog.md) has the full text of every tool.

## Held back

`create_contract` and `update_contract` (create and change a billing contract) are held back since 1.5.1. Gorelo has not
released contract writes yet: as of 2026-10-31 it answers every contract create with HTTP 500 (code 070501). Until it does,
those two tools are not registered under any setting, not with `GORELO_ENABLE_DESTRUCTIVE=1` and not when a list names
them. A list that names them is not an error: the server drops the names and logs one warning line before the ready line,
`gorelo-mcp held tools ignored: create_contract, update_contract (held back until Gorelo releases them)`, then starts as
usual. `list_contracts` and `get_contract` are not affected.

## Never available

Whatever you set, this server never calls these operations: deleting a client, a contact, a ticket, an agent asset (that
uninstalls the agent), a custom asset or a contract, and creating or revoking API keys. They are blocked in the code, and the
tests check that no tool can reach them.

## Turn gated tools on

`GORELO_ENABLE_DESTRUCTIVE` takes one of three forms:

| Value | Effect |
|---|---|
| blank or `0` | Off: no gated tool exists (the default). |
| `1` (or `true`, `yes`, `on`) | Every gated tool of the toolsets you enabled. |
| A comma list of tool names | Only those tools. |

A list is the safer choice: switch on only what you need. Examples:

```bash
# Let Claude clean up time entries and private comments only
GORELO_ENABLE_DESTRUCTIVE=delete_time_entry,delete_ticket_comment,delete_project_comment

# Billing: approve and void invoices
GORELO_ENABLE_DESTRUCTIVE=approve_invoice,delete_invoice
```

Then apply the change. The server reads `.env` only when it starts:

```bash
nano /opt/gorelo-mcp/app/.env
systemctl restart gorelo-mcp.service
sleep 5
journalctl -u gorelo-mcp.service --since "1 minute ago" --no-pager | grep -E "gorelo-mcp (gated tools|ready)|refusing"
```

Check the result in either place:

- The `gorelo-mcp gated tools:` line in the journal names the gated tools that are on (`none` when all are off).
- `health_check` in Claude lists them as `gated_tools`.

Start a new conversation in Claude to see the new tool list.

If the list has a mistake, the server does not start. That happens for a name that is not a gated tool, or for a tool whose
toolset `GORELO_TOOLSETS` does not enable (for example `share_document` without `documents`). The `refusing to start` line names
the bad names and lists the valid ones. Fix `.env` and restart.

## Turn them off again

Make `GORELO_ENABLE_DESTRUCTIVE` blank (or `0`) to switch every gated tool off. To switch off only some, take their names out
of the list. Then restart as above.

## Before you switch one on

- **Try it on a test client first.** This server cannot undo a delete or a void.
- **Invoices and your accounting system.** An Approved invoice reaches your accounting system as soon as it is approved.
  Whether a void in Gorelo also voids the accounting copy has varied between tests, so the tool texts tell Claude to warn you
  that the copy may stay open. After a void, check your accounting system.
- **Shared documents.** A public link works for anyone who has it until you unshare the document.
- **Contracts (held back).** `create_contract` and `update_contract` cannot be switched on in 1.5.1 (see "Held back"
  above): Gorelo has not released contract writes yet.
