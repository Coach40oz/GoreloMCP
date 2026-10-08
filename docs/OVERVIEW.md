# How the Gorelo MCP server works

This is the one place that explains how the server works, so that a person or an AI assistant does not have to read the code first. It describes mechanisms, not one deployment: "the operator" is whoever runs the server and owns its `.env`, "the test client" is a dedicated client in the Gorelo tenant that live tests may write to, and "the public hostname" is the address the AI client connects to. To install the server see `docs/INSTALL.md`, to run it day to day see `docs/OPERATIONS.md`, and when something is wrong see `docs/TROUBLESHOOTING.md`. The reasons behind the design are in `docs/why/README.txt`, and where production differed from the published spec is in `docs/API-OBSERVED-BEHAVIOR.md`. Read the code only when these do not answer.

Facts in this file are pinned to the code by `tests/test_overview_docs.py`. File paths, tool counts, module coverage and the numbers quoted below fail that test when they drift, so a change to the code and a change to this file go together.

In this file: 1. what it is and the request path, 2. the module map, 3. the safety layers, 4. the spec index and live overrides, 5. testing, 6. operations, 7. where to look for what.

## 1. What it is and the request path

**What it is.** A self-hosted MCP (Model Context Protocol) server, written in Python on FastMCP and httpx, that gives an AI client (claude.ai, through a custom connector) tools for the Gorelo PSA Public API at `https://api.usw.gorelo.io/v1`: tickets, clients, contacts, assets, time, billing and uptime, and optionally projects and forms. One process serves HTTP on the loopback interface, owns one shared Gorelo client and registers only the tools the operator has switched on. It works on a live PSA with real client data, so it is built to refuse instead of guess: every request is checked against an index of Gorelo's OpenAPI spec before it is sent, a fixed list of operations can never be called, deletes and other irreversible tools exist only when the operator enables them and then need a per-call `confirm`, and a write whose outcome is unclear is reported as unclear and never retried.

### Terms

| Term | Meaning |
|---|---|
| Toolset | A named group of tools the operator switches on or off: `core`, `tickets`, `time`, `billing`, `uptime`, `projects`, `forms` (`settings.TOOLSETS`). |
| Kind | Declared on every tool with `@gorelo_tool`. `read` changes nothing in Gorelo. `write` creates or changes data (an `update_*` or `set_*` tool that overwrites or clears data also sets the MCP destructive hint). `destructive` is a gated tool: it is registered only when the operator enables it and every call needs `confirm=true`. These are the delete and void tools and `create_approved_invoice`. |
| Operation key (op key) | `METHOD /v1/path` spelled exactly as `spec/spec_index.json` spells it, for example `GET /v1/tickets/{ticketId}`. Every request is named by one. |
| Declared ops | The op keys a tool lists in `ops=[...]`. While a tool runs it can send those and nothing else. |
| Shape | An op key with every `{placeholder}` written `{}`. Forbidden operations and side-effect GETs are compared by shape, so Gorelo renaming a placeholder cannot reopen one. |
| Envelope | Gorelo's response wrapper: `StatusCode`, `IsSuccess`, `Data`, `DataContext`, `Notifications`. |
| Unconfirmed write | A write that may or may not have been applied (timeout, connection error, 5xx, an answer that cannot be used). It is flagged `write_unconfirmed` and never retried. |

### The request path

| Hop | What happens | File and function |
|---|---|---|
| 1. Connector | The AI client calls `https://<public hostname>/mcp` (Streamable HTTP) with an OAuth bearer token. The address is the `PUBLIC_BASE_URL` setting plus `/mcp`. | Outside this repository. |
| 2. Tunnel | A Cloudflare Tunnel (`cloudflared`, token mode) ends TLS and forwards to the loopback port. There is no reverse proxy, and the server binds `127.0.0.1:8765` only. | Host setup, see `docs/INSTALL.md` step 5; `main.py` (`HOST`, `PORT`). |
| 3. Process start | `main()` loads `.env`, sets up logging, installs the log value filter, reads the settings, builds the OAuth provider and the FastMCP server, and serves HTTP. A settings error, an OAuth state file that cannot be trusted or a login gate that cannot be made safe is logged (the line ends in "refusing to start") and the process exits with status 1 before it serves anything. | `main.py`: `main`. |
| 4. OAuth and password page | A request to `/mcp` without a valid bearer token gets 401. A client registers itself (dynamic client registration: open, but its redirect addresses are checked and its rate is limited) and is sent to `/authorize`, which shows a consent page asking for the operator's password. The page checks the whole request (a registered client, an allowed redirect address, PKCE) before it looks at the password, and every invalid request gets the same answer. After the right password the framework issues an authorization code and then the tokens. When an access token expires the client sends its refresh token to `/token` and gets a new pair, with no password and no consent page. | `personal_auth.py`: `PersonalAuthProvider`, `get_routes`, `_make_authorize_endpoint`; its rules are in `oauth_guard.py` and its state in `oauth_store.py`. |
| 5. FastMCP server | `build_server` made the `FastMCP` object at startup: the instructions, one `GoreloClient` owned by the FastMCP lifespan, and the selected tools. For each call FastMCP checks the arguments against the tool's signature. Ids and `confirm` are strict types, so JSON `true` or `"5"` never become an id and `"true"` or `1` never confirm. | `server.py`: `build_server`; `tools/_common.py`: `StrictId`, `StrictBool`. |
| 6. Tool wrapper | The `@gorelo_tool` wrapper marks the tool as the running one (`CURRENT_TOOL`), runs it, and turns a `GoreloAPIError`, a `SpecViolation` or a `ValueError` into a `ToolError` whose text names the snake_case parameter to fix. Only the outermost tool translates errors. | `tools/_common.py`: `Registry.tool`, `_translate_errors`, `format_gorelo_error`. |
| 7. Tool function | Validates and normalizes its parameters (`positive_id`, `guid`, `utc_iso`, `region_code`), calls `require_confirm` before any HTTP when it is gated, builds the request body with `build_body` and calls one `GoreloClient` helper with `tool="<its name>"`. | `tools/<domain>.py`. |
| 8. Gorelo client | Checks, sends and parses one request. The order of the checks is below. | `gorelo_client.py`: `GoreloClient.request`. |
| 9. Gorelo API | Answers with an envelope. A 429 answer is not an envelope: the client reads `Retry-After` from its header or its body. | `https://api.usw.gorelo.io/v1`. |
| 10. After a write | The tool checks the answer, usually reads the record back, and returns it. See "After a write" below. | `tools/_common.py`: `created_id`, `expect_object`, `reread_after_write`. |

