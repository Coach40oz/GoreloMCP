"""Time entries and their lookups: billing roles, work types (toolset "time").

Seven tools, every one in toolset "time":

    list_time_entries    read         GET /v1/time-entries (paged)
    get_time_entry       read         GET /v1/time-entries/{timeEntryId}
    create_time_entry    write        POST /v1/time-entries, then GET /v1/time-entries/{timeEntryId} to return the record;
                                      a ticket number or display number is first looked up (GET /v1/tickets), and
                                      auto_service_line reads the ticket and, unless the ticket has its own service line,
                                      the client's Active contracts first
    update_time_entry    write        PATCH /v1/time-entries/{timeEntryId}; answers with the full entry; destructive_hint
    list_billing_roles   read         GET /v1/billing-roles (unpaged)
    list_work_types      read         GET /v1/work-types (unpaged)
    delete_time_entry    destructive  DELETE /v1/time-entries/{timeEntryId}; needs confirm=true

Ops owned by this module (each must exist in spec/spec_index.json, none may be in FORBIDDEN_OPS; keep
the list in step with the ops=[...] of the declarations below):

    GET /v1/time-entries
    GET /v1/time-entries/{timeEntryId}
    POST /v1/time-entries
    PATCH /v1/time-entries/{timeEntryId}
    DELETE /v1/time-entries/{timeEntryId}
    GET /v1/billing-roles
    GET /v1/work-types

and, for create_time_entry only, three reads that happen before the POST:

    GET /v1/tickets                  a ticket number or display number (tools/_ticket_ref.py); never for a GUID
    GET /v1/tickets/{ticketId}       auto_service_line: the ticket's ClientId, LocationId and BillingOverride.ServiceLine
    GET /v1/contracts                auto_service_line: the client's Active contracts, every page (not read when the ticket
                                     has its own service line)

Rules that shape the code (the evidence is in docs/API-OBSERVED-BEHAVIOR.md and in spec/spec_index.json):

* BillableStatusId is 1 Billable, 2 No charge, 3 Non-billable (match on Id, never on Name). Durations are
  decimal hours. Datetimes are UTC instants and always go through utc_iso (an offset is required).
* actual_hours and distance are strict numbers (Annotated[float, Strict()] with a bound): FastMCP's default lax mode would
  turn JSON true into 1.0 and false into 0.0 before the tool ran, so distance=false would be sent as a distance of 0.
* Every integer id parameter is a StrictId (JSON true, "5" or 5.0 is refused by the schema) and is then
  range-checked by positive_id / positive_ids; GUID parameters go through guid / guids.
* ServiceLineId null is not the same as ServiceLineId omitted. Omitted means "default it" on create and
  "keep it" on update; an explicit null takes the entry off every contract. The tools spell the null
  no_service_line=true (create) and remove_from_contract=true (update) and send it through build_body's
  clear mechanism, so the key is present with a JSON null and can never be produced by accident.
* POST answers with only {"Id": ...}: create_time_entry takes the Id with created_id (a missing or unusable
  Id raises an unconfirmed-write shape error) and reads the entry back with reread_after_write, which
  returns {"Id", "warning"} rather than raising if that read fails after the write succeeded. PATCH answers
  with the full TimeEntryModel, so update_time_entry does not re-read; its answer and the DELETE answer go
  through expect_object (a write whose Data is not a non-empty object raises).
* A time entry is attached to a ticket or to a task, never both and never neither; at least two of
  StartedOn, EndedOn and ActualHours are needed (Gorelo derives the third and, when all three are given,
  checks that they agree).
* The only delete this module offers is delete_time_entry, behind the destructive gate and confirm=true.
* create_time_entry takes ticket_id as a GUID, a number or a display number (resolve_ticket_id; a GUID costs no request) and
  has two helps for the service line. auto_service_line=true reads the ticket (ClientId, LocationId, BillingOverride, which
  must be an object whose ServiceLine is null or {Id, Name}). The ticket's own service line wins: its Id is sent, no contracts
  are read and a `note` says so. Otherwise the client's Active contracts are read (StatusIds 1, every page) and only those
  that cover the ticket count: IsForAllLocations true or the ticket's LocationId in LocationIds, and the entry's UTC day (the
  day of started_on, else of ended_on minus actual_hours) not clearly outside StartDate and EndDate (a margin of one day on each side,
  because of time zones). A ticket with no location and a location-scoped contract with an Hourly line is refused (the tool
  cannot tell whether it applies; pass service_line_id). A contract with an Hourly line that covers the day only through the
  margin (it starts the day after, or ended the day before) is refused the same way, naming the contract and its date; one
  with no Hourly line is skipped with a reason. The Id of the one covering service line whose LaborTerms is Hourly
  (HOURLY_LABOR_TERMS, compared by the code Id and not by the name) is sent; several are refused and listed; none sends no line.
  The other labor lines of the covering contracts (any LaborTerms Id that is neither Hourly nor NO_LABOR_TERMS: Unlimited,
  Block and Limited Hours, and a code this tool does not know) are collected too; when one Hourly line is sent beside some, a
  `note` says so (see below). A contract row of another client or not Active, a count mismatch, an unusable IsForAllLocations,
  LocationIds, StartDate or a missing or unusable EndDate (null is open-ended), and a ticket with no ClientId or no LocationId
  key (null is valid) are shape errors. The constants ACTIVE_CONTRACT_STATUS, HOURLY_LABOR_TERMS and NO_LABOR_TERMS are plain
  ints here (no tool module imports another); a test pins them to the tables of tools/contracts.py. Every read happens before
  the POST, so a refusal writes nothing, and every purely local refusal (a blank comment, an entry day that cannot be computed)
  comes before the first request. The list filter ticket_ids stays GUID only.
* Notes and warnings come from the read-back, never from what was sent (the key is added beside the record and never replaces
  a field or an existing warning, it is appended to it). A service line Id was sent (service_line_id, the pick or the
  ticket's own line) and the record has no ServiceLine or another one: a `warning` (check the entry before it is invoiced, do
  not log the time again). A record with no ServiceLine KEY is not "no service line": no claim is made about the line or the
  rate and a `warning` says the field is missing (check with get_time_entry). The ticket's own line and the "other labor lines"
  notes are added only when the record shows the line that was sent. auto_service_line found no Hourly line: a `note` that depends on the record (Gorelo applied a line
  by its own rule, a Billable entry with no line bills at the billing role's default rate, a not Billable one says nothing
  about rates, a failed read-back says to check ServiceLine with get_time_entry) and that counts and names the contracts it
  skipped for their location or dates. Nothing was sent and no auto_service_line or no_service_line: a Billable entry that
  reads back with no ServiceLine gets a `note` (a ticket entry is told service_line_id or auto_service_line=true, a task entry
  only service_line_id). no_service_line=true: no note.
* Two 409 refusals that Gorelo gives (plain refusals, Gorelo wrote nothing): create_time_entry ("Time entry
  cannot be created because ticket <id> is closed. Reopen the ticket first.") and update_time_entry ("Time entry <id>
  cannot be changed because it has already been invoiced. Only a time entry that is still open can be changed."). The
  description of create_time_entry says so before the call (a closed ticket refuses time, so log time before closing or ask the
  user before reopening it). The descriptions of update_time_entry and delete_time_entry already say that an invoiced entry is
  refused (409), so they got no new sentence. Gorelo's own 409 text still reaches the model unchanged. The tests keep the Gorelo
  codes of those two answers.
* Shape of a record (live probe 2026-10-03): GET /v1/time-entries and GET /v1/time-entries/{timeEntryId} return the
  published TimeEntryModel, which names the user, the ticket and the task as objects: User {Id, Name}, Ticket
  {Id, Number, Title} and Task {Id, Number, Title}, with Task null for a ticket entry (the spec: one of Ticket and Task
  is set and the other is null). There is no flat UserId, TicketId or TaskId. On 2026-10-02 the live API returned
  the flat ids UserId, TicketId and TaskId instead (see docs/API-OBSERVED-BEHAVIOR.md),
  so a reader keys on the objects. The tools hand records back unchanged and never read those fields, so no code here
  depends on either shape. The answer of PATCH was not probed for this, which is why only the two GET tools say so.
"""

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any

