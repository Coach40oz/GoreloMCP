"""Tickets and their lookups: statuses, types, tags, priorities, sources (toolset "tickets").

Tools: list_tickets, search_tickets, get_ticket, create_ticket, update_ticket, list_ticket_statuses,
list_ticket_types, list_ticket_tags, list_ticket_priorities, list_ticket_sources, list_sla_policies.

Ops used by this module (each tool declares every op it calls, re-read GETs included):

    GET /v1/tickets
    GET /v1/tickets/{ticketId}
    POST /v1/tickets
    PATCH /v1/tickets/{ticketId}
    GET /v1/tickets/statuses
    GET /v1/tickets/types
    GET /v1/tickets/tags
    GET /v1/tickets/priorities
    GET /v1/tickets/sources
    GET /v1/tickets/sla/policies

list_sla_policies (contract b0850edb962d, 2026-11-05) reads the SLA policies, unpaged, so that a ticket's Sla.PolicyId
(a field of get_ticket and list_tickets, passed through unchanged with Sla.FirstResponse and Sla.Resolution) can be
turned into a name and its per-priority targets. Policies are managed in the Gorelo app, not through the API.

list_ticket_priorities and list_ticket_sources read the live scales (GET /v1/tickets/priorities and
GET /v1/tickets/sources, unpaged {Id, Name}, the same for every account; they were local tables before 1.5.0).
The PRIORITIES and SOURCES tables below stay as the local checks of the priority and source ids a write or a
filter takes, and as the names search_tickets resolves a priority name against. There is no delete tool:
DELETE /v1/tickets/{ticketId} is in FORBIDDEN_OPS.

Rules this module follows:

* Priority is ONE scale everywhere (0 None, 1 Urgent, 2 High, 3 Normal, 4 Low); source ids are 1 to 6 on a
  write (create_ticket, update_ticket) and 1 to 7 on the source_ids filter (7 Messenger only appears on tickets read back).
  Status, type, group, user and tag ids are tenant data: they are never hardcoded, names are resolved
  through the list endpoints.
* Every integer id is typed StrictId (JSON true or "5" is refused) and checked with the shared
  positive_id/positive_ids; GUIDs go through guid/guids. This module keeps no copies of those helpers.
* GET /v1/tickets filters on the server (ids, text, dates, close dates, sources). search_tickets sends everything the API can
  filter and applies only IsWaitingOnThem, LeadAssigneeId, IsUnread and ClosedOn on the rows it read; its
  errors name the filter parameter involved. It stops reading as soon as it holds limit + 1 matches (stopped_early; matched
  is then a minimum), and exclude_closed without a status sends every status whose BaseStatus is not Closed (4).
* list_tickets and search_tickets take view="summary": the rows keep only TICKET_SUMMARY_FIELDS (summary_rows).
* A tool that takes one ticket (get_ticket, update_ticket) accepts a GUID, a number or a display number through
  resolve_ticket_id (tools/_ticket_ref.py) and declares TICKET_LIST_OP. A list of ticket ids would stay GUID only.
* POST and PATCH answer with only {"Id": ...}. created_id / expect_object refuse any other answer (a shape
  error that says the write may have been applied); then the record is read back with reread_after_write,
  which returns {"Id", "warning"} instead of raising when that read fails (the write already happened).
* PATCH treats null as "not given" (probe), so nothing can be cleared with null. List fields replace
  outright; [] is only sent through update_ticket's clear_fields. A BillingOverride that names only some of
  its parts may reset the others, so update_ticket reads the ticket first and sends the complete override.
  A record whose BillingOverride cannot be read with certainty (no such key, none of its four parts, a part
  that is not an object with an Id) is a shape error and no PATCH is sent; null, {}, null parts and an Id of
  null or 0 mean "not set".
* Local validation errors name the snake_case parameter and happen before any write, scan or lookup request (update_ticket
  checks everything and builds its body first, and resolves a ticket number only then). The requests a call may spend before
  its main one: search_tickets reads GET /v1/tickets/statuses or /types for a status or type NAME (ids go through as they are),
  and GET /v1/tickets/statuses for exclude_closed without a status (it sends the ids of the statuses whose BaseStatus is not
  Closed; with a status given by NAME the status row is read anyway and only decides a note); get_ticket and update_ticket
  look a ticket number or display number up with GET /v1/tickets (Query, at most 20 pages, see tools/_ticket_ref.py), a GUID
  costs nothing; and a partial billing override in update_ticket needs a read of the ticket before the write.
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
    FILTER_IDS_MAX,
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
    guids,
    list_result,
    non_empty,
    paged_result,
    positive_id,
    positive_ids,
    reread_after_write,
    summary_rows,
    utc_iso,
)
from tools._ticket_ref import QUERY_MAX_CHARS, TICKET_LIST_OP, check_ticket_reference, resolve_ticket_id

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
OP_PRIORITIES = "GET /v1/tickets/priorities"
OP_SOURCES = "GET /v1/tickets/sources"
OP_SLA_POLICIES = "GET /v1/tickets/sla/policies"

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

# Ticket sources a write accepts (TicketSource enum). Id 7 (Messenger) only appears on tickets read back: the
# source_ids filter and GET /v1/tickets/sources know it, a write does not accept it.
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
SOURCE_MESSENGER_ID = 7
SOURCE_FILTER_IDS = SOURCE_IDS + (SOURCE_MESSENGER_ID,)
SOURCE_FILTER_HELP = f"{SOURCE_HELP}, {SOURCE_MESSENGER_ID} Messenger"
SOURCES_NOTE = (
    "Id 7 (Messenger) only appears on tickets read back, and the source_ids filter takes it. It cannot be set: "
    "create_ticket and update_ticket accept source_id 1 to 6 only."
)

BILLABLE_STATUSES: tuple[tuple[int, str], ...] = ((1, "Billable"), (2, "No charge"), (3, "Non-billable"))
BILLABLE_IDS = tuple(bid for bid, _ in BILLABLE_STATUSES)
BILLABLE_HELP = ", ".join(f"{bid} {name}" for bid, name in BILLABLE_STATUSES)

TITLE_MAX_CHARS = 250  # CreateTicketCommand.Title and UpdateTicketCommand.Title: "Maximum 250 characters"

# search_tickets reads every page of the server-side matches, up to this many tickets, then filters.
SEARCH_SCAN_CAP = 5000
SEARCH_PAGE_SIZE = 200
SEARCH_LIMIT_DEFAULT = 50
SEARCH_LIMIT_MAX = 500

# GET /v1/tickets DeviceIds: "Comma-separated agent ids (GUIDs, up to 100)".
DEVICE_IDS_MAX = 100

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
    "device_ids": "DeviceIds",
    "source_ids": "SourceIds",
    "query": "Query",
    "updated_since": "UpdatedSince",
    "updated_before": "UpdatedBefore",
    "created_since": "CreatedSince",
    "created_before": "CreatedBefore",
    "closed_since": "ClosedSince",
    "closed_before": "ClosedBefore",
    "sort_by": "SortBy",
    "sort_order": "SortOrder",
}
LIST_ERROR_FIELDS: dict[str, str] = {**LIST_QUERY_FIELDS, "page_size": "PageSize", "cursor": "Cursor"}

SEARCH_ERROR_FIELDS: dict[str, str] = {
    "client_id": "ClientIds",
    "status": "StatusIds",
    "priority": "PriorityIds",
    "type": "TypeIds",
    "lead_assignee_ids": "LeadAssigneeIds",
    "contact_ids": "ContactIds",
    "tag_ids": "TagIds",
    "group_ids": "GroupIds",
    "device_ids": "DeviceIds",
    "source_ids": "SourceIds",
    "query": "Query",
    "updated_since": "UpdatedSince",
    "updated_before": "UpdatedBefore",
    "created_since": "CreatedSince",
    "created_before": "CreatedBefore",
    "closed_since": "ClosedSince",
    "closed_before": "ClosedBefore",
}

# The filters list_tickets and search_tickets share, with the texts both show.
LEAD_IDS_HELP = "Lead user ids (list_org_users)"
CONTACT_IDS_HELP = "Primary contact ids (list_contacts)"
TAG_IDS_HELP = "Tag ids, any match (list_ticket_tags)"
GROUP_IDS_HELP = "Group ids, any match (list_org_groups)"
DEVICE_IDS_HELP = "Agent asset GUIDs (list_agents), up to 100; a ticket matches if any is linked to it"

# view="summary" keeps these fields of a ticket row (all are in TicketListItemModel), in this order.
TICKET_SUMMARY_FIELDS: tuple[str, ...] = (
    "Id", "Number", "DisplayNumber", "Title", "ClientId", "Status", "Priority", "Type", "LeadAssigneeId", "ContactId",
    "CreatedOn", "UpdatedOn", "ClosedOn", "IsWaitingOnThem", "IsUnread",
)
VIEW_HELP = f"summary: only {', '.join(TICKET_SUMMARY_FIELDS)}. full: every field"

# StatusListItemModel.BaseStatus {Id, Name}: the base status whose tickets exclude_closed leaves out (1 New, 2 Open, 3 Solved,
# 4 Closed, 6 On Hold, measured 2026-11-09).
BASE_STATUS_CLOSED = 4

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
    "source_id": "SourceId",
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

# ticket_id is looked up with the Query filter of GET /v1/tickets when it is a number or a display number, so a Gorelo 400 on
# it reads as ticket_id.
UPDATE_ERROR_FIELDS: dict[str, str] = {**UPDATE_FIELDS, "ticket_id": "Query"}

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
    return _one_of(param, value, SOURCE_IDS, SOURCE_HELP, "; id 7 (Messenger) only appears on tickets read back and cannot be set")


def _source_filter_id(param: str, value: Any) -> int:
    return _one_of(param, value, SOURCE_FILTER_IDS, SOURCE_FILTER_HELP)


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


def _entity_filters(
    lead_assignee_ids: list[int] | None,
    contact_ids: list[int] | None,
    tag_ids: list[int] | None,
    group_ids: list[int] | None,
    device_ids: list[str] | None,
) -> dict[str, list[Any] | None]:
    """The checked lead, contact, tag, group and device filters of list_tickets and search_tickets, by parameter (None: not
    given). Ids are positive whole numbers, devices are GUIDs, at most DEVICE_IDS_MAX of them."""
    checked: dict[str, list[Any] | None] = {
        "lead_assignee_ids": positive_ids("lead_assignee_ids", lead_assignee_ids),
        "contact_ids": positive_ids("contact_ids", contact_ids),
        "tag_ids": positive_ids("tag_ids", tag_ids),
        "group_ids": positive_ids("group_ids", group_ids),
    }
    devices = guids("device_ids", device_ids)
    if devices is not None and len(devices) > DEVICE_IDS_MAX:
        raise ValueError(f"device_ids: at most {DEVICE_IDS_MAX} values, got {len(devices)}")
    checked["device_ids"] = devices
    return checked


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
    client: GoreloClient,
    param: str,
    name: str,
    op_key: str,
    *,
    noun: str,
    plural: str,
    tool: str,
    rows_read: list[Any] | None = None,
) -> tuple[int, str]:
    """Look a status or type NAME up in the tenant's list (GET op_key) and return its (id, canonical name). When `rows_read`
    is given the rows of that one read are put into it (search_tickets reads the BaseStatus of the status it found)."""
    rows = await client.get_list(op_key, tool=tool)
    if rows_read is not None:
        rows_read.extend(rows)
    return _match_name(param, name, _lookup_entries(rows), noun=noun, plural=plural)


def _status_is_closed(rows: list[Any], status_id: int) -> bool:
    """True when the status row with this Id has a BaseStatus whose Id is Closed (4); False when it has another base status or
    the row cannot be read (no row, no BaseStatus): the caller then says nothing about it."""
    for row in rows:
        if isinstance(row, dict) and row.get("Id") == status_id:
            base = row.get("BaseStatus")
            base_id = base.get("Id") if isinstance(base, dict) else None
            return isinstance(base_id, int) and not isinstance(base_id, bool) and base_id == BASE_STATUS_CLOSED
    return False


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


def _active_filters(
    *, awaiting_client: bool | None, unassigned_only: bool, unread_only: bool, exclude_closed: bool
) -> list[tuple[str, str]]:
    """(parameter, ticket field) of every client-side filter that is on."""
    active = {
        "awaiting_client": awaiting_client is not None,
        "unassigned_only": unassigned_only,
        "unread_only": unread_only,
        "exclude_closed": exclude_closed,
    }
    return [(param, field) for param, field in CLIENT_SIDE_FILTERS if active[param]]


def _require_objects(rows: list[Any], needed: list[tuple[str, str]]) -> None:
    """A row that is not an object cannot be filtered: an error that names the filters it blocks."""
    names = ", ".join(param for param, _ in needed)
    for row in rows:
        if not isinstance(row, dict):
            raise _shape_error(
                OP_LIST, f"a ticket row is not an object, so the filter(s) {names} cannot be applied; refusing to guess"
            )


def _row_passes(
    row: dict[str, Any], *, awaiting_client: bool | None, unassigned_only: bool, unread_only: bool, exclude_closed: bool
) -> bool:
    if awaiting_client is not None and _flag(row, "awaiting_client", "IsWaitingOnThem") is not awaiting_client:
        return False
    if unassigned_only and row.get("LeadAssigneeId") is not None:
        return False
    if unread_only and not _flag(row, "unread_only", "IsUnread"):
        return False
    return not (exclude_closed and row.get("ClosedOn") is not None)


def _client_side_filter(
    rows: list[Any], *, awaiting_client: bool | None, unassigned_only: bool, unread_only: bool, exclude_closed: bool
) -> list[Any]:
    """Apply the four filters the API cannot do. A field that no scanned row carries at all means Gorelo
    renamed or dropped it (IsAwaitingClient became IsWaitingOnThem once), so that is an error, never a
    silently wrong filter. Every error names the filter parameter it is about."""
    flags = {
        "awaiting_client": awaiting_client,
        "unassigned_only": unassigned_only,
        "unread_only": unread_only,
        "exclude_closed": exclude_closed,
    }
    needed = _active_filters(**flags)
    if not needed:
        return list(rows)
    _require_objects(rows, needed)
    for param, field in needed:
        if rows and not any(field in row for row in rows):
            raise _shape_error(
                OP_LIST,
                f"none of the {len(rows)} scanned ticket rows has a {field} field, so the {param} filter "
                "cannot be applied; Gorelo may have renamed it (refusing to guess)",
            )
    return [row for row in rows if _row_passes(row, **flags)]


def _count_matches(
    rows: list[Any], *, awaiting_client: bool | None, unassigned_only: bool, unread_only: bool, exclude_closed: bool
) -> int:
    """How many of `rows` pass the client-side filters (every row, when none is on). The scan calls it on each new page
    only; _client_side_filter then runs once on all the rows and keeps the checks of a field that no row carries."""
    flags = {
        "awaiting_client": awaiting_client,
        "unassigned_only": unassigned_only,
        "unread_only": unread_only,
        "exclude_closed": exclude_closed,
    }
    needed = _active_filters(**flags)
    if not needed:
        return len(rows)
    _require_objects(rows, needed)
    return sum(_row_passes(row, **flags) for row in rows)


def _open_status_ids(rows: list[Any]) -> tuple[list[int] | None, str | None]:
    """(the ids of every status whose BaseStatus is not Closed, None) or (None, why the search is not narrowed).

    exclude_closed without a status sends these ids as StatusIds, so Gorelo leaves the tickets of a Closed status out. Not
    narrowing is the old behaviour (every status is read, the ClosedOn check alone applies); it happens when a status row
    has no BaseStatus {Id} that is a whole number or a kept row has no usable Id, when no status would remain, and when more
    ids would remain than one id filter takes (FILTER_IDS_MAX)."""
    open_ids: list[int] = []
    for row in rows:
        base = row.get("BaseStatus") if isinstance(row, dict) else None
        base_id = base.get("Id") if isinstance(base, dict) else None
        if isinstance(base_id, bool) or not isinstance(base_id, int):
            return None, "a status row has no BaseStatus Id that is a whole number"
        if base_id == BASE_STATUS_CLOSED:
            continue
        status_id = row.get("Id")
        if isinstance(status_id, bool) or not isinstance(status_id, int) or status_id < 1:
            return None, "a status row has no usable Id"
        open_ids.append(status_id)
    if not open_ids:
        return None, "no status would remain"
    if len(open_ids) > FILTER_IDS_MAX:
        return None, f"{len(open_ids)} statuses remain and one filter takes at most {FILTER_IDS_MAX} ids"
    return open_ids, None


def _search_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= SEARCH_LIMIT_MAX:
        raise ValueError(
            f"limit: must be a whole number from 1 to {SEARCH_LIMIT_MAX}; narrow the filters, "
            "or page through list_tickets with next_cursor, to read more"
        )
    return value


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
    lead_assignee_ids: Annotated[list[StrictId] | None, Field(description=LEAD_IDS_HELP)] = None,
    contact_ids: Annotated[list[StrictId] | None, Field(description=CONTACT_IDS_HELP)] = None,
    tag_ids: Annotated[list[StrictId] | None, Field(description=TAG_IDS_HELP)] = None,
    group_ids: Annotated[list[StrictId] | None, Field(description=GROUP_IDS_HELP)] = None,
    device_ids: Annotated[list[str] | None, Field(description=DEVICE_IDS_HELP)] = None,
    source_ids: Annotated[
        list[StrictId] | None,
        Field(description="Source ids, up to 100: 1 Web, 2 Email, 3 Phone, 4 Chat, 5 Alert, 6 Api, 7 Messenger (list_ticket_sources)"),
    ] = None,
    query: Annotated[str | None, Field(description="Text in title, number or display number (max 200)")] = None,
    updated_since: Annotated[str | None, Field(description="Updated at or after (ISO 8601)")] = None,
    updated_before: Annotated[str | None, Field(description="Updated before (ISO 8601)")] = None,
    created_since: Annotated[str | None, Field(description="Created at or after (ISO 8601)")] = None,
    created_before: Annotated[str | None, Field(description="Created before (ISO 8601)")] = None,
    closed_since: Annotated[str | None, Field(description="Closed at or after (ISO 8601); never-closed tickets are left out")] = None,
    closed_before: Annotated[str | None, Field(description="Closed before (ISO 8601); never-closed tickets are left out")] = None,
    sort_by: Annotated[Literal["updatedOn", "createdOn"] | None, Field(description="Default updatedOn")] = None,
    sort_order: Annotated[Literal["asc", "desc"] | None, Field(description="Default desc")] = None,
    page_size: Annotated[int, Field(description="1-200")] = 50,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous call, same filters")] = None,
    view: Annotated[Literal["full", "summary"], Field(description=VIEW_HELP)] = "full",
) -> dict:
    """List one page of tickets, newest update first unless sorted. Returns items plus count, total_count, has_more,
    next_cursor.

    Notable fields: IsWaitingOnThem (renamed from IsAwaitingClient), Sla.FirstResponse.ElapsedBusinessMinutes,
    ChecklistSummary, ContactId (null without a primary contact). Description, time and billing are only in get_ticket.
    Sla: PolicyId (list_sla_policies), FirstResponse {Status, DueOn, AtRiskOn, RespondedOn, TargetBusinessMinutes},
    Resolution; a status is 1 NotApplicable, 2 Pending, 3 Met, 4 Breached.
    For status or priority NAMES, or waiting, unassigned, unread or open filters, use search_tickets.
    closed_since and closed_before keep only closed tickets (a never-closed ticket is left out).
    Side effects: None (read-only).
    Paging: pass next_cursor back as cursor with the SAME filters until has_more is false.
    """
    checked = {
        "status_ids": positive_ids("status_ids", status_ids),
        "client_ids": positive_ids("client_ids", client_ids),
        "priority_ids": _each("priority_ids", priority_ids, _priority_id),
        "type_ids": positive_ids("type_ids", type_ids),
    }
    checked.update(_entity_filters(lead_assignee_ids, contact_ids, tag_ids, group_ids, device_ids))
    checked["source_ids"] = _each("source_ids", source_ids, _source_filter_id)
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
        "closed_since": _instant("closed_since", closed_since),
        "closed_before": _instant("closed_before", closed_before),
    }
    _check_window("updated_since", moments["updated_since"], "updated_before", moments["updated_before"])
    _check_window("created_since", moments["created_since"], "created_before", moments["created_before"])
    _check_window("closed_since", moments["closed_since"], "closed_before", moments["closed_before"])
    for param, (moment_text, _) in moments.items():
        if moment_text is not None:
            api_query[LIST_QUERY_FIELDS[param]] = moment_text
            filters[param] = moment_text
    for param, value in (("sort_by", sort_by), ("sort_order", sort_order)):
        if value is not None:
            api_query[LIST_QUERY_FIELDS[param]] = value
            filters[param] = value
    if view == "summary":
        filters["view"] = view
    token = non_empty("cursor", cursor)
    page = await client_of(ctx).get_page(
        OP_LIST, query=api_query, page_size=clamp_page_size(page_size), cursor=token, tool="list_tickets"
    )
    if view == "summary":
        page = dataclasses.replace(
            page, items=summary_rows(page.items, TICKET_SUMMARY_FIELDS, op_key=OP_LIST, tool="list_tickets")
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
    lead_assignee_ids: Annotated[list[StrictId] | None, Field(description=LEAD_IDS_HELP)] = None,
    contact_ids: Annotated[list[StrictId] | None, Field(description=CONTACT_IDS_HELP)] = None,
    tag_ids: Annotated[list[StrictId] | None, Field(description=TAG_IDS_HELP)] = None,
    group_ids: Annotated[list[StrictId] | None, Field(description=GROUP_IDS_HELP)] = None,
    device_ids: Annotated[list[str] | None, Field(description=DEVICE_IDS_HELP)] = None,
    source_ids: Annotated[
        list[StrictId] | None,
        Field(description="Source ids, up to 100: 1 Web, 2 Email, 3 Phone, 4 Chat, 5 Alert, 6 Api, 7 Messenger (list_ticket_sources)"),
    ] = None,
    query: Annotated[str | None, Field(description="Text in title, number or display number (max 200)")] = None,
    updated_since: Annotated[str | None, Field(description="Updated at or after (ISO 8601)")] = None,
    updated_before: Annotated[str | None, Field(description="Updated before (ISO 8601)")] = None,
    created_since: Annotated[str | None, Field(description="Created at or after (ISO 8601)")] = None,
    created_before: Annotated[str | None, Field(description="Created before (ISO 8601)")] = None,
    closed_since: Annotated[str | None, Field(description="Closed at or after (ISO 8601); never-closed tickets are left out")] = None,
    closed_before: Annotated[str | None, Field(description="Closed before (ISO 8601); never-closed tickets are left out")] = None,
    unassigned_only: Annotated[bool, Field(description="True: only tickets with no lead assignee")] = False,
    awaiting_client: Annotated[
        bool | None, Field(description="True: waiting on the client; false: not; omit: both")
    ] = None,
    unread_only: Annotated[bool, Field(description="True: only unread tickets")] = False,
    exclude_closed: Annotated[
        bool,
        Field(
            description="True: with no status, ask only for non-Closed statuses; always drop tickets with a ClosedOn time "
            "(with a status, that is the only check)"
        ),
    ] = False,
    limit: Annotated[int, Field(description="Most tickets returned, 1-500")] = SEARCH_LIMIT_DEFAULT,
    view: Annotated[Literal["full", "summary"], Field(description=VIEW_HELP)] = "full",
) -> dict:
    """Find tickets by client, status, priority, type, source, text, dates, lead, contact, tag, group, device or state.
    Returns items, count, total_count, matched, scanned, truncated, complete_scan, stopped_early, count_mismatch, filters.

    awaiting_client, unassigned_only, unread_only and the ClosedOn check of exclude_closed run on the rows read, the rest
    on the server. Filters combine with AND. exclude_closed without status asks only for non-Closed statuses; with status it
    drops only tickets with a ClosedOn time, so a Closed status can still be listed.
    The scan stops once it holds limit + 1 matches: stopped_early is true and matched is a minimum (total_count is
    Gorelo's count for the server-side filters). Otherwise it reads up to 5000 tickets. truncated true: matches are
    missing: raise limit (max 500) or narrow the filters. count_mismatch true: repeat.
    Side effects: None (read-only).
    """
    client = client_of(ctx)
    wanted_limit = _search_limit(limit)
    client_value = _optional(positive_id, "client_id", client_id)
    query_text = _query_text("query", query)
    entities = _entity_filters(lead_assignee_ids, contact_ids, tag_ids, group_ids, device_ids)
    updated = (_instant("updated_since", updated_since), _instant("updated_before", updated_before))
    created = (_instant("created_since", created_since), _instant("created_before", created_before))
    closed = (_instant("closed_since", closed_since), _instant("closed_before", closed_before))
    _check_window("updated_since", updated[0], "updated_before", updated[1])
    _check_window("created_since", created[0], "created_before", created[1])
    _check_window("closed_since", closed[0], "closed_before", closed[1])
    if unassigned_only and lead_assignee_ids is not None:
        raise ValueError(
            "unassigned_only: cannot be combined with lead_assignee_ids (unassigned_only keeps tickets with no lead "
            "assignee, lead_assignee_ids keeps tickets with one of those leads, so nothing could match); drop one of them"
        )
    if exclude_closed and (closed[0][0] is not None or closed[1][0] is not None):
        raise ValueError(
            "exclude_closed: cannot be combined with closed_since or closed_before (they keep only closed tickets, "
            "exclude_closed drops them, so nothing could match); drop one of them"
        )
    sources = _each("source_ids", source_ids, _source_filter_id)
    if sources is not None:
        csv_ids("source_ids", sources)  # the 100 id limit of the filter, before any request
    # Local checks first (nothing is sent if one fails), then the lookups that need Gorelo.
    priority_match = _resolve_priority("priority", priority) if priority is not None else None
    status_target = _id_or_name("status", status) if status is not None else None
    type_target = _id_or_name("type", type) if type is not None else None
    status_match: tuple[int, str | None] | None = None
    status_rows: list[Any] = []  # the status list, when a status NAME made us read it
    if isinstance(status_target, int):
        status_match = (status_target, None)
    elif status_target is not None:
        status_match = await _resolve_named(
            client,
            "status",
            status_target,
            OP_STATUSES,
            noun="status",
            plural="statuses",
            tool="search_tickets",
            rows_read=status_rows,
        )
    type_match: tuple[int, str | None] | None = None
    if isinstance(type_target, int):
        type_match = (type_target, None)
    elif type_target is not None:
        type_match = await _resolve_named(
            client, "type", type_target, OP_TYPES, noun="type", plural="types", tool="search_tickets"
        )
    open_status_ids: list[int] | None = None
    notes: list[str] = []
    if exclude_closed and status_match is not None and _status_is_closed(status_rows, status_match[0]):
        # exclude_closed never changes the status filter that was given: only the ClosedOn check applies, so say so.
        notes.append(
            f"status {status_match[1]!r} is a Closed status (BaseStatus 4): exclude_closed did not remove its tickets, it "
            "only dropped those with a ClosedOn time, so closed tickets without one can still be listed"
        )
    if exclude_closed and status_match is None:  # no status given: ask Gorelo only for the tickets of a status that is not Closed
        open_status_ids, why_not = _open_status_ids(await client.get_list(OP_STATUSES, tool="search_tickets"))
        if why_not is not None:
            notes.append(
                f"exclude_closed did not narrow the search by status ({why_not}), so every status was read and only "
                "the ClosedOn check applied"
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
    if open_status_ids is not None:
        api_query["StatusIds"] = csv_ids("status_ids", open_status_ids)
        filters["exclude_closed_status_ids"] = open_status_ids
    for param, ids in entities.items():
        if ids is not None:
            api_query[LIST_QUERY_FIELDS[param]] = csv_ids(param, ids)
            filters[param] = ids
    if sources is not None:
        api_query["SourceIds"] = csv_ids("source_ids", sources)
        filters["source_ids"] = sources
    if query_text is not None:
        api_query["Query"] = query_text
        filters["query"] = query_text
    for param, key, (moment_text, _) in (
        ("updated_since", "UpdatedSince", updated[0]),
        ("updated_before", "UpdatedBefore", updated[1]),
        ("created_since", "CreatedSince", created[0]),
        ("created_before", "CreatedBefore", created[1]),
        ("closed_since", "ClosedSince", closed[0]),
        ("closed_before", "ClosedBefore", closed[1]),
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
    if view == "summary":
        filters["view"] = view

    client_side = {
        "awaiting_client": awaiting_client,
        "unassigned_only": unassigned_only,
        "unread_only": unread_only,
        "exclude_closed": exclude_closed,
    }
    rows_counted = 0
    matches_counted = 0
    limit_exceeded = False

    def enough(rows: list[Any]) -> bool:
        """get_all calls this after each page with every row read so far: count the matches of the new rows only."""
        nonlocal rows_counted, matches_counted, limit_exceeded
        matches_counted += _count_matches(rows[rows_counted:], **client_side)
        rows_counted = len(rows)
        limit_exceeded = matches_counted > wanted_limit
        return limit_exceeded

    scan = await client.get_all(
        OP_LIST, query=api_query, page_size=SEARCH_PAGE_SIZE, max_items=SEARCH_SCAN_CAP, until=enough, tool="search_tickets"
    )
    matches = _client_side_filter(scan.items, **client_side)
    stopped_early = limit_exceeded and not scan.complete  # on the last page the scan read everything, so it is exact
    shown = matches[:wanted_limit]
    if view == "summary":
        shown = summary_rows(shown, TICKET_SUMMARY_FIELDS, op_key=OP_LIST, tool="search_tickets")
    result = all_result(dataclasses.replace(scan, items=shown), filters)
    result["truncated"] = len(matches) > wanted_limit or not scan.complete
    result["matched"] = len(matches)
    result["scanned"] = len(scan.items)
    result["stopped_early"] = stopped_early
    if stopped_early:
        notes.append(
            f"the scan stopped early after {len(scan.items)} tickets because more than {wanted_limit} matched: "
            f"matched ({len(matches)}) is a minimum and {wanted_limit} are returned; total_count is Gorelo's count for "
            "the server-side filters (it equals the number of matches when no client-side filter is active); raise "
            f"limit (max {SEARCH_LIMIT_MAX}) or narrow the filters"
        )
    elif len(matches) > wanted_limit:
        notes.append(
            f"{len(matches)} tickets matched and {wanted_limit} are returned: raise limit (max {SEARCH_LIMIT_MAX}) "
            "or narrow the filters"
        )
    if not scan.complete and not stopped_early:
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

    Sla: PolicyId (list_sla_policies), FirstResponse {Status, DueOn, AtRiskOn, RespondedOn, TargetBusinessMinutes},
    Resolution; a status is 1 NotApplicable, 2 Pending, 3 Met, 4 Breached.

    Prefer the GUID or the display number (TCK-1234). A bare number (1234) is searched with Query and can scan several
    pages (capped at 20 pages of 50 tickets); no match or several matches is an error.
    Side effects: None (read-only).
    """
    client = client_of(ctx)
    ticket = await resolve_ticket_id(client, ticket_id, tool="get_ticket")
    record = await client.get_one(OP_GET, path_params={"ticketId": ticket}, tool="get_ticket")
    return expect_object(record, OP_GET, tool="get_ticket")


# --------------------------------------------------------------------------
# Writing tickets
# --------------------------------------------------------------------------


@gorelo_tool(toolset="tickets", kind="write", ops=[OP_CREATE, OP_GET], field_map=CREATE_FIELDS)
async def create_ticket(
    ctx: Context,
    title: Annotated[str, Field(description="Max 250 characters")],
    description: Annotated[
        str, Field(description="Ticket body as HTML. Write a literal < or > as &lt; / &gt;: text in angle brackets is read as a tag and lost.")
    ],
    client_id: Annotated[StrictId, Field(description="Client id (list_clients)")],
    status_id: Annotated[StrictId, Field(description="Status id (list_ticket_statuses)")],
    type_id: Annotated[StrictId, Field(description="Type id (list_ticket_types)")],
    priority_id: Annotated[StrictId, Field(description="0 None, 1 Urgent, 2 High, 3 Normal, 4 Low")],
    source_id: Annotated[StrictId, Field(description="1 Web, 2 Email, 3 Phone, 4 Chat, 5 Alert, 6 Api")],
    group_id: Annotated[StrictId, Field(description="Group id (list_org_groups)")],
    contact_id: Annotated[
        StrictId | None,
        Field(description="Primary contact id (list_contacts); may be a contact of any client (no client: a temporary contact)"),
    ] = None,
    cc_contact_ids: Annotated[
        list[StrictId] | None,
        Field(description="Contact ids to CC (list_contacts); may belong to any client (no client: temporary contacts)"),
    ] = None,
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
    is_unread: Annotated[bool | None, Field(description="false creates it read (default unread); not true if backdated")] = None,
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
    Backdating: created_on, updated_on, closed_on need an offset, no future times; updated_on and closed_on require created_on. Backdated tickets send no emails or notifications: send_created_email=true and is_unread=true are refused.
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
    backdated = "created_on" if created_at is not None else ("closed_on" if closed_at is not None else None)
    if backdated is not None:
        if send_created_email:
            raise ValueError(
                f"send_created_email: Gorelo ignores it when {backdated} is supplied (a backdated import sends no "
                f"emails); omit send_created_email or drop {backdated}"
            )
        if is_unread:
            raise ValueError(
                f"is_unread: Gorelo ignores it when {backdated} is supplied (the ticket is created read); "
                f"omit is_unread or drop {backdated}"
            )
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


@gorelo_tool(
    toolset="tickets",
    kind="write",
    ops=[OP_UPDATE, OP_GET, TICKET_LIST_OP],
    field_map=UPDATE_ERROR_FIELDS,
    destructive_hint=True,
)
async def update_ticket(
    ctx: Context,
    ticket_id: Annotated[str | StrictId, Field(description="Ticket GUID, display number (TCK-1234) or number (1234)")],
    title: Annotated[str | None, Field(description="New title, max 250 characters")] = None,
    status_id: Annotated[StrictId | None, Field(description="New status id (list_ticket_statuses)")] = None,
    priority_id: Annotated[StrictId | None, Field(description="0 None, 1 Urgent, 2 High, 3 Normal, 4 Low")] = None,
    source_id: Annotated[
        StrictId | None,
        Field(description="1 Web, 2 Email, 3 Phone, 4 Chat, 5 Alert, 6 Api (list_ticket_sources); 7 Messenger cannot be set"),
    ] = None,
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
    contact_id: Annotated[
        StrictId | None, Field(description="New primary contact id (list_contacts); may be a contact of any client")
    ] = None,
    cc_contact_ids: Annotated[
        list[StrictId] | None,
        Field(description="Complete new CC list (replaces; list_contacts); contacts of any client"),
    ] = None,
    group_ids: Annotated[
        list[StrictId] | None, Field(description="Complete new group ids (replaces; list_org_groups)")
    ] = None,
    lead_assignee_id: Annotated[
        StrictId | None,
        Field(
            description="New lead user id (list_org_users); unassigning (LeadAssigneeId 0) is not offered yet. Not also a watcher: Gorelo answers "
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
        Field(
            description=(
                "Close date (not future); needs a closed status. Also sets updated_on unless given. "
                "History: no status email to the contact, no automation."
            )
        ),
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

    Single-value fields cannot be cleared; unassigning (LeadAssigneeId 0) is not offered yet. List fields
    REPLACE the whole list; to empty one name it in clear_fields.
    Warnings: changing client_id resets the contact, CCs and billing override and unlinks assets and uptime checks (send
    new ones in the same call). closed_on without updated_on also sets UpdatedOn, and needs a closed status.
    A technician cannot be lead (or assisting) and watcher at once.
    Side effects: notifications and timeline entries fire for each changed field; a status change may
    email the contact and fire automation under the tenant's rules (not for a close with closed_on).
    The record is read back; if that fails the result is {Id, warning}: the update WAS applied, do not repeat it.
    """
    client = client_of(ctx)
    check_ticket_reference("ticket_id", ticket_id)  # local only; the lookup waits until every other local check has passed
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
        "source_id": _optional(_source_id, "source_id", source_id),
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
    # Every local check has passed and the body is built: only now may a number or display number cost lookup requests.
    ticket = await resolve_ticket_id(client, ticket_id, tool="update_ticket")
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


@gorelo_tool(toolset="tickets", kind="read", ops=[OP_PRIORITIES])
async def list_ticket_priorities(ctx: Context) -> dict:
    """List the ticket priorities (Id, Name): the fixed scale, the same for every account (0 None, 1 Urgent, 2 High,
    3 Normal, 4 Low). Returns {items, count}. The same ids work for create, update, filters and reads.
    Side effects: None (read-only)."""
    return list_result(await client_of(ctx).get_list(OP_PRIORITIES, tool="list_ticket_priorities"))


@gorelo_tool(toolset="tickets", kind="read", ops=[OP_SOURCES])
async def list_ticket_sources(ctx: Context) -> dict:
    """List the ticket sources (Id, Name): the fixed scale, the same for every account. Returns {items, count, note}.
    create_ticket and update_ticket accept every id but 7 (Messenger); the source_ids filter of list_tickets and
    search_tickets accepts all of them.
    Side effects: None (read-only)."""
    return {**list_result(await client_of(ctx).get_list(OP_SOURCES, tool="list_ticket_sources")), "note": SOURCES_NOTE}


@gorelo_tool(toolset="tickets", kind="read", ops=[OP_SLA_POLICIES])
async def list_sla_policies(ctx: Context) -> dict:
    """List the SLA policies (Id, Name, Order, Targets). Returns {items, count}.

    Use it to turn a ticket's Sla.PolicyId (from get_ticket or list_tickets) into a policy name and its targets. Order is
    the position in the automatic matching, lower first. Each target has PriorityId (the ticket priority scale: 0 None,
    1 Urgent, 2 High, 3 Normal, 4 Low), TargetBusinessMinutes (null: no target, that priority gets no SLA tracking) and
    Is24x7. Policies are managed in the Gorelo app; this server cannot change them.
    Side effects: None (read-only)."""
    return list_result(await client_of(ctx).get_list(OP_SLA_POLICIES, tool="list_sla_policies"))
