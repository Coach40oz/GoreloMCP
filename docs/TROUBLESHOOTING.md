# Troubleshooting

Find your symptom, check the likely cause, apply the fix. Commands run as root on the server. The service is `gorelo-mcp.service`; the first thing to look at is almost always its journal:

```bash
journalctl -u gorelo-mcp.service --since "30 minutes ago" --no-pager
```

## Quick table

| Symptom | Likely cause | Fix |
|---|---|---|
| `curl http://127.0.0.1:8765/mcp` prints `401` | Healthy. The endpoint wants a sign-in. | Nothing to fix. |
| The same `curl` prints `000` | The service is not running. | `systemctl status gorelo-mcp.service`, then read the journal. |
| Local `curl` prints `401` but the public address fails | The tunnel is down or points to the wrong place. | `systemctl status cloudflared.service`; check the hostname in the Cloudflare dashboard goes to `http://127.0.0.1:8765`. |
| Public address prints `530` or `502` (the page may mention Cloudflare error 1033) | Tunnel not connected, token wrong, or the service is down. | Same as above, and `journalctl -u cloudflared.service --no-pager | tail`. |
| The service restarts every 10 seconds and the journal says `refusing to start` | A setting in `.env` is missing or wrong, or the sign-in data cannot be used. | Read the message; it lists every problem. Fix `.env` and restart. |
| Status `226/NAMESPACE` | `.oauth-state` or `.state` is missing in `/opt/gorelo-mcp/app`. | Create them as in INSTALL step 2.3. |
| Status `203/EXEC` | The Python environment is missing or broken. | Rebuild it: INSTALL step 2.4. |
| claude.ai says it cannot connect to the server | Wrong URL, tunnel down, or the connector was added without `/mcp`. | See "Claude says it cannot connect" below. |
| claude.ai keeps asking you to sign in | The refresh did not work, or `PUBLIC_BASE_URL` does not match the address. | See "Sign-in loops" below. |
| The password page says too many attempts (HTTP 429) | Rate limit after wrong passwords. | Wait. See "Sign-in loops". |
| Tools are missing in Claude | Old conversation, toolset not enabled, or connector switched off for the chat. | See "Tools are missing" below. |
| Tool error `HTTP 403 ... does not have the 'Project' scope` (or `Forms`, or `Documents`) | The Gorelo key lacks that scope. The error names the missing access (read, write or delete) when Gorelo gives it. | Grant the scope on the key in Gorelo, or remove that toolset. |
| Tool error `HTTP 403 ... does not have the 'Documents' scope with read access (Documents:read)` | The Gorelo key lacks the `Documents` scope. | Grant the scope on the key in Gorelo, or take `documents` out of `GORELO_TOOLSETS`. |
| `health_check` returns `ok: false` with status 401, or a tool error `HTTP 401` | The key is wrong, revoked or incomplete, or `GORELO_BASE_URL` names a host that does not hold your Gorelo account. | See "API key rejected (HTTP 401)" below. |
| The start line `gorelo-mcp settings: api_host=...` does not name the host of your account's region | `GORELO_BASE_URL` is blank (the US host) or names another host | Set `GORELO_BASE_URL` to the host of your region (INSTALL step 3.3) and restart. |
| Copilot Studio or ChatGPT cannot connect, claude.ai works | The host the client is sent back to is not in `MCP_EXTRA_REDIRECT_DOMAINS`. | See "Copilot Studio cannot connect" below. |
| Tool error `Gorelo is rate limiting requests (HTTP 429)` | Too many requests to Gorelo. | Wait a minute and repeat. See "Gorelo errors". |
| Tool error `unexpected response shape; refusing to guess` | Gorelo changed its API. | Run `health_check`; look for a new release (INSTALL step 9) and check the watcher. |
| Tool error `Phone number is not valid for the specified country code` | Gorelo checks that a number is a real one for the region. A fictional number such as 5555550142 (area code 555) is refused for region `US`. | Use a real number with a real area code, for example 2025550142, and the two-letter region (`US`). |
| Tool error saying Gorelo did not confirm a write | A timeout or error after sending a change. It may or may not have been applied. | Check with a read (ask Claude to look the record up) before repeating it. |
| The journal shows `ip=127.0.0.1` on `authorize outcome=` lines | The server does not see the visitor's real address, so the password limits count everyone together. | See "The journal shows ip=127.0.0.1" below. |
| A delete, void, `create_approved_invoice` or `share_document` tool does not exist | The destructive switch is off (the default), or it lists other names. | See INSTALL step 8.2 if you really need it. |
| A document tool is missing | The `documents` toolset is off (the default). | Add `documents` to `GORELO_TOOLSETS`. A non-blank GORELO_TOOLSETS replaces the default set: list every toolset you want, for example `core,tickets,time,billing,uptime,documents`; see "Document tools" below. |
| `systemctl --failed` lists `gorelo-changelog-watch.service` | The watcher exited with 1 or 3. | See "The watcher failed". |
| An invoice you voided is still open in your accounting system | Gorelo voids it in Gorelo only. | Void it in the accounting system by hand. |