from fastmcp import Context
from pydantic import Field, Strict

from gorelo_client import GoreloAPIError, GoreloClient
from tools._common import (
    StrictBool,
    StrictId,
    build_body,
    clamp_page_size,
    client_of,
    created_id,
    csv_ids,
    describe_value,
    expect_object,
    gorelo_tool,
    guid,
    guids,
    list_result,
    non_empty,
    paged_result,
    positive_id,
    positive_ids,
    reread_after_write,
    require_confirm,
    utc_iso,
)
from tools._ticket_ref import TICKET_LIST_OP, resolve_ticket_id

LIST_OP = "GET /v1/time-entries"
GET_OP = "GET /v1/time-entries/{timeEntryId}"
CREATE_OP = "POST /v1/time-entries"
UPDATE_OP = "PATCH /v1/time-entries/{timeEntryId}"
DELETE_OP = "DELETE /v1/time-entries/{timeEntryId}"
ROLES_OP = "GET /v1/billing-roles"
WORK_TYPES_OP = "GET /v1/work-types"
TICKET_GET_OP = "GET /v1/tickets/{ticketId}"  # auto_service_line: the ticket's ClientId, LocationId and own service line
CONTRACTS_OP = "GET /v1/contracts"  # auto_service_line: the client's Active contracts, their scope and their service lines

# Gorelo's BillableStatus ids. There is no lookup endpoint and no enum in the spec: this table is the contract.
BILLABLE_STATUSES = {1: "Billable", 2: "No charge", 3: "Non-billable"}
BILLABLE_STATUS_ID = 1  # the key of "Billable" above

# auto_service_line: the StatusIds value of an Active contract and the LaborTerms code of an Hourly service line of
# GET /v1/contracts. Plain ints on purpose (no tool module imports another); a test pins them to the tables of tools/contracts.py.
ACTIVE_CONTRACT_STATUS = 1
HOURLY_LABOR_TERMS = 2
NO_LABOR_TERMS = 4  # "No Labor Terms": a line that bills no labour. Every other code that is not Hourly counts as another labor line.
CONTRACTS_PAGE_SIZE = 200
SKIPPED_LISTED = 5  # contracts named in a note before "and N more"
OTHER_LINES_LISTED = 3  # other labor lines named in a note before "and N more"

# Why auto_service_line skipped an Active contract (named in the note when it found no Hourly line).
SKIP_DATES = "outside its dates"
SKIP_LOCATION = "not for the ticket's location"
SKIP_NO_LOCATION = "limited to locations and the ticket has none"

# Notes and warnings. They say what the read-back of the entry shows and what was sent, nothing else.
OWN_LINE_NOTE = "The ticket's own service line {label} (Id {line_id}) was used."
OTHER_LABOR_NOTE = (
    "auto_service_line sent the Hourly line {label} (Id {line_id}), but the covering contracts also have {count}: {listed}. "
    "Gorelo may have picked one of those on its own; service_line_id picks another line, and leaving out auto_service_line "
    "lets Gorelo choose."
)
LINE_LOST_WARNING = (
    "service line {sent} was sent but the entry reads back with {shown}; check the entry in Gorelo before it is "
    "invoiced; do not log the time again."
)
SHOWN_NONE = "no service line"
SHOWN_LINE = "service line {label} (Id {line_id})"
SHOWN_UNREADABLE = "a ServiceLine that cannot be read"
NO_FIELD_WARNING = (
    "The answer carries no ServiceLine field, so the service line could not be checked; check the entry with get_time_entry."
)
NO_FIELD_SENT_WARNING = (
    "service line {sent} was sent but the answer carries no ServiceLine field, so it could not be checked; check the entry "
    "with get_time_entry before it is invoiced; do not log the time again."
)
NO_HOURLY_SENT = "auto_service_line found no Hourly service line for this ticket, so none was sent"
NO_HOURLY_APPLIED = "; Gorelo applied service line {label} (Id {line_id}) by its own rule."
NO_HOURLY_BILLS_DEFAULT = "; the entry has no service line and bills at the billing role's default rate."
NO_HOURLY_UNREAD = "; the entry could not be read back, so check its ServiceLine with get_time_entry."
NO_HOURLY_UNREADABLE = "; the entry's ServiceLine could not be read from Gorelo's answer, so check it with get_time_entry."
NO_SERVICE_LINE_NOTE_TICKET = (
    "The entry has no service line and bills at the billing role's default rate. A client with a contract usually needs "
    "service_line_id (get_contract lists the lines) or auto_service_line=true."
)
NO_SERVICE_LINE_NOTE_TASK = (
    "The entry has no service line and bills at the billing role's default rate. A client with a contract usually needs "
    "service_line_id (get_contract lists the lines)."
)

# CreateTimeEntryCommand.Attachments: "The serialised list must be 5000 characters or fewer."
ATTACHMENTS_MAX_CHARS = 5000

# snake_case tool parameter -> Gorelo query name (GET /v1/time-entries). The same map turns a Gorelo
# PropertyName back into the parameter to fix in error messages.
LIST_QUERY = {
    "client_ids": "ClientIds",
    "location_ids": "LocationIds",
    "ticket_ids": "TicketIds",
    "task_ids": "TaskIds",
    "user_ids": "UserIds",
    "invoice_ids": "InvoiceIds",
    "started_since": "StartedSince",
    "started_before": "StartedBefore",
    "created_since": "CreatedSince",
    "created_before": "CreatedBefore",
    "updated_since": "UpdatedSince",
    "updated_before": "UpdatedBefore",
}
LIST_FIELDS = {**LIST_QUERY, "page_size": "PageSize", "cursor": "Cursor"}

