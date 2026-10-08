# Install guide

This guide takes you from a blank Debian 13 server to Claude talking to your Gorelo. Follow the steps in order. Every command can be copied and pasted. Each step says what you should see, and what to do if you do not.

Time needed: about 45 minutes.

Conventions:

- In a code block, a line that starts with `#` is a comment for you. You can paste it along with the rest.
- You run everything as `root` (log in as root, or run `sudo -i` first). The guide says when a command runs as another user.
- `your-host` is the full name you will use for the server, for example `mcp.example.com`. Replace it with your own.

## 0. What you need

| You need | Notes |
|---|---|
| A Gorelo API key | Create it in Gorelo (Settings, API keys). Give it read and write scopes for the areas you want Claude to use: clients, contacts, tickets, assets, time, billing (invoices, items, taxes, contracts), alerts and uptime. Only add the `Project` and `Forms` scopes if you will switch those toolsets on (step 8). Start with fewer scopes: you can add more later. The key is shown once, so copy it somewhere safe. |
| A Debian 13 server | A VM, an LXC container or a cloud VPS. 1 CPU, 1 GB of RAM and 5 GB of disk are plenty. Root access and outbound internet. |
| A domain on Cloudflare | The domain's DNS must be managed by Cloudflare (a free account is fine). You will create a tunnel and a hostname such as `mcp.example.com`. |
| A claude.ai plan with custom connectors | Check Settings, Connectors in claude.ai. On team and enterprise plans an owner may need to allow custom connectors. |

If the exact name of a scope on the Gorelo key screen differs from this list, pick the one for the same area. If a tool later says a scope is missing, the error names it (see [TROUBLESHOOTING.md](TROUBLESHOOTING.md)).

## 1. Prepare the server

### 1.1 Update the system and add the basics

```bash
apt update && apt full-upgrade -y
apt install -y python3 python3-venv curl ca-certificates git openssl nano nftables unattended-upgrades
```

You should see apt finish without errors. If it asks to restart services, accept the defaults.

### 1.2 Turn on automatic security updates

This is recommended, and it is part of the setup. With Debian's default configuration, `unattended-upgrades` installs security fixes and Debian stable point-release updates by itself, and it does not reboot the server on its own. You choose when to reboot (for example `systemctl reboot` in a quiet hour after an update that needs it).

```bash
dpkg-reconfigure -plow unattended-upgrades
```

Choose Yes. Check it:

```bash
systemctl is-enabled apt-daily-upgrade.timer
```

You should see `enabled`.

### 1.3 Firewall: drop everything coming in

This server never needs an open port. The service listens only on the local machine (`127.0.0.1:8765`) and Cloudflare Tunnel connects outward. So the firewall can refuse all incoming connections.

