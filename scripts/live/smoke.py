"""Live smoke test: every read tool of the server, once, against the real Gorelo API. Read-only.

    python -m scripts.live.smoke

The server is built in process (build_server(live_settings(), event_hooks={"request": [guard, pacer]})) and
driven through a fastmcp Client, like scripts/live/cleanup.py does. The guard is a LiveGuard in READ mode, so a
request that is not a GET is refused before it leaves the process, and the pacer keeps the requests about one
second apart. Destructive tools are not even registered (the Settings are copied with destructive off).

What it calls: every tool whose registry kind is "read", with arguments discovered from earlier list results
(the first client for get_client, the first ticket of list_tickets for get_ticket, preferring one whose last update
was a comment, then that ticket's comments and conversations, and the first time entry, contract, item, uptime
check, agent, invoice, project, task and form). get_ticket is
called three times, once per way to name a ticket: its GUID, its number and its display number. A get whose list
came back empty is skipped, and the row says which list. export_invoice_pdf (a write: it records an export
event) and every other non-read tool are never called: the plan is checked against the registry first, and
a read tool the plan does not know is reported as FAILED so a new tool cannot go untested unnoticed.

What it checks (shapes only, never values):

    paged list     items is a list, count == len(items), total_count is an int, has_more is a bool, a next
                   cursor when there are more pages, and on a single page count == total_count
    unpaged list   items is a list and count == len(items)
    auto-paged     list_org_users and search_tickets: items, count, truncated and complete_scan flags
    single record  a dict with an Id (the one that was asked for)

Outcomes per row: ok, scope missing (a 403 with Notification code 080203 from a Projects or Forms tool: the API key
simply does not have that scope yet), skipped, FAILED. Each row is printed as soon as its call is done. The notes hold
counts (for a FAILED row Gorelo's own error message), never record contents and never the API key. The exit status is
0 when nothing FAILED and the guard refused nothing, 1 otherwise, 2 for a setup error, 130 when interrupted.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import logging
import re
import sys
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Client

from scripts.live._env import EnvError, Pacer, live_settings, scrub
from scripts.live import guard as live_guard
from scripts.site_config import SiteConfigError, site
from scripts.live.guard import LiveGuard
from server import build_server
from settings import Settings
from tools._common import REGISTRY

PACE_SECONDS = 1.0
LOG_NAMES = ("fastmcp", "mcp", "httpx", "httpcore", "gorelo_client")
# A scope 403 from these toolsets is "scope missing" (the API key lacks the scope), not a failure.
SCOPE_TOOLSETS = ("projects", "forms")

OK, SCOPE_MISSING, SKIPPED, FAILED = "ok", "scope missing", "skipped", "FAILED"

_STATUS = re.compile(r"\(HTTP (\d{3})")
_SCOPE = re.compile(r"the API key does not have the '([^']+)' scope")
_NOTE_LIMIT = 200


# --------------------------------------------------------------------------
# Talking to the tools (shared with scripts/live/write_matrix.py)
# --------------------------------------------------------------------------


class GuardTripped(RuntimeError):
    """The live guard refused a request while a tool was running. Nothing was sent. `str(exc)` is the guard's text."""


class ToolFailed(Exception):
    """A tool answered with an error. `text` is the error text the model would see (API key scrubbed).

    sent      True when at least one request left the process during the call (the guard let it through).
    status    the first HTTP status named in the text, or None (a local validation error has none).
    scope     the API key scope a 403 / code 080203 answer says is missing ("unknown" when it names none), else None.
    rejected  True for a 4xx answer that Gorelo gave without applying anything (so a create did not happen).
    """

    def __init__(self, tool: str, text: str, *, sent: bool) -> None:
        super().__init__(text)
        self.tool = tool
        self.text = text
        self.sent = sent
        match = _STATUS.search(text)
        self.status = int(match.group(1)) if match else None
        self.scope = scope_name(text)

    @property
    def rejected(self) -> bool:
        unconfirmed = "may have applied" in self.text or "did not confirm" in self.text
        return self.status is not None and 400 <= self.status < 500 and not unconfirmed

    @property
    def detail(self) -> str:
        """What Gorelo said: the text after "Gorelo rejected <tool> (HTTP nnn, code nnn): " (all of it when absent)."""
        head, separator, rest = self.text.partition("): ")
        return rest if separator and head.startswith("Gorelo rejected") else self.text