# snake_case tool parameter -> CreateTimeEntryCommand field. Every spec field is here and nothing else, except
# CreatedOn (new in contract 6c30770dffc0), which 1.5.0 leaves out: an entry sent with it is a
# backdated historical import, never invoiced, and cannot be changed or deleted through the API afterwards.
CREATE_FIELDS = {
    "ticket_id": "TicketId",
    "task_id": "TaskId",
    "user_id": "UserId",
    "started_on": "StartedOn",
    "ended_on": "EndedOn",
    "actual_hours": "ActualHours",
    "billable_status_id": "BillableStatusId",
    "billing_role_id": "BillingRoleId",
    "work_type_id": "WorkTypeId",
    "service_line_id": "ServiceLineId",
    "comment": "Comment",
    "distance": "Distance",
    "attachments": "Attachments",
}

# snake_case tool parameter -> UpdateTimeEntryCommand field. TicketId and Attachments cannot be changed
# (Gorelo answers 400), so they are not here.
UPDATE_FIELDS = {
    "started_on": "StartedOn",
    "ended_on": "EndedOn",
    "actual_hours": "ActualHours",
    "billable_status_id": "BillableStatusId",
    "user_id": "UserId",
    "billing_role_id": "BillingRoleId",
    "work_type_id": "WorkTypeId",
    "service_line_id": "ServiceLineId",
    "comment": "Comment",
    "distance": "Distance",
}

# Clearing service_line_id means "send null" (take the entry off its contract); clearing comment sends "".
_CLEAR_VALUES = {"service_line_id": None}


# --------------------------------------------------------------------------
# Local validation (every message names the snake_case parameter)
# --------------------------------------------------------------------------


def _optional_id(param: str, value: Any) -> int | None:
    """positive_id for an optional parameter: None (not given) stays None."""
    return None if value is None else positive_id(param, value)


def _moment(text: str) -> datetime:
    """Parse the output of utc_iso (...Z) back into an aware datetime."""
    return datetime.fromisoformat(text)


def _check_order(earlier_param: str, earlier: str | None, later_param: str, later: str | None, *, why: str) -> None:
    """Both are utc_iso strings or None. When both are given `later` must be after `earlier`."""
    if earlier is not None and later is not None and _moment(later) <= _moment(earlier):
        raise ValueError(
            f"{later_param}: must be later than {earlier_param} ({earlier_param}={earlier}, {later_param}={later}); {why}"
        )


def _hours(param: str, value: Any) -> float | None:
    """Decimal hours: a finite number greater than 0."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"{param}: must be a number of decimal hours greater than 0, for example 1.5, got {describe_value(value)}"
        )
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{param}: must be a number of decimal hours greater than 0, for example 1.5, got {value!r}")
    return value


def _distance(param: str, value: Any) -> float | None:
    """Distance travelled: a finite number of 0 or more."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{param}: must be a distance of 0 or more, got {describe_value(value)}")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{param}: must be a distance of 0 or more, got {value!r}")
    return value


def _billable_status(param: str, value: Any) -> int | None:
    """1 Billable, 2 No charge or 3 Non-billable."""
    if value is None:
        return None
    accepted = ", ".join(f"{status} ({label})" for status, label in BILLABLE_STATUSES.items())
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{param}: must be one of {accepted}, got {describe_value(value)}")
    if value not in BILLABLE_STATUSES:
        raise ValueError(f"{param}: must be one of {accepted}, got {value}")
    return value


