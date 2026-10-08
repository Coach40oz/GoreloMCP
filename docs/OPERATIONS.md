# Operations guide

Day-to-day tasks for the person who runs the server. Run these as root on the server. It assumes you followed [INSTALL.md](INSTALL.md): the service is called `gorelo-mcp.service` and lives in `/opt/gorelo-mcp/app`.

## Check health

Quick checks, in order:

```bash
systemctl is-active gorelo-mcp.service cloudflared.service
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/mcp
curl -s -o /dev/null -w '%{http_code}\n' https://your-host/mcp
systemctl --failed --no-pager
```

You should see `active` twice, then `401` twice (healthy: the endpoint wants a sign-in), then `0 loaded units listed`. A `000` on the first `curl` means the service is down. A wrong answer on the second only means the tunnel is down.

Then, in a new Claude conversation, ask "Run health_check". `ok: true` means Claude reaches your server, and your server reaches Gorelo.

## Read the logs

```bash
journalctl -u gorelo-mcp.service --since today --no-pager     # everything today
journalctl -u gorelo-mcp.service -f                           # follow live (Ctrl+C to stop)
journalctl -u gorelo-changelog-watch.service --since today    # the API watcher
```

What normal looks like:

- At start: `oauth state loaded format=v2 (format=none before the first sign-in) clients=N access=N refresh=N pruned=N` and `gorelo-mcp ready: toolsets=... destructive=False tools=N`. If `tools=N` is not what [tool-catalog.md](tool-catalog.md) lists for your toolsets, the environment is not what you think.
- For every Gorelo request, one line: the tool, the operation, the status and the time taken. Only the names of fields are logged. Values, your key and the password are never logged.
- Sign-in lines such as `authorize outcome=approved`, `token outcome=issued` and `token outcome=refreshed`. The access token of a first sign-in lasts 30 days; after its first refresh, a session in use refreshes about once an hour. `access outcome=expired` followed by `refreshed` is normal.

What deserves attention:

- `wrong_password`, `rate_limited` or `register outcome=refused`: someone is guessing the password or probing registration. Look at the `ip=` value. If it is not you, use a longer password (below), and consider signing everyone out.
- `refusing to start`: the service is not running. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#service-will-not-start).
- A warning that the password is shorter than 16 characters: change it.
- `refresh_reuse_detected` (warning): an old refresh token was used again after its grace period. If you did not do anything unusual, sign everyone out and sign in again.

Logs older than the journal's size limit disappear on their own.

## Rotate the Gorelo API key

1. In Gorelo, create a new key with the same scopes. Leave the old key active for now.
2. Edit the key:

   ```bash
   nano /opt/gorelo-mcp/app/.env
   systemctl restart gorelo-mcp.service
   ```

3. Check the logs for the `ready:` line, then in a new Claude conversation run `health_check`.
4. If you use the watcher, put the new key in its file too (it reads it on its next run):

   ```bash
   nano /etc/gorelo-mcp/watcher.env
   ```

5. Delete the old key in Gorelo.

## Change the password

1. Make a new one: `openssl rand -hex 24`. Keep it in your password manager.
2. Edit `MCP_AUTH_PASSWORD` in `/opt/gorelo-mcp/app/.env`, then `systemctl restart gorelo-mcp.service`.
3. Sessions that already exist keep working. The password is only asked when someone connects for the first time. To end them, sign everyone out (next section).

## Sign out all Claude sessions

Do this if you think someone else has the password or a token, or after you changed the password and want the old sessions gone. Every Claude connection must then sign in again.

The script `scripts/oauth_state.py` edits the sign-in data safely. It prints only counts and short identifiers, never a token. To change anything it needs the lock that the running service holds, so it refuses (exit status 3, nothing changed) while the service runs. That is why you stop the service first.

```bash
cd /opt/gorelo-mcp/app
systemctl stop gorelo-mcp.service
runuser -u gorelo-mcp -- env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/oauth_state.py revoke-all --apply
systemctl start gorelo-mcp.service
```

You should see the script print `verified=yes`, and then the service starts. Run it as `gorelo-mcp`, as shown, not as root: a root run leaves a file the service cannot write.

Do not delete the `.oauth-state` folder instead. It also holds the registrations of the Claude clients, and a missing file looks like a first start.

Other read-only commands that are safe while the service runs:

```bash
cd /opt/gorelo-mcp/app
runuser -u gorelo-mcp -- env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/oauth_state.py check
runuser -u gorelo-mcp -- env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/oauth_state.py inventory
runuser -u gorelo-mcp -- env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/oauth_state.py purge --stale --plan
```

`check` should end with `result: ok`. `inventory` lists the clients and tokens. `purge --stale --plan` shows old entries that `purge --stale --apply` (service stopped) would remove.

## Backups

Everything except these four things comes back from git. Back these up:

| What | Where | If you lose it |
|---|---|---|
| Settings and secrets | `/opt/gorelo-mcp/app/.env` | Create a new Gorelo key and a new password. |
| Sign-in data | `/opt/gorelo-mcp/app/.oauth-state/` | Every Claude connection must sign in again. |
| Watcher settings | `/opt/gorelo-mcp/app/site.local.toml` (owner `root:gorelo-mcp`, mode 0640; the watcher needs only `[watcher] alert_client_id`) and `/etc/gorelo-mcp/watcher.env` | Create them again from the examples. |
| Tunnel token | `/etc/cloudflared/tunnel.env` | Get a new token in the Cloudflare dashboard. |