def scope_name(text: str) -> str | None:
    """The missing scope an error text reports (Gorelo's 403 with code 080203), "unknown" when it names none, else None."""
    if "080203" not in text and "HTTP 403" not in text:
        return None
    found = _SCOPE.search(text)
    if found:
        return found.group(1)
    return "unknown" if "080203" in text else None


def emit(text: str) -> None:
    """print() that flushes: progress must show while a run is going, also when stdout is a pipe or a file."""
    print(text, flush=True)


@contextlib.contextmanager
def quiet_logs() -> Iterator[None]:
    """FastMCP logs every refused tool call with a traceback; the printed result already says what failed."""
    loggers = [logging.getLogger(name) for name in LOG_NAMES]
    previous = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        for logger, level in zip(loggers, previous):
            logger.setLevel(level)


class ToolSession:
    """Calls tools through an in-process fastmcp Client and turns every outcome into a result or an exception.

    call() returns the tool's structured result (a dict), raises ToolFailed for a tool error and GuardTripped when
    the guard refused a request meanwhile (the guard's violation list grew). `allowed`, when given, is the only set
    of tool names that may be called: anything else is a RuntimeError before any call is made.
    """

    def __init__(
        self,
        tools: Client,
        guard: LiveGuard,
        pacer: Pacer,
        secret: str,
        *,
        allowed: frozenset[str] | None = None,
    ) -> None:
        self.tools = tools
        self.guard = guard
        self.pacer = pacer
        self.secret = secret
        self.allowed = allowed

    def _safe(self, text: str) -> str:
        return scrub(" ".join(text.split()), self.secret)

    async def call_result(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        """The raw CallToolResult of a call that succeeded, for a tool whose answer is not a structured object (the
        invoice PDF export answers a text and an embedded file). Everything else is as in call()."""
        if self.allowed is not None and name not in self.allowed:
            raise RuntimeError(f"{name} is not one of the tools this script may call")
        violations = len(self.guard.violations)
        sent = self.pacer.requests
        try:
            result = await self.tools.call_tool(name, dict(arguments or {}), raise_on_error=False)
        except Exception as exc:
            if len(self.guard.violations) > violations:
                raise GuardTripped(self._safe(self.guard.violations[-1])) from None
            raise ToolFailed(name, f"{name} could not be called ({type(exc).__name__})", sent=self.pacer.requests > sent) from None
        if len(self.guard.violations) > violations:
            raise GuardTripped(self._safe(self.guard.violations[-1]))
        if result.is_error:
            text = " ".join(getattr(block, "text", "") for block in result.content).strip() or f"{name} failed"
            raise ToolFailed(name, self._safe(text), sent=self.pacer.requests > sent)
        return result

    async def call(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        sent = self.pacer.requests
        result = await self.call_result(name, arguments)
        data = result.structured_content
        if not isinstance(data, dict):
            raise ToolFailed(name, f"{name} returned no structured result", sent=self.pacer.requests > sent)
        return data


def read_tools() -> dict[str, str]:
    """Registry name -> toolset of every tool whose kind is "read"."""
    return {spec.name: spec.toolset for spec in REGISTRY.specs if spec.kind == "read"}


@asynccontextmanager
async def open_session(
    settings: Settings, *, transport: Any = None, pace: float = PACE_SECONDS
) -> AsyncIterator[ToolSession]:
    """An in-process server behind a READ-mode guard and a pacer, and a session that may call read tools only."""
    guard = LiveGuard("read", None)
    pacer = Pacer(pace)
    safe = dataclasses.replace(settings, destructive=False)  # no delete tool is even registered
    with quiet_logs():
        server = build_server(safe, transport=transport, event_hooks={"request": [guard, pacer]})
        async with Client(server) as tools:
            yield ToolSession(tools, guard, pacer, settings.api_key, allowed=frozenset(read_tools()))


# --------------------------------------------------------------------------
# Shape checks (each raises ShapeError naming what is wrong, never a value)
# --------------------------------------------------------------------------


class ShapeError(Exception):
    """A tool answered, but not in the shape the house result rules promise."""


class Skip(Exception):
    """A case cannot run: the argument it needs was not discovered. The text says why."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _items(result: Mapping[str, Any]) -> list[Any]:
    items = result.get("items")
    if not isinstance(items, list):
        raise ShapeError("items is not a list")
    count = result.get("count")
    if not _is_int(count) or count != len(items):
        raise ShapeError(f"count is not len(items): count={count!r}, {len(items)} items")
    return items


def check_list(result: Mapping[str, Any], *, nonempty: bool = False) -> str:
    items = _items(result)
    if nonempty and not items:
        raise ShapeError("no rows (this list is never empty in a working tenant)")
    return f"count={len(items)}"


def check_paged(result: Mapping[str, Any], *, nonempty: bool = False) -> str:
    items = _items(result)
    total = result.get("total_count")
    if not _is_int(total):
        raise ShapeError("total_count is not an integer")
    more = result.get("has_more")
    if not isinstance(more, bool):
        raise ShapeError("has_more is not a boolean")
    cursor = result.get("next_cursor")
    if more and not (isinstance(cursor, str) and cursor):
        raise ShapeError("has_more is true but there is no next_cursor")
    size = result.get("page_size")
    if not _is_int(size) or size < 1:
        raise ShapeError("page_size is not a positive integer")
    if not more and len(items) != total:
        raise ShapeError(f"this is the only page but count={len(items)} differs from total_count={total}")
    if nonempty and not items:
        raise ShapeError("no rows (this list is never empty in a working tenant)")
    return f"count={len(items)} total_count={total}" + (" (more pages)" if more else "")


def check_all(result: Mapping[str, Any], *, nonempty: bool = False, complete: bool = False) -> str:
    items = _items(result)
    truncated, scan = result.get("truncated"), result.get("complete_scan")
    if not isinstance(truncated, bool) or not isinstance(scan, bool):
        raise ShapeError("truncated or complete_scan is not a boolean")
    total = result.get("total_count")
    if total is not None and not _is_int(total):
        raise ShapeError("total_count is not an integer")
    if result.get("count_mismatch") is True:
        raise ShapeError("count_mismatch: the rows read differ from Gorelo's total_count")
    if complete and (truncated or not scan):
        raise ShapeError("the list is not complete (truncated or complete_scan false)")
    if nonempty and not items:
        raise ShapeError("no rows (this list is never empty in a working tenant)")
    return f"count={len(items)} total_count={total}" + (" (truncated)" if truncated else "")


def check_search(result: Mapping[str, Any]) -> str:
    note = check_all(result)
    matched, scanned = result.get("matched"), result.get("scanned")
    if not _is_int(matched) or not _is_int(scanned):
        raise ShapeError("matched or scanned is not an integer")
    if not (len(result["items"]) <= matched <= scanned):
        raise ShapeError("count, matched and scanned are not in order")
    return f"{note} matched={matched} scanned={scanned}"


def check_record(result: Mapping[str, Any], expected: Any = None) -> str:
    if "Id" not in result or result["Id"] in (None, ""):
        raise ShapeError("the record has no Id")
    if expected is not None and str(result["Id"]).lower() != str(expected).lower():
        raise ShapeError("the record's Id is not the one that was asked for")
    return "Id matches" if expected is not None else "Id present"


def check_health(result: Mapping[str, Any]) -> str:
    if result.get("ok") is not True:
        raise ShapeError("health_check says ok is not true: " + str(result.get("error", "no error text"))[:_NOTE_LIMIT])
    total = result.get("total_clients")
    if not _is_int(total):
        raise ShapeError("total_clients is not an integer")
    return f"total_clients={total}"


# --------------------------------------------------------------------------
# The plan: one Case per call, in dependency order
# --------------------------------------------------------------------------


class Found:
    """What the lists taught the smoke so far (ids to use later), and why something is missing."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.why: dict[str, str] = {}

    def need(self, key: str, what: str) -> Any:
        if key not in self.values:
            raise Skip(f"needs {what}: {self.why.get(key, 'not discovered')}")
        return self.values[key]


def _first_id(result: Mapping[str, Any]) -> Any:
    items = result.get("items")
    first = items[0] if isinstance(items, list) and items else None
    return first.get("Id") if isinstance(first, dict) else None


def _pick_ticket(result: Mapping[str, Any]) -> Any:
    """The ticket to explore: the first row, unless a row's last update was a comment (more to read there)."""
    rows = [
        row for row in result.get("items", []) if isinstance(row, dict) and isinstance(row.get("Id"), str) and row["Id"]
    ]
    if not rows:
        return None
    chosen = rows[0]
    for row in rows:
        update = row.get("LastUpdate")
        kind = update.get("UpdateType") if isinstance(update, dict) else None
        if isinstance(kind, str) and "comment" in kind.casefold():
            chosen = row
            break
    return {key: chosen.get(key) for key in ("Id", "Number", "DisplayNumber", "ClientId")}


def _pick_approval(result: Mapping[str, Any]) -> Any:
    """The Id of the first approval among a ticket's or task's conversations (type 4), or None."""
    for row in result.get("items", []):
        kind = row.get("Type") if isinstance(row, dict) else None
        if not isinstance(kind, dict) or not row.get("Id"):
            continue
        name = kind.get("Name")
        if kind.get("Id") == 4 or (isinstance(name, str) and name.casefold() == "approval"):
            return row["Id"]
    return None


def _ticket(found: Found) -> dict[str, Any]:
    return found.need("ticket", "a ticket")


def _ticket_guid(found: Found) -> dict[str, Any]:
    return {"ticket_id": _ticket(found)["Id"]}


def _ticket_number(found: Found) -> dict[str, Any]:
    number = _ticket(found).get("Number")
    if not _is_int(number) or number < 1:
        raise Skip("the listed ticket has no usable Number")
    return {"ticket_id": number}


def _ticket_display(found: Found) -> dict[str, Any]:
    display = _ticket(found).get("DisplayNumber")
    if not isinstance(display, str) or not display.strip():
        raise Skip("the listed ticket has no DisplayNumber")
    return {"ticket_id": display}


def _search_args(found: Found) -> dict[str, Any]:
    ticket = _ticket(found)
    client = ticket.get("ClientId")
    if _is_int(client):
        return {"client_id": client, "limit": 5}
    return {"query": ticket.get("DisplayNumber") or str(ticket.get("Number")), "limit": 5}


@dataclass(frozen=True)
class Case:
    """One smoke call.

    tool      the read tool to call (a registry name)
    shape     what the answer must look like: health, paged, list, all, search, record or local (no HTTP at all)
    args      builds the arguments from what was found so far; raises Skip when something is missing
    label     the row name when it is not the tool name (get_ticket is called three times)
    provides  the Found keys this case teaches; pick(result) returns the value (None: the list was empty)
    same_id   for a record: the Found key whose value the record's Id must equal ("ticket" compares ticket["Id"])
    """

    tool: str
    shape: str
    args: Callable[[Found], dict[str, Any]] = lambda found: {}
    label: str | None = None
    provides: str | None = None
    pick: Callable[[Mapping[str, Any]], Any] = _first_id
    same_id: str | None = None
    nonempty: bool = False
    complete: bool = False

    @property
    def row(self) -> str:
        return self.label or self.tool


def _by(key: str, what: str, param: str, **more: Any) -> Callable[[Found], dict[str, Any]]:
    return lambda found: {param: found.need(key, what), **more}


def _project_task(found: Found) -> dict[str, Any]:
    return {"project_id": found.need("project", "a project"), "task_id": found.need("task", "a project task")}


CASES: tuple[Case, ...] = (
    # core
    Case("health_check", "health"),
    Case("list_clients", "paged", nonempty=True, provides="client"),
    Case("get_client", "record", args=_by("client", "a client", "client_id"), same_id="client"),
    Case("list_client_locations", "list", args=_by("client", "a client", "client_id"), nonempty=True),
    Case("list_contacts", "paged", args=lambda found: {"page_size": 20}, provides="contact"),
    Case("get_contact", "record", args=_by("contact", "a contact", "contact_id"), same_id="contact"),
    Case("list_org_groups", "list", nonempty=True),
    Case("list_org_users", "all", nonempty=True, complete=True),
    Case("list_agents", "paged", args=lambda found: {"page_size": 10}, provides="agent"),
    Case("get_agent", "record", args=_by("agent", "an agent", "agent_id"), same_id="agent"),
    Case("list_custom_assets", "paged", args=lambda found: {"page_size": 10}),
    Case("list_alerts", "paged", args=lambda found: {"page_size": 20}),
    # tickets
    Case("list_ticket_statuses", "list", nonempty=True),
    Case("list_ticket_types", "list", nonempty=True),
    Case("list_ticket_tags", "list"),
    Case("list_ticket_priorities", "local", nonempty=True),
    Case("list_ticket_sources", "local", nonempty=True),
    Case("list_tickets", "paged", args=lambda found: {"page_size": 50}, provides="ticket", pick=_pick_ticket),
    Case("search_tickets", "search", args=_search_args),
    Case("get_ticket", "record", label="get_ticket (guid)", args=_ticket_guid, same_id="ticket"),
    Case("get_ticket", "record", label="get_ticket (number)", args=_ticket_number, same_id="ticket"),
    Case("get_ticket", "record", label="get_ticket (display number)", args=_ticket_display, same_id="ticket"),
    Case(
        "list_ticket_comments",
        "paged",
        args=lambda found: {**_ticket_guid(found), "page_size": 20},
        provides="comment",
    ),
    Case(
        "get_ticket_comment",
        "record",
        args=lambda found: {**_ticket_guid(found), "comment_id": found.need("comment", "a ticket comment")},
        same_id="comment",
    ),
    Case("list_ticket_conversations", "list", args=_ticket_guid, provides="approval", pick=_pick_approval),
    Case(
        "get_ticket_approval",
        "record",
        args=lambda found: {**_ticket_guid(found), "approval_id": found.need("approval", "a ticket approval")},
        same_id="approval",
    ),
    # time
    Case("list_billing_roles", "list", nonempty=True),
    Case("list_work_types", "list", nonempty=True),
    Case("list_time_entries", "paged", args=lambda found: {"page_size": 20}, provides="time_entry"),
    Case("get_time_entry", "record", args=_by("time_entry", "a time entry", "time_entry_id"), same_id="time_entry"),
    # billing
    Case("list_contracts", "paged", args=lambda found: {"page_size": 20}, provides="contract"),
    Case("get_contract", "record", args=_by("contract", "a contract", "contract_id"), same_id="contract"),
    Case("list_invoices", "paged", args=lambda found: {"page_size": 20}, provides="invoice"),
    Case("get_invoice", "record", args=_by("invoice", "an invoice", "invoice_id"), same_id="invoice"),
    Case("list_items", "paged", args=lambda found: {"page_size": 20}, provides="item"),
    Case("get_item", "record", args=_by("item", "a catalog item", "item_id"), same_id="item"),
    Case("list_item_categories", "list"),
    Case("list_taxes", "list"),
    # uptime
    Case("list_uptime_checks", "paged", args=lambda found: {"page_size": 20}, provides="check"),
    Case("get_uptime_check", "record", args=_by("check", "an uptime check", "check_id"), same_id="check"),
    # projects (scope Project)
    Case("list_projects", "paged", args=lambda found: {"page_size": 20}, provides="project"),
    Case("get_project", "record", args=_by("project", "a project", "project_id"), same_id="project"),
    Case("list_project_tags", "list"),
    Case("list_project_types", "list"),
    Case("list_project_sections", "list", args=_by("project", "a project", "project_id")),
    Case(
        "list_project_tasks",
        "paged",
        args=lambda found: {"project_id": found.need("project", "a project"), "page_size": 20},
        provides="task",
    ),
    Case("get_project_task", "record", args=_project_task, same_id="task"),
    Case("list_task_conversations", "list", args=_project_task, provides="task_approval", pick=_pick_approval),
    Case(
        "get_task_approval",
        "record",
        args=lambda found: {**_project_task(found), "approval_id": found.need("task_approval", "a task approval")},
        same_id="task_approval",
    ),
    Case(
        "list_project_comments",
        "paged",
        args=lambda found: {"project_id": found.need("project", "a project"), "page_size": 20},
        provides="project_comment",
    ),
    Case(
        "get_project_comment",
        "record",
        args=lambda found: {
            "project_id": found.need("project", "a project"),
            "comment_id": found.need("project_comment", "a project comment"),
        },
        same_id="project_comment",
    ),
    # forms (scope Forms)
    Case("list_forms", "paged", args=lambda found: {"page_size": 20}, provides="form"),
    Case(
        "list_form_responses",
        "paged",
        args=lambda found: {"form_id": found.need("form", "a form"), "page_size": 10},
    ),
)


def _validate(case: Case, found: Found, result: Mapping[str, Any]) -> str:
    """The note of a good answer; ShapeError when the answer is not in the promised shape."""
    shape = case.shape
    if shape == "health":
        return check_health(result)
    if shape in ("list", "local"):
        return check_list(result, nonempty=case.nonempty)
    if shape == "paged":
        return check_paged(result, nonempty=case.nonempty)
    if shape == "all":
        return check_all(result, nonempty=case.nonempty, complete=case.complete)
    if shape == "search":
        return check_search(result)
    if shape == "record":
        expected = None
        if case.same_id is not None:
            held = found.values.get(case.same_id)
            expected = held["Id"] if isinstance(held, dict) else held
        return check_record(result, expected)
    raise ShapeError(f"the plan gives {case.tool} the unknown shape {shape!r}")


# --------------------------------------------------------------------------
# Running the plan
# --------------------------------------------------------------------------


@dataclass
class Row:
    label: str
    outcome: str
    note: str = ""


def format_row(label: str, outcome: str, note: str, width: int) -> str:
    return f"{label:<{width}}{outcome:<15}{note}".rstrip()


@dataclass
class SmokeReport:
    """The rows of a smoke run and what was sent. `ok` is True when no row FAILED."""

    rows: list[Row] = field(default_factory=list)
    requests: int = 0
    refused: int = 0

    @property
    def failed(self) -> list[Row]:
        return [row for row in self.rows if row.outcome == FAILED]

    @property
    def ok(self) -> bool:
        return not self.failed and self.refused == 0

    def counts(self) -> dict[str, int]:
        outcomes = (OK, SCOPE_MISSING, SKIPPED, FAILED)
        return {outcome: sum(1 for row in self.rows if row.outcome == outcome) for outcome in outcomes}

    def summary_lines(self) -> list[str]:
        counts = self.counts()
        return [
            f"{len(self.rows)} checks: {counts[OK]} ok, {counts[SCOPE_MISSING]} scope missing, "
            f"{counts[SKIPPED]} skipped, {counts[FAILED]} FAILED; "
            f"{self.requests} requests sent, {self.refused} refused by the guard",
            "result: " + ("PASSED" if self.ok else "FAILED"),
        ]

    def render(self) -> list[str]:
        """The whole table: header, one line per row, then the summary (run_smoke prints the rows as they come)."""
        width = max([len("tool"), *(len(row.label) for row in self.rows)]) + 2
        lines = [format_row("tool", "outcome", "note", width)]
        lines.extend(format_row(row.label, row.outcome, row.note, width) for row in self.rows)
        return lines + self.summary_lines()


class PlanError(RuntimeError):
    """The plan names a tool that is not a registered read tool. Raised before anything is sent."""


class _Smoke:
    def __init__(
        self,
        session: ToolSession,
        plan: Sequence[Case],
        reads: Mapping[str, str],
        on_row: Callable[[Row], None] | None = None,
    ) -> None:
        self.session = session
        self.plan = plan
        self.reads = reads
        self.on_row = on_row
        self.found = Found()
        self.rows: list[Row] = []

    def add(self, row: Row) -> None:
        self.rows.append(row)
        if self.on_row is not None:
            self.on_row(row)

    def _teach(self, case: Case, outcome: str, result: Mapping[str, Any] | None, reason: str | None = None) -> None:
        if case.provides is None:
            return
        value = case.pick(result) if result is not None else None
        if value is not None:
            self.found.values[case.provides] = value
        elif reason is not None:
            self.found.why[case.provides] = reason
        elif outcome == OK:
            self.found.why[case.provides] = f"{case.tool} returned no rows"
        else:
            self.found.why[case.provides] = f"{case.tool}: {outcome}"

    async def run(self) -> bool:
        """Run the plan. False when the guard refused a request (the rest was not run)."""
        for index, case in enumerate(self.plan):
            if not await self._run_case(case):
                for later in self.plan[index + 1:]:
                    self.add(Row(later.row, SKIPPED, "not run: the guard refused an earlier request"))
                return False
        return True

    async def _run_case(self, case: Case) -> bool:
        try:
            arguments = case.args(self.found)
        except Skip as skip:
            self.add(Row(case.row, SKIPPED, str(skip)))
            self._teach(case, SKIPPED, None, reason=str(skip))
            return True
        try:
            result = await self.session.call(case.tool, arguments)
        except GuardTripped as exc:
            self.add(Row(case.row, FAILED, f"the guard refused the request: {exc}"[:_NOTE_LIMIT]))
            self._teach(case, FAILED, None)
            return False
        except ToolFailed as exc:
            if exc.scope is not None and self.reads.get(case.tool) in SCOPE_TOOLSETS:
                self.add(Row(case.row, SCOPE_MISSING, f"{exc.scope} scope"))
                self._teach(case, SCOPE_MISSING, None)
            else:
                self.add(Row(case.row, FAILED, exc.text[:_NOTE_LIMIT]))
                self._teach(case, FAILED, None)
            return True
        try:
            note = _validate(case, self.found, result)
        except ShapeError as exc:
            self.add(Row(case.row, FAILED, str(exc)))
            self._teach(case, FAILED, None)
            return True
        self.add(Row(case.row, OK, note))
        self._teach(case, OK, result)
        return True


def plan_problems(plan: Sequence[Case], reads: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """(tools the plan calls that are not registered read tools, read tools the plan does not cover)."""
    named = {case.tool for case in plan}
    return sorted(named - set(reads)), sorted(set(reads) - named)


async def run_smoke(
    *,
    settings: Settings | None = None,
    transport: Any = None,
    pace: float = PACE_SECONDS,
    echo: Callable[[str], None] = emit,
    cases: Sequence[Case] | None = None,
) -> SmokeReport:
    """Call every read tool once (see the module docstring) and print the table through `echo`: the header, each
    row as soon as its call is done (a run takes a minute or more), then the summary.

    settings   default live_settings() (reads the API key from the app .env); tests pass their own.
    transport  an httpx transport for the Gorelo client (tests pass httpx.MockTransport); default is the network.
    pace       seconds between two requests (0 in tests).
    cases      the plan, default CASES.
    Raises PlanError, before any request, when the plan names a tool that is not a registered read tool.
    """
    plan = tuple(CASES if cases is None else cases)
    reads = read_tools()
    foreign, uncovered = plan_problems(plan, reads)
    if foreign:
        raise PlanError("the smoke plan names tools that are not registered read tools: " + ", ".join(foreign))
    site()  # no site config, no run: refuse before the API key is read or anything is sent
    chosen = settings if settings is not None else live_settings()
    await live_guard.verify_site_clients(chosen.api_key, transport=transport)  # read-only, but the ids must be the named clients
    width = max([len("tool"), *(len(case.row) for case in plan)]) + 2

    def say(text: str) -> None:
        echo(scrub(text, chosen.api_key))

    def show(row: Row) -> None:
        say(format_row(row.label, row.outcome, row.note, width))

    say(format_row("tool", "outcome", "note", width))
    async with open_session(chosen, transport=transport, pace=pace) as session:
        smoke = _Smoke(session, plan, reads, on_row=show)
        await smoke.run()
        for tool in uncovered:
            smoke.add(Row(tool, FAILED, "a read tool with no smoke case: add one to scripts/live/smoke.py"))
        report = SmokeReport(rows=smoke.rows, requests=session.pacer.requests, refused=len(session.guard.violations))
    for line in report.summary_lines():
        say(line)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.live.smoke",
        description="Call every read tool of the Gorelo MCP server once against the live API (GET only, "
        "about one request per second) and check the shape of each answer.",
    )
    parser.parse_args(argv)
    try:
        report = asyncio.run(run_smoke())
    except (EnvError, SiteConfigError) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