def _attachments(param: str, value: Any) -> list[dict[str, str]] | None:
    """[{"name", "url"}] as Gorelo's [{"Name", "Url"}]. Checked here because Gorelo cannot say which item was bad."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(
            f'{param}: expected a non-empty list of objects such as [{{"name": "report.pdf", "url": "https://..."}}]; '
            f"omit {param} to attach nothing"
        )
    cleaned: list[dict[str, str]] = []
    for position, item in enumerate(value):
        where = f"{param}[{position}]"
        if not isinstance(item, Mapping):
            raise ValueError(f'{where}: must be an object with the keys "name" and "url", got {describe_value(item)}')
        unknown = sorted(str(key) for key in item if key not in ("name", "url"))
        if unknown:
            raise ValueError(f'{where}: unknown key(s) {", ".join(unknown)}; only "name" and "url" are accepted')
        for key in ("name", "url"):
            text = item.get(key)
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"{where}.{key}: required, and must be non-empty text")
        cleaned.append({"Name": item["name"], "Url": item["url"]})
    # a lower bound of what Gorelo measures (compact JSON, nothing escaped), so it never refuses a list Gorelo accepts
    size = len(json.dumps(cleaned, separators=(",", ":"), ensure_ascii=False))
    if size > ATTACHMENTS_MAX_CHARS:
        raise ValueError(
            f"{param}: the serialised list is {size} characters and Gorelo accepts at most {ATTACHMENTS_MAX_CHARS}; "
            "attach fewer or shorter entries"
        )
    return cleaned


# --------------------------------------------------------------------------
# auto_service_line: the ticket's own service line, else the one Hourly line that covers the ticket (all reads, before the POST)
# --------------------------------------------------------------------------


def _entry_day(started: str | None, ended: str | None, hours: float | None) -> date:
    """The UTC day of the entry: the day of started_on when it is given, else the day of ended_on minus actual_hours.

    started and ended are utc_iso strings. create_time_entry needs two of started_on, ended_on and actual_hours, so without
    started_on the other two are there; anything else is a bug and raises instead of guessing a day.
    """
    if started is not None:
        return _moment(started).astimezone(timezone.utc).date()
    if ended is None or hours is None:
        raise ValueError("started_on, ended_on and actual_hours: cannot date the entry without started_on or both of the others")
    try:
        span = timedelta(hours=hours)
    except OverflowError:
        raise ValueError(f"actual_hours: {hours!r} is too large to date the entry (auto_service_line needs a real day)") from None
    try:
        return (_moment(ended) - span).astimezone(timezone.utc).date()
    except OverflowError:
        raise ValueError(
            f"ended_on and actual_hours: ended_on={ended} minus actual_hours={hours!r} falls outside the dates that can be "
            "represented, so auto_service_line cannot date the entry; check both values"
        ) from None


def _unusable(op: str, detail: str) -> GoreloAPIError:
    """A successful READ whose answer cannot be used. Nothing has been written yet when it is raised."""
    return GoreloAPIError(
        f"{op}: {detail}; refusing to guess, so nothing was written", status=200, op_key=op, kind="shape"
    )


def _whole_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _label(name: Any) -> str:
    return repr(name.strip()) if isinstance(name, str) and name.strip() else "(no name)"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


@dataclass(frozen=True)
class _Ticket:
    client_id: int | None
    location_id: int | None
    own_line: tuple[int, str] | None  # BillingOverride.ServiceLine as (Id, label), None when the ticket has none


async def _read_ticket(client: GoreloClient, ticket: str) -> _Ticket:
    """The ClientId, LocationId and BillingOverride.ServiceLine of the ticket, or a shape error for anything unusable."""
    data = await client.get_one(TICKET_GET_OP, path_params={"ticketId": ticket}, tool="create_time_entry")
    if not isinstance(data, dict):
        raise _unusable(TICKET_GET_OP, f"Data is {describe_value(data)}, not the ticket")
    for key in ("ClientId", "LocationId"):
        if key not in data:
            raise _unusable(TICKET_GET_OP, f"Data has no {key} (null when the ticket has none)")
    client_id = data["ClientId"]
    if client_id is not None and not _whole_id(client_id):
        raise _unusable(TICKET_GET_OP, f"ClientId is {describe_value(client_id)}, not a client id")
    location_id = data["LocationId"]
    if location_id is not None and not _whole_id(location_id):
        raise _unusable(TICKET_GET_OP, f"LocationId is {describe_value(location_id)}, not a location id")
    override = data.get("BillingOverride")
    if not isinstance(override, dict):
        raise _unusable(TICKET_GET_OP, f"BillingOverride is {describe_value(override)}, not an object")
    if "ServiceLine" not in override:
        raise _unusable(TICKET_GET_OP, "BillingOverride has no ServiceLine (null when the ticket has none)")
    own = override["ServiceLine"]
    if own is None:
        return _Ticket(client_id, location_id, None)
    if not isinstance(own, dict) or not _whole_id(own.get("Id")):
        raise _unusable(TICKET_GET_OP, "BillingOverride.ServiceLine is neither null nor an object with a whole number Id")
    return _Ticket(client_id, location_id, (own["Id"], _label(own.get("Name"))))


@dataclass(frozen=True)
class _Contract:
    label: str
    start: date
    end: date | None
    all_locations: bool
    location_ids: frozenset[int]
    hourly: dict[int, str]  # Id -> label of each Hourly service line
    others: dict[int, str]  # Id -> "<label> (<labor terms name>)" of each other labor line (not Hourly, not No Labor Terms)


_DAY_TEXT = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[T ].*)?")


def _day(text: Any) -> date | None:
    """The calendar day of a Gorelo date (2026-10-01, or a date-time starting with one), None when it is not one."""
    found = _DAY_TEXT.fullmatch(text.strip()) if isinstance(text, str) else None
    if found is None:
        return None
    try:
        return date.fromisoformat(found.group(1))
    except ValueError:
        return None


def _read_contract(position: int, row: Any, client_id: int) -> _Contract:
    """One row of GET /v1/contracts as a _Contract. Anything Gorelo's filters or the model should have guaranteed and did
    not is a shape error: a row is never used on a guess."""
    where = f"contract {position}"
    if not isinstance(row, dict):
        raise _unusable(CONTRACTS_OP, f"{where} is {describe_value(row)}, not an object")
    owner = row.get("ClientId")
    if not _whole_id(owner) or owner != client_id:
        raise _unusable(CONTRACTS_OP, f"{where} does not belong to the ticket's client (Gorelo's ClientIds filter did not hold)")
    status = row.get("Status")
    status_id = status.get("Id") if isinstance(status, dict) else None
    if not _whole_id(status_id) or status_id != ACTIVE_CONTRACT_STATUS:
        raise _unusable(CONTRACTS_OP, f"{where} is not an Active contract (Gorelo's StatusIds filter did not hold)")
    everywhere = row.get("IsForAllLocations")
    if not isinstance(everywhere, bool):
        raise _unusable(CONTRACTS_OP, f"{where} has IsForAllLocations as {describe_value(everywhere)}, not true or false")
    locations = row.get("LocationIds")
    if not isinstance(locations, list) or not all(_whole_id(item) for item in locations):
        raise _unusable(CONTRACTS_OP, f"{where} has LocationIds as {describe_value(locations)}, not a list of location ids")
    start = _day(row.get("StartDate"))
    if start is None:
        raise _unusable(CONTRACTS_OP, f"{where} has StartDate as {describe_value(row.get('StartDate'))}, not a date")
    if "EndDate" not in row:
        raise _unusable(CONTRACTS_OP, f"{where} has no EndDate (null when the contract is open-ended)")
    end = None
    if row["EndDate"] is not None:
        end = _day(row["EndDate"])
        if end is None:
            raise _unusable(CONTRACTS_OP, f"{where} has EndDate as {describe_value(row['EndDate'])}, not a date")
    lines = row.get("ServiceLines")
    if not isinstance(lines, list):
        raise _unusable(CONTRACTS_OP, f"{where} has ServiceLines as {describe_value(lines)}, not a list")
    hourly: dict[int, str] = {}
    others: dict[int, str] = {}
    for line in lines:
        if not isinstance(line, dict) or not _whole_id(line.get("Id")):
            raise _unusable(CONTRACTS_OP, f"{where} has a service line without a whole number Id")
        terms = line.get("LaborTerms")
        if not isinstance(terms, dict) or not _whole_id(terms.get("Id")):
            raise _unusable(CONTRACTS_OP, f"{where}, service line {line['Id']}, has no usable LaborTerms Id")
        if terms["Id"] == HOURLY_LABOR_TERMS:
            hourly[line["Id"]] = _label(line.get("Name"))
        elif terms["Id"] != NO_LABOR_TERMS:  # Unlimited, Block, Limited Hours, and any code this tool does not know
            name = terms.get("Name")
            shown = name.strip() if isinstance(name, str) and name.strip() else f"labor terms Id {terms['Id']}"
            others[line["Id"]] = f"{_label(line.get('Name'))} ({shown})"
    return _Contract(_label(row.get("Name")), start, end, everywhere, frozenset(locations), hourly, others)


@dataclass(frozen=True)
class _Pick:
    """What auto_service_line decided: the line to send (None: none), whether it is the ticket's own, and the contracts it skipped."""

    line_id: int | None
    own_label: str | None = None  # set when line_id is the ticket's own service line
    skipped: tuple[str, ...] = ()  # "<contract> (<why>)" for each Active contract that does not cover the ticket or the day
    hourly_label: str | None = None  # the label of the Hourly line that was picked
    others: tuple[str, ...] = ()  # "<line> (<labor terms>)" for each other labor line of the contracts that cover the ticket


def _margin_refusal(contract: _Contract, day: date) -> str:
    """The refusal for a contract with an Hourly line that covers the entry's day only through the one-day margin."""
    if contract.start > day:
        when = f"starts on {contract.start.isoformat()}, the day after"
    else:
        when = f"ended on {contract.end.isoformat()}, the day before"
    return (
        f"auto_service_line: contract {contract.label} {when} the entry's day ({day.isoformat()}, UTC), and has an Hourly "
        "service line, so the tool cannot tell whether it applies (Gorelo's contract dates are calendar days in a time zone "
        "it does not know); pass service_line_id with the Id of the line to bill"
    )