## Claude says it cannot connect

Work from the inside out.

1. The service: `systemctl is-active gorelo-mcp.service` must print `active`. If not, see "Service will not start".
2. Local endpoint: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/mcp` must print `401`.
3. The tunnel: `systemctl is-active cloudflared.service` must print `active`, and the Cloudflare dashboard should show the tunnel as Healthy. If the tunnel service keeps stopping, read `journalctl -u cloudflared.service --no-pager | tail -n 20`. A token error means the token in `/etc/cloudflared/tunnel.env` is incomplete or from a deleted tunnel.
4. The hostname: in the dashboard, the public hostname must point to the service `http://127.0.0.1:8765` (type HTTP, not HTTPS).
5. The public address: `curl -s -o /dev/null -w '%{http_code}\n' https://your-host/mcp` must print `401`. Also try `curl -s https://your-host/.well-known/oauth-authorization-server`; it should print JSON mentioning your host. If the JSON shows a different host, `PUBLIC_BASE_URL` in `.env` is wrong: fix it and restart.
6. The connector: the URL in claude.ai must be exactly `https://your-host/mcp`, with `https` and with `/mcp`.
7. If a Cloudflare Access policy or a WAF rule sits in front of the hostname, it blocks claude.ai before it reaches the sign-in page. Remove it for this hostname; the server has its own sign-in.

## Copilot Studio cannot connect

The same steps apply to ChatGPT (the host to add is `chatgpt.com`) and to any other client that signs in over OAuth. The symptom: the client says it cannot connect or cannot register, and claude.ai still works. The server only sends a sign-in code to claude.ai, claude.com, localhost and the domains you list in `MCP_EXTRA_REDIRECT_DOMAINS`.

1. Read what the server saw:

   ```bash
   journalctl -u gorelo-mcp.service --since "30 minutes ago" --no-pager | grep -E "register outcome|redirect_not_allowed|blocked_redirect"
   ```

   A line `register outcome=refused reason=redirect_not_allowed redirect=<host> ip=...` means the client tried to register with a redirect on a host that is not allowed. The host after `redirect=` is the one to add. Use exactly that host, never a guess: Microsoft documents `https://global.consent.azure-apim.net/redirect/<connector>`, but the host can differ by Microsoft cloud. Other reasons on that line (`redirect_not_https`, `redirect_userinfo`, `redirect_fragment`, `redirect_scheme`) mean the address itself is not acceptable, and no setting changes that. A line `authorize outcome=blocked_redirect ... redirect=<host>` means a client that registered earlier was sent to a host that is no longer allowed.
