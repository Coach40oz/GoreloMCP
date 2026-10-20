"""Organization lookups: groups and users (toolset "core").

Tools: list_org_groups, list_org_users.

Ops owned by this module:

    GET /v1/organization/groups
    GET /v1/organization/users

GET /v1/organization/groups is NOT paged and takes no query parameters at all: the legacy tool sent
pageSize=200 and got a 400 from Gorelo, so list_org_groups makes a plain GET through get_list.
GET /v1/organization/users is a paged op, but the list is tiny (technicians), so list_org_users reads
every page itself (get_all) and returns the all-result shape with its completeness flags. Its StatusIds
filter (1 Pending, 2 Active, 3 Disabled; omitted, Gorelo lists Pending and Active) is checked locally
against that scale.
"""

from typing import Annotated, Any

from fastmcp import Context
from pydantic import Field

from tools._common import (
    MAX_ID,
    StrictId,
    all_result,
    client_of,
    csv_ids,
    gorelo_tool,
    list_result,
    positive_id,
    positive_ids,
)

GROUPS_OP = "GET /v1/organization/groups"
USERS_OP = "GET /v1/organization/users"

# Omitted, Gorelo lists Pending and Active; given, only the statuses named. Any other id is a 400.
USER_STATUS_LABELS = {1: "Pending", 2: "Active", 3: "Disabled"}
USER_STATUS_SCALE = ", ".join(f"{status} {label}" for status, label in USER_STATUS_LABELS.items())
USER_STATUSES = (
    f"{USER_STATUS_SCALE}. Omitted: Pending and Active; given, only those statuses. [1, 2, 3] lists every user, disabled included."
)


# --------------------------------------------------------------------------
# Private helpers (shared helpers live in tools/_common.py only)
# --------------------------------------------------------------------------


def _status(param: str, value: Any) -> int:
    """One user status: 1 Pending, 2 Active or 3 Disabled.

    Every whole number outside the scale is refused here, before any HTTP call, and answered with the scale and quoted
    back, 0 and the negative numbers included (positive_id's "expected a positive whole number such as 123" would
    suggest that any id will do). Whatever else is not an id (a boolean, a decimal, text, a list, a number beyond
    int64) keeps the message of positive_id."""
    if isinstance(value, int) and not isinstance(value, bool) and -MAX_ID <= value < 1:
        status = int(value)
    else:
        status = positive_id(param, value)
    if status not in USER_STATUS_LABELS:
        raise ValueError(f"{param}: expected {USER_STATUS_SCALE}, got {status}")
    return status


def _statuses(param: str, values: Any) -> list[int] | None:
    """The status_ids filter: every item on the user scale, a bad one named by its index.

    None, an empty list and a value that is not a list keep the messages of positive_ids."""
    if isinstance(values, (list, tuple)) and values:
        return [_status(f"{param}[{index}]", item) for index, item in enumerate(values)]
    return positive_ids(param, values)


@gorelo_tool(toolset="core", kind="read", ops=[GROUPS_OP])
async def list_org_groups(ctx: Context) -> dict:
    """List every technician group (not paged); returns {items, count}. A row's Id is the group id the ticket tools take."""
    items = await client_of(ctx).get_list(GROUPS_OP, tool="list_org_groups")
    return list_result(items)


@gorelo_tool(toolset="core", kind="read", ops=[USERS_OP], field_map={"status_ids": "StatusIds"})
async def list_org_users(
    ctx: Context,
    status_ids: Annotated[list[StrictId] | None, Field(description=USER_STATUSES)] = None,
) -> dict:
    """List users (technicians); without status_ids Gorelo lists Pending and Active. Pass status_ids=[1, 2, 3] for every user, disabled ones included; all pages are read for you. Returns {items, total_count, truncated, ...}: truncated is false when the list is complete.
    A row's Id is the technician id other tools take (e.g. lead_assignee_id).
    """
    filters = {"status_ids": _statuses("status_ids", status_ids)}
    result = await client_of(ctx).get_all(
        USERS_OP,
        query={"StatusIds": csv_ids("status_ids", filters["status_ids"])},
        page_size=200,
        tool="list_org_users",
    )
    return all_result(result, filters)