async def _auto_service_line(client: GoreloClient, ticket: str, day: date) -> _Pick:
    """The service line to send for this ticket and day. Every read happens here, before the POST.

    The ticket's own service line (BillingOverride.ServiceLine) wins and costs no contracts request. Otherwise the Active
    contracts of the ticket's client are read and only those that cover the ticket count: its location (IsForAllLocations, or
    its LocationId in LocationIds) and the day of the entry. Exactly one Hourly line among them is sent; several are a
    ValueError that lists them, none is a pick with no line. A contract with an Hourly line that covers the day only through
    the one-day margin is a ValueError too. The pick also carries the other labor lines of the covering contracts.
    """
    seen = await _read_ticket(client, ticket)
    if seen.own_line is not None:
        return _Pick(line_id=seen.own_line[0], own_label=seen.own_line[1])
    if seen.client_id is None:
        raise ValueError(
            "auto_service_line: the ticket has no client (ClientId is empty), so there are no contracts to search; "
            "pass service_line_id or no_service_line, or leave auto_service_line false"
        )
    scan = await client.get_all(
        CONTRACTS_OP,
        query={
            "ClientIds": csv_ids("client_ids", [seen.client_id]),
            "StatusIds": csv_ids("status_ids", [ACTIVE_CONTRACT_STATUS]),
        },
        page_size=CONTRACTS_PAGE_SIZE,
        tool="create_time_entry",
    )
    if scan.count_mismatch:
        raise _unusable(
            CONTRACTS_OP, f"Gorelo reported {scan.total_count} contracts but {len(scan.items)} rows were read"
        )
    # every row is checked before any is used, so a good Hourly line beside a bad row cannot rescue it
    contracts = [_read_contract(position, row, seen.client_id) for position, row in enumerate(scan.items, start=1)]
    # The margin of one day on both sides is on purpose: the entry's day is a UTC date, while Gorelo's contract dates are
    # calendar dates in a time zone this tool does not know, so only a contract clearly outside the day is skipped, and one
    # that covers the day through the margin alone (it starts the day after, or ended the day before) is refused when it has
    # an Hourly line (the tool cannot tell whether it applies) and skipped with a reason when it has none.
    earliest, latest = day - timedelta(days=1), day + timedelta(days=1)
    found: dict[int, tuple[str, str]] = {}  # line Id -> (contract label, line label)
    others: dict[int, str] = {}  # line Id -> "<label> (<labor terms>)" of the other labor lines of the covering contracts
    skipped: list[str] = []
    for contract in contracts:
        if contract.start > latest or (contract.end is not None and contract.end < earliest):
            skipped.append(f"{contract.label} ({SKIP_DATES})")
        elif contract.all_locations or (seen.location_id is not None and seen.location_id in contract.location_ids):
            if contract.start > day or (contract.end is not None and contract.end < day):
                # inside the margin only: the contract may or may not apply, depending on a time zone this tool does not know
                if contract.hourly:
                    raise ValueError(_margin_refusal(contract, day))
                skipped.append(f"{contract.label} ({SKIP_DATES})")
                continue
            found.update({line_id: (contract.label, name) for line_id, name in contract.hourly.items()})
            others.update(contract.others)
        elif seen.location_id is None and contract.hourly:
            raise ValueError(
                f"auto_service_line: the ticket has no location (LocationId is empty) and contract {contract.label} is "
                "limited to locations and has an Hourly service line, so the tool cannot tell whether it applies; "
                "pass service_line_id with the Id of the line to bill"
            )
        else:
            skipped.append(f"{contract.label} ({SKIP_NO_LOCATION if seen.location_id is None else SKIP_LOCATION})")
    if len(found) > 1:
        listing = "; ".join(
            f"contract {contract}, line {line_name}, line Id {line_id}" for line_id, (contract, line_name) in found.items()
        )
        raise ValueError(
            f"auto_service_line: client {seen.client_id} has {len(found)} Hourly service lines on Active contracts that "
            f"cover the ticket ({listing}); pass service_line_id with the Id of the one to bill"
        )
    picked = next(iter(found), None)
    return _Pick(
        line_id=picked,
        skipped=tuple(skipped),
        hourly_label=found[picked][1] if picked is not None else None,
        others=tuple(others.values()) if picked is not None else (),
    )


# --------------------------------------------------------------------------
# The notes and warnings of create_time_entry: they say only what the read-back shows
# --------------------------------------------------------------------------


def _skipped_text(skipped: tuple[str, ...]) -> str:
    """Empty, or a sentence that counts and names the contracts auto_service_line skipped."""
    if not skipped:
        return ""
    listed = "; ".join(skipped[:SKIPPED_LISTED])
    more = f"; and {len(skipped) - SKIPPED_LISTED} more" if len(skipped) > SKIPPED_LISTED else ""
    return (
        f" {_plural(len(skipped), 'Active contract')} skipped because the contract does not cover the ticket's location "
        f"or the entry's day: {listed}{more}."
    )


def _other_lines_text(others: tuple[str, ...]) -> tuple[str, str]:
    """("N other labor lines", "<line> (<terms>); ... and M more") for the note about the other labor lines."""
    listed = "; ".join(others[:OTHER_LINES_LISTED])
    more = f"; and {len(others) - OTHER_LINES_LISTED} more" if len(others) > OTHER_LINES_LISTED else ""
    return _plural(len(others), "other labor line"), listed + more


def _read_back_line(record: dict[str, Any]) -> tuple[str, Any]:
    """What the record says about the service line: ("failed", None) for the {"Id", "warning"} of a failed read-back,
    ("missing", None) when the record has no ServiceLine key at all (not the same as a null one), ("none", None),
    ("line", (Id, label)) or ("unreadable", None)."""
    if set(record) == {"Id", "warning"}:
        return "failed", None
    if "ServiceLine" not in record:
        return "missing", None
    shown = record["ServiceLine"]
    if shown is None:
        return "none", None
    if isinstance(shown, dict) and _whole_id(shown.get("Id")):
        return "line", (shown["Id"], _label(shown.get("Name")))
    return "unreadable", None


def _is_billable(record: dict[str, Any]) -> bool:
    status = record.get("BillableStatus")
    code = status.get("Id") if isinstance(status, dict) else None
    return type(code) is int and code == BILLABLE_STATUS_ID


def _append(result: dict[str, Any], key: str, text: str) -> None:
    """Add `text` under `key` without replacing what the result already has there."""
    result[key] = f"{result[key]} {text}" if key in result else text


