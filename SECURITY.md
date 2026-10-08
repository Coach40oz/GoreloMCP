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
capped, redirects are limited to claude.ai, claude.com and localhost, and the server refuses to
start without a password. Use a long random password (16 characters or more; the server warns
below that). See `docs/why/05-built-in-oauth-one-password.txt`.

What the Gorelo key can do. Every tool call uses the one Gorelo API key in `.env`. So the limits
on what a signed-in client can do are the scopes of that key, the toolsets you enable
(`GORELO_TOOLSETS`; projects and forms are off by default) and whether you enabled the gated
delete and void tools and `create_approved_invoice` (`GORELO_ENABLE_DESTRUCTIVE`, off by default, and each call also needs
`confirm=true`). Scope the key minimally: give it only the areas you use, and do not grant
Project or Forms unless you enable those toolsets. Some operations can never be called whatever
the key allows (client, contact, ticket, asset and contract deletes, and creating API keys; see
`docs/why/03-forbidden-and-gated-operations.txt`).

Prompt injection. Ticket, comment and form text is written by third parties. The server tells the
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
- [ ] A Gorelo key with only the scopes you need; keep the gated tools off unless you need them.
- [ ] The watcher has its own environment file holding only the API key.
- [ ] Keep dependencies as locked (`uv.lock`); update on purpose and run the tests first.
- [ ] Enable unattended upgrades for the operating system (INSTALL step 1.2): Debian's default
      configuration installs security fixes and Debian stable point-release updates, and does not
      reboot by itself.
- [ ] Back up `.oauth-state/` and rotate the password, the Gorelo key and the tunnel token if any
      of them may have leaked.
