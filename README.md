# Gorelo MCP server

A self-hosted server that lets Claude (on claude.ai) work with your Gorelo PSA. You run it on a small Linux server of your own. Claude connects to it through a custom connector and gets tools for tickets, clients (including their custom fields), contacts, assets (including custom assets), time entries, invoices and uptime checks, using the Gorelo Public API (`https://api.usw.gorelo.io/v1`, the US region; an account hosted in Australia sets `GORELO_BASE_URL`, see "Regions" below). Projects, forms and documents are available as optional extras: they are off by default and need the `Project`, `Forms` and `Documents` scopes on your Gorelo API key. Other AI clients that sign in over OAuth, such as ChatGPT and Microsoft Copilot Studio, can connect too (see "Connecting other clients"). Your Gorelo API key stays on your server and is sent only to Gorelo's API.

## What you can ask Claude to do

- "Show me all open tickets for Acme that are High priority or above."
- "Create a ticket for Acme: the office printer is offline. Assign it to Dana and set it to Normal priority."
- "Add a private note to ticket 1234 saying I called the client and left a voicemail."
- "Log 45 minutes on ticket 1234 as remote support."
- "Add a custom asset for Acme: the reception printer, serial number ABC123, at the main office."
- "Show the custom fields of Acme, then set its Support plan field to Gold."
- "List the contacts at Acme and tell me which ones have no phone number."
- "Draft an invoice for Acme with two hours of remote support and one monthly backup item."
- "Which of my uptime checks are failing right now?"
- "Find the backup procedure document for Acme and show me its last three versions." (needs the optional `documents` toolset)
- "Run health_check and tell me if the Gorelo connection is fine."

## What's new in 1.3.0

Version 1.3.0 follows Gorelo's API description of 2026-10-17. It adds the optional `documents` toolset (list, read, create, edit, move, archive and restore documents, their versions and related items, and a PDF export), `list_deletions` (a feed of records deleted in Gorelo), `get_custom_asset`, and moving a custom asset to another client. Two settings fix problems reported by a user of the public repository: `GORELO_BASE_URL` for a Gorelo account hosted in Australia, and `MCP_EXTRA_REDIRECT_DOMAINS` so that Microsoft Copilot Studio (and ChatGPT) can sign in. `GORELO_ENABLE_DESTRUCTIVE` can now list just the gated tools you want. Status ids are checked against Gorelo's scales, a custom field that Gorelo hides from the API is handled, and `create_project_comment` no longer takes `body_text`. The documents tools, hidden custom fields and the sign-in of ChatGPT and Copilot Studio have not been tested against a live Gorelo account or the real services yet. Version 1.2.0 added custom fields (read with `get_client` and `get_agent`, set and cleared with `update_client`) and version 1.1.0 added custom assets. See [CHANGELOG.md](CHANGELOG.md).

## What it does not do, and how it stays safe

- The gated tools (deletes, voids, document sharing and approved invoices) are switched off by default. If you switch them on, every call still needs an explicit `confirm=true`. You can switch on only some of them, by name, and switch them off again at any time ([docs/INSTALL.md](docs/INSTALL.md), step 8.2).
- Making a document readable outside Gorelo (a public link, or publishing to the client portal) is also one of the switched-off tools: `share_document`. The tool that edits documents cannot change sharing, and a document body is treated as data, never as instructions.
- Some operations can never be called, even with everything switched on: deleting clients, contacts, tickets, agent assets, custom assets and contracts, and creating or revoking API keys.
- Ticket comments are private by default and email nobody. Claude is told to say who will be emailed before it posts anything public.
- Invoices created by the normal tool are always Drafts. Creating an Approved invoice is one of the switched-off tools.
- Every request is checked against Gorelo's published API description before it is sent, so a mistyped field is refused locally.
- Sign-in is built in (OAuth 2.1) and protected by one password that you choose. There is no outside login service to trust. The server listens only on the local machine, and you publish it through a Cloudflare Tunnel, so no inbound port is open.
- Anyone who has the password can use every tool you enabled, with the permissions of your one Gorelo API key. Give the key only the scopes you need.

## Use at your own risk

This server gives an AI model access to your live Gorelo account with the permissions of your API key. The read tools show your client data to the model, and the write tools create and change records. AI models make mistakes: read what the model is about to do before you confirm a change, and try new tools on a test client first. Switch on only the toolsets and gated tools you need. This server cannot undo a delete or a void.

You run this software at your own risk. It is provided "as is", without warranty of any kind (see [LICENSE](LICENSE)), and you are responsible for how it is used with your data.

## Requirements

- A Gorelo account and an API key (see [docs/INSTALL.md](docs/INSTALL.md) for the scopes). Set `GORELO_BASE_URL` to the host of the region your account is hosted in (blank is the US host; Australia: `https://api.aue.gorelo.io`).
- A Debian 13 server (a VM, an LXC container or a cloud VPS) with root access and outbound internet. No open inbound ports are needed.
- A domain on Cloudflare (free plan is fine) for the tunnel.
- A claude.ai plan that allows custom connectors.

## Quick start

1. Get a Debian 13 server and a Gorelo API key.
2. Prepare the server and install the code: steps 1 and 2 of [docs/INSTALL.md](docs/INSTALL.md).
3. Fill in `.env` and start the service: steps 3 and 4.
4. Publish it with a Cloudflare Tunnel: step 5.
5. In claude.ai add the custom connector `https://your-host/mcp` and run `health_check`: step 6.

