"""Organization lookups: roles, groups and users (toolset "core").

Tools: list_org_roles, list_org_groups, get_org_group, list_org_users, get_org_user.

Ops owned by this module:

    GET /v1/organization/roles
    GET /v1/organization/groups
    GET /v1/organization/groups/{groupId}
    GET /v1/organization/users
    GET /v1/organization/users/{userId}

GET /v1/organization/roles and GET /v1/organization/groups are NOT paged and take no query parameters at all (the legacy
list_org_groups sent pageSize=200 and got a 400 from Gorelo), so they make a plain GET through get_list.
GET /v1/organization/users is a paged op, but the list is tiny (technicians), so list_org_users reads every page itself
(get_all) and returns the all-result shape with its completeness flags. Its StatusIds filter (1 Pending, 2 Active,
3 Disabled; omitted, Gorelo lists Pending and Active) is checked locally against that scale. Its GroupIds (up to 100),
Query (at most 200 characters) and UpdatedSince (a UTC instant) filters are checked here too.
The single-record reads answer one row each: a user carries RoleId and GroupIds (get_org_user), a group the same fields
as its list row (get_org_group). An id that is not of this account is a 404.
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
    describe_value,
    expect_object,
    gorelo_tool,
    list_result,
    non_empty,
    positive_id,
    positive_ids,
    utc_iso,
)

ROLES_OP = "GET /v1/organization/roles"
GROUPS_OP = "GET /v1/organization/groups"
GROUP_OP = "GET /v1/organization/groups/{groupId}"
USERS_OP = "GET /v1/organization/users"
USER_OP = "GET /v1/organization/users/{userId}"

# Omitted, Gorelo lists Pending and Active; given, only the statuses named. Any other id is a 400.
USER_STATUS_LABELS = {1: "Pending", 2: "Active", 3: "Disabled"}
USER_STATUS_SCALE = ", ".join(f"{status} {label}" for status, label in USER_STATUS_LABELS.items())
USER_STATUSES = (
    f"{USER_STATUS_SCALE}. Omitted: Pending and Active; given, only those statuses. [1, 2, 3] lists every user, disabled included."
)
QUERY_MAX = 200

# {tool param: Gorelo name}. The map also turns a Gorelo PropertyName back into the parameter to fix in error messages.
LIST_USERS_MAP = {
    "status_ids": "StatusIds",
    "group_ids": "GroupIds",
    "query": "Query",
    "updated_since": "UpdatedSince",
}
USER_MAP = {"user_id": "userId"}
GROUP_MAP = {"group_id": "groupId"}


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


def _query(param: str, value: Any) -> str | None:
    """Text of at most 200 characters that is not blank. None stays None (not given)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{param}: expected text, got {describe_value(value)}")
    non_empty(param, value)
    if len(value) > QUERY_MAX:
        raise ValueError(f"{param}: at most {QUERY_MAX} characters, got {len(value)}")
    return value


# --------------------------------------------------------------------------
# Roles, groups
# --------------------------------------------------------------------------


@gorelo_tool(toolset="core", kind="read", ops=[ROLES_OP])
async def list_org_roles(ctx: Context) -> dict:
    """List the service provider's user roles, not paged; returns {items, count}. A user's RoleId (get_org_user) is one row's Id.
    Resolve ids first: none.
    Side effects: None (read-only).
    """
    items = await client_of(ctx).get_list(ROLES_OP, tool="list_org_roles")
    return list_result(items)


@gorelo_tool(toolset="core", kind="read", ops=[GROUPS_OP])
async def list_org_groups(ctx: Context) -> dict:
    """List every technician group (not paged); returns {items, count}. A row's Id is the group id the ticket tools take."""
    items = await client_of(ctx).get_list(GROUPS_OP, tool="list_org_groups")
    return list_result(items)


@gorelo_tool(toolset="core", kind="read", ops=[GROUP_OP], field_map=GROUP_MAP)
async def get_org_group(
    ctx: Context,
    group_id: Annotated[StrictId, Field(description="Group id from list_org_groups (its Id).")],
) -> dict:
    """Get one technician group by id, in the same shape as a list_org_groups row. Its members: list_org_users with group_ids.
    Resolve ids first: group_id -> list_org_groups.
    Side effects: None (read-only).
    """
    group = positive_id("group_id", group_id)
    data = await client_of(ctx).get_one(GROUP_OP, path_params={"groupId": group}, tool="get_org_group")
    return expect_object(data, GROUP_OP, tool="get_org_group")


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------


@gorelo_tool(toolset="core", kind="read", ops=[USERS_OP], field_map=LIST_USERS_MAP)
async def list_org_users(
    ctx: Context,
    status_ids: Annotated[list[StrictId] | None, Field(description=USER_STATUSES)] = None,
    group_ids: Annotated[
        list[StrictId] | None,
        Field(description="Group ids (list_org_groups): users in any of them. Up to 100."),
    ] = None,
    query: Annotated[
        str | None,
        Field(description="First name, last name or email keyword. At most 200 characters."),
    ] = None,
    updated_since: Annotated[
        str | None,
        Field(description="Updated at or after this instant, with a UTC offset."),
    ] = None,
) -> dict:
    """List users (technicians); without status_ids Gorelo lists Pending and Active. Pass status_ids=[1, 2, 3] for every user, disabled ones included; all pages are read for you. Returns {items, total_count, truncated, ...}: truncated is false when the list is complete. A row's Id is the technician id other tools take (e.g. lead_assignee_id). Rows lack RoleId and GroupIds: get_org_user has them."""
    filters = {
        "status_ids": _statuses("status_ids", status_ids),
        "group_ids": positive_ids("group_ids", group_ids),
        "query": _query("query", query),
        "updated_since": utc_iso("updated_since", updated_since),
    }
    wire = {
        "StatusIds": csv_ids("status_ids", filters["status_ids"]),
        "GroupIds": csv_ids("group_ids", filters["group_ids"]),
        "Query": filters["query"],
        "UpdatedSince": filters["updated_since"],
    }
    result = await client_of(ctx).get_all(USERS_OP, query=wire, page_size=200, tool="list_org_users")
    return all_result(result, filters)


@gorelo_tool(toolset="core", kind="read", ops=[USER_OP], field_map=USER_MAP)
async def get_org_user(
    ctx: Context,
    user_id: Annotated[StrictId, Field(description="User id from list_org_users (the technician id).")],
) -> dict:
    """Get one user (technician) by id, a disabled one included: RoleId (its name: list_org_roles), GroupIds (names: list_org_groups), TimeZone and the dates.
    Resolve ids first: user_id -> list_org_users.
    Side effects: None (read-only).
    """
    user = positive_id("user_id", user_id)
    data = await client_of(ctx).get_one(USER_OP, path_params={"userId": user}, tool="get_org_user")
    return expect_object(data, USER_OP, tool="get_org_user")