def _annotate(
    record: dict[str, Any],
    *,
    sent: int | None,
    pick: _Pick | None,
    no_service_line: bool,
    for_ticket: bool,
) -> dict[str, Any]:
    """The record, plus a `note` or `warning` built from what the read-back shows. Existing keys are never replaced.

    sent: the ServiceLineId that went out (service_line_id, the pick of auto_service_line or the ticket's own line), else None.
    pick: the decision of auto_service_line, None when it was not used.
    """
    result = dict(record)
    kind, shown = _read_back_line(record)
    if kind == "missing":
        _append(result, "warning", NO_FIELD_SENT_WARNING.format(sent=sent) if sent is not None else NO_FIELD_WARNING)
    if sent is not None:
        if kind == "line" and shown[0] == sent and pick is not None:  # the read-back shows the line that was sent
            if pick.own_label is not None:
                _append(result, "note", OWN_LINE_NOTE.format(label=pick.own_label, line_id=sent))
            elif pick.others:
                count, listed = _other_lines_text(pick.others)
                _append(
                    result,
                    "note",
                    OTHER_LABOR_NOTE.format(label=pick.hourly_label, line_id=sent, count=count, listed=listed),
                )
        if kind == "none":
            _append(result, "warning", LINE_LOST_WARNING.format(sent=sent, shown=SHOWN_NONE))
        elif kind == "unreadable":
            _append(result, "warning", LINE_LOST_WARNING.format(sent=sent, shown=SHOWN_UNREADABLE))
        elif kind == "line" and shown[0] != sent:
            _append(result, "warning", LINE_LOST_WARNING.format(sent=sent, shown=SHOWN_LINE.format(label=shown[1], line_id=shown[0])))
    elif pick is not None:  # auto_service_line found no Hourly line, so none was sent
        if kind == "failed":
            tail = NO_HOURLY_UNREAD
        elif kind in ("unreadable", "missing"):
            tail = NO_HOURLY_UNREADABLE
        elif kind == "line":
            tail = NO_HOURLY_APPLIED.format(label=shown[1], line_id=shown[0])
        elif _is_billable(record):
            tail = NO_HOURLY_BILLS_DEFAULT
        else:
            tail = "."
        _append(result, "note", NO_HOURLY_SENT + tail + _skipped_text(pick.skipped))
    elif not no_service_line and kind == "none" and _is_billable(record):
        _append(result, "note", NO_SERVICE_LINE_NOTE_TICKET if for_ticket else NO_SERVICE_LINE_NOTE_TASK)
    return result


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


@gorelo_tool(toolset="time", kind="read", ops=[LIST_OP], field_map=LIST_FIELDS)
async def list_time_entries(
    ctx: Context,
    client_ids: Annotated[list[StrictId] | None, Field(description="Client ids (list_clients).")] = None,
    location_ids: Annotated[
        list[StrictId] | None,
        Field(description="Ticket location ids (list_client_locations); task entries are left out."),
    ] = None,
    ticket_ids: Annotated[
        list[str] | None, Field(description="Ticket GUIDs (list_tickets), not display numbers.")
    ] = None,
    task_ids: Annotated[
        list[str] | None, Field(description="Task GUIDs (list_project_tasks, projects toolset).")
    ] = None,
    user_ids: Annotated[list[StrictId] | None, Field(description="User ids (list_org_users).")] = None,
    invoice_ids: Annotated[list[str] | None, Field(description="Invoice GUIDs (list_invoices).")] = None,
    started_since: Annotated[
        str | None,
        Field(description="Work started at or after this instant (ISO 8601 with UTC offset)."),
    ] = None,
    started_before: Annotated[str | None, Field(description="Work started before this instant.")] = None,
    created_since: Annotated[str | None, Field(description="Created at or after this instant.")] = None,
    created_before: Annotated[str | None, Field(description="Created before this instant.")] = None,
    updated_since: Annotated[
        str | None, Field(description="Updated at or after this instant; never-changed entries are left out.")
    ] = None,
    updated_before: Annotated[
        str | None, Field(description="Updated before this instant; never-changed entries are left out.")
    ] = None,
    page_size: Annotated[int, Field(description="Rows per page, 1-200.")] = 100,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous page.")] = None,
) -> dict:
    """List logged time across all tickets and tasks, one page at a time.

    An entry must match every filter given, and any id within a list. ActualHours is the time logged; AdjustedHours is the billed duration after the work type's minimum and increment (null: ActualHours is billed). Rows name their user, ticket and task as objects: User {Id, Name}, Ticket and Task {Id, Number, Title} (Task is null for a ticket entry). On 2026-10-02 the API returned flat UserId, TicketId and TaskId instead, so read the objects.
    Paging: pass next_cursor back as cursor with the SAME filters until has_more is false.
    """
    filters: dict[str, Any] = {
        "client_ids": positive_ids("client_ids", client_ids),
        "location_ids": positive_ids("location_ids", location_ids),
        "ticket_ids": guids("ticket_ids", ticket_ids),
        "task_ids": guids("task_ids", task_ids),
        "user_ids": positive_ids("user_ids", user_ids),
        "invoice_ids": guids("invoice_ids", invoice_ids),
        "started_since": utc_iso("started_since", started_since),
        "started_before": utc_iso("started_before", started_before),
        "created_since": utc_iso("created_since", created_since),
        "created_before": utc_iso("created_before", created_before),
        "updated_since": utc_iso("updated_since", updated_since),
        "updated_before": utc_iso("updated_before", updated_before),
    }
    for since, before in (
        ("started_since", "started_before"),
        ("created_since", "created_before"),
        ("updated_since", "updated_before"),
    ):
        _check_order(since, filters[since], before, filters[before], why="that window is empty, it can match nothing")
    non_empty("cursor", cursor)
    size = clamp_page_size(page_size)
    params = {
        LIST_QUERY[name]: csv_ids(name, value) if isinstance(value, list) else value for name, value in filters.items()
    }
    page = await client_of(ctx).get_page(LIST_OP, query=params, page_size=size, cursor=cursor, tool="list_time_entries")
    return paged_result(page, filters)


@gorelo_tool(toolset="time", kind="read", ops=[GET_OP], field_map={"time_entry_id": "timeEntryId"})
async def get_time_entry(
    ctx: Context,
    time_entry_id: Annotated[StrictId, Field(description="Time entry id (list_time_entries).")],
) -> dict:
    """Get one time entry by id: the same record as a list_time_entries row, with User, Ticket and Task as objects.

    User is {Id, Name}; Ticket and Task are {Id, Number, Title} (Task is null for a ticket entry). On 2026-10-02 the API returned flat UserId, TicketId and TaskId instead, so read the objects. A deleted entry is a 404, like an unknown id.
    """
    entry_id = positive_id("time_entry_id", time_entry_id)
    data = await client_of(ctx).get_one(GET_OP, path_params={"timeEntryId": entry_id}, tool="get_time_entry")
    return expect_object(data, GET_OP, tool="get_time_entry")


@gorelo_tool(toolset="time", kind="read", ops=[ROLES_OP])
async def list_billing_roles(ctx: Context) -> dict:
    """List every billing role (Id, Name, HourlyRate, Tax, CoaCode); there is no paging.

    A role's Id is the billing_role_id of create_time_entry and update_time_entry.
    """
    items = await client_of(ctx).get_list(ROLES_OP, tool="list_billing_roles")
    return list_result(items)