### Inside the Gorelo client

`GoreloClient.request` does these steps in this order. Everything before step 4 happens before any HTTP.

1. Refuse a forbidden operation (error kind `forbidden`), then an operation the running tool did not declare (kind `spec`). Calls made outside any tool (tests, scripts) skip the second check.
2. Look the op key up in the spec index; an unknown one is refused (kind `spec`).
3. Validate path ids (`spec.validate_path_param`: uuid, integer or plain token, sent in canonical form and percent-encoded), query names (`spec.normalize_query`, also for filters that are unset) and the body (`spec.validate_body`: field names in exact case, and the shape of each value).
4. Send with the `X-API-Key` header, at most 4 requests in flight and a 30 s timeout per request. First compare the path on the wire with the path that was validated (`_check_path_unchanged`).
5. Retry a 429 (Gorelo did not process the request) up to 3 times within about 20 s, honoring `Retry-After`. Nothing else is retried, not a timeout, not a connection error, not a 5xx.
6. Parse the answer. It must be a Gorelo envelope with a boolean `IsSuccess`; anything else is a `shape` error ("refusing to guess"), never `[]` or `{}`. An envelope with a non-2xx status is kind `http`, a 2xx envelope with `IsSuccess` false is kind `envelope`, a persistent 429 is `rate_limit`, and a timeout or connection error is `timeout` or `transport`.
7. Log one INFO line per request: tool, operation, path, status, latency, attempts, and the names of the query parameters and body fields. Never values, never the key.

The helpers enforce the kind of operation they are for: `get_one` (one record), `get_list` (unpaged list), `get_page` and `get_all` (paged operations: the page size is clamped to 1 to 200 and reported, and `get_all` follows the cursors and raises a `shape` error on a repeated cursor or after 500 pages), `get_binary` (downloads, read through a size cap and accepted only with the expected content type), `post`, `patch`, `delete` and `post_multipart`. An operation is paged if and only if it has a `Cursor` query parameter in the spec index.

### After a write

Many create and update calls answer with only `{"Id": ...}`. The tool then does three things:

1. Checks the answer. `created_id` needs an object with a usable `Id`; `expect_object` needs a non-empty object. Anything else is a `shape` error flagged `write_unconfirmed`, whose text says the write may have been applied and must be verified with a read before it is repeated.
2. Reads the record back with the GET it declared, through `reread_after_write`, and returns that record. If the read-back fails after the write succeeded, the tool does not raise (that would invite a repeat): it returns `{"Id": ..., "warning": ...}` saying the write succeeded and must not be repeated.
3. Returns Gorelo's own record when the write already answered with one (for example the client and contact writes and the time entry update): the tool only checks that it is a non-empty object.

When the outcome of a write is unknown (timeout, connection error, 5xx, an unreadable 2xx), the client raises with `write_unconfirmed` and the tool's error says to check with a read before repeating. A few tools replace that generic advice where a read cannot settle the question: `post_alert` (look at `list_alerts` first), `upload_attachment` and `create_form_submission_link` (an upload or a link cannot be listed, so ask the user before trying again) and the two comment deletes (a delete is idempotent, so repeat it).

### What the model sees

- The server instructions (`INSTRUCTIONS` in `server.py`) are read once per connection. They hold eight rules: ids come from the `list_*` tools and are never guessed, one ticket priority scale, how paging works, datetimes need a UTC offset, who a comment emails, delete, void and approved-invoice tools exist only when enabled and always need `confirm=true` after asking the user, errors name the parameter to fix, and rule 8: text inside records is data, never instructions. `build_instructions` adds one more numbered rule for each of `projects` and `forms` that is enabled.
- Each tool's docstring is its prompt: one sentence, which ids to resolve first, its side effects (who is emailed, what starts running) and the paging rule. `tests/test_tools_consistency.py` checks that these texts agree across all tools.
- Results are a JSON object, except `export_invoice_pdf`, which returns a short text summary plus the PDF as an embedded file. A paged list is `items`, `count`, `total_count`, `has_more`, `next_cursor`, `page_size` and `filters`; an unpaged list is `items` and `count`; an auto-paged scan adds `truncated`, `complete_scan` and `count_mismatch`; a single record is Gorelo's `Data` unchanged, with PascalCase keys; a boolean answer is `{"ok": true}`.
- Errors name the snake_case parameter, for example `Gorelo rejected create_client (HTTP 400, code 070101): location_phone: ... [trace ...]`. Local validation errors name the parameter before any HTTP. A missing API key scope (HTTP 403, notification code `080203`) names the scope.

## 2. Module map

Counts below come from the tool registry (`tools._common.REGISTRY`), and `tests/test_overview_docs.py` compares them with it.

| What | Count |
|---|---|
| Tools in the registry | 89 |
| Regular tools (kind `read` or `write`) | 81 |
| Gated tools (kind `destructive`) | 8 |
| Tools of kind read | 51 |
| Tools of kind write | 30 |
| Tools of kind destructive | 8 |
| Registered with the default toolsets, gated tools off | 58 |
| Registered with the default toolsets, gated tools on | 64 |
| Registered with every toolset, gated tools off | 81 |
| Registered with every toolset, gated tools on | 89 |

| Toolset | In the default set | Tools | Gated | Modules |
|---|---|---|---|---|
| `core` | yes | 17 | 0 | meta, org, assets, alerts, clients, contacts |
| `tickets` | yes | 19 | 1 | tickets, conversations, attachments |
| `time` | yes | 7 | 1 | time_entries |
| `billing` | yes | 15 | 3 | invoices, catalog, contracts |
| `uptime` | yes | 6 | 1 | uptime |
| `projects` | no | 22 | 2 | projects, project_tasks |
| `forms` | no | 3 | 0 | forms |

### Core modules

