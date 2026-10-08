"""Organization lookups: groups and users (toolset "core").

Tools: list_org_groups, list_org_users.

Ops owned by this module:

    GET /v1/organization/groups
    GET /v1/organization/users

GET /v1/organization/groups is NOT paged and takes no query parameters at all: the legacy tool sent
pageSize=200 and got a 400 from Gorelo, so list_org_groups makes a plain GET through get_list.
GET /v1/organization/users is a paged op, but the list is tiny (technicians), so list_org_users reads
every page itself (get_all) and returns the all-result shape with its completeness flags.
"""

from fastmcp import Context

from tools._common import all_result, client_of, gorelo_tool, list_result

GROUPS_OP = "GET /v1/organization/groups"
USERS_OP = "GET /v1/organization/users"


@gorelo_tool(toolset="core", kind="read", ops=[GROUPS_OP])
async def list_org_groups(ctx: Context) -> dict:
    """List every technician group (not paged); returns {items, count}. A row's Id is the group id the ticket tools take."""
    items = await client_of(ctx).get_list(GROUPS_OP, tool="list_org_groups")
    return list_result(items)


@gorelo_tool(toolset="core", kind="read", ops=[USERS_OP])
async def list_org_users(ctx: Context) -> dict:
    """List every user (technician); all pages are read for you. Returns {items, total_count, truncated, ...}: truncated is false when the list is complete.
    A row's Id is the technician id other tools take (e.g. lead_assignee_id).
    """
    result = await client_of(ctx).get_all(USERS_OP, page_size=200, tool="list_org_users")
    return all_result(result, None)
