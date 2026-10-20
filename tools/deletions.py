"""Deletions: list the records deleted in Gorelo (toolset "core").

Tools:

    list_deletions  read  GET /v1/deletions

Ops owned by this module:

    GET /v1/deletions

GET /v1/deletions (new in contract a7954b24844b, 2026-10-17) is a paged feed of deleted records, so that a copy of
Gorelo data kept in sync through this API can remove them. It is sorted by DeletedOn ascending, then type id, then id,
and it uses cursor paging. The published text, in short:

* Each record appears once, with its latest deletion; this is not a history. A record restored in the Gorelo app
  leaves the feed and shows in its normal list again.
* DeletedOn is the deleted record's last update, so it can move later if the deleted record is edited. A record with
  no recorded last update has no deletion time and is not listed.
* A contact is listed when it was deleted and also when its client was deleted (GET /v1/contacts stops listing a
  deleted client's contacts); a task is listed when it was deleted and also when its project was deleted. Such a row
  carries the later of the two last updates.
* A voided invoice is not deleted: GET /v1/invoices still returns it.
* TypeIds (required): 1 Client, 2 Contact, 3 Contract, 4 Ticket, 5 Item, 6 Invoice, 7 Custom asset, 8 Project,
  9 Task; up to 10 values, any other value is a 400. Each type needs read access on its own scope (clients Clients,
  contacts Contacts, contracts, items and invoices Billing, tickets Tickets, custom assets Assets, projects and tasks
  Project). Asking for a type the key cannot read is a 403 with code 070208 on TypeIds.
* Since: an ISO-8601 UTC timestamp, keeps only deletions at or after it. PageSize: 1 to 200 (default 50), out of
  range is rejected, not clamped.
* A row (DeletionResponse) is {Type: {Id, Name}, Id, DeletedOn}. Id is text: a number for clients, contacts and
  contracts, a GUID for tickets, items, invoices, custom assets, projects and tasks.

The tool checks type_ids and since itself and clamps page_size to 1 to 200 like every other paged tool (the client
does it, so Gorelo never sees an out-of-range size). It returns Gorelo's rows unchanged inside the house paged shape
and does not enrich them. A 403 with code 070208 reaches the model as "type_ids: <Gorelo's message>".
"""

from typing import Annotated, Any

from fastmcp import Context
from pydantic import Field

from tools._common import (
    StrictId,
    clamp_page_size,
    client_of,
    csv_ids,
    describe_value,
    gorelo_tool,
    non_empty,
    paged_result,
    utc_iso,
)

LIST_OP = "GET /v1/deletions"

# The record types of TypeIds, as the spec text of that parameter lists them.
DELETION_TYPES = {
    1: "Client",
    2: "Contact",
    3: "Contract",
    4: "Ticket",
    5: "Item",
    6: "Invoice",
    7: "Custom asset",
    8: "Project",
    9: "Task",
}
TYPE_HELP = ", ".join(f"{key} {name}" for key, name in DELETION_TYPES.items())

# {tool param: Gorelo query name} for every query name of GET /v1/deletions. The map also turns the PropertyName of a
# Gorelo 400 or of the 403 with code 070208 back into the snake_case param in error messages.
LIST_FIELD_MAP = {"type_ids": "TypeIds", "since": "Since", "page_size": "PageSize", "cursor": "Cursor"}


def _type_filter(values: Any) -> list[int]:
    """type_ids on the published scale (1 to 9), distinct, in the caller's order. Required: an id off the scale is
    a 400 and none at all is a 400, so both are errors naming the scale before anything is sent."""
    if values is None:
        raise ValueError(f"type_ids: required; give a list such as [1, 4] (types: {TYPE_HELP})")
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise ValueError(
            f"type_ids: expected a list of type ids such as [1, 4], got {describe_value(values)}; types: {TYPE_HELP}"
        )
    if not values:
        raise ValueError(f"type_ids: expected at least one type id, got an empty list; types: {TYPE_HELP}")
    for position, item in enumerate(values):
        if isinstance(item, bool) or not isinstance(item, int) or item not in DELETION_TYPES:
            shown = item if isinstance(item, int) and not isinstance(item, bool) else describe_value(item)
            raise ValueError(f"type_ids[{position}]: {shown} is not a record type; valid ids: {TYPE_HELP}")
    return list(dict.fromkeys(values))


@gorelo_tool(toolset="core", kind="read", ops=[LIST_OP], field_map=LIST_FIELD_MAP)
async def list_deletions(
    ctx: Context,
    type_ids: Annotated[
        list[StrictId],
        Field(
            description=(
                "Required. Record types: 1 Client, 2 Contact, 3 Contract, 4 Ticket, 5 Item, 6 Invoice, 7 Custom asset, "
                "8 Project, 9 Task. A type the key cannot read fails the whole call with a 403."
            )
        ),
    ],
    since: Annotated[
        str | None,
        Field(
            description=(
                "At or after this instant, UTC offset required (for example 2026-10-01T00:00:00Z). "
                "Omit for no time filter; a record with no recorded last update is never listed."
            )
        ),
    ] = None,
    page_size: Annotated[int, Field(description="1-200, clamped.")] = 50,
    cursor: Annotated[
        str | None,
        Field(description="next_cursor from the previous call; same type_ids and since; repeat until has_more is false."),
    ] = None,
) -> dict:
    """List records deleted in Gorelo, oldest first, one page per call; returns {items, has_more, next_cursor, ...}.
    Not a history: each record appears once, with its latest deletion; a record restored in the Gorelo app leaves the list. Id is text: a number for clients, contacts and contracts, a GUID for the others. DeletedOn is the record's last update, so it can move later if the record is edited; since is inclusive. A contact is also listed when its client was deleted, a task when its project was. A voided invoice is not deleted.
    Read scope per type: clients Clients, contacts Contacts, contracts, items and invoices Billing, tickets Tickets, custom assets Assets, projects and tasks Project. A type the key cannot read fails the call with a 403, code 070208 on type_ids.
    Side effects: None (read-only).
    Paging: pass next_cursor back as cursor with the SAME filters until has_more is false.
    """
    types = _type_filter(type_ids)
    moment = utc_iso("since", since)
    filters = {"type_ids": types, "since": moment}
    wire = {"TypeIds": csv_ids("type_ids", types), "Since": moment}
    page = await client_of(ctx).get_page(
        LIST_OP,
        query=wire,
        page_size=clamp_page_size(page_size),
        cursor=non_empty("cursor", cursor),
        tool="list_deletions",
    )
    return paged_result(page, filters)