@gorelo_tool(toolset="time", kind="read", ops=[WORK_TYPES_OP])
async def list_work_types(ctx: Context) -> dict:
    """List every work type (Id, Name, BillableStatus, HourlyMultiplier, minimum and increment minutes, ...); there is no paging.

    A work type's Id is the work_type_id of create_time_entry and update_time_entry. It sets the rate multiplier and the minimum and increment rounding.
    """
    items = await client_of(ctx).get_list(WORK_TYPES_OP, tool="list_work_types")
    return list_result(items)


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


# ticket_id maps to TicketId (the body field) and not to "Query" (the lookup filter of tools/_ticket_ref.py): the POST is the
# call whose 400 the model has to fix, and the resolver checks the text of a number before it sends it.
@gorelo_tool(
    toolset="time",
    kind="write",
    ops=[CREATE_OP, GET_OP, TICKET_LIST_OP, TICKET_GET_OP, CONTRACTS_OP],
    field_map=CREATE_FIELDS,
)
async def create_time_entry(
    ctx: Context,
    user_id: Annotated[StrictId, Field(description="User whose time it is (list_org_users).")],
    ticket_id: Annotated[
        str | StrictId | None,
        Field(description="Ticket GUID (list_tickets), display number (TCK-1234) or number (1234). A GUID is cheapest."),
    ] = None,
    task_id: Annotated[
        str | None, Field(description="Task GUID (list_project_tasks, projects toolset).")
    ] = None,
    started_on: Annotated[
        str | None, Field(description="Start (ISO 8601 with UTC offset).")
    ] = None,
    ended_on: Annotated[str | None, Field(description="End, later than started_on.")] = None,
    actual_hours: Annotated[
        float | None, Strict(), Field(gt=0, description="Decimal hours worked, above 0. Do not pre-round.")
    ] = None,
    billable_status_id: Annotated[
        StrictId | None,
        Field(description="1 Billable, 2 No charge, 3 Non-billable. Default: ticket's, else work type's, else Billable."),
    ] = None,
    billing_role_id: Annotated[
        StrictId | None,
        Field(description="Billing role (list_billing_roles). Default: ticket's, else user's, else the provider's first."),
    ] = None,
    work_type_id: Annotated[
        StrictId | None,
        Field(description="Work type (list_work_types). Default: ticket's, else user's, else the provider's first."),
    ] = None,
    service_line_id: Annotated[
        StrictId | None,
        Field(
            description="Service line Id (list_contracts) of the client's active contract covering labour. "
            "Default: ticket's, else first matching role and work type, else none."
        ),
    ] = None,
    no_service_line: Annotated[
        bool,
        Field(
            description="true bills no contract and stops automatic contract assignment on approval. "
            "Not with service_line_id."
        ),
    ] = False,
    auto_service_line: Annotated[
        StrictBool,
        Field(
            description="true: use the ticket's own service line, else the one Hourly line of the client's Active "
            "contracts covering the ticket's location and the entry's day. Needs ticket_id; not with service_line_id or "
            "no_service_line."
        ),
    ] = False,
    comment: Annotated[str | None, Field(description="Free-text note.")] = None,
    distance: Annotated[float | None, Strict(), Field(ge=0, description="Distance travelled, 0 or more.")] = None,
    attachments: Annotated[
        list[dict[str, str]] | None,
        Field(
            description='[{"name": "a.pdf", "url": "https://..."}]. Pass only the name and url from upload_attachment, '
            "unchanged."
        ),
    ] = None,
) -> dict:
    """Log time against ONE ticket or ONE task and return the entry.

    Give at least two of started_on, ended_on and actual_hours (the third is derived; all three must agree). Billed time is rounded UP by the work type's minimum and increment. Gorelo refuses time on a closed ticket (409): log time before closing, or ask the user before reopening it.
    Contract client: pass service_line_id or auto_service_line=true (the ticket's own line wins, else the Hourly line over other labor lines). A Billable entry reading back with no line gets a note; a line that did not stick, a warning.
    Side effects: creates the entry, billed to the ticket's client (task: its project's), priced at once; can draw down contract hours. Logging twice records the time twice: after "did not confirm" check list_time_entries before repeating. {"Id", "warning"} means it exists and only the re-read failed: do not repeat the write.
    """
    task = guid("task_id", task_id) if task_id is not None else None
    if (ticket_id is None) == (task is None):
        got = "both" if ticket_id is not None else "neither"
        raise ValueError(
            "ticket_id and task_id: give exactly one of them (a time entry is logged against one ticket or one "
            f"task), got {got}"
        )
    user = positive_id("user_id", user_id)
    started = utc_iso("started_on", started_on)
    ended = utc_iso("ended_on", ended_on)
    hours = _hours("actual_hours", actual_hours)
    given = [
        name
        for name, value in (("started_on", started), ("ended_on", ended), ("actual_hours", hours))
        if value is not None
    ]
    if len(given) < 2:
        seen = f"only {given[0]}" if given else "none of them"
        raise ValueError(
            "started_on, ended_on and actual_hours: give at least two of them (Gorelo derives the third; with all "
            f"three it checks that they agree), got {seen}"
        )
    _check_order("started_on", started, "ended_on", ended, why="a time entry cannot end before or when it starts")
    if no_service_line and service_line_id is not None:
        raise ValueError(
            "no_service_line: cannot be true together with service_line_id (no_service_line bills the entry to no "
            "contract, service_line_id bills it to that service line); give one of them"
        )
    if auto_service_line:
        if task is not None:
            raise ValueError(
                "auto_service_line: needs ticket_id (the client is read from the ticket), so it cannot be used for a "
                "task entry; pass service_line_id or no_service_line, or leave auto_service_line false"
            )
        if service_line_id is not None or no_service_line:
            raise ValueError(
                "auto_service_line: cannot be true together with service_line_id or no_service_line (auto_service_line "
                "picks the service line itself); give one of them"
            )
    values = {
        "ticket_id": None,  # the GUID, once resolved below
        "task_id": task,
        "user_id": user,
        "started_on": started,
        "ended_on": ended,
        "actual_hours": hours,
        "billable_status_id": _billable_status("billable_status_id", billable_status_id),
        "billing_role_id": _optional_id("billing_role_id", billing_role_id),
        "work_type_id": _optional_id("work_type_id", work_type_id),
        "service_line_id": _optional_id("service_line_id", service_line_id),
        "comment": non_empty("comment", comment),  # a blank comment is refused here, before any request
        "distance": _distance("distance", distance),
        "attachments": _attachments("attachments", attachments),
    }
    day = _entry_day(started, ended, hours) if auto_service_line else None  # local, so it also costs no request when it refuses
    # every read happens here, before the POST: a refusal below writes nothing
    client = client_of(ctx)
    if ticket_id is not None:
        values["ticket_id"] = await resolve_ticket_id(client, ticket_id, tool="create_time_entry")
    pick = None
    if auto_service_line:
        pick = await _auto_service_line(client, values["ticket_id"], day)
        if pick.line_id is not None:
            values["service_line_id"] = pick.line_id
    body = build_body(
        values,
        CREATE_FIELDS,
        clear=["service_line_id"] if no_service_line else [],
        clear_values=_CLEAR_VALUES,
    )
    written = await client.post(CREATE_OP, json_body=body, tool="create_time_entry")
    new_id = created_id(written, CREATE_OP, tool="create_time_entry")
    record = await reread_after_write(
        ctx, GET_OP, path_params={"timeEntryId": new_id}, tool="create_time_entry", written_id=new_id
    )
    return _annotate(
        record, sent=values["service_line_id"], pick=pick, no_service_line=no_service_line, for_ticket=task is None
    )


