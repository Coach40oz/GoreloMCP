# Security policy

## Supported versions

Only the latest tagged release is supported. Fixes are made on the main branch and released as a
new tag; older tags do not get backports. If you run an older tag, update before reporting
anything that a newer tag may already have fixed.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub private vulnerability
reporting on this repository: open the Security tab and choose "Report a vulnerability". Include
what you found, how to reproduce it, the tag or commit, and what an attacker gains. Never include
real credentials, tokens or client data. You will get an acknowledgement as soon as the author can
look at it; this is a small project, so there is no guaranteed response time.

## Threat model in short

What the internet can reach. The server listens only on 127.0.0.1. The only public path is the
tunnel you set up, and through it an attacker can reach `/mcp` and the OAuth pages (`/authorize`,
`/token`, `/register`, `/revoke` and the OAuth metadata). Nothing else is exposed. `/mcp` needs a
valid access token.

What the password protects. The consent page asks for one password (`MCP_AUTH_PASSWORD`). Whoever
knows it can sign in an AI client and then call every tool that is registered, with the full power
of the Gorelo key. There are no per-user permissions. Guessing is limited (5 wrong passwords per
client address in 15 minutes, 30 an hour overall, then HTTP 429), registration is rate limited and
capped, redirects are limited to claude.ai, claude.com and localhost (plus any domain you add
with `MCP_EXTRA_REDIRECT_DOMAINS`, see "The redirect allowlist" below), and the server refuses to
start without a password. Use a long random password (16 characters or more; the server warns
below that). See `docs/why/05-built-in-oauth-one-password.txt`.

What the Gorelo key can do. Every tool call uses the one Gorelo API key in `.env`. So the limits
on what a signed-in client can do are the scopes of that key, the toolsets you enable
(`GORELO_TOOLSETS`; projects, forms and documents are off by default) and whether you enabled the
gated tools (`GORELO_ENABLE_DESTRUCTIVE`, off by default, and each call also needs `confirm=true`):
the delete and void tools, `create_approved_invoice`, and `share_document`, which makes a document
readable outside Gorelo. You can enable them one by one by listing their names. Scope the key
minimally: give it only the areas you use, and do not grant Project, Forms or Documents unless you
enable those toolsets. Some operations can never be called whatever the key allows (client,
contact, ticket, asset and contract deletes, and creating or revoking API keys (`POST /v1/api-keys`
and `DELETE /v1/api-keys/{apiKeyId}`); see `docs/why/03-forbidden-and-gated-operations.txt`).

Where the key goes. The key is sent only to the Gorelo host named by `GORELO_BASE_URL` (the US host
when it is blank). The setting must be https on `gorelo.io` or a subdomain of it, with no user name,
port, query or fragment, or the server refuses to start, so a typo or a pasted foreign address
cannot send the key elsewhere. The server never follows a redirect, so the key cannot be sent on to another host.

The redirect allowlist. After a client signs in, the server sends an authorization code to the
redirect address the client registered, and only to claude.ai, claude.com, localhost and the domains
you list in `MCP_EXTRA_REDIRECT_DOMAINS`. Each entry admits that host and all its subdomains (https
only; http only to a loopback host), so each added domain is a trust grant: whoever answers at that
host can receive a sign-in code. Add the most specific host, never a parent domain, never a shared
hosting or public-suffix domain, and never a domain you do not control or trust. For example the
Microsoft Copilot Studio host `global.consent.azure-apim.net` is shared by every Power Platform
tenant, so the allowlist cannot tell your connector from another tenant's there. The password page,
PKCE and the registration limits still apply, and a code is issued only after the password is typed on
the consent page, which shows the client's name and redirect address: approve only a sign-in you
started. But anyone who can receive a code on an allowed host (on the Copilot Studio host, every
Power Platform tenant) could register a client there and wait for you or another user to approve
it. The entries are listed in the start line of the journal,
and removing an entry blocks new sign-ins from the clients that registered on it. Removing a domain stops new sign-ins
only; sign out every session to cut off a connected client. The `resource` a client names in
its requests is accepted but not enforced: tokens are not bound to it, because the server has one
resource and one password.

Documents. A public link and publishing to the client portal make a document readable by people
who are not signed in to Gorelo, and the public link is a secret in itself: anyone who has it can
read the document. Turning either on is the gated tool `share_document`; the tool that edits
documents cannot do it, and `unshare_document` only turns it off. Give a link only to the person
who should have it. A document body, its attachments and its images are written by people, so the
server tells the model to treat them as data, and the urls of attachments and images carry a
time-limited token that must not be stored.

Several clients, one key. Every client that signs in with the password gets the same tools and
acts with the same Gorelo key. Connect only clients you trust, and keep the toolsets and gated
tools to what all of them may do.

Prompt injection. Ticket, comment, form and document text is written by third parties. The server tells the
model to treat it as data, but a model can still be misled. The gated tools, the Private-by-default
comments and the key scopes are what limit the damage, so do not rely on the model alone.

Secrets on disk. The `.env` file (Gorelo key, password, public URL) and `.oauth-state/` (sessions)
are secrets. Keep them out of git and out of backups that others can read. The tunnel token is a
secret as well.

Out of scope. A compromised host, a leaked Gorelo key or password, and vulnerabilities in Gorelo,
the tunnel provider, claude.ai or the dependencies themselves (report those to their owners).

## Hardening checklist

Details and reasons are in `docs/why/08-server-hardening.txt`; the steps are in `docs/INSTALL.md`
and the example units are in `deploy/`.

- [ ] Reach the server only through the tunnel; no inbound ports; inbound firewall default-drop.
- [ ] Run as an unprivileged service user; code tree owned by root and read-only to it.
- [ ] Start with the venv's Python directly, with `UMask=0077` and the sandbox drop-in installed.
- [ ] `.env` mode 0640 (root:service group); `.oauth-state/` mode 0700, file 0600; never committed.
- [ ] A long random `MCP_AUTH_PASSWORD`, and the real client address visible in the log (not
      127.0.0.1), so the rate limits count visitors.
- [ ] A Gorelo key with only the scopes you need; keep the gated tools off unless you need them, and list only the ones you need.
- [ ] Every domain in `MCP_EXTRA_REDIRECT_DOMAINS` is the most specific host of a client you use.
- [ ] The watcher has its own environment file holding only the API key (and, for an account hosted in Australia, `GORELO_BASE_URL`).
- [ ] Keep dependencies as locked (`uv.lock`); update on purpose and run the tests first.
- [ ] Enable unattended upgrades for the operating system (INSTALL step 1.2): Debian's default
      configuration installs security fixes and Debian stable point-release updates, and does not
      reboot by itself.
- [ ] Back up `.oauth-state/` and rotate the password, the Gorelo key and the tunnel token if any
      of them may have leaked.
