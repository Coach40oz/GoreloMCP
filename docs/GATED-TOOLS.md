# Gated tools: what is blocked and how to turn it on

Some tools can delete data, void an invoice, push an invoice to your accounting system, change a billing contract or make a
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

## The gated tools (18)

| Toolset | Tool | What it does | Can it be undone? |
|---|---|---|---|
| `billing` | `approve_invoice` | Approves a Draft or Submitted invoice. Gorelo pushes it to your connected accounting system at once, and may email the invoice to the client's contacts. | No. It can only be voided (`delete_invoice`), and a void may not reach the accounting system: check it there. |
| `billing` | `create_approved_invoice` | Creates an invoice that is already Approved, so it is pushed to your accounting system at once; it emails the addresses you give it, and Gorelo may email the client's contacts too. | No. It can only be voided, and a void may not reach the accounting system: check it there. |
| `billing` | `delete_invoice` | Deletes a Draft invoice, or voids an Approved one (it stays listed as Void). Gorelo says a void is sent to the accounting system too: check it there. | No. |
| `billing` | `create_contract` | Creates a billing contract. A Block Hours line raises a Draft invoice at once; `auto_approve_and_send` makes Gorelo approve and email the recurring invoices on its own. | Archive it with `update_contract` when that tool is enabled, else in the Gorelo app; this server cannot delete contracts. |
| `billing` | `update_contract` | Changes a contract, removes service lines or line items, archives or restores it. A new or converted Block Hours line raises a Draft invoice at once; `auto_approve_and_send=true` makes Gorelo approve and email the recurring invoices on its own and push them to your accounting system. | Partly: archive and restore are reversible, removed lines are not, and an approved invoice that was pushed to the accounting system cannot be taken back. |
| `billing` | `delete_item` | Deletes a catalog item. | No. |
| `billing` | `delete_product_charge` | Removes a product charged to a ticket or task. | Charge it again with `create_product_charge`. |
| `core` | `delete_asset_type` | Deletes a custom asset type (Gorelo's own types cannot be deleted). | No. |
| `core` | `delete_contact` | Deletes a contact. Gorelo marks it inactive and keeps its ticket history, and it leaves every list at once (Gorelo refuses a contact that is the primary or a secondary contact of a ticket that is not closed). | Not through this server. |
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

## Never available

Whatever you set, this server never calls these operations: deleting a client, a ticket, an agent asset (that
uninstalls the agent), a custom asset or a contract, and creating or revoking API keys. (Deleting a contact is possible
since 1.5.2, but only through the gated `delete_contact` above.) They are blocked in the code, and the
tests check that no tool can reach them.

## Turn gated tools on

`GORELO_ENABLE_DESTRUCTIVE` takes one of three forms:

| Value | Effect |
|---|---|
| blank or `0` | Off: no gated tool exists (the default). |
| `1` (or `true`, `yes`, `on`) | Every gated tool of the toolsets you enabled. |
| A comma list of tool names | Only those tools. |

A list is the safer choice: switch on only what you need. A name must belong to a toolset that is on (`delete_project_comment`
needs `projects` in `GORELO_TOOLSETS`, which the default set leaves out). Examples:

```bash
# Let Claude clean up time entries and private ticket comments only
GORELO_ENABLE_DESTRUCTIVE=delete_time_entry,delete_ticket_comment

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
- **Invoices and your accounting system.** An Approved invoice reaches your accounting system as soon as it is approved, and
  Gorelo may email it to the client's contacts on its own. Gorelo's description says a void is sent to the accounting
  system too. A void has reached it in one test, and in another the copy was still open a few minutes later, so the tool
  texts tell Claude to ask you to check the accounting system. After a void, check your accounting system.
- **Shared documents.** A public link works for anyone who has it until you unshare the document.
- **Deleting a contact.** `delete_contact` makes the contact leave every list at once (Gorelo marks it inactive and keeps its
  ticket history), and this server cannot bring it back. Claude is told to name the contact and ask you before
  it sets `confirm=true`. A 409 means the contact is still the primary or a secondary contact of a ticket that is not closed:
  reassign it on those tickets or close them, then try again. Gorelo answers the same "deleted" for a contact that is already gone, so the tool reads the contact
  first and reports what it deleted.
- **Contracts.** Both tools were tried against Gorelo on 2026-11-03: a contract was created with and without dates, read back and
  archived with `update_contract`. A contract cannot be deleted through this server, so archive one you no longer want
  (with `update_contract` when it is enabled, else in the Gorelo app). If you leave out `start_date`, Gorelo sets today in
  your account's time zone, which can differ from the UTC date by a day in either direction. A line item's `start_date`
  defaults to that day, or to the contract's start if that is later. `auto_approve_and_send` makes Gorelo approve and
  email the recurring invoices without asking again.