- **`main.py`**: the entry point, thin on purpose. `main()` loads `.env`, sets up logging, installs the log value filter, reads `Settings`, builds the OAuth provider and the server, and serves Streamable HTTP on `127.0.0.1:8765`. Host and port are constants in the file, not settings, so configuration cannot move the listener off the loopback interface. It refuses to start, with one error line that ends in "refusing to start" and exit status 1, on a settings error, on a state file that cannot be trusted (`StateFileError`) and on a login gate that cannot be made safe (`GateError`). Importing the module has no side effects.
- **`server.py`**: `build_server(settings, auth=None, transport=None, event_hooks=None, spec=None, registry=None)` returns the FastMCP server. It selects the tools (`Registry.select`), checks their ops against the spec (`_verify_ops`), builds the instructions (`build_instructions`), shrinks the advertised input schemas without changing what the tools accept (`compact_input_schema`) and gives every tool the one shared `GoreloClient` through the FastMCP lifespan (`tools._common.client_of`). The same module holds the log value filter. It makes no network call and writes no file; the optional arguments are how tests and the live harness inject a mock transport, request hooks or a registry. At startup it logs one ready line: `gorelo-mcp ready: toolsets=... destructive=... tools=N`.
- **`settings.py`**: `Settings.from_env` is the only place environment variables are read. It reports every problem in one `SettingsError`, so the operator fixes the environment in one pass. It owns `TOOLSETS`, `DEFAULT_TOOLSETS` and the variable names. The `repr` of the settings never shows the API key or the password.
- **`spec.py`**: loads `spec/spec_index.json` (`load_spec_index`, cached, re-read when the file changes) with `spec/live_overrides.json` applied on top (`apply_live_overrides`), and validates requests against it: `normalize_query`, `validate_body` and `validate_path_param`. A violation is a `SpecViolation` that names the field and lists the allowed names. It never touches the network.
- **`gorelo_client.py`**: all HTTP to Gorelo. `GoreloClient` owns one `httpx.AsyncClient`, applies the checks of section 1 and holds `FORBIDDEN_OPS` and `SIDE_EFFECT_GETS` with their shape matchers (`is_forbidden_op`, `is_side_effect_get`). Failures are `GoreloAPIError`, with a `kind`, Gorelo's notifications, the trace id and the `write_unconfirmed` flag.
- **`personal_auth.py`**: the OAuth 2.1 provider the AI client signs in through, built on FastMCP's in-memory provider. It enables dynamic client registration (open: registering needs no credential, but it is limited and checked), turns on `/revoke`, and replaces `/authorize` with a consent page that asks for the operator's password. The password is required: without one, or on a `fastmcp` or `mcp` version the gate was not checked against, the server refuses to start. One function issues every token, for a first sign-in and for a refresh alike: an access token (`pat_...`) and a refresh token (`prt_...`), paired. A first sign-in gets an access token valid for 30 days and a refresh token that never expires; a refresh gets an access token valid for 1 hour and a new refresh token, and the old refresh token is retired: the state keeps only its sha256 and its family. A retired refresh token that is presented again is logged (INFO within 5 minutes, which is a retry; WARNING later), and the policy that would also revoke its family exists but is off. An expired access token is refused and nothing else changes: its refresh token stays valid, so the client refreshes instead of signing in again. An authorization code is issued only inside a request that the consent endpoint approved after the password matched.
- **`oauth_store.py`**: the one place that reads, checks, migrates, prunes and writes the state file `.oauth-state/oauth_tokens.json`. A file that is not valid JSON, has the wrong shape or a format version it does not know raises `StateFileError`: the server refuses to start and the file is left exactly as it was, instead of starting with every session forgotten. One record that does not validate is skipped and counted. The format is versioned: version 1 is what earlier releases wrote, version 2 only adds keys (the family of each refresh token and the tombstones of retired ones), so the older code still reads it, and a version 1 file is migrated in memory and stays byte for byte as it was until the first save. Writes are atomic (a temporary file with mode 0600, fsync, rename, fsync of the directory) and happen under the exclusive lock on `.oauth-state/.lock`, which the server holds for as long as it runs, so there is never a second writer. Pruning removes access tokens and refresh tokens that expired more than 7 days ago and tombstones older than 90 days, and never a client.
- **`oauth_guard.py`**: the rules of the login gate, each one small, tested on its own and free of framework state. The failure limiter counts wrong passwords per client address (an IPv6 address as its /64; never per `client_id`, which the caller chooses): 5 wrong passwords per client address in 15 minutes, and 30 wrong passwords in an hour from all addresses, end in HTTP 429 for the right password too, and at most 4096 addresses are remembered. The registration limiter allows 10 registrations an hour per address and 50 a day overall, and at most 50 clients are stored (an idle one older than a day makes room). A body over 16 KiB is refused, whatever the method, on `/authorize`, `/token`, `/register` and `/revoke`, and an OPTIONS request that carries a body is refused with 400 (a CORS preflight has none); the cap is never applied on `/mcp`. One redirect validator decides what an acceptable redirect address is (https on the allowlist, or http to a loopback host that the allowlist names; no userinfo, fragment, backslash, space or control character), at registration and again at `/authorize`. Anything a visitor controls reaches the journal only as printable ASCII, escaped and shortened. Every consent response carries the same security headers. The fail-closed checks (`check_framework_versions`, `require_password`) stop the server from starting.
- **`tools/_common.py`**: the shared base every tool module builds on. The registry and `@gorelo_tool` (declaration checks, MCP annotations, error translation, `CURRENT_TOOL`), `Registry.select` (which tools a server gets), the strict types, the parameter helpers (`positive_id`, `guid`, `csv_ids`, `utc_iso`, `region_code`, `non_empty`, `clamp_page_size`, `require_confirm`), `build_body` (snake_case parameters to PascalCase fields; `None` is omitted; blank values are refused unless the tool offers an explicit clear), the answer checks (`created_id`, `expect_object`), `reread_after_write`, the result shapes (`paged_result`, `list_result`, `all_result`, `ok_result`) and `format_gorelo_error`. Tool modules must not add shared helpers here.
- **`tools/__init__.py`**: imports every tool module, so their decorators fill the registry. `server.build_server` relies on that import.

### Tool modules

Each module owns one domain and declares every operation its tools call, including the GET it uses to read a record back. Counts and toolsets are from the registry.