Warning: if you manage this server over SSH, the rule below keeps port 22 open so you do not lock yourself out. If you reach the server only from a console (a hypervisor console or the provider's web console), you can delete the `tcp dport 22 accept` line to close SSH as well. If your SSH server listens on another port, change 22 to that port before you load the rules.

```bash
cat > /etc/nftables.conf <<'EOF'
#!/usr/sbin/nft -f
flush ruleset

table inet filter {
  chain input {
    type filter hook input priority filter; policy drop;
    ct state established,related accept
    ct state invalid drop
    iif "lo" accept
    ip protocol icmp accept
    meta l4proto ipv6-icmp accept
    tcp dport 22 accept
  }
  chain forward {
    type filter hook forward priority filter; policy drop;
  }
}
EOF
nft -c -f /etc/nftables.conf && echo "syntax ok"
```

You should see `syntax ok`. If you see an error, check that you pasted the whole block. Now load it and make it permanent:

```bash
systemctl enable --now nftables
nft list ruleset | head -n 8
```

You should see `table inet filter` with `policy drop`. Keep your current SSH session open and open a second SSH session to confirm you can still log in before you close the first.

On a cloud VPS you may also have a firewall in the provider's panel. Leaving it closed to everything except SSH is fine.

### 1.4 Create the service user

The server runs as its own user with no password and no shell. It cannot change the code.

```bash
useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin gorelo-mcp
install -d -m 0755 -o root -g root /opt/gorelo-mcp
id gorelo-mcp
```

You should see a line starting `uid=` with `gorelo-mcp`. If you see `useradd: user 'gorelo-mcp' already exists`, that is fine.

## 2. Install uv and Python, and get the code

### 2.1 Check Python

```bash
python3 --version
```

You should see `Python 3.13.x` (Debian 13). Anything 3.11 or newer works, but the commands below name `/usr/bin/python3.13`. If you have a different version, replace `3.13` in the commands that use it.

### 2.2 Install uv

`uv` is the tool that builds the Python environment exactly as the project locked it. You install it for root only.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
/root/.local/bin/uv --version
```

You should see a version number such as `uv 0.x.y`. If `curl` fails, check that the server has internet access (`curl -I https://astral.sh`).

### 2.3 Get the code

```bash
git clone https://github.com/Coach40Oz/GoreloMCP.git /opt/gorelo-mcp/app
```

You should see `Cloning into '/opt/gorelo-mcp/app'...` and no error.

The code stays owned by root, and the service can only read it. That way, a flaw in the server cannot rewrite the server. Only two folders are writable by the service. Create them:

```bash
cd /opt/gorelo-mcp/app
chown -R root:root /opt/gorelo-mcp/app
install -d -m 0700 -o gorelo-mcp -g gorelo-mcp /opt/gorelo-mcp/app/.oauth-state
install -d -m 0755 -o gorelo-mcp -g gorelo-mcp /opt/gorelo-mcp/app/.state
```

Both folders must exist before the service starts, or systemd refuses to start it (status `226/NAMESPACE`).

Use a released version, not the moving development branch. Releases are git tags; the first public release is `v1.0.0`. Check out the newest tag:

```bash
cd /opt/gorelo-mcp/app
git tag --sort=-v:refname | head -n 5
TAG=$(git tag --sort=-v:refname | head -n 1)
git checkout "$TAG"
git describe --tags
```

You should see a list of tags with the newest first, and `git describe --tags` printing the same name as the first one (`v1.0.0` or newer). Write that name down: it is the version to go back to if an update goes wrong (step 9.2). If the list is empty, you cloned something other than this project's repository; stop and check the address.

### 2.4 Build the Python environment (as root)

```bash
cd /opt/gorelo-mcp/app
UV_PYTHON=/usr/bin/python3.13 UV_PYTHON_DOWNLOADS=never /root/.local/bin/uv sync --frozen
```

You should see uv installing a list of packages and no error. `--frozen` means "install exactly what the project locked, change nothing". Check:

```bash
/opt/gorelo-mcp/app/.venv/bin/python -c "import fastmcp, httpx; print('imports ok')"
```

You should see `imports ok`. If uv complains that it cannot find Python 3.13, run `ls /usr/bin/python3*` and use the version you have in `UV_PYTHON`.

Why root builds it: the service user cannot write to the code folder, so nothing can swap the installed packages behind your back.

### 2.5 Optional: run the offline tests

The tests use no network and need no Gorelo account. They are for developers and are not needed to run the server. If you want to run them, do it in a separate clone, never in `/opt/gorelo-mcp/app`:

```bash
git clone https://github.com/Coach40Oz/GoreloMCP.git ~/gorelo-mcp-test
TAG=$(git -C /opt/gorelo-mcp/app describe --tags)   # the tag you installed in step 2.3
git -C ~/gorelo-mcp-test checkout "$TAG"
```

The first run needs network (it fetches the test tools): leave out `UV_OFFLINE=1` once.

Then follow "Run the offline tests" in [CONTRIBUTING.md](../CONTRIBUTING.md) from that folder, with `/root/.local/bin/uv` (or run `source /root/.local/bin/env` first so that `uv` is on your PATH).

## 3. Configure `.env`

The `.env` file holds your secrets. Copy the template with the right owner and permissions (root owns it, the service reads it through its group, nobody else can):

```bash
install -m 0640 -o root -g gorelo-mcp /opt/gorelo-mcp/app/.env.example /opt/gorelo-mcp/app/.env
```

### 3.1 Make a strong password

```bash
openssl rand -hex 24
```

You should see 48 random letters and digits. Copy it into your password manager. This is the password you type once when you connect Claude. Letters and digits only means no quoting problems in the file.

### 3.2 Edit the file

```bash
nano /opt/gorelo-mcp/app/.env
```

Fill in each value after the `=` sign. No quotes, no spaces.

| Variable | Required | What to put |
|---|---|---|
| `GORELO_API_KEY` | yes | The raw Gorelo API key. No `Bearer ` in front. |
| `PUBLIC_BASE_URL` | yes | The public address of this server, for example `https://mcp.example.com`. No `/mcp` and no trailing slash. It is the address Claude signs in at. If you change it later, Claude must sign in again. |
| `MCP_AUTH_PASSWORD` | yes | The password from 3.1. Use 16 characters or more (the service warns if it is shorter). |
| `GORELO_TOOLSETS` | no | Leave blank for the default: `core,tickets,time,billing,uptime`. See step 8. |
| `GORELO_ENABLE_DESTRUCTIVE` | no | Leave blank (off). See step 8. |

Save with Ctrl+O, Enter, then exit with Ctrl+X.

Check the owner and mode:

```bash
stat -c '%U:%G %a %n' /opt/gorelo-mcp/app/.env
```

You should see `root:gorelo-mcp 640 /opt/gorelo-mcp/app/.env`. If not, repeat the `install` command above only if the file is still the empty template, or fix it with `chown root:gorelo-mcp /opt/gorelo-mcp/app/.env && chmod 0640 /opt/gorelo-mcp/app/.env`.

Never paste the file contents into a chat, ticket or screenshot.

## 4. Install the service and start it

### 4.1 Install the unit and the sandbox drop-in

The unit file tells systemd how to run the server. The drop-in adds extra restrictions (it cannot see devices, change the clock, load kernel modules and so on).

```bash
install -m 0644 /opt/gorelo-mcp/app/deploy/gorelo-mcp.service /etc/systemd/system/gorelo-mcp.service
install -d -m 0755 /etc/systemd/system/gorelo-mcp.service.d
install -m 0644 /opt/gorelo-mcp/app/deploy/gorelo-mcp.service.d/10-sandbox.conf /etc/systemd/system/gorelo-mcp.service.d/10-sandbox.conf
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/gorelo-mcp.service
```

`systemd-analyze verify` should print nothing. If it prints warnings about the drop-in in an LXC container, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#service-will-not-start).

### 4.2 Start it

```bash
systemctl enable --now gorelo-mcp.service
sleep 5
systemctl status gorelo-mcp.service --no-pager | head -n 12
```

You should see `Active: active (running)`.

### 4.3 Read the first log lines

```bash
journalctl -u gorelo-mcp.service --since "5 minutes ago" --no-pager
```

You should see a line like `gorelo-mcp ready: toolsets=billing,core,tickets,time,uptime destructive=False tools=58` and a line containing `oauth state loaded`. If you see `refusing to start`, the line names every setting that is wrong: fix `.env` and run `systemctl restart gorelo-mcp.service`.

### 4.4 Check the local endpoint

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/mcp
```

You should see `401`. That is the correct, healthy answer: the endpoint refuses anyone who has not signed in. If you see `000`, the service is not running (go back to 4.3).

## 5. Publish it with a Cloudflare Tunnel

A tunnel makes your server reachable at `https://your-host` through an outbound connection from the server to Cloudflare. TLS is handled by Cloudflare.

### 5.1 Install cloudflared from Cloudflare's apt repository

```bash
mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | tee /etc/apt/sources.list.d/cloudflared.list
apt-get update && apt-get install cloudflared
cloudflared --version
```

Answer Y if apt asks to continue. You should see a version line. If apt cannot find the package, or reports a signature error (`NO_PUBKEY` or `EXPKEYSIG`), compare the key and repository lines above with Cloudflare's current install page at pkg.cloudflare.com, as they may have changed.

### 5.2 Create the tunnel in the dashboard

1. Log in to the Cloudflare dashboard and open Zero Trust and find Tunnels (under Networks).
2. Create a tunnel of type Cloudflared. Name it `gorelo-mcp`.
3. When Cloudflare shows install commands, do NOT run them. Copy only the long token (the text after `--token` or `service install`).
4. On the Public Hostname (or Published application) tab, add a hostname: subdomain and domain such as `mcp.example.com`, service type `HTTP`, URL `127.0.0.1:8765` (that is `http://127.0.0.1:8765`).
5. Save.

The hostname you choose here must match `PUBLIC_BASE_URL` in `.env` (with `https://` in front).

### 5.3 Store the token in a root-only file

The token is a secret that lets someone run your tunnel. Keep it out of the command line (which other users can see and the shell history keeps). Open the file in an editor and paste it there:

```bash
install -d -m 0755 /etc/cloudflared
install -m 0600 -o root -g root /dev/null /etc/cloudflared/tunnel.env
nano /etc/cloudflared/tunnel.env
```

The file must hold exactly one line (replace the text after `=` with your token, no quotes):

```
TUNNEL_TOKEN=paste-the-token-here
```

Save and exit.

### 5.4 Create the tunnel service

This replaces `cloudflared service install`, which would put the token into the unit file.

```bash
useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin cloudflared
cat > /etc/systemd/system/cloudflared.service <<'EOF'
[Unit]
Description=Cloudflare Tunnel for the Gorelo MCP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=cloudflared
EnvironmentFile=/etc/cloudflared/tunnel.env
ExecStart=/usr/bin/cloudflared --no-autoupdate tunnel run
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now cloudflared.service
sleep 8
systemctl status cloudflared.service --no-pager | head -n 8
```

You should see `Active: active (running)`. The EnvironmentFile is read by systemd as root, so the `cloudflared` user never needs to read the token file. In the Cloudflare dashboard the tunnel should show as Healthy within a minute. If the service stops at once, run `journalctl -u cloudflared.service --no-pager | tail -n 20`. An error about the token usually means the token was copied incompletely.

### 5.5 Check the public address

Replace `your-host` with your hostname:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://your-host/mcp
```

You should see `401`, the same as locally. If you see `530` or `502` (the page may mention Cloudflare error 1033), the tunnel is not connected or the hostname does not point to `http://127.0.0.1:8765`; see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#claude-says-it-cannot-connect).

### 5.6 Check that the server sees the visitor's address

The password limits count wrong passwords per visitor address. They only work if the server sees the real address of each visitor and not the tunnel's `127.0.0.1`. Ask the public address for the sign-in page without any parameters (it refuses the request, which is what you want) and read the journal line it left:

```bash
curl -s -o /dev/null https://your-host/authorize
journalctl -u gorelo-mcp.service --since "1 minute ago" --no-pager | grep "authorize outcome="
```

You should see a line such as `authorize outcome=invalid_request reason=... client=- ip=203.0.113.7`, where the `ip=` value is a public address (here your own server's, or the address you ran `curl` from), never `127.0.0.1`. If it shows `ip=127.0.0.1`, stop and read "The journal shows ip=127.0.0.1" in [TROUBLESHOOTING.md](TROUBLESHOOTING.md#the-journal-shows-ip127001) before you go on. Once you have connected Claude in step 6, the lines `authorize outcome=consent_shown` and `authorize outcome=approved` carry the same `ip=` field and should show the address you connected from.

## 6. Connect claude.ai

1. Open claude.ai, then Settings, Connectors.
2. Choose Add custom connector.
3. Name: `Gorelo`. URL: `https://your-host/mcp` (with `/mcp` at the end).
4. Add it and choose Connect. A password page from your server opens.
5. Enter the `MCP_AUTH_PASSWORD` you made in step 3 and approve.
6. Start a new conversation, enable the connector for it, and ask: "Run health_check".

You should see a result with `ok: true`, `api: reachable`, a client count, and the toolsets that are on. If you see `ok: false`, the message names the problem (usually the API key).

Five wrong passwords from one address in 15 minutes lock that address out for a while. Copy the password from your password manager instead of typing it.

## 7. Optional: the daily API watcher

Gorelo announces API changes in its public changelog. The watcher runs once a day, compares the changelog and the live API description with the one this server was built for, and posts one alert in Gorelo when something changed. It is optional but recommended.

### 7.1 Create the watcher key file

The watcher gets its own small file, with only the API key. It may be the same key as the server's, or a separate one that can create alerts.

```bash
install -d -m 0755 /etc/gorelo-mcp
install -m 0600 -o root -g root /opt/gorelo-mcp/app/deploy/watcher.env.example /etc/gorelo-mcp/watcher.env
nano /etc/gorelo-mcp/watcher.env
```

Replace `replace-with-the-gorelo-api-key` with the key. Save and exit.

### 7.2 Create `site.local.toml`

The watcher posts its alert against a Gorelo client of yours, for example an internal client named after your own company. Find that client's numeric id in Gorelo (it is in the address bar when you open the client).

```bash
cp /opt/gorelo-mcp/app/site.example.toml /opt/gorelo-mcp/app/site.local.toml
nano /opt/gorelo-mcp/app/site.local.toml
```

The watcher needs only one value: `alert_client_id` under `[watcher]`. Set it to your client id (a whole number greater than zero) and leave the other sections out or delete them. The other sections belong to the live test harness, which is for developers; a watcher-only install does not need them. The file is refused if it is the example file itself or if a value is still the placeholder from the example (for instance `999999002`). The example values are numbers that cannot be real tenant ids, and they are refused on purpose.

Install it owned by root, readable by the service group:

```bash
chown root:gorelo-mcp /opt/gorelo-mcp/app/site.local.toml
chmod 0640 /opt/gorelo-mcp/app/site.local.toml
stat -c '%U:%G %a %n' /opt/gorelo-mcp/app/site.local.toml
```

You should see `root:gorelo-mcp 640 /opt/gorelo-mcp/app/site.local.toml`.

### 7.3 Install the watcher units

```bash
cd /opt/gorelo-mcp/app/deploy
install -m 0644 gorelo-changelog-watch.service /etc/systemd/system/gorelo-changelog-watch.service
install -m 0644 gorelo-changelog-watch.timer /etc/systemd/system/gorelo-changelog-watch.timer
install -d -m 0755 /etc/systemd/system/gorelo-changelog-watch.service.d
install -m 0644 gorelo-changelog-watch.service.d/10-sandbox.conf /etc/systemd/system/gorelo-changelog-watch.service.d/10-sandbox.conf
systemctl daemon-reload
systemctl enable --now gorelo-changelog-watch.timer
systemctl list-timers gorelo-changelog-watch.timer --no-pager
```

You should see the timer with a `NEXT` time within about a day.

### 7.4 Run it once

```bash
systemctl start gorelo-changelog-watch.service || true
systemctl show -p ExecMainStatus --value gorelo-changelog-watch.service
journalctl -u gorelo-changelog-watch.service --since "5 minutes ago" --no-pager
```

The `show` command prints the script's exit code: 0 nothing new, 1 an API change was found, 2 news that is not about the API, 3 the check failed. The first run only records a baseline, so you should see `0` and no error lines. If the service fails with `ERROR`, the line names the problem (a missing or placeholder `alert_client_id` in `site.local.toml` is the most common one; the exit code is then 3). If the start command printed a failure but the code is 0, the script never ran: read the journal (usually a missing `/etc/gorelo-mcp/watcher.env`). Notes on what an alert means are in [OPERATIONS.md](OPERATIONS.md#when-the-watcher-reports-an-api-change).

Note: if you installed this tree from a release that is older or newer than the live Gorelo API, the first run may alert once. That is the check doing its job.

## 8. Optional: toolsets and the destructive switch

### 8.1 Toolsets

`GORELO_TOOLSETS` in `.env` chooses which groups of tools exist. Blank means `core,tickets,time,billing,uptime`.

| Toolset | In the default set | Covers |
|---|---|---|
| `core` | yes | clients, locations, contacts, assets, groups, users, alerts, `health_check` |
| `tickets` | yes | tickets, comments, side conversations, approvals, attachments |
| `time` | yes | time entries, billing roles, work types |
| `billing` | yes | invoices (Draft), items, categories, taxes, contracts |
| `uptime` | yes | uptime checks and maintenance windows |
| `projects` | no | projects, sections, tasks (needs the `Project` scope on the key) |
| `forms` | no | forms, responses, submission links (needs the `Forms` scope on the key) |

Examples: `GORELO_TOOLSETS=all` turns every toolset on. `GORELO_TOOLSETS=core,tickets` gives a smaller set. Fewer tools means a smaller risk and a shorter list for Claude to read.

### 8.2 The destructive switch

By default the delete and void tools, and the tool that creates an Approved invoice, do not exist. `GORELO_ENABLE_DESTRUCTIVE=1` adds them. Every call still needs `confirm=true`, and Claude is told to ask you first. An Approved invoice is sent to your connected accounting system at once and may email its recipients, and voiding it in Gorelo does not void the accounting system's copy. Switch this on only if you need it. Some operations (deleting clients, contacts, tickets, assets and contracts) can never be called, whatever you set.

### 8.3 Apply the change

```bash
nano /opt/gorelo-mcp/app/.env
systemctl restart gorelo-mcp.service
journalctl -u gorelo-mcp.service --since "1 minute ago" --no-pager | grep ready
```

You should see the `ready:` line with your toolsets and the `destructive=` flag. Start a new conversation in Claude to see the new tool list. A wrong value stops the service with an error naming it.

The full list of tools is in [tool-catalog.md](tool-catalog.md).

## 9. Updating to a new version, and rolling back

Before updating, take a backup (see [OPERATIONS.md](OPERATIONS.md#backups)). Updates are done as root in the code folder.

### 9.1 Update

First write down the version you run now (this is what you roll back to), then look for newer tags:

```bash
cd /opt/gorelo-mcp/app
git describe --tags
git fetch --tags
git tag --sort=-v:refname | head -n 5
```

If the newest tag is the one you already run, there is nothing to do. If there is a newer one, check what the new version changes before you stop anything:

```bash
NEW=$(git tag --sort=-v:refname | head -n 1)
git diff --stat HEAD "$NEW"
```

If only files under `docs/` and `*.md` are listed, run `git checkout "$NEW"` and stop there: nothing needs stopping, installing or restarting. Otherwise:

```bash
systemctl stop gorelo-mcp.service
git checkout "$NEW"
UV_PYTHON=/usr/bin/python3.13 UV_PYTHON_DOWNLOADS=never /root/.local/bin/uv sync --frozen
git diff --stat HEAD@{1} HEAD -- deploy/
```

If the `git diff --stat` line lists files under `deploy/`, the new version changed the service files: install them again as in step 4.1 (and 7.3 if you use the watcher) and run `systemctl daemon-reload` now, before you start the service.

```bash
systemctl start gorelo-mcp.service
sleep 5
journalctl -u gorelo-mcp.service --since "1 minute ago" --no-pager | grep -E "ready|refusing"
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/mcp
```

You should see the `ready:` line and `401`.

Read the release notes (the commit messages or the GitHub release page) for any steps specific to the version.

Then start a new conversation in Claude, since tool lists are read when a conversation starts.

### 9.2 Roll back

Use the tag you wrote down before the update (`PREVIOUS_TAG` below, for example `v1.0.0`). `git tag --sort=-v:refname` lists the tags that exist.

```bash
cd /opt/gorelo-mcp/app
systemctl stop gorelo-mcp.service
git checkout PREVIOUS_TAG
UV_PYTHON=/usr/bin/python3.13 UV_PYTHON_DOWNLOADS=never /root/.local/bin/uv sync --frozen
git -C /opt/gorelo-mcp/app diff --stat HEAD@{1} HEAD -- deploy/
```

If that last command lists files, the service files differ between the two versions: install them again as in step 4.1 (and 7.3 if the watcher is installed), then run `systemctl daemon-reload` before you start the service.

```bash
systemctl start gorelo-mcp.service
sleep 5
journalctl -u gorelo-mcp.service --since "1 minute ago" --no-pager | grep -E "ready|refusing"
```

If you forgot the old tag, `git reflog | head` shows where you were. The sign-in data in `.oauth-state` is kept across versions. If either version refuses to start because of the sign-in data file, do not delete it: see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#service-will-not-start).

## 10. Uninstall

Stop everything first, so that nothing is deleted while it runs. The second and third lines are for the optional watcher of step 7 and do nothing if you never installed it:

```bash
systemctl disable --now gorelo-mcp.service cloudflared.service
systemctl disable --now gorelo-changelog-watch.timer 2>/dev/null || true
systemctl stop gorelo-changelog-watch.service 2>/dev/null || true
rm -f /etc/systemd/system/gorelo-mcp.service /etc/systemd/system/gorelo-changelog-watch.service /etc/systemd/system/gorelo-changelog-watch.timer /etc/systemd/system/cloudflared.service
rm -rf /etc/systemd/system/gorelo-mcp.service.d /etc/systemd/system/gorelo-changelog-watch.service.d
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true
rm -rf /opt/gorelo-mcp /etc/gorelo-mcp /etc/cloudflared
userdel gorelo-mcp
userdel cloudflared
```

Then:

1. In Gorelo, delete the API key (and the watcher's key if separate).
2. In the Cloudflare dashboard, delete the tunnel and its hostname record.
3. In claude.ai, Settings, Connectors, remove the connector.
4. Delete `/opt/gorelo-mcp-backups` and any off-server copies of the backups, including the copies of `.env` and `.oauth-state`, which hold secrets.
5. Optional: if you made a test checkout, run `rm -rf ~/gorelo-mcp-test`. If nothing else on the server uses uv, run `rm -rf ~/.local/bin/uv ~/.local/bin/uvx ~/.local/bin/env ~/.local/bin/env.fish ~/.config/uv ~/.cache/uv`, and delete the line that sources `~/.local/bin/env` from `~/.profile` and `~/.bashrc`.

If you want to remove cloudflared itself, remove the package and delete `/etc/apt/sources.list.d/cloudflared.list` and `/usr/share/keyrings/cloudflare-main.gpg`:

```bash
apt remove -y cloudflared
rm -f /etc/apt/sources.list.d/cloudflared.list /usr/share/keyrings/cloudflare-main.gpg
```