## Toolsets

`GORELO_TOOLSETS` chooses which groups of tools exist. The default is `core,tickets,time,billing,uptime`; `all` turns every toolset on.

| Toolset | In the default set | Covers |
|---|---|---|
| `core` | yes | clients and their locations and custom fields, contacts, agent assets and custom assets (get, list, create, update, move), the feed of deleted records, groups and users, alerts, `health_check` |
| `tickets` | yes | tickets, comments, side conversations, approvals, attachments (`upload_attachment`) |
| `time` | yes | time entries, billing roles, work types |
| `billing` | yes | invoices (Drafts), items, categories, taxes, contracts |
| `uptime` | yes | uptime checks and maintenance windows |
| `projects` | no | projects, sections, tasks, comments and approvals; needs the `Project` scope |
| `forms` | no | forms, responses, submission links; needs the `Forms` scope |
| `documents` | no | documents: list, read, create, update, move, archive, restore, duplicate, versions, related items and a PDF export; needs the `Documents` scope. `share_document` and `remove_document_related_item` are gated |

[docs/tool-catalog.md](docs/tool-catalog.md) lists every tool. The delete and void tools, `create_approved_invoice`, `share_document` and `remove_document_related_item` are the gated tools: they do not exist unless `GORELO_ENABLE_DESTRUCTIVE` switches them on (`1` for all of them, or a comma list of tool names for just those, such as `delete_time_entry,delete_item`), and every call still needs `confirm=true`. They are off by default; [docs/INSTALL.md](docs/INSTALL.md), step 8.2, shows how to turn them on and back off.

## Settings you may need

The full table is in [docs/INSTALL.md](docs/INSTALL.md), step 3. Besides the API key, the public address and the password:

| Variable | What it does |
|---|---|
| `GORELO_TOOLSETS` | Which toolsets exist. Blank is the default set above. A non-blank GORELO_TOOLSETS replaces the default set: list every toolset you want, for example `core,tickets,time,billing,uptime,documents`. Add `projects`, `forms` or `documents` only when your key has that scope. |
| `GORELO_ENABLE_DESTRUCTIVE` | Blank is off. `1`, or a comma list of gated tool names, switches the gated tools on. |
| `GORELO_BASE_URL` | The Gorelo API address. Blank is the US host (`https://api.usw.gorelo.io/v1`). For an account hosted in Australia use `https://api.aue.gorelo.io`. |
| `MCP_EXTRA_REDIRECT_DOMAINS` | Extra domains an AI client may be sent back to after it signs in. Blank is `claude.ai`, `claude.com` and `localhost` only. See below. |

## Regions

Your Gorelo account is hosted in one region. Set `GORELO_BASE_URL` to the host of that region, in `.env` and, if you use the daily watcher, in its own environment file. After you start the service, read the start line in the journal, `gorelo-mcp settings: api_host=<host>`: the host it names is the one the server calls, and it must be the host of your region. [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) has the steps if a key is rejected or that host is not the host of your region.

## Connecting other clients

claude.ai needs nothing extra. Another AI client can use the same server if its sign-in redirect is allowed: add the domain it is sent back to in `MCP_EXTRA_REDIRECT_DOMAINS` (a comma list), restart the service, and check the start line `gorelo-mcp settings: ... extra_redirect_domains=...` in the journal.

| Client | Add to `MCP_EXTRA_REDIRECT_DOMAINS` | Steps |
|---|---|---|
| ChatGPT (custom connector in Developer Mode) | `chatgpt.com` | [docs/INSTALL.md](docs/INSTALL.md), step 6.1 |
| Microsoft Copilot Studio | `global.consent.azure-apim.net` (use the host that the journal names if yours differs) | [docs/INSTALL.md](docs/INSTALL.md), step 6.2 |

Every client signs in with the same password and then acts with the same Gorelo API key and the same tools, so connect only clients you trust. Each domain you add receives sign-in codes: add the most specific host, never a parent domain (see [SECURITY.md](SECURITY.md)). Removing a domain later stops new sign-ins from that client only: a client that is already connected keeps working until you sign out every session ([docs/OPERATIONS.md](docs/OPERATIONS.md), "Sign out all Claude sessions"). These two clients are checked by offline tests that replay their documented sign-in flows; they have not been tried against the real services yet.

## Documentation

| File | What it is for |
|---|---|
| [CHANGELOG.md](CHANGELOG.md) | What changed in each release. |
| [docs/INSTALL.md](docs/INSTALL.md) | Step-by-step install from a blank Debian 13 server. |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Day-to-day tasks: health, logs, rotating secrets, backups. |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Symptom, cause and fix. |
| [docs/OVERVIEW.md](docs/OVERVIEW.md) | How the server works inside (for the curious and for developers). |
| [docs/tool-catalog.md](docs/tool-catalog.md) | Every tool, the Gorelo operation it uses and its required parameters. |
| [SECURITY.md](SECURITY.md) | How to report a security problem, and the threat model in short. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to propose changes and run the offline tests. |
| [docs/API-OBSERVED-BEHAVIOR.md](docs/API-OBSERVED-BEHAVIOR.md) | Where the live Gorelo API differs from its published spec. |
| [docs/why/](docs/why/) | Why the design is the way it is. |

## License

MIT, see [LICENSE](LICENSE).

## Credits

Created by Ulises Paiz.

This project is not affiliated with, endorsed by or supported by Gorelo or Anthropic. "Gorelo" and "Claude" belong to their owners.