| Module | Tools | Toolset | What it covers and what to know |
|---|---|---|---|
| `tools/meta.py` | 1 | core | `health_check`: one `GET /v1/clients` page of size 1, plus what the server knows about itself (toolsets, gated flag, spec hash). The only tool that returns a Gorelo failure as data (`ok: false`) instead of raising. |
| `tools/org.py` | 2 | core | Organization groups (an unpaged list) and users (every page is read). |
| `tools/assets.py` | 3 | core | Agent assets and custom assets, read only. There is no delete: deleting an agent asset uninstalls the RMM agent, so it is forbidden. |
| `tools/alerts.py` | 2 | core | `post_alert` (a severity is required, 1 to 4, and strict; alerts cannot be deleted through the API, so an unknown outcome has its own advice) and `list_alerts` (paged). |
| `tools/clients.py` | 5 | core | Clients and their locations. Gorelo answers a client write with the whole record, so there is no read-back. A client field cannot be cleared through the API, so `update_client` has no clear option. |
| `tools/contacts.py` | 4 | core | Contacts. Gorelo's update replaces the whole contact, so `update_contact` reads it first and sends the complete command. Secondary emails cannot be read, so the tool refuses until the caller gives the full list or `clear_secondary_email_ok` after asking the user. |
| `tools/tickets.py` | 10 | tickets | Tickets and their lookups. Priority is one scale (0 None to 4 Low). `search_tickets` filters on the server and applies a few filters on the rows it read. Create and update answer `{Id}` and the ticket is read back. List fields are cleared only through `clear_fields`. `list_ticket_priorities` and `list_ticket_sources` are local tables with no HTTP. |
| `tools/conversations.py` | 8 | tickets | Comments, side conversations and approvals. A comment is Private by default; a Public one emails the ticket contact and the CCs, and side conversation and approval comments email their recipients. Approvers must be contacts tagged as approvers in the Gorelo app. Only private comments can be deleted. |
| `tools/attachments.py` | 1 | tickets | `upload_attachment` takes the file inline (base64 or text) and never fetches a URL; the decoded file is capped at 10 MB. An upload for a task or project is refused unless the `projects` toolset is on, because the file could never be attached. |
| `tools/time_entries.py` | 7 | time | Time entries, billing roles and work types. `ServiceLineId` null (off every contract) is different from omitted, so the tools have explicit `no_service_line` and `remove_from_contract` switches. A delete can answer `Reopened` and need a second call with `confirm=true`. |
| `tools/invoices.py` | 6 | billing | `list_invoices`, `get_invoice`, `create_invoice` (always a Draft), `create_approved_invoice` (gated), `export_invoice_pdf` (a write: Gorelo records the export) and `delete_invoice` (gated; a Draft is deleted, an Approved invoice is voided). See section 3. |
| `tools/catalog.py` | 7 | billing | Catalog items, categories and taxes. Create and update answer `{Id}` and the item is read back. `update_item` clears stored data only through `clear_fields`, never sends `TypeId`, and takes a negative unit price (a credit line) but not a negative cost. |
| `tools/contracts.py` | 2 | billing | Contracts, read only. Deleting a contract is forbidden. A service line's `Id` is what time entries call `ServiceLineId`. |
| `tools/uptime.py` | 6 | uptime | Uptime checks and maintenance windows. A new check starts monitoring at once. Enabling maintenance needs a start and a duration: Gorelo refuses a window without a start, and the tool never reads the clock or invents a length for the caller. |
| `tools/projects.py` | 13 | projects | Projects, sections, tags, types and the comments of projects and of tasks. Needs the `Project` API key scope. Deleting a project or a section is not offered because each would soft-delete every task under it. Values are removed only through `clear_fields`. |
| `tools/project_tasks.py` | 9 | projects | Project tasks, task conversations and task approvals. Needs the `Project` scope. Task comments are posted with the comment tools of `tools/projects.py`. |
| `tools/forms.py` | 3 | forms | Forms, form responses and submission links. Needs the `Forms` scope. A submission link opens the form without a login, so the instructions tell the model to give it only to the person who should have it. |

### Scripts

| Script | What it does |
|---|---|
| `scripts/spec_snapshot.py` | Fetches the live OpenAPI spec (no authentication), saves the raw JSON under `backups/` and repoints `backups/swagger-latest.json`, then builds `spec/spec_index.json`. Deterministic: the same spec gives the same file. Parameter `style` and `explode` values equal to the OpenAPI default for their location are dropped before indexing and hashing, so the two renderings Gorelo flips between give the same contract hash. Nothing is written unless the whole spec indexed. |
| `scripts/spec_diff.py` | Compares two snapshots (raw swagger files or indexes) and writes a markdown report of added, removed and changed operations and schemas. Exit status 0: identical or documentation only; 1: recorded differences; 2: an input error; 3: the recorded attributes are identical but the raw contract differs (compare the raw files). |
| `scripts/gen_tool_catalog.py` | Generates `docs/tool-catalog.md` from the registry (`--check` changes nothing and exits 1 if the file is stale). A test fails when the committed catalog differs from a fresh one. |
| `scripts/watch_gorelo_changelog.py` | The daily watcher. See section 6. |
| `scripts/site_config.py` | Loads the site settings of this installation from the untracked `site.local.toml` or the file named by `GORELO_SITE_CONFIG`. The keys are per consumer: the watcher reads only `[watcher] alert_client_id`; the live harness reads every other section (client ids and exact client names, operator, leftovers, hosts). A missing key, the example file itself or a value left at its placeholder is refused. Before the harness writes anything (and in smoke) it reads each configured client from Gorelo and stops with exit 2 unless the Name matches exactly. `site.example.toml` documents every key. |
| `scripts/oauth_state.py` | The one way to look at or change the OAuth state file by hand, for the operator: `check`, `inventory`, `purge --stale` (with `--plan` or `--apply`), `expire-access` and `revoke-all`. It prints metadata only (token type prefixes, the first 8 characters of client ids, client names, dates and counts), never a token or a secret, and an error prints its type only. A change (`--apply`) takes the state lock first, so it refuses while the server runs, and it never creates a state directory. A state file that other users can read is a problem that `check` reports, with a hint that names the one repair. When the store refuses the file, `check` still prints its mode, its owner and the lock before the error, because they need no read of the file, so a file left to another user can be told from a damaged one. See `docs/OPERATIONS.md`. |

### Live harness (`scripts/live/`)

Run by the operator against the real API, never by the offline suite. See section 5.

| File | What it does |
|---|---|
| `scripts/live/__init__.py` | The package docstring: how the pieces below are wired together. |
| `scripts/live/_env.py` | Reads only the `GORELO_API_KEY` line of the live service's env file when it is called, and never prints it. `live_settings()` returns settings with every toolset and the gated tools on, because the guard is the backstop. `Pacer` keeps requests at least one second apart. |
| `scripts/live/guard.py` | `LiveGuard`, an httpx request hook that refuses every request outside the allowlist before it is sent. |
| `scripts/live/manifest.py` | The run manifest: every record a run creates, written before and after the create call. |
| `scripts/live/cleanup.py` | Deletes what a manifest says a run created. Also a command: `python -m scripts.live.cleanup`. |
| `scripts/live/smoke.py` | The read-only smoke test: every read tool once. |
| `scripts/live/write_matrix.py` | The write matrix: the write tools against the test client, then cleanup. |
| `scripts/live/probes_20261001.py` | A one-off probe script from the first contract probes (2026-10-01). It predates the guard (it calls the API directly with its own small assertion), so it is a record, not part of the routine and not a model for new live code. |

