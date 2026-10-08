"""Tickets and their lookups: statuses, types, tags, priorities, sources (toolset "tickets").

Tools: list_tickets, search_tickets, get_ticket, create_ticket, update_ticket, list_ticket_statuses,
list_ticket_types, list_ticket_tags, list_ticket_priorities, list_ticket_sources.

Ops used by this module (each tool declares every op it calls, re-read GETs included):

    GET /v1/tickets
    GET /v1/tickets/{ticketId}
    POST /v1/tickets
    PATCH /v1/tickets/{ticketId}
    GET /v1/tickets/statuses
    GET /v1/tickets/types
    GET /v1/tickets/tags

list_ticket_priorities and list_ticket_sources are local tables: they declare GET /v1/tickets for
traceability and make no HTTP call. There is no delete tool: DELETE /v1/tickets/{ticketId} is in
FORBIDDEN_OPS.

Rules this module follows:

* Priority is ONE scale everywhere (0 None, 1 Urgent, 2 High, 3 Normal, 4 Low); source ids are 1 to 6.
  Status, type, group, user and tag ids are tenant data: they are never hardcoded, names are resolved
  through the list endpoints.
* Every integer id is typed StrictId (JSON true or "5" is refused) and checked with the shared
  positive_id/positive_ids; GUIDs go through guid/guids. This module keeps no copies of those helpers.
* GET /v1/tickets filters on the server (ids, text, dates). search_tickets sends everything the API can
  filter and applies only IsWaitingOnThem, LeadAssigneeId, IsUnread and ClosedOn on the rows it read; its
  errors name the filter parameter involved.
* POST and PATCH answer with only {"Id": ...}. created_id / expect_object refuse any other answer (a shape
  error that says the write may have been applied); then the record is read back with reread_after_write,
  which returns {"Id", "warning"} instead of raising when that read fails (the write already happened).
* PATCH treats null as "not given" (probe), so nothing can be cleared with null. List fields replace
  outright; [] is only sent through update_ticket's clear_fields. A BillingOverride that names only some of
  its parts may reset the others, so update_ticket reads the ticket first and sends the complete override.
  A record whose BillingOverride cannot be read with certainty (no such key, none of its four parts, a part
  that is not an object with an Id) is a shape error and no PATCH is sent; null, {}, null parts and an Id of
  null or 0 mean "not set".
* Local validation errors name the snake_case parameter and happen before any write or scan. Only a status or
  type NAME in search_tickets needs a lookup GET first (ids go through as they are), and only a partial
  billing override in update_ticket needs a read before the write.
* The read tools that take filters pass the query parameter names as their field_map, so a Gorelo 400 on
  StatusIds or PageSize reaches the model as status_ids or page_size.
"""

import dataclasses
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, get_args

from fastmcp import Context
from pydantic import Field

from gorelo_client import GoreloAPIError, GoreloClient
from tools._common import (
    StrictBool,
    StrictId,
    all_result,
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
    utc_iso,
)

# --------------------------------------------------------------------------
# Operations, tables and limits
# --------------------------------------------------------------------------

OP_LIST = "GET /v1/tickets"
OP_GET = "GET /v1/tickets/{ticketId}"
OP_CREATE = "POST /v1/tickets"
OP_UPDATE = "PATCH /v1/tickets/{ticketId}"
OP_STATUSES = "GET /v1/tickets/statuses"
OP_TYPES = "GET /v1/tickets/types"
OP_TAGS = "GET /v1/tickets/tags"

# One priority scale for filters, reads and writes (since 2026-08-05). Match on Id, never on Name.
PRIORITIES: tuple[tuple[int, str], ...] = (
    (0, "None"),
    (1, "Urgent"),
    (2, "High"),
    (3, "Normal"),
    (4, "Low"),
)
PRIORITY_IDS = tuple(pid for pid, _ in PRIORITIES)
PRIORITY_HELP = ", ".join(f"{pid} {name}" for pid, name in PRIORITIES)

# Ticket sources Gorelo accepts. Id 7 appears on real tickets with an empty name; it is not in the spec enum.
SOURCES: tuple[tuple[int, str], ...] = (
    (1, "Web"),
    (2, "Email"),
    (3, "Phone"),
    (4, "Chat"),
    (5, "Alert"),
    (6, "Api"),
)
SOURCE_IDS = tuple(sid for sid, _ in SOURCES)
SOURCE_HELP = ", ".join(f"{sid} {name}" for sid, name in SOURCES)
SOURCES_NOTE = (
    "Id 7 also appears on older tickets with an empty name (unnamed, seen on existing tickets). "
    "It is not in Gorelo's enum and cannot be set: create_ticket accepts source_id 1 to 6 only."
)

BILLABLE_STATUSES: tuple[tuple[int, str], ...] = ((1, "Billable"), (2, "No charge"), (3, "Non-billable"))
BILLABLE_IDS = tuple(bid for bid, _ in BILLABLE_STATUSES)
BILLABLE_HELP = ", ".join(f"{bid} {name}" for bid, name in BILLABLE_STATUSES)

QUERY_MAX_CHARS = 200  # GET /v1/tickets Query: "Up to 200 characters"
TITLE_MAX_CHARS = 250  # CreateTicketCommand.Title and UpdateTicketCommand.Title: "Maximum 250 characters"

# search_tickets reads every page of the server-side matches, up to this many tickets, then filters.
SEARCH_SCAN_CAP = 5000
SEARCH_PAGE_SIZE = 200
SEARCH_LIMIT_DEFAULT = 50
SEARCH_LIMIT_MAX = 500

# get_ticket looks a number or display number up with Query (50 per page) and stops at the first page
# that holds an exact match; this bounds the walk for a short number that matches many tickets.
LOOKUP_PAGE_SIZE = 50
LOOKUP_MAX_PAGES = 20

# Snake_case parameter -> query parameter of GET /v1/tickets (also used to name the parameter in errors).
LIST_QUERY_FIELDS: dict[str, str] = {
    "status_ids": "StatusIds",
    "client_ids": "ClientIds",
    "priority_ids": "PriorityIds",
    "type_ids": "TypeIds",
    "lead_assignee_ids": "LeadAssigneeIds",
    "contact_ids": "ContactIds",
    "tag_ids": "TagIds",
    "group_ids": "GroupIds",
    "query": "Query",
    "updated_since": "UpdatedSince",
    "updated_before": "UpdatedBefore",
    "created_since": "CreatedSince",
    "created_before": "CreatedBefore",
    "sort_by": "SortBy",
    "sort_order": "SortOrder",
}
LIST_ERROR_FIELDS: dict[str, str] = {**LIST_QUERY_FIELDS, "page_size": "PageSize", "cursor": "Cursor"}

SEARCH_ERROR_FIELDS: dict[str, str] = {
    "client_id": "ClientIds",
    "status": "StatusIds",
    "priority": "PriorityIds",
    "type": "TypeIds",
    "query": "Query",
    "updated_since": "UpdatedSince",
    "created_since": "CreatedSince",
}