@gorelo_tool(
    toolset="time",
    kind="write",
    ops=[UPDATE_OP],
    field_map={**UPDATE_FIELDS, "time_entry_id": "timeEntryId"},
    destructive_hint=True,
)
async def update_time_entry(
    ctx: Context,
    time_entry_id: Annotated[StrictId, Field(description="Time entry id (list_time_entries).")],
    started_on: Annotated[str | None, Field(description="New start (ISO 8601 with UTC offset).")] = None,
    ended_on: Annotated[str | None, Field(description="New end, after the start.")] = None,
    actual_hours: Annotated[
        float | None, Strict(), Field(gt=0, description="New duration in decimal hours, above 0.")
    ] = None,
    billable_status_id: Annotated[
        StrictId | None, Field(description="1 Billable, 2 No charge, 3 Non-billable.")
    ] = None,
    user_id: Annotated[StrictId | None, Field(description="New user (list_org_users).")] = None,
    billing_role_id: Annotated[
        StrictId | None, Field(description="New billing role (list_billing_roles).")
    ] = None,
    work_type_id: Annotated[StrictId | None, Field(description="New work type (list_work_types).")] = None,
    service_line_id: Annotated[
        StrictId | None,
        Field(
            description="Service line Id (list_contracts), same rules as create_time_entry. "
            "Not with remove_from_contract."
        ),
    ] = None,
    comment: Annotated[
        str | None, Field(description="New note; to erase the note use clear_comment.")
    ] = None,
    distance: Annotated[float | None, Strict(), Field(ge=0, description="Distance travelled, 0 or more.")] = None,
    remove_from_contract: Annotated[
        bool,
        Field(
            description="true takes the entry off its contract and stops automatic contract assignment on "
            "approval. Not with service_line_id."
        ),
    ] = False,
    clear_comment: Annotated[bool, Field(description="true erases the comment. Not with comment.")] = False,
) -> dict:
    """Change an existing OPEN time entry and return the updated entry.

    Only fields you send change; send at least one. Time fields merge with the entry's current values: started_on or ended_on alone keeps the other instant, actual_hours alone moves ended_on. The ticket, task and attachments cannot change: to move time, delete the entry and log it again (delete_time_entry, when deletes are enabled). Approved, completed, invoiced and void entries are refused (409).
    Side effects: overwrites what you send. Changing the duration, work type, role, billable status or service line re-prices the entry and moves its contract hours from the old contract to the new; changing the user re-stamps its cost rate.
    """
    entry_id = positive_id("time_entry_id", time_entry_id)
    started = utc_iso("started_on", started_on)
    ended = utc_iso("ended_on", ended_on)
    _check_order("started_on", started, "ended_on", ended, why="a time entry cannot end before or when it starts")
    if remove_from_contract and service_line_id is not None:
        raise ValueError(
            "remove_from_contract: cannot be true together with service_line_id (remove_from_contract takes the "
            "entry off its contract, service_line_id moves it to one); give one of them"
        )
    if clear_comment and comment is not None:
        raise ValueError(
            "clear_comment: cannot be true together with comment (comment replaces the note, clear_comment erases "
            "it); give one of them"
        )
    if comment is not None and not comment.strip():
        raise ValueError("comment: must not be empty or whitespace only; to erase the note use clear_comment=true")
    values = {
        "started_on": started,
        "ended_on": ended,
        "actual_hours": _hours("actual_hours", actual_hours),
        "billable_status_id": _billable_status("billable_status_id", billable_status_id),
        "user_id": _optional_id("user_id", user_id),
        "billing_role_id": _optional_id("billing_role_id", billing_role_id),
        "work_type_id": _optional_id("work_type_id", work_type_id),
        "service_line_id": _optional_id("service_line_id", service_line_id),
        "comment": comment,
        "distance": _distance("distance", distance),
    }
    clear = (["service_line_id"] if remove_from_contract else []) + (["comment"] if clear_comment else [])
    body = build_body(values, UPDATE_FIELDS, clear=clear, clear_values=_CLEAR_VALUES)
    if not body:
        raise ValueError(
            "started_on, ended_on, actual_hours, billable_status_id, user_id, billing_role_id, work_type_id, "
            "service_line_id, comment, distance, remove_from_contract and clear_comment: give at least one of them, "
            "there is nothing to change"
        )
    updated = await client_of(ctx).patch(
        UPDATE_OP, path_params={"timeEntryId": entry_id}, json_body=body, tool="update_time_entry"
    )
    return expect_object(updated, UPDATE_OP, tool="update_time_entry")


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------


@gorelo_tool(toolset="time", kind="destructive", ops=[DELETE_OP], field_map={"time_entry_id": "timeEntryId"})
async def delete_time_entry(
    ctx: Context,
    time_entry_id: Annotated[StrictId, Field(description="Time entry id (list_time_entries).")],
    confirm: Annotated[StrictBool, Field(description="Must be true to delete; ask the user first.")] = False,
) -> dict:
    """Delete ONE open time entry and return Gorelo's {Id, Outcome}.

    Approved, completed, invoiced and void entries, and entries on a closed ticket (reopen it first), are refused (409). An entry waiting for approval is first returned to unclosed (Outcome "Reopened") and needs a second call with confirm=true to be removed (Outcome "Deleted"); any other open entry is removed at once. An already deleted entry reports "Deleted" and changes nothing.
    Side effects: "Deleted" deactivates the entry and reverts its billing ledger entries; no tool restores it.
    Ask the user first; needs confirm=true.
    """
    entry_id = positive_id("time_entry_id", time_entry_id)
    require_confirm(
        confirm,
        action=f"delete time entry {entry_id}",
        effect=(
            "Deleting deactivates the entry and reverts its billing ledger entries; an entry waiting for approval "
            "is first reopened instead and needs a second delete."
        ),
    )
    data = await client_of(ctx).delete(DELETE_OP, path_params={"timeEntryId": entry_id}, tool="delete_time_entry")
    result = expect_object(data, DELETE_OP, tool="delete_time_entry")
    outcome = result.get("Outcome")
    if not isinstance(outcome, str) or not outcome.strip():
        raise GoreloAPIError(
            f"{DELETE_OP}: Data has no Outcome (expected Deleted or Reopened); refusing to guess",
            status=200, op_key=DELETE_OP, kind="shape", write_unconfirmed=True,
        )
    return result