### Everything else in the repository

| Path | What it holds |
|---|---|
| `spec/` | `spec/spec_index.json`, the generated index, and `spec/live_overrides.json` (section 4). |
| `backups/` | Raw copies of Gorelo's published spec, one per snapshot, and `backups/swagger-latest.json`, which points at the newest. |
| `tests/` | The offline suite (section 5). |
| `deploy/` | The systemd units: `deploy/gorelo-mcp.service` with its sandbox drop-in in `deploy/gorelo-mcp.service.d/`, the changelog watcher's `deploy/gorelo-changelog-watch.service`, `deploy/gorelo-changelog-watch.timer` and drop-in directory, and `deploy/watcher.env.example`. `docs/INSTALL.md` installs them. |
| `docs/` | This file, `docs/INSTALL.md`, `docs/OPERATIONS.md`, `docs/TROUBLESHOOTING.md`, `docs/API-OBSERVED-BEHAVIOR.md`, the generated `docs/tool-catalog.md` and the design notes in `docs/why/`. |
| `pyproject.toml`, `uv.lock` | The locked dependencies. Change them only in a deliberate dependency change (see `CONTRIBUTING.md`); `tests/test_hygiene.py` pins their hashes. |
| `.env.example` | The template of the environment file. |
| `.oauth-state/`, `.state/`, `.live-runs/` | Git-ignored runtime state: the OAuth sessions (credentials: look at them only through `scripts/oauth_state.py`, which shows metadata only, and back them up as `docs/OPERATIONS.md` describes), the watcher's memory and the live harness manifests. |

## 3. Safety layers

| Layer | What it stops | Where it is enforced |
|---|---|---|
| Loopback bind and tunnel | Direct network access. The server listens on `127.0.0.1` only; the public hostname reaches it only through the tunnel. | `main.py` (`HOST`, `PORT`); host setup in `docs/INSTALL.md`. |
| OAuth and password page | Tool calls without a token that was issued after the operator's password. The consent page checks the whole request (a registered client, an allowed redirect address, PKCE) before it looks at the password, answers every invalid request with the same page whatever the password was, and compares the password last, as UTF-8 bytes in constant time. An authorization code goes only to a redirect address on claude.ai, claude.com or localhost. Registration is open, but its redirect addresses must pass the same check, its rate is limited and the stored clients are capped. Wrong passwords are counted per client address, not per `client_id`: 5 wrong passwords per client address in 15 minutes, and 30 wrong passwords in an hour from all addresses, kept in memory, then HTTP 429 for the right password too. Consent responses carry no-store and anti-framing headers, bodies are capped, and the journal never gets a visitor's raw text. | `personal_auth.py` (`PersonalAuthProvider`, `_make_authorize_endpoint`); `oauth_guard.py`. |
| Login gate fails closed | A password page that is not in the path. The server refuses to start without a password, on a `fastmcp` or `mcp` version the gate was not checked against, unless the framework produced exactly one route each for `/authorize`, `/token`, `/register` and `/revoke`, and on a state file that cannot be trusted. `authorize()` issues a code only inside a request that the consent endpoint approved after the password matched. | `oauth_guard.py` (`check_framework_versions`, `require_password`, `GateError`); `personal_auth.py` (`get_routes`, `authorize`); `main.py`. |
| OAuth state file | Sessions silently forgotten, a half-written file, two writers, a rotated token that can be used again unnoticed, credentials printed. The file is written atomically under an exclusive lock and one that cannot be trusted is never replaced; a rotated refresh token leaves only its sha256; expired tokens are pruned after 7 days; the file is looked at only through `scripts/oauth_state.py`, which prints metadata only. | `oauth_store.py` (`write_state`, `StateLock`, `prune`); `scripts/oauth_state.py`; `main.py`. |
| Strict settings | Starting with a missing key, an unknown toolset or an invalid flag. Every problem is reported at once and the process exits before it serves. | `settings.py` (`Settings.from_env`); `main.py`. |
| Toolsets | Tools the operator did not switch on. `projects` and `forms` are off by default. | `Registry.select` in `tools/_common.py`, used by `server.build_server`; `DEFAULT_TOOLSETS` in `settings.py`. |
| Destructive gating and `confirm` | The delete and void tools and `create_approved_invoice` existing unless the operator sets `GORELO_ENABLE_DESTRUCTIVE`. A call without a strict `confirm=true` sends nothing: the text "true" and the number 1 do not count, and the refusal says what would happen. | `Registry.select`; `_require_confirm_parameter` (checked when a tool is declared); `require_confirm` (called before any HTTP). |
| Forbidden operations, by shape | The deletes of clients, contacts, tickets, agent assets, custom assets and contracts, and `POST /v1/api-keys`, which would mint credentials. A tool cannot declare one, and the client refuses one with no HTTP even under another placeholder spelling. | `gorelo_client.py` (`FORBIDDEN_OPS`, `is_forbidden_op`, `_check_not_forbidden`); `server._verify_ops`. |
| Side-effect GETs, by shape | The invoice PDF export, a GET that Gorelo records as an event, passing as a harmless read. A `read` tool cannot declare it, and a failed export is reported as unconfirmed, because a retry records another export. | `gorelo_client.py` (`SIDE_EFFECT_GETS`, `is_side_effect_get`); `Registry.tool`; `server._verify_ops`. |
| Declared ops | A tool calling anything it did not declare (refused with no HTTP, kind `spec`), and a declared op that is not in the spec index (the server does not start). | `gorelo_client.py` (`CURRENT_TOOL`, `_check_declared`); `tools/_common.py` (`_enter_tool`); `server._verify_ops`. |
| Spec validation | Unknown operations, misspelled query names (even unset ones), unknown body fields, values of the wrong shape, path ids of the wrong type, and a path that changed on the wire. All before any HTTP. | `spec.py` (`normalize_query`, `validate_body`, `validate_path_param`); `gorelo_client.py` (`_fill_path`, `_check_path_unchanged`). |
| Strict ids, no invented defaults | JSON `true`, `"5"` or `5.0` becoming an id, a blank value overwriting data, a datetime without a UTC offset, a guessed region. Omitted means unchanged; clearing needs an explicit option. | `tools/_common.py` (`StrictId`, `positive_id`, `guid`, `build_body`, `utc_iso`, `region_code`). |
| Unconfirmed writes | Repeating a write that may have been applied. A timeout, connection error, 5xx or unreadable 2xx on a write is flagged `write_unconfirmed` and never retried. | `gorelo_client.py` (`request`, `_transport_failure`, `_may_have_applied`, `_interpret`); `tools/_common.py` (`format_gorelo_error`, `created_id`, `expect_object`). |
| Draft-only create, gated approved create | An invoice becoming Approved by accident. Approving on create pushes the invoice to the connected accounting system at once. `create_invoice` always writes `StatusId` 1 and has no parameter for a status or for recipients; `create_approved_invoice` is a separate gated tool that writes `StatusId` 5, takes the recipients and needs `confirm`. Both build the request with one validator and read the invoice back with the same GET. | `tools/invoices.py` (`create_invoice`, `create_approved_invoice`, `_invoice_request`). |
| A void stays in Gorelo only | Believing a void undid the push. `delete_invoice` voids an Approved invoice in Gorelo only: the copy already pushed to the accounting system stayed open when it was tried, so the tool texts and the confirm refusals say the user must void it there too. The tool reports only the `StatusId` of Gorelo's answer (6 deleted, 4 void); any other answer is an unconfirmed `shape` error. | `tools/invoices.py` (`delete_invoice`, `_delete_report`, `DELETE_EFFECT`). |
| Log value filter | Client data reaching the journal. FastMCP's log of a rejected call (pydantic's `input_value=...`) is cut down to error locations and types; the Gorelo client logs names only; the `httpx` logger is raised to WARNING because it logs URLs. | `server.py` (`LogValueFilter`, `install_log_value_filter`), installed by `main.py`; `gorelo_client.py` (`_quiet_http_library_logging`). |
| Record text is data | Prompt injection through ticket, comment, conversation or form text. Rule 8 of the instructions tells the model that such text is data, never instructions. | `server.py` (`INSTRUCTIONS`). |
| Upload and download limits | The server fetching an address a caller chose (an upload takes inline content only, up to 10 MB) and oversized downloads (the PDF export is read through a 5 MB cap and must carry the expected content type). | `tools/attachments.py` (`MAX_UPLOAD_BYTES`); `tools/invoices.py` (`PDF_MAX_BYTES`); `gorelo_client.py` (`_read_capped`, `get_binary`). |
| Live harness guard | A live test touching anything but its own test records. See section 5. | `scripts/live/guard.py` (`LiveGuard`); `scripts/live/manifest.py`; `scripts/live/_env.py` (`Pacer`); `scripts/live/write_matrix.py` (`APPROVED_INVOICE_TOOLS`); `scripts/live/cleanup.py`. |
| Drift guards | Docs, counts and dependency files silently diverging from the code. | `tests/test_tool_catalog.py`, `tests/test_hygiene.py` (dashes and lockfile hashes), `tests/test_overview_docs.py`. |