A simple local backup (copy the archive to another machine afterwards, because a backup on the same disk does not survive the disk):

```bash
install -d -m 0700 /opt/gorelo-mcp-backups
( umask 077; tar -czf /opt/gorelo-mcp-backups/gorelo-mcp-$(date +%Y%m%d).tgz \
  /opt/gorelo-mcp/app/.env /opt/gorelo-mcp/app/.oauth-state \
  /opt/gorelo-mcp/app/site.local.toml /etc/gorelo-mcp/watcher.env /etc/cloudflared/tunnel.env )
ls -l /opt/gorelo-mcp-backups
```

The parentheses run `umask 077` in a subshell, so it applies to `tar` alone and is not left set in your shell (a bare `umask 077` would make later files you create, such as a `git checkout` of new code, unreadable to the service). (Leave out the files you do not have, such as the watcher files if you did not install the watcher; `tar` complains but still makes the archive of the others.) The archive holds secrets: keep it mode 600 in a mode 700 folder, and encrypt it if it leaves the server.

The sign-in data changes every time a session refreshes. A restored copy loses the sign-ins made after the backup was taken, and people just sign in again. To restore, stop the service, extract, fix the owners, and start it:

```bash
systemctl stop gorelo-mcp.service
tar -xzf /opt/gorelo-mcp-backups/FILE.tgz -C / opt/gorelo-mcp/app/.env opt/gorelo-mcp/app/.oauth-state
chown root:gorelo-mcp /opt/gorelo-mcp/app/.env && chmod 0640 /opt/gorelo-mcp/app/.env
chown -R gorelo-mcp:gorelo-mcp /opt/gorelo-mcp/app/.oauth-state
systemctl start gorelo-mcp.service
```

## When the watcher reports an API change

The watcher (if you installed it) checks Gorelo's changelog and the live API description every day. It posts one alert in Gorelo per new change (against the client set as `[watcher] alert_client_id` in `site.local.toml`, resource `gorelo-mcp`, severity 2), and the service run ends as failed so `systemctl --failed` shows it.

Exit codes of a run:

| Code | Meaning | Systemd treats it as |
|---|---|---|
| 0 | Nothing new. | success |
| 1 | An API change was found and an alert was posted (or could not be posted, and the journal says why). | failed |
| 2 | New changelog news that is not about the API. | success |
| 3 | A check itself failed (could not fetch, could not parse, or a config problem). | failed |

What to do after an exit 1:

1. Read the journal: `journalctl -u gorelo-changelog-watch.service --since today --no-pager`. It names a report file under `/opt/gorelo-mcp/app/.state/reports/`. Read that report: it says what changed.
2. Run `health_check` in Claude and try the tools you use most. Most changes do not break the server: it refuses to guess, and shows `unexpected response shape; refusing to guess` for a response it does not understand, instead of returning wrong data.
3. Check the repository for a new release that supports the new API (see updating in [INSTALL.md](INSTALL.md#9-updating-to-a-new-version-and-rolling-back)). A new release updates the file the watcher compares with, which quiets the watcher.
4. The same change never alerts twice. After you have dealt with it, clear the failed state: `systemctl reset-failed gorelo-changelog-watch.service`.
5. The watcher keeps its reports and saved API descriptions in `.state/`. Delete old ones by hand when they pile up.

For exit 3, read the `ERROR` line in the journal. An `ERROR site config ...` line means `site.local.toml` is missing, has no `alert_client_id` under `[watcher]`, still holds the example value, or is the example file itself: fix the file as in INSTALL step 7.2 and run the watcher again. Nothing is posted and nothing is remembered on that run. See also [TROUBLESHOOTING.md](TROUBLESHOOTING.md#the-watcher-failed).

To run the watcher by hand and see everything it does (this makes real requests, and posts an alert if something is new). Run it as `gorelo-mcp`, never as root, because a root run leaves root-owned files in `.state/` that the service cannot update:

```bash
(set -a; . /etc/gorelo-mcp/watcher.env; set +a; cd /opt/gorelo-mcp/app && runuser -u gorelo-mcp -- env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/watch_gorelo_changelog.py; echo "exit status: $?")
```

## Check the security of the service

```bash
systemd-analyze security gorelo-mcp.service --no-pager | tail -n 3
```

The last line shows an overall exposure score. With the unit and the sandbox drop-in from this repository it is around 1.5 and says `OK`. A higher number or `MEDIUM`, `EXPOSED` or `UNSAFE` means a protection is missing: check that the drop-in is installed at `/etc/systemd/system/gorelo-mcp.service.d/10-sandbox.conf` and that you ran `systemctl daemon-reload` and restarted. The same command works for the watcher: `systemd-analyze security gorelo-changelog-watch.service --no-pager | tail -n 3`.

Also check the ownership once in a while:

```bash
stat -c '%U:%G %a %n' /opt/gorelo-mcp/app /opt/gorelo-mcp/app/.env /opt/gorelo-mcp/app/.oauth-state /opt/gorelo-mcp/app/.state
```

You should see `root:root` for the code folder, `root:gorelo-mcp 640` for `.env` and `gorelo-mcp:gorelo-mcp` for the other two.