2. Add the host to `/opt/gorelo-mcp/app/.env`, for example `MCP_EXTRA_REDIRECT_DOMAINS=global.consent.azure-apim.net` (a comma list for several), then `systemctl restart gorelo-mcp.service`. The start line must show it: `journalctl -u gorelo-mcp.service --since "1 minute ago" --no-pager | grep "gorelo-mcp settings"` prints `extra_redirect_domains=...` with your host. A change takes effect only at a restart.
3. Each entry also admits its subdomains and receives sign-in codes, so add the most specific host and nothing broader (see [SECURITY.md](../SECURITY.md)). An entry the server refuses (a scheme, a path, a port, a wildcard, an IP address or `localhost`) stops the service with `MCP_EXTRA_REDIRECT_DOMAINS entry '...'` in the journal: fix the entry.
4. If the journal shows no `register` line at all, the request did not reach the login code. Either it never arrived (a Cloudflare bot or WAF rule in front of the hostname can block the back end of a vendor: check the security events of that hostname), or the MCP framework refused it before this server saw it (a registration whose `grant_types` lack `refresh_token` is one; the server cannot log that).
5. A form in the client that insists on a manual client id and secret cannot be served: this server supports dynamic client registration only.
6. If you remove an entry later, the clients that registered on it stay stored but their next sign-in is refused (`authorize outcome=blocked_redirect`). `scripts/oauth_state.py purge --stale` removes idle ones ([OPERATIONS.md](OPERATIONS.md)). Removing a domain stops new sign-ins only; see [OPERATIONS.md](OPERATIONS.md#sign-out-all-claude-sessions), "Sign out all Claude sessions", to cut off a connected client.

## API key rejected (HTTP 401)

`health_check` returns `ok: false` with status 401 (its error ends with a hint that names the Gorelo host the server called, `GORELO_API_KEY` and `GORELO_BASE_URL`), or another tool fails with `HTTP 401`. Work through it in this order:

1. The key itself: it is complete (no `Bearer ` in front, no quotes or spaces, nothing cut off when it was pasted), it was not revoked or deleted in Gorelo, and it is in `GORELO_API_KEY` of `/opt/gorelo-mcp/app/.env`. A value that is not a key at all gets a 401 whose body is Gorelo's gateway JSON (`{"error": "Invalid or missing API key format", ...}`), not a normal Gorelo answer; the hint is added for that too.
2. The region: `GORELO_BASE_URL` must be the host of the region your Gorelo account is hosted in. The server calls `https://api.usw.gorelo.io/v1` when the variable is blank; an account hosted in Australia needs `GORELO_BASE_URL=https://api.aue.gorelo.io`. Edit `.env`, run `systemctl restart gorelo-mcp.service`, then check the start line (see "The API host is not the one of your region" below): `api_host` there must be the host of your region.
3. Open a NEW conversation and run `health_check` again. If it still answers 401, check steps 1 and 2 again.
4. The watcher is separate. It has its own key file, `/etc/gorelo-mcp/watcher.env`, which can carry the same `GORELO_BASE_URL` line. An alert refused with HTTP 401 names the host the watcher called.

## The API host is not the one of your region

The start line names the host the server calls. The server logs it before any API call, so read it in the journal:

```bash
journalctl -u gorelo-mcp.service --no-pager | grep "gorelo-mcp settings" | tail -n 1
```

If the start line's `api_host` is not the host of the region your Gorelo account is hosted in (US `api.usw.gorelo.io`, Australia `api.aue.gorelo.io`):

1. Set `GORELO_BASE_URL` in `.env` to the host of your region (INSTALL step 3.3). Blank is the US host.
2. Restart the service with `systemctl restart gorelo-mcp.service`, then read the start line again: it must now name the host of your region.

## Document tools

- A document tool answers `HTTP 403 ... does not have the 'Documents' scope with read access (Documents:read)` (Gorelo code `080203`): the key lacks the `Documents` scope. When Gorelo names the missing access (read, write or delete), the error names that access too. Grant it in Gorelo (no restart needed) or take `documents` out of `GORELO_TOOLSETS`.
- `update_document` is refused when the refusal says that an attachment "would be removed": the attachment list replaces the whole list in Gorelo, so a list that leaves out a current attachment is refused. Ask Claude to read the document with `get_document` first and send the complete list, name the attachment to remove in `remove_attachment_names`, or empty the list through `clear_fields`.
- A document write fails with "Gorelo did not confirm" or an answer that does not show the change: it may have been applied. Ask Claude to read the document with `get_document` before repeating it.
- A restored document is at the top level, not under its old parent. `archive_document` archives the document together with the documents under it, so note the parent first.
- `share_document` does not exist: it needs `GORELO_ENABLE_DESTRUCTIVE` to switch it on (`1`, or a list that names `share_document`), the `documents` toolset, and `confirm=true` on every call.

## Sign-in loops

The access token of a first sign-in lasts 30 days; after its first refresh, claude.ai refreshes it by itself about once an hour, without asking you for the password. If it asks you to sign in again, the refresh did not work. Common causes:

- You ran `revoke-all`, or restored an older backup of `.oauth-state`. Signing in again once is the normal fix.
- `PUBLIC_BASE_URL` was changed. Everything signed in before must sign in again, and the new value must match the public address exactly.
- The state file was deleted. Sign in again.
- The password page loops back: the password is wrong, or it contains a stray space from copying. Paste it again without spaces.

To see what happened, look at the journal for `authorize outcome=`, `token outcome=` and `access outcome=` lines. If you see `wrong_password` or `rate_limited`:

- Five wrong passwords from one address in 15 minutes block that address for a while. Wait for the `retry_in=` seconds.
- Thirty wrong passwords from all addresses in an hour block every new sign-in, even with the right password, until the older ones age out (up to an hour). Existing sessions are not affected. Restarting the service clears the counters at once.

Then, in claude.ai: Settings, Connectors, open the connector, disconnect and connect again, and enter the password.

## The journal shows ip=127.0.0.1

The wrong-password limits count visitors by the address the server sees. The `authorize outcome=` lines in `journalctl -u gorelo-mcp.service` carry that address as `ip=`. For a visit that came through the tunnel it should be a public address. If it is `127.0.0.1`, all visitors look like one, so five wrong passwords from anyone would lock out everyone, and the journal cannot tell you who tried.

1. Make sure you test through the public address: `curl -s -o /dev/null https://your-host/authorize`, then `journalctl -u gorelo-mcp.service --since "1 minute ago" --no-pager | grep "authorize outcome="`. A request sent to `http://127.0.0.1:8765` directly always shows `127.0.0.1`, which is correct.
2. Check that `cloudflared` runs on the same machine as the service and that the public hostname in the dashboard points to `http://127.0.0.1:8765` (INSTALL step 5.2). The server reads the address the tunnel forwards only from a connection that comes from the same machine; a different proxy or a tunnel on another host hides it.
3. If you put another proxy in front of the tunnel, remove it, or accept that the limits cannot tell visitors apart.

## Tools are missing

1. Open a NEW conversation. Tool lists are read when a conversation starts, so an old one does not see new tools.
2. Make sure the connector is switched on for that conversation.
3. Check the `ready:` line: `journalctl -u gorelo-mcp.service --no-pager | grep ready | tail -n 1`. The `toolsets=` part must contain the toolset you expect (`projects`, `forms` and `documents` are off by default; see INSTALL step 8). The `tools=` number must match [tool-catalog.md](tool-catalog.md).
4. Delete, void, approved-invoice and sharing tools only exist when `GORELO_ENABLE_DESTRUCTIVE` switches them on: `1` for all of them, or a list that names the tool. The `gorelo-mcp gated tools:` line in the journal and the `gated_tools` field of `health_check` say which are on.
5. After you change `.env`, restart the service: `systemctl restart gorelo-mcp.service`.

## Gorelo errors

Errors from Gorelo come back to Claude in plain words and name the parameter to fix. Three you will meet:

- **403 with a missing scope** (message `API key does not have '<Scope>' scope`): the key lacks a permission. Open the key in Gorelo and add the named scope, or remove the toolset that needs it from `GORELO_TOOLSETS`. After editing the key in Gorelo you do not need to restart the server. After editing `.env`, you do.
- **429 rate limit**: Gorelo allows only a few requests per second per endpoint. The server already waits and retries up to 3 times, about 20 seconds. If the error still appears, wait and repeat, and avoid asking Claude for many large bulk operations at once.
- **401 or "invalid API key"** from Gorelo: first check the region (see "API key rejected (HTTP 401)" above: `GORELO_BASE_URL` must be the host of your region). Then the key in `.env` is wrong, expired or deleted: create a new one and follow "Rotate the Gorelo API key" in [OPERATIONS.md](OPERATIONS.md#rotate-the-gorelo-api-key).

Start every investigation by asking Claude to run `health_check`. It tells you whether Gorelo answers and whether the server understands the answer.

## Service will not start

Read the journal first:

```bash
systemctl status gorelo-mcp.service --no-pager
journalctl -u gorelo-mcp.service --since "10 minutes ago" --no-pager | tail -n 30
```

Look for `refusing to start`. Then:

- **Settings errors.** One message lists every problem at once, for example `missing or blank required environment variable(s): PUBLIC_BASE_URL, MCP_AUTH_PASSWORD`, or an unknown toolset name (the message lists the valid names), or a bad value for `GORELO_ENABLE_DESTRUCTIVE` (use `1`, a comma list of gated tool names, or leave it blank; for a list the message names each bad name and lists the valid ones), or a bad address in `GORELO_BASE_URL` (`GORELO_BASE_URL must start with https://`, or a host that is not under `gorelo.io`), or a bad entry in `MCP_EXTRA_REDIRECT_DOMAINS` (`MCP_EXTRA_REDIRECT_DOMAINS entry 'localhost' must not be localhost`). Edit `/opt/gorelo-mcp/app/.env`, fix every item, run `systemctl restart gorelo-mcp.service`.
- **Sign-in data problems** (the message mentions the OAuth state file, its folder or its lock). Another process may hold the lock (a second copy of the service, or the `oauth_state.py` script): look at `ps aux | grep -E "main.py|oauth_state"` and stop it. Or the file is damaged, or owned by root. Do NOT delete or edit it. Check it with:

  ```bash
  cd /opt/gorelo-mcp/app
  runuser -u gorelo-mcp -- env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/oauth_state.py check
  ```

  If it says the file is owned by another user, fix the owner: `chown -R gorelo-mcp:gorelo-mcp /opt/gorelo-mcp/app/.oauth-state`. If the file is truly damaged, restore it from your last backup (OPERATIONS, Backups). As a last resort you can move the file away; everyone then signs in again.
- **Login gate message** (an unchecked `fastmcp` or `mcp` version): the Python packages were changed. Rebuild them exactly as locked: INSTALL step 2.4 (`uv sync --frozen`), then restart.
- **Status `226/NAMESPACE`:** create the missing folders: `install -d -m 0700 -o gorelo-mcp -g gorelo-mcp /opt/gorelo-mcp/app/.oauth-state` and `install -d -m 0755 -o gorelo-mcp -g gorelo-mcp /opt/gorelo-mcp/app/.state`.
- **Status `203/EXEC`:** `/opt/gorelo-mcp/app/.venv/bin/python` is missing. Run INSTALL step 2.4.
- **Status `218/CAPABILITIES`, `SECCOMP` or a crash right after start, in an LXC container:** a setting in the sandbox drop-in is not allowed in your container. Find it by renaming the drop-in away and starting again: `mv /etc/systemd/system/gorelo-mcp.service.d/10-sandbox.conf /root/` then `systemctl daemon-reload && systemctl restart gorelo-mcp.service`. If that works, put the file back and delete lines from it one at a time (each setting is on its own line with a comment) until the service starts. Keep as many as you can.
- **Port already in use** (`address already in use`): another program uses port 8765. `ss -ltnp | grep 8765` shows which one.

## Permission denied on a file under /opt/gorelo-mcp/app after an update

A command run as root with a restrictive umask (for example a bare `umask 077` left set in your shell) can leave new code or venv files unreadable to the service. Repair the code and the venv, and leave `.env`, `.oauth-state` and `.state` as they are:

```bash
cd /opt/gorelo-mcp/app
find . -path ./.env -prune -o -path ./.oauth-state -prune -o -path ./.state -prune -o -path ./site.local.toml -prune -o -exec chmod u=rwX,go=rX {} +
systemctl restart gorelo-mcp.service
```

Then run `ls -ld .env .oauth-state .state site.local.toml` and confirm they are unchanged (`.env` mode 0640 root:gorelo-mcp, `site.local.toml` mode 0640 root:gorelo-mcp, `.oauth-state` mode 0700 gorelo-mcp, `.state` mode 0755 gorelo-mcp). In new shells, run `umask` and expect `0022`.

## The watcher failed

`systemctl --failed` lists `gorelo-changelog-watch.service`. Read why:

```bash
journalctl -u gorelo-changelog-watch.service --since "1 day ago" --no-pager
systemctl status gorelo-changelog-watch.service --no-pager | grep -E "status=|Main PID"
```

| Exit code | What it means | What to do |
|---|---|---|
| 1 | Gorelo changed its API (a changelog entry or the live API description). An alert was posted in Gorelo, or the journal says it could not be. | Follow "When the watcher reports an API change" in [OPERATIONS.md](OPERATIONS.md#when-the-watcher-reports-an-api-change). |
| 2 | News that is not about the API. Not a failure: the unit is not marked failed. | Nothing to fix. |
| 3 | A check could not complete. | Find the `ERROR` line in the journal. It names the failing part. |

Common causes of exit 3 or a start failure:

- Exit 3 with `ERROR site config ... file not found`, `missing key [watcher] alert_client_id` or `still holds the placeholder`: `/opt/gorelo-mcp/app/site.local.toml` is missing, lacks `alert_client_id` under `[watcher]`, or still has the value from `site.example.toml`. Recreate it as in INSTALL step 7.2. The watcher needs only that one key. Check the owner and mode: `root:gorelo-mcp 0640`.
- Network: the server could not reach the changelog or the Gorelo API description. Try again later with `systemctl start gorelo-changelog-watch.service`.
- A file in `.state/` cannot be read, usually because someone ran the watcher as root: `chown -R gorelo-mcp:gorelo-mcp /opt/gorelo-mcp/app/.state`. If a state file is damaged, deleting it makes the watcher start from a fresh baseline.
- The unit fails at once with `Failed to load environment files` (`/etc/gorelo-mcp/watcher.env` is missing) or `226/NAMESPACE` (`/opt/gorelo-mcp/app/.state` is missing). Create the missing one as in INSTALL step 7.
- Exit 1 with a message that the alert could not be posted: the API key in `/etc/gorelo-mcp/watcher.env` is missing or wrong, or it lacks the scope to create alerts. The change is remembered as pending, and the next run tries again. If the message is an HTTP 401 and your account is not hosted in the US, the watcher called the US host (its default): put `GORELO_BASE_URL=https://api.aue.gorelo.io` (or the host of your region) in `/etc/gorelo-mcp/watcher.env` too. The message names the host it called.

After you fix it, clear the failed mark and run it once more:

```bash
systemctl reset-failed gorelo-changelog-watch.service
systemctl start gorelo-changelog-watch.service || true
systemctl show -p ExecMainStatus --value gorelo-changelog-watch.service
```

The last command prints the script's exit code: 0 nothing new, 1 an API change was found, 2 news that is not about the API, 3 the check failed.

## The live test harness will not start

This applies only to developers who run the scripts in `scripts/live/`. The harness reads `site.local.toml` and, before it writes anything (and in the smoke run), reads each configured client from Gorelo.

- `cannot start: client 1234 is named X, site config says Y` (exit 2): the id in `[clients]` belongs to a client with a different name than `test_client_name` or `second_client_name`. Fix the id or the name so they match exactly what Gorelo shows. Nothing was written.
- `cannot start: cannot check client 1234 ...`: Gorelo did not answer, or answered with an error such as 403 or 404. Check the API key, the id and the network.
- `cannot start: site config ...`: the file is missing, a key is missing (the harness needs every section except `[watcher]`; `[harness]` and the `[leftovers]` name rules are optional), a value is of the wrong type, the file is the example file itself, or a value is still a placeholder from `site.example.toml`.

## Still stuck

Collect the last 50 lines of the journal (`journalctl -u gorelo-mcp.service -n 50 --no-pager`). Remove anything that looks like a secret. Before you post the lines in a public issue, replace the record ids, the client ids and names, the IP addresses, the host names and the text of tool errors (a tool error can quote a name, a number or a date that the call was given, and Gorelo's own messages), or use private reporting (see [SECURITY.md](../SECURITY.md)). Then open an issue on the project's GitHub page with the lines, the Debian version and what you were doing.
