# Gorelo MCP server

A self-hosted server that lets Claude (on claude.ai) work with your Gorelo PSA. You run it on a small Linux server of your own. Claude connects to it through a custom connector and gets tools for tickets, clients, contacts, assets, time entries, invoices and uptime checks, using the Gorelo Public API (`https://api.usw.gorelo.io/v1`). Projects and forms are available as optional extras. Your Gorelo API key stays on your server and is sent only to Gorelo's API.

## What you can ask Claude to do

- "Show me all open tickets for Acme that are High priority or above."
- "Create a ticket for Acme: the office printer is offline. Assign it to Dana and set it to Normal priority."
- "Add a private note to ticket 1234 saying I called the client and left a voicemail."
- "Log 45 minutes on ticket 1234 as remote support."
- "List the contacts at Acme and tell me which ones have no phone number."
- "Draft an invoice for Acme with two hours of remote support and one monthly backup item."
- "Which of my uptime checks are failing right now?"
- "Run health_check and tell me if the Gorelo connection is fine."

## What it does not do, and how it stays safe

- Delete and void tools are switched off by default. If you switch them on, every call still needs an explicit `confirm=true`.
- Some operations can never be called, even with everything switched on: deleting clients, contacts, tickets, agent assets, custom assets and contracts, and creating API keys.
- Ticket comments are private by default and email nobody. Claude is told to say who will be emailed before it posts anything public.
- Invoices created by the normal tool are always Drafts. Creating an Approved invoice is one of the switched-off tools.
- Every request is checked against Gorelo's published API description before it is sent, so a mistyped field is refused locally.
- Sign-in is built in (OAuth 2.1) and protected by one password that you choose. There is no outside login service to trust. The server listens only on the local machine, and you publish it through a Cloudflare Tunnel, so no inbound port is open.
- Anyone who has the password can use every tool you enabled, with the permissions of your one Gorelo API key. Give the key only the scopes you need.

## Requirements

- A Gorelo account and an API key (see [docs/INSTALL.md](docs/INSTALL.md) for the scopes).
- A Debian 13 server (a VM, an LXC container or a cloud VPS) with root access and outbound internet. No open inbound ports are needed.
- A domain on Cloudflare (free plan is fine) for the tunnel.
- A claude.ai plan that allows custom connectors.

## Quick start

1. Get a Debian 13 server and a Gorelo API key.
2. Prepare the server and install the code: steps 1 and 2 of [docs/INSTALL.md](docs/INSTALL.md).
3. Fill in `.env` and start the service: steps 3 and 4.
4. Publish it with a Cloudflare Tunnel: step 5.
5. In claude.ai add the custom connector `https://your-host/mcp` and run `health_check`: step 6.

## Documentation

| File | What it is for |
|---|---|
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