# The four search_tickets filters the API cannot do: parameter -> the ticket field it reads.
CLIENT_SIDE_FILTERS: tuple[tuple[str, str], ...] = (
    ("awaiting_client", "IsWaitingOnThem"),
    ("unassigned_only", "LeadAssigneeId"),
    ("unread_only", "IsUnread"),
    ("exclude_closed", "ClosedOn"),
)

# Snake_case parameter -> body field of POST /v1/tickets (CreateTicketCommand).
CREATE_FIELDS: dict[str, str] = {
    "title": "Title",
    "description": "Description",
    "client_id": "ClientId",
    "status_id": "StatusId",
    "type_id": "TypeId",
    "priority_id": "PriorityId",
    "source_id": "SourceId",
    "group_id": "GroupId",
    "contact_id": "ContactId",
    "cc_contact_ids": "CcContactIds",
    "location_id": "LocationId",
    "lead_assignee_id": "LeadAssigneeId",
    "assisting_assignee_ids": "AssistingAssigneeIds",
    "watcher_ids": "WatcherIds",
    "tag_ids": "TagIds",
    "agent_asset_ids": "AgentAssetIds",
    "custom_asset_ids": "CustomAssetIds",
    "uptime_ids": "UptimeIds",
    "created_on": "CreatedOn",
    "updated_on": "UpdatedOn",
    "closed_on": "ClosedOn",
    "created_by_name": "CreatedByName",
    "is_unread": "IsUnread",
    "send_created_email": "SendTicketCreatedEmail",
}

# Snake_case parameter -> body field of PATCH /v1/tickets/{ticketId} (UpdateTicketCommand). The four
# billing_* parameters nest into BillingOverride (never the old ContractServiceId).
UPDATE_FIELDS: dict[str, str] = {
    "title": "Title",
    "status_id": "StatusId",
    "priority_id": "PriorityId",
    "type_id": "TypeId",
    "client_id": "ClientId",
    "location_id": "LocationId",
    "contact_id": "ContactId",
    "cc_contact_ids": "CcContactIds",
    "group_ids": "GroupIds",
    "lead_assignee_id": "LeadAssigneeId",
    "assisting_assignee_ids": "AssistingAssigneeIds",
    "watcher_ids": "WatcherIds",
    "tag_ids": "TagIds",
    "agent_asset_ids": "AgentAssetIds",
    "custom_asset_ids": "CustomAssetIds",
    "uptime_ids": "UptimeIds",
    "closed_on": "ClosedOn",
    "updated_on": "UpdatedOn",
    "updated_by_name": "UpdatedByName",
    "billing_service_line_id": "BillingOverride.ServiceLineId",
    "billing_role_id": "BillingOverride.BillingRoleId",
    "billing_work_type_id": "BillingOverride.WorkTypeId",
    "billable_status_id": "BillingOverride.BillableStatusId",
}

# The billing_* parameters of update_ticket -> the part of GET /v1/tickets/{ticketId} BillingOverride
# ({Id, Name} objects) that holds the current value.
BILLING_PARTS: dict[str, str] = {
    "billing_service_line_id": "ServiceLine",
    "billing_role_id": "BillingRole",
    "billing_work_type_id": "WorkType",
    "billable_status_id": "BillableStatus",
}

# The only fields update_ticket can empty. Every other field is a single value that Gorelo cannot clear
# (null counts as absent), or a list that must keep at least one entry (group_ids). The Literal puts the
# allowed names in the advertised schema (like update_item's clear_fields).
ClearableField = Literal[
    "cc_contact_ids",
    "assisting_assignee_ids",
    "watcher_ids",
    "tag_ids",
    "agent_asset_ids",
    "custom_asset_ids",
    "uptime_ids",
]
CLEARABLE_FIELDS: tuple[str, ...] = get_args(ClearableField)

# update_ticket fields that only stamp the other changes of the same request ("not an update on its own").
STAMP_FIELDS = ("UpdatedOn", "UpdatedByName")

_DIGITS = re.compile(r"[0-9]+")


# --------------------------------------------------------------------------
# Local validation helpers (each raises ValueError naming the snake_case parameter). Ids and GUIDs use the
# shared positive_id(s) and guid(s); what is here is specific to tickets.
# --------------------------------------------------------------------------


def _optional(check: Callable[[str, Any], Any], param: str, value: Any) -> Any:
    """check(param, value) for a value that was given; None stays None (not given)."""
    return None if value is None else check(param, value)


def _one_of(param: str, value: Any, allowed: tuple[int, ...], choices: str, note: str = "") -> int:
    """A whole number from a fixed table (never a bool). The message lists the table; it does not quote the value."""
    if isinstance(value, bool) or not isinstance(value, int) or value not in allowed:
        got = "" if isinstance(value, int) and not isinstance(value, bool) else f", got {describe_value(value)}"
        raise ValueError(f"{param}: must be one of {choices}{got}{note}")
    return value


def _priority_id(param: str, value: Any) -> int:
    return _one_of(param, value, PRIORITY_IDS, PRIORITY_HELP)


def _source_id(param: str, value: Any) -> int:
    return _one_of(param, value, SOURCE_IDS, SOURCE_HELP, "; Id 7 exists on older tickets but cannot be set")


def _billable_status_id(param: str, value: Any) -> int:
    return _one_of(param, value, BILLABLE_IDS, BILLABLE_HELP)


def _each(param: str, values: Any, check: Callable[[str, Any], Any]) -> list[Any] | None:
    """check() on every item of a non-empty list, each named param[index] (like positive_ids). None passes."""
    if values is None:
        return None
    if not isinstance(values, list) or not values:
        raise ValueError(f"{param}: expected a non-empty list of ids, got {describe_value(values)}")
    return [check(f"{param}[{index}]", item) for index, item in enumerate(values)]


def _replacing(
    param: str,
    values: list[Any] | None,
    check: Callable[[str, Any], Any],
    *,
    clearable: bool,
    clearing: bool,
) -> list[Any] | None:
    """A list field of update_ticket: it REPLACES the current list, so [] is never sent as a value.

    [] is read as "not given" when the caller also named the field in clear_fields (build_body then sends
    the clearing value); for a clearable field without that it is refused with a pointer to clear_fields.
    check is positive_ids or guids, which refuse [] themselves for the fields that cannot be cleared."""
    if isinstance(values, list) and not values:
        if clearing:
            return None
        if clearable:
            raise ValueError(
                f"{param}: an empty list is not accepted here; to remove every entry pass "
                f"clear_fields=['{param}'], or omit {param} to leave it unchanged"
            )
    return check(param, values)


def _title(param: str, value: str | None) -> str | None:
    if value is None:
        return None
    non_empty(param, value)
    if len(value) > TITLE_MAX_CHARS:
        raise ValueError(f"{param}: at most {TITLE_MAX_CHARS} characters, got {len(value)}")
    return value


def _query_text(param: str, value: str | None) -> str | None:
    if value is None:
        return None
    non_empty(param, value)
    text = value.strip()
    if len(text) > QUERY_MAX_CHARS:
        raise ValueError(f"{param}: at most {QUERY_MAX_CHARS} characters, got {len(text)}; use a shorter keyword")
    return text


