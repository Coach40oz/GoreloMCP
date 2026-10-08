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
| Tool error `HTTP 403 ... does not have the 'Project' scope` (or `Forms`) | The Gorelo key lacks that scope. | Grant the scope on the key in Gorelo, or remove that toolset. |
| Tool error `Gorelo is rate limiting requests (HTTP 429)` | Too many requests to Gorelo. | Wait a minute and repeat. See "Gorelo errors". |
| Tool error `unexpected response shape; refusing to guess` | Gorelo changed its API. | Run `health_check`; look for a new release (INSTALL step 9) and check the watcher. |
| Tool error saying Gorelo did not confirm a write | A timeout or error after sending a change. It may or may not have been applied. | Check with a read (ask Claude to look the record up) before repeating it. |
| The journal shows `ip=127.0.0.1` on `authorize outcome=` lines | The server does not see the visitor's real address, so the password limits count everyone together. | See "The journal shows ip=127.0.0.1" below. |
| A delete, void or `create_approved_invoice` tool does not exist | The destructive switch is off (the default). | See INSTALL step 8.2 if you really need it. |
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

The wrong-password limits count visitors by the address the server sees. Every `authorize outcome=` line in `journalctl -u gorelo-mcp.service` ends with `ip=`. For a visit that came through the tunnel it should be a public address. If it is `127.0.0.1`, all visitors look like one, so five wrong passwords from anyone would lock out everyone, and the journal cannot tell you who tried.

1. Make sure you test through the public address: `curl -s -o /dev/null https://your-host/authorize`, then `journalctl -u gorelo-mcp.service --since "1 minute ago" --no-pager | grep "authorize outcome="`. A request sent to `http://127.0.0.1:8765` directly always shows `127.0.0.1`, which is correct.
2. Check that `cloudflared` runs on the same machine as the service and that the public hostname in the dashboard points to `http://127.0.0.1:8765` (INSTALL step 5.2). The server reads the address the tunnel forwards only from a connection that comes from the same machine; a different proxy or a tunnel on another host hides it.
3. If you put another proxy in front of the tunnel, remove it, or accept that the limits cannot tell visitors apart.

## Tools are missing

1. Open a NEW conversation. Tool lists are read when a conversation starts, so an old one does not see new tools.
2. Make sure the connector is switched on for that conversation.
3. Check the `ready:` line: `journalctl -u gorelo-mcp.service --no-pager | grep ready | tail -n 1`. The `toolsets=` part must contain the toolset you expect (`projects` and `forms` are off by default; see INSTALL step 8). The `tools=` number must match [tool-catalog.md](tool-catalog.md).
4. Delete and void tools and `create_approved_invoice` only exist with `GORELO_ENABLE_DESTRUCTIVE=1`.
5. After you change `.env`, restart the service: `systemctl restart gorelo-mcp.service`.

## Gorelo errors

Errors from Gorelo come back to Claude in plain words and name the parameter to fix. Two you will meet:

- **403 with a missing scope** (message `API key does not have '<Scope>' scope`): the key lacks a permission. Open the key in Gorelo and add the named scope, or remove the toolset that needs it from `GORELO_TOOLSETS`. After editing the key in Gorelo you do not need to restart the server. After editing `.env`, you do.
- **429 rate limit**: Gorelo allows only a few requests per second per endpoint. The server already waits and retries up to 3 times, about 20 seconds. If the error still appears, wait and repeat, and avoid asking Claude for many large bulk operations at once.
- **401 or "invalid API key"** from Gorelo: the key in `.env` is wrong, expired or deleted. Create a new one and follow "Rotate the Gorelo API key" in [OPERATIONS.md](OPERATIONS.md#rotate-the-gorelo-api-key).

Start every investigation by asking Claude to run `health_check`. It tells you whether Gorelo answers and whether the server understands the answer.

## Service will not start

Read the journal first:

```bash
systemctl status gorelo-mcp.service --no-pager
journalctl -u gorelo-mcp.service --since "10 minutes ago" --no-pager | tail -n 30
```

Look for `refusing to start`. Then:

- **Settings errors.** One message lists every problem at once, for example `missing or blank required environment variable(s): PUBLIC_BASE_URL, MCP_AUTH_PASSWORD`, or an unknown toolset name (the message lists the valid names), or a bad value for `GORELO_ENABLE_DESTRUCTIVE` (use `1` or leave it blank). Edit `/opt/gorelo-mcp/app/.env`, fix every item, run `systemctl restart gorelo-mcp.service`.
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
- Exit 1 with a message that the alert could not be posted: the API key in `/etc/gorelo-mcp/watcher.env` is missing or wrong, or it lacks the scope to create alerts. The change is remembered as pending, and the next run tries again.

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

Collect the last 50 lines of the journal (`journalctl -u gorelo-mcp.service -n 50 --no-pager`). Remove anything that looks like a secret (the logs should not hold any), and open an issue on the project's GitHub page with the lines, the Debian version and what you were doing.