Gated tools (kind `destructive`, registered only with `GORELO_ENABLE_DESTRUCTIVE`): `create_approved_invoice`, `delete_invoice`, `delete_item`, `delete_project_comment`, `delete_project_task`, `delete_ticket_comment`, `delete_time_entry`, `delete_uptime_check`.

Forbidden operations (`FORBIDDEN_OPS`): `DELETE /v1/clients/{clientId}`, `DELETE /v1/contacts/{contactId}`, `DELETE /v1/tickets/{ticketId}`, `DELETE /v1/assets/agents/{deviceId}`, `DELETE /v1/assets/custom/{customAssetId}`, `DELETE /v1/contracts/{contractId}`, `POST /v1/api-keys`.

Side-effect GETs (`SIDE_EFFECT_GETS`): `GET /v1/invoices/{invoiceId}/pdf`.

Trust model: access is all or nothing. Anyone who signs in with the operator's password gets a token that can call every registered tool, and every call uses the one Gorelo API key, so the limits are the toolsets and gated tools the operator enabled and the scopes of that key. The server has no per-user permissions.

Principles that run through every layer: fail loudly (no silent empty result, no invented default, an answer the client does not understand is an error); error messages name the snake_case parameter; a tool reports only what Gorelo's answer says, never what the caller expected; and the model is told a side effect before it acts.

## 4. The spec index and live overrides

**The spec index.** `spec/spec_index.json` is a compact, deterministic digest of Gorelo's published OpenAPI spec: every operation (98 operations at this writing) with its method, path, query parameters, body fields, required fields, response kind and paging rule, plus every component schema. It keeps no descriptions. Its header records the source URL, `sha256` of the raw spec and `contract_sha256`, the hash of the spec without descriptions, summaries and examples. Code and tests validate against it (section 3, "Spec validation"). It is generated, never edited by hand.

**Refreshing it.** Run `scripts/spec_snapshot.py`: it fetches the live spec, saves the raw copy as `backups/swagger-<UTC>.json`, repoints `backups/swagger-latest.json` and rewrites the index. Then run `scripts/spec_diff.py OLD NEW --out FILE` against the previous snapshot to see what changed. Then update the tools. The daily watcher (section 6) compares the live `contract_sha256` with the index and raises an alert when they differ, so a published change is noticed even if nobody looks.

**Live overrides.** The published spec is sometimes wrong about the live API, and a spec hash cannot show that; only live calls can. `spec/live_overrides.json` lists the operations to swap: `replace_ops` maps a published op key to the live op key, and each live key needs `evidence` (`verified_on` and dated probes with the request and the HTTP status). `spec.load_spec_index()` applies the file on top of the untouched index, so the client, the tools, the live guard and the tests all see the live operation. The index itself stays a mirror of the published spec.

**Retiring an override.** When the published spec catches up (the refreshed index already contains the live key), the loader and `tests/test_live_overrides.py` fail on purpose with a message that says to drop the override. A person re-verifies the live API and then moves the entry to the `retired` section with `retired_on`, `reason`, `published_as`, `replaced` and its old `evidence`; nothing is deleted, an empty `replace_ops` is valid, and the loader ignores `retired`. Today `replace_ops` is empty. The two entries in `retired` are the client and contact update operations whose path forms production served before Gorelo published them.

After any Gorelo release run the live write matrix as well (section 5): it is what finds a moved route.

## 5. Testing

### The offline suite

The suite in `tests/` never touches the network: an autouse fixture in `tests/conftest.py` blocks sockets, Gorelo is an `httpx.MockTransport` (`MockGorelo`), and tools run through an in-process `fastmcp` client. It pins:

- the client: envelope, errors, paging, 429 handling, uploads and downloads, forbidden operations, side-effect GETs;
- the spec index and its loader, and the live overrides layer;
- every tool: the exact method, path, query names and PascalCase body, the result shape, a Gorelo error mapped to the snake_case parameter, each local validation error with zero HTTP calls, and for gated tools the refusal without `confirm`;
- the whole tool surface: exact tool names per toolset, gating, MCP hints, and that every spec operation is either declared by a tool, forbidden or deliberately excluded (`tests/test_integration.py`);
- the settings, the server build, the log filter and the schema compaction;
- the live harness code (guard, manifest, cleanup, smoke, write matrix) against fake Gorelo servers;
- the login code: the state file, the tokens, the password page, the limits, registration, the start of the server and the admin script, each against temporary directories with fake data, and the real HTTP app driven in process through ASGI with a chosen client address (no sockets). A frozen copy of an earlier provider proves that earlier versions and the current one read each other's state file;
- the docs: `tests/test_tool_catalog.py` regenerates the catalog, `tests/test_hygiene.py` forbids em and en dashes and pins the dependency files, and `tests/test_overview_docs.py` pins this file.

Run it from a checkout of this repository, never from the tree a running service uses. The exact command is in `CONTRIBUTING.md`; in general form:

```bash
cd <a checkout of this repository>
UV_OFFLINE=1 UV_PYTHON=<python 3.13> PYTHONDONTWRITEBYTECODE=1 \
  uv run --frozen -q --with pytest --with anyio python -m pytest -q -p no:cacheprovider
```

### The live harness

The harness checks the things the offline suite cannot: that Gorelo still behaves the way the spec index and the tools assume. It builds the real server in process (`build_server` with request hooks) and drives it through a `fastmcp` client, so it exercises the tools exactly as the model would. It writes only to the test client, to temporary records it creates itself and, for the `email` area, to the operator's own client and contact. It names every record `MCPTEST-<run id>`, cleans up in a `finally`, and paces itself at one request per second.

**Smoke** (`python -m scripts.live.smoke`): read only. The guard is in read mode, the gated tools are not registered, and every `read` tool is called, with ids discovered from earlier list results (a single-record tool whose list came back empty is skipped, and the row says so). It checks shapes and counts, never values. A 403 with code `080203` from a `projects` or `forms` tool is reported as "scope missing", not as a failure. Exit status 0 when nothing failed and the guard refused nothing.

**Write matrix** (`python -m scripts.live.write_matrix`): the write tools, area by area, in the order below (`forms` is another name for `projects`). A guard refusal stops the run; any other failure fails only its area; the cleanup always runs afterwards. Everything is a tool call, and the test client is the target unless an area says otherwise.

| Area | What it does |
|---|---|
| `clients` | Creates a temporary client with a location phone and a region, updates its alternate name, reads it back. The test client itself is never changed. |
| `contacts` | Creates a contact, updates it through the full-command path and checks that its phone survived, and checks that an update without a secondary-email choice is refused locally. |
| `tickets` | Resolves status, type, group, source and priority by name; creates a plain ticket and a backdated Closed one; reads by GUID, number and display number; updates title, watcher and lead (never a watcher and a lead in one request); fills the billing override partially; searches. |
| `comments` | Creates, lists, reads and deletes a private comment; uploads a text attachment and attaches it to a private comment. |
| `email` | The operator's own client and contact only: a ticket with the creation email, a public comment, a status change, and a side conversation and an approval that each get a comment. |
| `time` | Creates a time entry, updates its comment, deletes it (again while the answer is `Reopened`). |
| `items` | Creates, updates and deletes one catalog product. |
| `invoices` | Creates a Draft invoice, reads and lists it, exports its PDF, deletes it. |
| `approved_invoice` | Approves one invoice of $1, waits for its push to the accounting system, voids it. |
| `uptime` | Creates an HTTP check on a domain the operator owns, sets a maintenance window, updates the description, deletes the check. |
| `projects` | Lists projects and forms first; a 403 for a missing scope is recorded as skipped. With the scope: creates a project, a section, a task and a private task comment, then deletes the comment and the task. |

| Flag | Effect |
|---|---|
| `--skip-email` | Leave out the `email` area, which writes to the operator's own client and contact and sends real mail to the operator. |
| `--only AREA,...` | Run only those areas, always in the order above. |
| `--leftovers` | Also delete the approved leftovers of earlier probes, after checking their names. |
| `--with-items` | Run the `items` area: create, update and delete one catalog product on the test client. Off by default. |
| `--with-invoices` | Run the `invoices` area: create a Draft, read it, list it, export its PDF, delete it. Off by default; it uses up one invoice number. |
| `--with-approved-invoice` | Run the `approved_invoice` area, alone: approve one invoice of $1 on the test client, which Gorelo pushes to the connected accounting system, wait for the push, then void it at once. It needs `--only approved_invoice` and no other flag, and it creates a real Approved invoice, so run it only on purpose. |

The approved-invoice run is the only one that ever calls `create_approved_invoice`. Its tool calls are limited to `create_approved_invoice`, `get_invoice`, `list_invoices`, `delete_invoice`, `list_items`, `list_contacts` and `list_client_locations` (`APPROVED_INVOICE_TOOLS`). The area is skipped, with nothing written, unless every contact of the test client is inactive and no location names a billing contact. Its one create is sent with the 429 retries switched off, so it can never be sent twice. Its void is in Gorelo only, so after every such run the operator voids the invoice in the accounting system by hand. A voided invoice stays listed as Void and is reported as a known residue, not a leftover.

**The guard** (`scripts/live/guard.py`, `LiveGuard`) is an httpx request hook installed on every client of a run. A request it does not allow raises before anything is sent, and every refusal is also kept in `guard.violations` so a run stops even if a tool swallowed the exception. In write mode it allows:

- GET for every operation of the spec index except the invoice PDF export;
- a create only for the test client (tickets also for the operator's own client) or for a temporary client whose name starts with `MCPTEST-`, and, when `require_intents` is on as it is for the write matrix, only when the manifest holds an open intent of that kind;
- a change or delete only for a record the run itself created (a cleanup guard also deletes the approved leftovers of earlier probes, listed in `site.local.toml` `[leftovers]`, ids and name rules), never a DELETE under assets or on contracts, never `POST /v1/api-keys` or `POST /v1/alerts`, and never a DELETE of the test client or the operator's own client or contact;
- email addresses only the operator's address or the reserved `example.invalid` domain, and a public ticket comment only when the ticket's stored contact and CCs are none or the operator's contact;
- invoices only as a Draft with no recipients, with the PDF export and the DELETE limited to a Draft the run created. The one exception is the opt-in approved-invoice area (the guard option `allow_approved_invoice`): one Approved invoice for the test client with one line of at most $1, no tax and no recipients, and its void.

The guard takes the test clients, the operator's contact, user and email address, the leftovers (ids and name rules, listed in `site.local.toml` `[leftovers]`) and the probe domain from `site.local.toml` through `scripts/site_config.py`, and refuses to be built without it.

**Manifests** (`.live-runs/<run id>.json`, git-ignored, run id `MCPTEST-<UTC yyyymmddHHMMSS>`): every create is written as an intent before the call and as a record with its id right after; every cleanup outcome is written too. Writes are atomic. A create whose answer was lost is reported as an unresolved intent, to be searched for by the run id. Secrets are never recorded.

**Cleanup** (`python -m scripts.live.cleanup <manifest> [--leftovers] [--void-approved]`): deletes in reverse creation order, with the gated delete tools where they exist and raw soft deletes (tickets, contacts, the temporary client, projects) behind the same kind of guard. It is safe to run again. Without `--void-approved` it never voids an Approved invoice; with it, it voids one that a run recorded as Approved, in Gorelo only. Two things cannot be removed and are reported apart as known residue: an uploaded attachment file and a voided invoice. Exit status 0 only when nothing is left over.

The reasoning behind the harness is in `docs/why/07-live-test-harness.txt`.

## 6. Operations

**Settings.** Read once at start from the environment (`.env` in production, owned by root, group `gorelo-mcp`, mode 0640; see `docs/INSTALL.md` step 3) by `Settings.from_env`:

| Variable | Required | Meaning |
|---|---|---|
| `GORELO_API_KEY` | yes | The Gorelo API key, sent as `X-API-Key`. The watcher does not read `.env`: it has its own key file, `/etc/gorelo-mcp/watcher.env`. |
| `PUBLIC_BASE_URL` | yes | The public HTTPS address of the server; it is the OAuth issuer. If it changes every client must authorize again. |
| `MCP_AUTH_PASSWORD` | yes | The password on the consent page. |
| `GORELO_TOOLSETS` | no | A comma list of toolsets, or `all`. Default `core,tickets,time,billing,uptime`. |
| `GORELO_ENABLE_DESTRUCTIVE` | no | `1`, `true`, `yes` or `on` registers the gated tools. Default off. Any other value than those and `0`, `false`, `no`, `off` stops the start. |

Things to know about the settings and the running server:

- `projects` and `forms` are off by default and need the API key scopes `Project` and `Forms`. Without a scope Gorelo answers 403 with code `080203`, and the tool says which scope is missing.
- The startup line `gorelo-mcp ready: toolsets=... destructive=... tools=N` must agree with the counts in section 2 and with `docs/tool-catalog.md`. The `health_check` tool reports the same facts and whether the API answers.
- The AI client loads the tool list when a conversation starts, so after a deploy open a new conversation.
- An update keeps the OAuth state, so the session of the AI client survives it and an access token that expires is refreshed without a sign-in. The state is looked at only through `scripts/oauth_state.py` (see `docs/OPERATIONS.md`), and the server never repairs a state file it refused. A `MCP_AUTH_PASSWORD` shorter than 16 characters starts the server with a warning, because a short one is much easier to guess.

**Updates and rollback.** A release is a git tag; the first public release is `v1.0.0`. An update stops the service, checks out the newest tag, rebuilds the environment with `uv sync --frozen`, starts the service and checks the journal for the ready line and a 401 from the endpoint. A rollback checks out the previous tag the same way. The commands are in `docs/INSTALL.md` section 9, and a backup first is in `docs/OPERATIONS.md`. Rotating secrets, reading logs and troubleshooting are in `docs/OPERATIONS.md` and `docs/TROUBLESHOOTING.md`.

**The changelog watcher.** Gorelo announces API changes in its public changelog, so `scripts/watch_gorelo_changelog.py` runs daily from a systemd timer (`deploy/gorelo-changelog-watch.timer`, up to 30 minutes of random delay; the unit is `deploy/gorelo-changelog-watch.service`).

- Two checks: new changelog entries that look API-related, and the live spec's `contract_sha256` against `spec/spec_index.json`.
- For each new API change it posts one Gorelo alert (`POST /v1/alerts`) against a client of the operator's own, whose id is `alert_client_id` in the `[watcher]` section of `site.local.toml`, and remembers it in `.state/` so the same change never alerts twice.
- Its only call to the Gorelo API proper is that alert: it sends the key to that one request, and it does not go through `GoreloClient`. The key comes from its own file, `/etc/gorelo-mcp/watcher.env`, and never from `.env`.
- Exit status: 0 nothing new, 1 an API change (the unit is left failed so `systemctl --failed` shows it), 2 news that is not about the API, 3 a check failed.
- When it alerts, read the report it names and follow "When the watcher reports an API change" in `docs/OPERATIONS.md`. Installing its units is `docs/INSTALL.md` step 7, and its failures are in `docs/TROUBLESHOOTING.md`.

## 7. Where to look for what

| Question | Where |
|---|---|
| How do I install the server on a blank Debian host, and how do I update or roll back? | `docs/INSTALL.md`. |
| How do I check health, read logs, rotate secrets, back up or react to a watcher alert? | `docs/OPERATIONS.md`. |
| Something is broken. What do I check? | `docs/TROUBLESHOOTING.md`. |
| How does Gorelo's API behave where it differed from its published spec? | `docs/API-OBSERVED-BEHAVIOR.md`. |
| Why is it built this way (self-hosting, request validation, gated operations, OAuth, the watcher, the harness)? | `docs/why/README.txt` and the notes beside it. |
| How do I add or change a tool, write its tests and propose it? | `CONTRIBUTING.md`. |
| What are the security assumptions and how do I report a problem? | `SECURITY.md`. |
| Which tools exist, with their kind, operations and required parameters? | `docs/tool-catalog.md`, generated by `scripts/gen_tool_catalog.py`. |
| How do I look at the OAuth sessions, expire one for the refresh test, or revoke them all? | `scripts/oauth_state.py` and `docs/OPERATIONS.md`. |
| What does a tool tell the model? | Its docstring in `tools/<domain>.py`: the docstring is the prompt. |
| What is in the spec index? | `spec/spec_index.json` and the raw snapshots in `backups/`. |
| How is the live harness designed? | `docs/why/07-live-test-harness.txt` and the docstrings of the modules in `scripts/live/`. |
| Which variables does the server read? | `.env.example` and `settings.py`. |