def _instant(param: str, value: str | None) -> tuple[str | None, datetime | None]:
    """(the UTC text for the request, the same moment as a datetime). Naive datetimes are errors."""
    text = utc_iso(param, value)
    if text is None:
        return None, None
    return text, datetime.fromisoformat(text)


def _not_in_future(param: str, text: str | None, moment: datetime | None) -> None:
    if moment is not None and moment > datetime.now(timezone.utc):
        raise ValueError(f"{param}: {text} is in the future; Gorelo does not accept a future time here")


def _check_window(
    since_param: str,
    since: tuple[str | None, datetime | None],
    before_param: str,
    before: tuple[str | None, datetime | None],
) -> None:
    """since is inclusive and before is exclusive, so since >= before can never match anything."""
    (since_text, since_at), (before_text, before_at) = since, before
    if since_at is not None and before_at is not None and since_at >= before_at:
        raise ValueError(
            f"{since_param}: {since_text} is not earlier than {before_param} ({before_text}); "
            "that range is empty (since is inclusive, before is exclusive)"
        )


def _shape_error(op_key: str, detail: str) -> GoreloAPIError:
    """A successful READ whose answer cannot be used (nothing was written, so it is not write_unconfirmed)."""
    return GoreloAPIError(f"{op_key}: {detail}", status=200, op_key=op_key, kind="shape")


# --------------------------------------------------------------------------
# Name resolution for search_tickets
# --------------------------------------------------------------------------


