"""Time entries and their lookups: billing roles, work types (toolset "time").

Seven tools, every one in toolset "time":

    list_time_entries    read         GET /v1/time-entries (paged)
    get_time_entry       read         GET /v1/time-entries/{timeEntryId}
    create_time_entry    write        POST /v1/time-entries, then GET /v1/time-entries/{timeEntryId} to return the record
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

Rules that shape the code (the evidence is in docs/API-OBSERVED-BEHAVIOR.md and in spec/spec_index.json):

* BillableStatusId is 1 Billable, 2 No charge, 3 Non-billable (match on Id, never on Name). Durations are
  decimal hours. Datetimes are UTC instants and always go through utc_iso (an offset is required).
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
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any

from fastmcp import Context
from pydantic import Field

from gorelo_client import GoreloAPIError
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

LIST_OP = "GET /v1/time-entries"
GET_OP = "GET /v1/time-entries/{timeEntryId}"
CREATE_OP = "POST /v1/time-entries"
UPDATE_OP = "PATCH /v1/time-entries/{timeEntryId}"
DELETE_OP = "DELETE /v1/time-entries/{timeEntryId}"
ROLES_OP = "GET /v1/billing-roles"
WORK_TYPES_OP = "GET /v1/work-types"

# Gorelo's BillableStatus ids. There is no lookup endpoint and no enum in the spec: this table is the contract.
BILLABLE_STATUSES = {1: "Billable", 2: "No charge", 3: "Non-billable"}

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

# snake_case tool parameter -> CreateTimeEntryCommand field. Every spec field is here and nothing else.
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


@gorelo_tool(toolset="time", kind="write", ops=[CREATE_OP, GET_OP], field_map=CREATE_FIELDS)
async def create_time_entry(
    ctx: Context,
    user_id: Annotated[StrictId, Field(description="User whose time it is (list_org_users).")],
    ticket_id: Annotated[
        str | None, Field(description="Ticket GUID (list_tickets), not the display number.")
    ] = None,
    task_id: Annotated[
        str | None, Field(description="Task GUID (list_project_tasks, projects toolset).")
    ] = None,
    started_on: Annotated[
        str | None, Field(description="Start (ISO 8601 with UTC offset).")
    ] = None,
    ended_on: Annotated[str | None, Field(description="End, later than started_on.")] = None,
    actual_hours: Annotated[
        float | None, Field(description="Decimal hours worked, above 0. Do not pre-round.")
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
    comment: Annotated[str | None, Field(description="Free-text note.")] = None,
    distance: Annotated[float | None, Field(description="Distance travelled, 0 or more.")] = None,
    attachments: Annotated[
        list[dict[str, str]] | None,
        Field(
            description='[{"name": "a.pdf", "url": "https://..."}]. Pass only the name and url from upload_attachment, '
            "unchanged."
        ),
    ] = None,
) -> dict:
    """Log time against ONE ticket or ONE task and return the created entry.

    Give exactly one of ticket_id and task_id, and at least two of started_on, ended_on and actual_hours (the third is derived; all three must agree). The billed duration (AdjustedHours) is rounded UP by the work type's minimum and increment.
    Side effects: creates a real time entry billed to the ticket's client (task: its project's client) and priced at once; it can draw down contract hours. Logging twice records the time twice: after a "did not confirm" error check list_time_entries before repeating. A result of {"Id", "warning"} means the entry exists and only the re-read failed: do not repeat the write.
    """
    ticket = guid("ticket_id", ticket_id) if ticket_id is not None else None
    task = guid("task_id", task_id) if task_id is not None else None
    if (ticket is None) == (task is None):
        got = "both" if ticket is not None else "neither"
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
    values = {
        "ticket_id": ticket,
        "task_id": task,
        "user_id": user,
        "started_on": started,
        "ended_on": ended,
        "actual_hours": hours,
        "billable_status_id": _billable_status("billable_status_id", billable_status_id),
        "billing_role_id": _optional_id("billing_role_id", billing_role_id),
        "work_type_id": _optional_id("work_type_id", work_type_id),
        "service_line_id": _optional_id("service_line_id", service_line_id),
        "comment": comment,
        "distance": _distance("distance", distance),
        "attachments": _attachments("attachments", attachments),
    }
    body = build_body(
        values,
        CREATE_FIELDS,
        clear=["service_line_id"] if no_service_line else [],
        clear_values=_CLEAR_VALUES,
    )
    written = await client_of(ctx).post(CREATE_OP, json_body=body, tool="create_time_entry")
    new_id = created_id(written, CREATE_OP, tool="create_time_entry")
    return await reread_after_write(
        ctx, GET_OP, path_params={"timeEntryId": new_id}, tool="create_time_entry", written_id=new_id
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
    actual_hours: Annotated[float | None, Field(description="New duration in decimal hours, above 0.")] = None,
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
    distance: Annotated[float | None, Field(description="Distance travelled, 0 or more.")] = None,
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