def _lookup_entries(rows: list[Any]) -> list[tuple[int, str]]:
    entries: list[tuple[int, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_id = row.get("Id")
        if isinstance(row_id, bool) or not isinstance(row_id, int):
            continue
        name = row.get("Name")
        entries.append((row_id, name.strip() if isinstance(name, str) else ""))
    return entries


def _valid_choices(entries: list[tuple[int, str]]) -> str:
    return ", ".join(f"{name or '(unnamed)'} ({row_id})" for row_id, name in entries) or "none"


def _id_or_name(param: str, value: str | int) -> int | str:
    """A status or type given as an int, or as text made only of digits, is an id (sent as it is); any other
    text is a name, returned stripped. Blank text and ids below 1 are errors."""
    if isinstance(value, str):
        non_empty(param, value)
        text = value.strip()
        if not _DIGITS.fullmatch(text):
            return text
        value = int(text)
    return positive_id(param, value)


def _match_name(param: str, name: str, entries: list[tuple[int, str]], *, noun: str, plural: str) -> tuple[int, str]:
    """The (id, canonical name) of the one entry whose name equals `name`, ignoring case. Anything else is an
    error that lists every valid name with its id."""
    folded = name.casefold()
    named = [(row_id, label) for row_id, label in entries if label and label.casefold() == folded]
    if len(named) == 1:
        return named[0]
    if named:
        ids = ", ".join(str(row_id) for row_id, _ in named)
        raise ValueError(f"{param}: {name!r} matches several ticket {plural} (ids {ids}); pass the numeric id instead")
    raise ValueError(
        f"{param}: no ticket {noun} is named {name!r}. Valid {plural}: {_valid_choices(entries)}; "
        "a numeric id is accepted too"
    )


async def _resolve_named(
    client: GoreloClient, param: str, name: str, op_key: str, *, noun: str, plural: str, tool: str
) -> tuple[int, str]:
    """Look a status or type NAME up in the tenant's list (GET op_key) and return its (id, canonical name)."""
    rows = await client.get_list(op_key, tool=tool)
    return _match_name(param, name, _lookup_entries(rows), noun=noun, plural=plural)


def _resolve_priority(param: str, value: str | int) -> tuple[int, str]:
    """A priority by name (None, Urgent, High, Normal, Low; any case) or by id 0 to 4, from the constant table."""
    entries = list(PRIORITIES)
    wanted: int | str = value
    if isinstance(value, bool):
        raise ValueError(f"{param}: expected a priority name or an id from 0 to 4, got a boolean")
    if isinstance(value, str):
        non_empty(param, value)
        wanted = value.strip()
        if _DIGITS.fullmatch(wanted):
            wanted = int(wanted)
    if isinstance(wanted, int):
        for pid, name in entries:
            if pid == wanted:
                return pid, name
        raise ValueError(f"{param}: no ticket priority has id {wanted}. Valid priorities: {_valid_choices(entries)}")
    return _match_name(param, wanted, entries, noun="priority", plural="priorities")


# --------------------------------------------------------------------------
# Client-side filters of search_tickets
# --------------------------------------------------------------------------


def _flag(row: dict[str, Any], param: str, field: str) -> bool:
    value = row.get(field)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise _shape_error(
        OP_LIST, f"ticket field {field} is not a boolean, so the {param} filter cannot be applied; refusing to guess"
    )


def _client_side_filter(
    rows: list[Any], *, awaiting_client: bool | None, unassigned_only: bool, unread_only: bool, exclude_closed: bool
) -> list[Any]:
    """Apply the four filters the API cannot do. A field that no scanned row carries at all means Gorelo
    renamed or dropped it (IsAwaitingClient became IsWaitingOnThem once), so that is an error, never a
    silently wrong filter. Every error names the filter parameter it is about."""
    active = {
        "awaiting_client": awaiting_client is not None,
        "unassigned_only": unassigned_only,
        "unread_only": unread_only,
        "exclude_closed": exclude_closed,
    }
    needed = [(param, field) for param, field in CLIENT_SIDE_FILTERS if active[param]]
    if not needed:
        return list(rows)
    names = ", ".join(param for param, _ in needed)
    for row in rows:
        if not isinstance(row, dict):
            raise _shape_error(
                OP_LIST, f"a ticket row is not an object, so the filter(s) {names} cannot be applied; refusing to guess"
            )
    for param, field in needed:
        if rows and not any(field in row for row in rows):
            raise _shape_error(
                OP_LIST,
                f"none of the {len(rows)} scanned ticket rows has a {field} field, so the {param} filter "
                "cannot be applied; Gorelo may have renamed it (refusing to guess)",
            )
    kept: list[Any] = []
    for row in rows:
        if awaiting_client is not None and _flag(row, "awaiting_client", "IsWaitingOnThem") is not awaiting_client:
            continue
        if unassigned_only and row.get("LeadAssigneeId") is not None:
            continue
        if unread_only and not _flag(row, "unread_only", "IsUnread"):
            continue
        if exclude_closed and row.get("ClosedOn") is not None:
            continue
        kept.append(row)
    return kept


def _search_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= SEARCH_LIMIT_MAX:
        raise ValueError(
            f"limit: must be a whole number from 1 to {SEARCH_LIMIT_MAX}; narrow the filters, "
            "or page through list_tickets with next_cursor, to read more"
        )
    return value


# --------------------------------------------------------------------------
# Lookups behind get_ticket
# --------------------------------------------------------------------------


def _ticket_reference(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        positive_id("ticket_id", value)
    text = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    if not text:
        raise ValueError(
            "ticket_id: must not be empty; give a ticket GUID, a number such as 1234 or a display number such as "
            "TCK-1234"
        )
    if len(text) > QUERY_MAX_CHARS:
        raise ValueError(f"ticket_id: at most {QUERY_MAX_CHARS} characters, got {len(text)}")
    return text


def _is_reference_match(row: dict[str, Any], wanted: str) -> bool:
    number = row.get("Number")
    if number is not None and not isinstance(number, bool) and str(number).casefold() == wanted:
        return True
    display = row.get("DisplayNumber")
    return isinstance(display, str) and display.strip().casefold() == wanted


def _row_id(row: dict[str, Any]) -> str:
    row_id = row.get("Id")
    if not isinstance(row_id, str) or not row_id.strip():
        raise _shape_error(OP_LIST, "a ticket row has no Id; refusing to guess")
    return row_id.strip()


def _label(row: dict[str, Any]) -> str:
    return f"{row.get('DisplayNumber') or row.get('Number')} (Id {row.get('Id')})"


async def _find_ticket_id(client: GoreloClient, reference: str) -> str:
    """Resolve a ticket number or display number to the ticket GUID: search with Query, keep the rows whose
    Number or DisplayNumber equals the text (case-insensitive). Stops at the first page with a match, and
    never reads more than LOOKUP_MAX_PAGES pages."""
    wanted = reference.casefold()
    matches: dict[str, dict[str, Any]] = {}
    seen_cursors: set[str] = set()
    cursor: str | None = None
    rows_read = 0
    total: int | None = None
    more = False
    for _ in range(LOOKUP_MAX_PAGES):
        page = await client.get_page(
            OP_LIST, query={"Query": reference}, page_size=LOOKUP_PAGE_SIZE, cursor=cursor, tool="get_ticket"
        )
        rows_read += len(page.items)
        total = page.total_count
        for row in page.items:
            if not isinstance(row, dict):
                raise _shape_error(OP_LIST, "a ticket row is not an object; refusing to guess")
            if _is_reference_match(row, wanted):
                matches.setdefault(_row_id(row), row)
        more = page.has_more
        if matches or not more:
            break
        cursor = page.next_cursor
        if cursor is None or cursor in seen_cursors:
            raise _shape_error(OP_LIST, "Gorelo returned a cursor it had already served; refusing to loop")
        seen_cursors.add(cursor)
    if len(matches) == 1:
        return next(iter(matches))
    if matches:
        listing = "; ".join(_label(row) for row in matches.values())
        raise ValueError(
            f"ticket_id: {reference!r} matches several tickets: {listing}. Pass the ticket GUID of the one you mean"
        )
    if more:
        shown_total = f" of {total}" if total is not None else ""
        raise ValueError(
            f"ticket_id: {reference!r} was not found in the first {rows_read}{shown_total} tickets Gorelo returned for "
            "that text (the lookup reads at most "
            f"{LOOKUP_MAX_PAGES} pages), so it is inconclusive. Pass the ticket GUID, or a longer display number "
            "such as TCK-1234"
        )
    found = (
        f"Gorelo returned {rows_read} ticket(s) matching that text, none with that exact number"
        if rows_read
        else "Gorelo found no ticket matching that text"
    )
    raise ValueError(
        f"ticket_id: no ticket has the number or display number {reference!r} ({found}). Pass a ticket GUID, "
        "a number such as 1234 or a display number such as TCK-1234; to search titles use list_tickets with query"
    )


# --------------------------------------------------------------------------
# Reading tickets
# --------------------------------------------------------------------------


@gorelo_tool(toolset="tickets", kind="read", ops=[OP_LIST], field_map=LIST_ERROR_FIELDS)
async def list_tickets(
    ctx: Context,
    status_ids: Annotated[list[StrictId] | None, Field(description="Status ids (list_ticket_statuses)")] = None,
    client_ids: Annotated[list[StrictId] | None, Field(description="Client ids (list_clients)")] = None,
    priority_ids: Annotated[
        list[StrictId] | None,
        Field(description="Priority ids: 0 None, 1 Urgent, 2 High, 3 Normal, 4 Low (list_ticket_priorities)"),
    ] = None,
    type_ids: Annotated[list[StrictId] | None, Field(description="Type ids (list_ticket_types)")] = None,
    lead_assignee_ids: Annotated[list[StrictId] | None, Field(description="Lead user ids (list_org_users)")] = None,
    contact_ids: Annotated[list[StrictId] | None, Field(description="Primary contact ids (list_contacts)")] = None,
    tag_ids: Annotated[list[StrictId] | None, Field(description="Tag ids, any match (list_ticket_tags)")] = None,
    group_ids: Annotated[list[StrictId] | None, Field(description="Group ids, any match (list_org_groups)")] = None,
    query: Annotated[str | None, Field(description="Text in title, number or display number (max 200)")] = None,
    updated_since: Annotated[str | None, Field(description="Updated at or after (ISO 8601)")] = None,
    updated_before: Annotated[str | None, Field(description="Updated before (ISO 8601)")] = None,
    created_since: Annotated[str | None, Field(description="Created at or after (ISO 8601)")] = None,
    created_before: Annotated[str | None, Field(description="Created before (ISO 8601)")] = None,
    sort_by: Annotated[Literal["updatedOn", "createdOn"] | None, Field(description="Default updatedOn")] = None,
    sort_order: Annotated[Literal["asc", "desc"] | None, Field(description="Default desc")] = None,
    page_size: Annotated[int, Field(description="1-200")] = 50,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous call, same filters")] = None,
) -> dict:
    """List one page of tickets, newest update first unless sorted. Returns items plus count, total_count, has_more,
    next_cursor.

    Notable fields: IsWaitingOnThem (renamed from IsAwaitingClient), Sla.FirstResponse.ElapsedBusinessMinutes,
    ChecklistSummary, ContactId (null without a primary contact). Description, time and billing are only in get_ticket.
    For status or priority NAMES, or waiting, unassigned, unread or open filters, use search_tickets.
    Side effects: None (read-only).
    Paging: pass next_cursor back as cursor with the SAME filters until has_more is false.
    """
    checked = {
        "status_ids": positive_ids("status_ids", status_ids),
        "client_ids": positive_ids("client_ids", client_ids),
        "priority_ids": _each("priority_ids", priority_ids, _priority_id),
        "type_ids": positive_ids("type_ids", type_ids),
        "lead_assignee_ids": positive_ids("lead_assignee_ids", lead_assignee_ids),
        "contact_ids": positive_ids("contact_ids", contact_ids),
        "tag_ids": positive_ids("tag_ids", tag_ids),
        "group_ids": positive_ids("group_ids", group_ids),
    }
    api_query: dict[str, Any] = {}
    filters: dict[str, Any] = {}
    for param, ids in checked.items():
        if ids is not None:
            api_query[LIST_QUERY_FIELDS[param]] = csv_ids(param, ids)
            filters[param] = ids
    text = _query_text("query", query)
    if text is not None:
        api_query[LIST_QUERY_FIELDS["query"]] = text
        filters["query"] = text
    moments = {
        "updated_since": _instant("updated_since", updated_since),
        "updated_before": _instant("updated_before", updated_before),
        "created_since": _instant("created_since", created_since),
        "created_before": _instant("created_before", created_before),
    }
    _check_window("updated_since", moments["updated_since"], "updated_before", moments["updated_before"])
    _check_window("created_since", moments["created_since"], "created_before", moments["created_before"])
    for param, (moment_text, _) in moments.items():
        if moment_text is not None:
            api_query[LIST_QUERY_FIELDS[param]] = moment_text
            filters[param] = moment_text
    for param, value in (("sort_by", sort_by), ("sort_order", sort_order)):
        if value is not None:
            api_query[LIST_QUERY_FIELDS[param]] = value
            filters[param] = value
    token = non_empty("cursor", cursor)
    page = await client_of(ctx).get_page(
        OP_LIST, query=api_query, page_size=clamp_page_size(page_size), cursor=token, tool="list_tickets"
    )
    return paged_result(page, filters)


@gorelo_tool(toolset="tickets", kind="read", ops=[OP_LIST, OP_STATUSES, OP_TYPES], field_map=SEARCH_ERROR_FIELDS)
async def search_tickets(
    ctx: Context,
    client_id: Annotated[StrictId | None, Field(description="Client id (list_clients)")] = None,
    status: Annotated[
        str | StrictId | None, Field(description="Status name (exact, any case) or id (list_ticket_statuses)")
    ] = None,
    priority: Annotated[
        str | StrictId | None, Field(description="Name or id: 0 None, 1 Urgent, 2 High, 3 Normal, 4 Low")
    ] = None,
    type: Annotated[
        str | StrictId | None, Field(description="Type name (exact, any case) or id (list_ticket_types)")
    ] = None,
    query: Annotated[str | None, Field(description="Text in title, number or display number (max 200)")] = None,
    updated_since: Annotated[str | None, Field(description="Updated at or after (ISO 8601)")] = None,
    created_since: Annotated[str | None, Field(description="Created at or after (ISO 8601)")] = None,
    unassigned_only: Annotated[bool, Field(description="True: only tickets with no lead assignee")] = False,
    awaiting_client: Annotated[
        bool | None, Field(description="True: waiting on the client; false: not; omit: both")
    ] = None,
    unread_only: Annotated[bool, Field(description="True: only unread tickets")] = False,
    exclude_closed: Annotated[bool, Field(description="True: drop tickets with a close date")] = False,
    limit: Annotated[int, Field(description="Most tickets returned, 1-500")] = SEARCH_LIMIT_DEFAULT,
) -> dict:
    """Find tickets by client, status, priority, type, text, dates and waiting, unassigned, unread or open state,
    scanning every matching page. Returns items plus count, total_count, matched, scanned, truncated, complete_scan,
    count_mismatch, filters.

    awaiting_client, unassigned_only, unread_only and exclude_closed are applied to the rows read, the rest on the
    server (total_count counts those matches). Filters combine with AND.
    The scan reads up to 5000 tickets. truncated true: matches are missing (limit reached, or complete_scan false
    because of that cap): raise limit (max 500) or narrow the filters. count_mismatch true: tickets changed mid-scan,
    repeat the search.
    Side effects: None (read-only).
    """
    client = client_of(ctx)
    wanted_limit = _search_limit(limit)
    client_value = _optional(positive_id, "client_id", client_id)
    query_text = _query_text("query", query)
    updated = _instant("updated_since", updated_since)
    created = _instant("created_since", created_since)
    # Local checks first (nothing is sent if one fails), then the lookups that need Gorelo.
    priority_match = _resolve_priority("priority", priority) if priority is not None else None
    status_target = _id_or_name("status", status) if status is not None else None
    type_target = _id_or_name("type", type) if type is not None else None
    status_match: tuple[int, str | None] | None = None
    if isinstance(status_target, int):
        status_match = (status_target, None)
    elif status_target is not None:
        status_match = await _resolve_named(
            client, "status", status_target, OP_STATUSES, noun="status", plural="statuses", tool="search_tickets"
        )
    type_match: tuple[int, str | None] | None = None
    if isinstance(type_target, int):
        type_match = (type_target, None)
    elif type_target is not None:
        type_match = await _resolve_named(
            client, "type", type_target, OP_TYPES, noun="type", plural="types", tool="search_tickets"
        )

    api_query: dict[str, Any] = {}
    filters: dict[str, Any] = {}
    if client_value is not None:
        api_query["ClientIds"] = str(client_value)
        filters["client_id"] = client_value
    for param, key, match in (
        ("status", "StatusIds", status_match),
        ("priority", "PriorityIds", priority_match),
        ("type", "TypeIds", type_match),
    ):
        if match is not None:
            api_query[key] = str(match[0])
            if match[1] is not None:  # an id given as such has no name to report
                filters[param] = match[1]
            filters[f"{param}_id"] = match[0]
    if query_text is not None:
        api_query["Query"] = query_text
        filters["query"] = query_text
    for param, key, (moment_text, _) in (
        ("updated_since", "UpdatedSince", updated),
        ("created_since", "CreatedSince", created),
    ):
        if moment_text is not None:
            api_query[key] = moment_text
            filters[param] = moment_text
    if unassigned_only:
        filters["unassigned_only"] = True
    if awaiting_client is not None:
        filters["awaiting_client"] = awaiting_client
    if unread_only:
        filters["unread_only"] = True
    if exclude_closed:
        filters["exclude_closed"] = True
    filters["limit"] = wanted_limit

    scan = await client.get_all(
        OP_LIST, query=api_query, page_size=SEARCH_PAGE_SIZE, max_items=SEARCH_SCAN_CAP, tool="search_tickets"
    )
    matches = _client_side_filter(
        scan.items,
        awaiting_client=awaiting_client,
        unassigned_only=unassigned_only,
        unread_only=unread_only,
        exclude_closed=exclude_closed,
    )
    shown = matches[:wanted_limit]
    result = all_result(dataclasses.replace(scan, items=shown), filters)
    result["truncated"] = len(matches) > wanted_limit or not scan.complete
    result["matched"] = len(matches)
    result["scanned"] = len(scan.items)
    notes: list[str] = []
    if len(matches) > wanted_limit:
        notes.append(
            f"{len(matches)} tickets matched and {wanted_limit} are returned: raise limit (max {SEARCH_LIMIT_MAX}) "
            "or narrow the filters"
        )
    if not scan.complete:
        of_total = f" of {scan.total_count}" if scan.total_count is not None else ""
        notes.append(
            f"the scan stopped after {len(scan.items)} tickets (cap {SEARCH_SCAN_CAP}){of_total}: "
            "add client_id, status, priority, type, query or date filters so Gorelo narrows the set"
        )
    if scan.count_mismatch:
        notes.append(
            f"Gorelo reported {scan.total_count} matches but {len(scan.items)} rows were read (tickets changed "
            "during the scan): repeat the search"
        )
    if notes:
        result["note"] = "; ".join(notes)
    return result


@gorelo_tool(toolset="tickets", kind="read", ops=[OP_GET, OP_LIST], field_map={"ticket_id": "Query"})
async def get_ticket(
    ctx: Context,
    ticket_id: Annotated[str | StrictId, Field(description="Ticket GUID, display number (TCK-1234) or number (1234)")],
) -> dict:
    """Get one ticket in full: the list fields plus Description, Time, Products, BillingOverride, Shipments, Banner and
    linked asset and uptime ids.

    Prefer the GUID or the display number (TCK-1234). A bare number (1234) is searched with Query and can scan several
    pages (capped at 20 pages of 50 tickets); no match or several matches is an error.
    Side effects: None (read-only).
    """
    client = client_of(ctx)
    reference = _ticket_reference(ticket_id)
    try:
        ticket = guid("ticket_id", reference)
    except ValueError:  # not a GUID: a number or a display number, found with one search
        ticket = await _find_ticket_id(client, reference)
    record = await client.get_one(OP_GET, path_params={"ticketId": ticket}, tool="get_ticket")
    return expect_object(record, OP_GET, tool="get_ticket")


# --------------------------------------------------------------------------
# Writing tickets
# --------------------------------------------------------------------------


@gorelo_tool(toolset="tickets", kind="write", ops=[OP_CREATE, OP_GET], field_map=CREATE_FIELDS)
async def create_ticket(
    ctx: Context,
    title: Annotated[str, Field(description="Max 250 characters")],
    description: Annotated[str, Field(description="Ticket text")],
    client_id: Annotated[StrictId, Field(description="Client id (list_clients)")],
    status_id: Annotated[StrictId, Field(description="Status id (list_ticket_statuses)")],
    type_id: Annotated[StrictId, Field(description="Type id (list_ticket_types)")],
    priority_id: Annotated[StrictId, Field(description="0 None, 1 Urgent, 2 High, 3 Normal, 4 Low")],
    source_id: Annotated[StrictId, Field(description="1 Web, 2 Email, 3 Phone, 4 Chat, 5 Alert, 6 Api")],
    group_id: Annotated[StrictId, Field(description="Group id (list_org_groups)")],
    contact_id: Annotated[StrictId | None, Field(description="Primary contact id (list_contacts)")] = None,
    cc_contact_ids: Annotated[list[StrictId] | None, Field(description="Contact ids to CC (list_contacts)")] = None,
    location_id: Annotated[StrictId | None, Field(description="Location id (list_client_locations)")] = None,
    lead_assignee_id: Annotated[StrictId | None, Field(description="Lead user id (list_org_users)")] = None,
    assisting_assignee_ids: Annotated[
        list[StrictId] | None, Field(description="Assisting user ids (list_org_users)")
    ] = None,
    watcher_ids: Annotated[list[StrictId] | None, Field(description="Watcher user ids (list_org_users)")] = None,
    tag_ids: Annotated[list[StrictId] | None, Field(description="Tag ids (list_ticket_tags)")] = None,
    agent_asset_ids: Annotated[list[str] | None, Field(description="Agent asset GUIDs (list_agents)")] = None,
    custom_asset_ids: Annotated[list[str] | None, Field(description="Custom asset GUIDs (list_custom_assets)")] = None,
    uptime_ids: Annotated[list[str] | None, Field(description="Uptime check GUIDs (list_uptime_checks)")] = None,
    created_on: Annotated[str | None, Field(description="Backdates creation (imports)")] = None,
    updated_on: Annotated[str | None, Field(description="Backdates last activity; needs created_on")] = None,
    closed_on: Annotated[
        str | None, Field(description="Imported close date; needs created_on and a closed status_id")
    ] = None,
    created_by_name: Annotated[str | None, Field(description="Creator name when no contact is resolved")] = None,
    is_unread: Annotated[bool | None, Field(description="false creates it read (default unread)")] = None,
    send_created_email: Annotated[
        StrictBool,
        Field(
            description=(
                "True emails the contact a ticket-created message. False (default) suppresses only that email: "
                "tenant automation rules may still notify contacts"
            )
        ),
    ] = False,
) -> dict:
    """Create a ticket and return its full record. This server cannot delete tickets: fix a wrong one with
    update_ticket.

    Resolve every id first, never guess.
    Side effects: creates the ticket and runs the tenant's ticket automation, which may still notify contacts;
    send_created_email=true also emails the contact.
    Backdating (imports): created_on, updated_on, closed_on need an offset and no future times; updated_on and
    closed_on require created_on.
    The record is read back; if that fails the result is {Id, warning}: the ticket WAS created, do not create it again.
    """
    _title("title", title)
    non_empty("description", description)
    created_text, created_at = _instant("created_on", created_on)
    updated_text, updated_at = _instant("updated_on", updated_on)
    closed_text, closed_at = _instant("closed_on", closed_on)
    if created_at is None:
        for param, moment in (("updated_on", updated_at), ("closed_on", closed_at)):
            if moment is not None:
                raise ValueError(
                    f"{param}: Gorelo only accepts {param} together with created_on (backdating is for importing "
                    f"tickets); give created_on as well or omit {param}"
                )
    for param, text, moment in (
        ("created_on", created_text, created_at),
        ("updated_on", updated_text, updated_at),
        ("closed_on", closed_text, closed_at),
    ):
        _not_in_future(param, text, moment)
    for param, text, moment in (("updated_on", updated_text, updated_at), ("closed_on", closed_text, closed_at)):
        if moment is not None and created_at is not None and moment < created_at:
            raise ValueError(f"{param}: {text} is earlier than created_on ({created_text}); it must not be")
    values: dict[str, Any] = {
        "title": title,
        "description": description,
        "client_id": positive_id("client_id", client_id),
        "status_id": positive_id("status_id", status_id),
        "type_id": positive_id("type_id", type_id),
        "priority_id": _priority_id("priority_id", priority_id),
        "source_id": _source_id("source_id", source_id),
        "group_id": positive_id("group_id", group_id),
        "contact_id": _optional(positive_id, "contact_id", contact_id),
        "cc_contact_ids": positive_ids("cc_contact_ids", cc_contact_ids),
        "location_id": _optional(positive_id, "location_id", location_id),
        "lead_assignee_id": _optional(positive_id, "lead_assignee_id", lead_assignee_id),
        "assisting_assignee_ids": positive_ids("assisting_assignee_ids", assisting_assignee_ids),
        "watcher_ids": positive_ids("watcher_ids", watcher_ids),
        "tag_ids": positive_ids("tag_ids", tag_ids),
        "agent_asset_ids": guids("agent_asset_ids", agent_asset_ids),
        "custom_asset_ids": guids("custom_asset_ids", custom_asset_ids),
        "uptime_ids": guids("uptime_ids", uptime_ids),
        "created_on": created_text,
        "updated_on": updated_text,
        "closed_on": closed_text,
        "created_by_name": non_empty("created_by_name", created_by_name),
        "is_unread": is_unread,
        "send_created_email": send_created_email,
    }
    body = build_body(values, CREATE_FIELDS)
    written = await client_of(ctx).post(OP_CREATE, json_body=body, tool="create_ticket")
    new_id = created_id(written, OP_CREATE, tool="create_ticket")
    if not isinstance(new_id, str):  # the ticket Id is a GUID text; a number cannot be read back
        raise GoreloAPIError(
            f"{OP_CREATE}: Gorelo reported success but the new ticket's Id is {describe_value(new_id)}, not a GUID; "
            "the write may have been applied, so verify it with a read before repeating it",
            status=200, op_key=OP_CREATE, kind="shape", write_unconfirmed=True,
        )
    new_id = new_id.strip()
    return await reread_after_write(
        ctx, OP_GET, path_params={"ticketId": new_id}, tool="create_ticket", written_id=new_id
    )


async def _current_billing(client: GoreloClient, ticket: str) -> dict[str, int]:
    """The ids of the ticket's current BillingOverride, by update_ticket parameter (one GET, nothing written).

    Not set, so left out: a null override, {}, a null part, and a part whose Id is null or 0. Everything else
    must be readable with certainty, because the parts the caller did not give are sent back as they are: a
    record with no BillingOverride key, a non-empty override that carries none of its four parts (Gorelo may
    have renamed them), a part that is not an object with an Id, and an Id that is not a whole number raise a
    shape error, and the caller sends no PATCH. Never guessed."""
    record = expect_object(
        await client.get_one(OP_GET, path_params={"ticketId": ticket}, tool="update_ticket"),
        OP_GET,
        tool="update_ticket",
    )
    cannot_keep = "so the billing fields you did not give cannot be kept; refusing to guess"
    if "BillingOverride" not in record:
        raise _shape_error(OP_GET, f"the ticket has no BillingOverride field, {cannot_keep}")
    override = record["BillingOverride"]
    if override is None:
        return {}
    if not isinstance(override, dict):
        raise _shape_error(OP_GET, f"BillingOverride is {describe_value(override)}, not an object, {cannot_keep}")
    if not override:
        return {}
    if not any(key in override for key in BILLING_PARTS.values()):
        raise _shape_error(
            OP_GET,
            f"BillingOverride has none of {', '.join(BILLING_PARTS.values())} (Gorelo may have renamed them), "
            f"{cannot_keep}",
        )
    current: dict[str, int] = {}
    for param, key in BILLING_PARTS.items():
        part = override.get(key)
        if part is None:
            continue
        if not isinstance(part, dict):
            raise _shape_error(
                OP_GET, f"BillingOverride.{key} is {describe_value(part)}, not an object with an Id, {cannot_keep}"
            )
        if "Id" not in part:
            raise _shape_error(OP_GET, f"BillingOverride.{key} has no Id, {cannot_keep}")
        part_id = part["Id"]
        if part_id is None:
            continue
        if isinstance(part_id, int) and not isinstance(part_id, bool) and part_id >= 0:
            if part_id:  # 0 means "not set"
                current[param] = part_id
            continue
        what = "negative" if isinstance(part_id, int) and not isinstance(part_id, bool) else describe_value(part_id)
        raise _shape_error(OP_GET, f"BillingOverride.{key}.Id is {what}, not an id, {cannot_keep}")
    return current


@gorelo_tool(toolset="tickets", kind="write", ops=[OP_UPDATE, OP_GET], field_map=UPDATE_FIELDS, destructive_hint=True)
async def update_ticket(
    ctx: Context,
    ticket_id: Annotated[str, Field(description="Ticket GUID (Id from get_ticket or list_tickets)")],
    title: Annotated[str | None, Field(description="New title, max 250 characters")] = None,
    status_id: Annotated[StrictId | None, Field(description="New status id (list_ticket_statuses)")] = None,
    priority_id: Annotated[StrictId | None, Field(description="0 None, 1 Urgent, 2 High, 3 Normal, 4 Low")] = None,
    type_id: Annotated[StrictId | None, Field(description="New type id (list_ticket_types)")] = None,
    client_id: Annotated[
        StrictId | None,
        Field(
            description=(
                "Move to this client (list_clients); resets contact, CCs, billing; unlinks assets, uptime checks"
            )
        ),
    ] = None,
    location_id: Annotated[StrictId | None, Field(description="New location id (list_client_locations)")] = None,
    contact_id: Annotated[StrictId | None, Field(description="New primary contact id (list_contacts)")] = None,
    cc_contact_ids: Annotated[
        list[StrictId] | None, Field(description="Complete new CC list (replaces; list_contacts)")
    ] = None,
    group_ids: Annotated[
        list[StrictId] | None, Field(description="Complete new group ids (replaces; list_org_groups)")
    ] = None,
    lead_assignee_id: Annotated[
        StrictId | None,
        Field(
            description="New lead user id (list_org_users); cannot unassign. Not also a watcher: Gorelo answers "
            '400 "Technician already exists"'
        ),
    ] = None,
    assisting_assignee_ids: Annotated[
        list[StrictId] | None,
        Field(
            description="Complete new assisting user ids (replaces; list_org_users). Not also a watcher: Gorelo "
            'answers 400 "Technician already exists"'
        ),
    ] = None,
    watcher_ids: Annotated[
        list[StrictId] | None,
        Field(
            description="Complete new watcher user ids (replaces; list_org_users). Not also lead or assisting: "
            'Gorelo answers 400 "Technician already exists"'
        ),
    ] = None,
    tag_ids: Annotated[
        list[StrictId] | None, Field(description="Complete new tag ids (replaces; list_ticket_tags)")
    ] = None,
    agent_asset_ids: Annotated[
        list[str] | None, Field(description="Complete new agent asset GUIDs (replaces; list_agents)")
    ] = None,
    custom_asset_ids: Annotated[
        list[str] | None, Field(description="Complete new custom asset GUIDs (replaces; list_custom_assets)")
    ] = None,
    uptime_ids: Annotated[
        list[str] | None, Field(description="Complete new uptime check GUIDs (replaces; list_uptime_checks)")
    ] = None,
    closed_on: Annotated[
        str | None,
        Field(description="Close date (not future); needs a closed status. Also sets updated_on unless given"),
    ] = None,
    updated_on: Annotated[
        str | None, Field(description="Last-activity time instead of now; stamps this call only")
    ] = None,
    updated_by_name: Annotated[
        str | None, Field(description="Author name recorded for this update; stamps this call only")
    ] = None,
    billing_service_line_id: Annotated[
        StrictId | None, Field(description="Billing: service line id (list_contracts or get_contract)")
    ] = None,
    billing_role_id: Annotated[StrictId | None, Field(description="Billing: role id (list_billing_roles)")] = None,
    billing_work_type_id: Annotated[
        StrictId | None, Field(description="Billing: work type id (list_work_types)")
    ] = None,
    billable_status_id: Annotated[
        StrictId | None, Field(description="Billing: 1 Billable, 2 No charge, 3 Non-billable")
    ] = None,
    clear_fields: Annotated[
        list[ClearableField] | None,
        Field(description="List params to EMPTY: any list param except group_ids (e.g. tag_ids)"),
    ] = None,
) -> dict:
    """Change fields of a ticket and return its full record. Only given fields change; billing_* fields you leave out
    keep their current values.

    Single-value fields cannot be cleared, so a ticket cannot be unassigned. List fields REPLACE the whole list; to
    empty one name it in clear_fields ([] is refused).
    Warnings: changing client_id resets the contact, CCs and billing override and unlinks assets and uptime checks (send
    new ones in the same call). closed_on without updated_on also sets UpdatedOn, and needs a closed status.
    A technician cannot be lead (or assisting) and watcher at once.
    Side effects: Gorelo's normal notifications and timeline entries fire for each changed field; a status change may
    email the contact and fire automation under the tenant's rules.
    The record is read back; if that fails the result is {Id, warning}: the update WAS applied, do not repeat it.
    """
    try:
        ticket = guid("ticket_id", ticket_id)
    except ValueError as err:
        raise ValueError(
            f"{err}; for a ticket number or display number such as TCK-1234, call get_ticket first and pass its Id"
        ) from None
    closed_text, closed_at = _instant("closed_on", closed_on)
    updated_text, updated_at = _instant("updated_on", updated_on)
    _not_in_future("closed_on", closed_text, closed_at)
    _not_in_future("updated_on", updated_text, updated_at)
    clear: list[str] | None = None
    if clear_fields is not None:
        non_empty("clear_fields", clear_fields)
        for index, name in enumerate(clear_fields):
            if name not in CLEARABLE_FIELDS:
                raise ValueError(
                    f"clear_fields[{index}]: not a field that can be cleared; allowed: {', '.join(CLEARABLE_FIELDS)}. "
                    "Single-value fields cannot be cleared through the API (null counts as absent) and group_ids "
                    "must keep at least one group"
                )
        clear = list(clear_fields)
    clearing = set(clear or [])

    def ids_of(param: str, values: list[int] | None) -> list[int] | None:
        return _replacing(param, values, positive_ids, clearable=param in CLEARABLE_FIELDS, clearing=param in clearing)

    def guids_of(param: str, values: list[str] | None) -> list[str] | None:
        return _replacing(param, values, guids, clearable=True, clearing=param in clearing)

    values: dict[str, Any] = {
        "title": _title("title", title),
        "status_id": _optional(positive_id, "status_id", status_id),
        "priority_id": _optional(_priority_id, "priority_id", priority_id),
        "type_id": _optional(positive_id, "type_id", type_id),
        "client_id": _optional(positive_id, "client_id", client_id),
        "location_id": _optional(positive_id, "location_id", location_id),
        "contact_id": _optional(positive_id, "contact_id", contact_id),
        "cc_contact_ids": ids_of("cc_contact_ids", cc_contact_ids),
        "group_ids": ids_of("group_ids", group_ids),
        "lead_assignee_id": _optional(positive_id, "lead_assignee_id", lead_assignee_id),
        "assisting_assignee_ids": ids_of("assisting_assignee_ids", assisting_assignee_ids),
        "watcher_ids": ids_of("watcher_ids", watcher_ids),
        "tag_ids": ids_of("tag_ids", tag_ids),
        "agent_asset_ids": guids_of("agent_asset_ids", agent_asset_ids),
        "custom_asset_ids": guids_of("custom_asset_ids", custom_asset_ids),
        "uptime_ids": guids_of("uptime_ids", uptime_ids),
        "closed_on": closed_text,
        "updated_on": updated_text,
        "updated_by_name": non_empty("updated_by_name", updated_by_name),
        "billing_service_line_id": _optional(positive_id, "billing_service_line_id", billing_service_line_id),
        "billing_role_id": _optional(positive_id, "billing_role_id", billing_role_id),
        "billing_work_type_id": _optional(positive_id, "billing_work_type_id", billing_work_type_id),
        "billable_status_id": _optional(_billable_status_id, "billable_status_id", billable_status_id),
    }
    body = build_body(values, UPDATE_FIELDS, clear=clear, clear_values={param: [] for param in CLEARABLE_FIELDS})
    if not set(body) - set(STAMP_FIELDS):
        raise ValueError(
            "update_ticket: nothing to change. Give at least one field to change besides ticket_id (Gorelo rejects "
            "an empty PATCH; updated_on and updated_by_name only stamp the other changes of the same call): "
            f"{', '.join(param for param in UPDATE_FIELDS if param not in ('updated_on', 'updated_by_name'))} "
            "or clear_fields"
        )
    client = client_of(ctx)
    given = [param for param in BILLING_PARTS if values[param] is not None]
    if given and len(given) < len(BILLING_PARTS) and client_id is None:
        # A partial BillingOverride may reset the parts it leaves out: read the current ones and send them all.
        # (With client_id the override is reset by the move anyway, so the old client's values are not carried over.)
        override = body["BillingOverride"]
        for param, existing in (await _current_billing(client, ticket)).items():
            override.setdefault(UPDATE_FIELDS[param].split(".", 1)[1], existing)
    written = await client.patch(OP_UPDATE, path_params={"ticketId": ticket}, json_body=body, tool="update_ticket")
    expect_object(written, OP_UPDATE, tool="update_ticket")
    return await reread_after_write(
        ctx, OP_GET, path_params={"ticketId": ticket}, tool="update_ticket", written_id=ticket
    )


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------


@gorelo_tool(toolset="tickets", kind="read", ops=[OP_STATUSES])
async def list_ticket_statuses(ctx: Context) -> dict:
    """List the tenant's ticket statuses (Id, Name, ...) for status ids. Returns {items, count}.
    Side effects: None (read-only)."""
    return list_result(await client_of(ctx).get_list(OP_STATUSES, tool="list_ticket_statuses"))


@gorelo_tool(toolset="tickets", kind="read", ops=[OP_TYPES])
async def list_ticket_types(ctx: Context) -> dict:
    """List the tenant's ticket types (Id, Name, ...) for type ids. Returns {items, count}.
    Side effects: None (read-only)."""
    return list_result(await client_of(ctx).get_list(OP_TYPES, tool="list_ticket_types"))


@gorelo_tool(toolset="tickets", kind="read", ops=[OP_TAGS])
async def list_ticket_tags(ctx: Context) -> dict:
    """List the tenant's ticket tags (Id, Name, ...) for tag ids. Returns {items, count}.
    Side effects: None (read-only)."""
    return list_result(await client_of(ctx).get_list(OP_TAGS, tool="list_ticket_tags"))


@gorelo_tool(toolset="tickets", kind="read", ops=[OP_LIST])
async def list_ticket_priorities() -> dict:
    """List the ticket priorities: 0 None, 1 Urgent, 2 High, 3 Normal, 4 Low. Returns {items, count}. The same ids work
    for create, update, filters and reads.
    Side effects: None (read-only)."""
    return list_result([{"Id": pid, "Name": name} for pid, name in PRIORITIES])


@gorelo_tool(toolset="tickets", kind="read", ops=[OP_LIST])
async def list_ticket_sources() -> dict:
    """List the ticket sources for create_ticket: 1 Web, 2 Email, 3 Phone, 4 Chat, 5 Alert, 6 Api. Id 7 appears on
    older tickets with no name and cannot be set. Returns {items, count, note}.
    Side effects: None (read-only)."""
    return {**list_result([{"Id": sid, "Name": name} for sid, name in SOURCES]), "note": SOURCES_NOTE}
