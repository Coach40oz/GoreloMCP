"""Assets: RMM agents and custom assets, read only (toolset "core").

Tools: list_agents, get_agent, list_custom_assets.

Ops owned by this module:

    GET /v1/assets/agents
    GET /v1/assets/agents/{deviceId}
    GET /v1/assets/custom

Agents carry LocationId (renamed from ClientLocationId on 2026-08-21) and WarrantyStartDate /
WarrantyEndDate (replaced WarrantyExpiryDate the same day). The agent list rows are DeviceListItemResponse
records and the single agent is a DeviceResponse; the published spec gives both the same fields. The agent
list takes StatusIds; the custom asset list has no StatusIds, so list_custom_assets does not offer it
(Gorelo rejects names an op does not declare).

There are no delete tools: DELETE /v1/assets/agents/{deviceId} uninstalls the RMM agent from the host and
DELETE /v1/assets/custom/{customAssetId} is not allowed; both are in FORBIDDEN_OPS.
"""

from typing import Annotated, Any

from fastmcp import Context
from pydantic import Field

from tools._common import (
    StrictId,
    clamp_page_size,
    client_of,
    csv_ids,
    expect_object,
    gorelo_tool,
    guid,
    non_empty,
    paged_result,
    positive_ids,
    utc_iso,
)

LIST_AGENTS_OP = "GET /v1/assets/agents"
GET_AGENT_OP = "GET /v1/assets/agents/{deviceId}"
LIST_CUSTOM_OP = "GET /v1/assets/custom"

SINCE_HELP = "ISO 8601 with UTC offset, e.g. 2026-10-01T00:00:00Z."

# snake_case tool parameter -> Gorelo query name, used to name the parameter in Gorelo's errors.
AGENT_LIST_FIELD_MAP = {
    "status_ids": "StatusIds",
    "client_ids": "ClientIds",
    "query": "Query",
    "created_since": "CreatedSince",
    "created_before": "CreatedBefore",
    "updated_since": "UpdatedSince",
    "updated_before": "UpdatedBefore",
    "page_size": "PageSize",
    "cursor": "Cursor",
}
CUSTOM_LIST_FIELD_MAP = {name: query for name, query in AGENT_LIST_FIELD_MAP.items() if name != "status_ids"}
# The path placeholder of get_agent, so that a Gorelo error about the id names agent_id.
GET_AGENT_FIELD_MAP = {"agent_id": "deviceId"}


def _shared_filters(
    client_ids: list[int] | None,
    query: str | None,
    created_since: str | None,
    created_before: str | None,
    updated_since: str | None,
    updated_before: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The filters both asset lists share, validated: (Gorelo query params, filters echoed in the result)."""
    ids = csv_ids("client_ids", positive_ids("client_ids", client_ids))
    keyword = non_empty("query", query)
    created_after = utc_iso("created_since", created_since)
    created_until = utc_iso("created_before", created_before)
    updated_after = utc_iso("updated_since", updated_since)
    updated_until = utc_iso("updated_before", updated_before)
    params = {
        "ClientIds": ids,
        "Query": keyword,
        "CreatedSince": created_after,
        "CreatedBefore": created_until,
        "UpdatedSince": updated_after,
        "UpdatedBefore": updated_until,
    }
    shown = {
        "client_ids": client_ids,
        "query": keyword,
        "created_since": created_after,
        "created_before": created_until,
        "updated_since": updated_after,
        "updated_before": updated_until,
    }
    return params, shown


@gorelo_tool(toolset="core", kind="read", ops=[LIST_AGENTS_OP], field_map=AGENT_LIST_FIELD_MAP)
async def list_agents(
    ctx: Context,
    status_ids: Annotated[list[StrictId] | None, Field(description="Status.Id values from agent records.")] = None,
    client_ids: Annotated[list[StrictId] | None, Field(description="Only these clients' agents (list_clients).")] = None,
    query: Annotated[str | None, Field(description="Matches device name, display name and description.")] = None,
    created_since: Annotated[str | None, Field(description=f"Created at or after; {SINCE_HELP}")] = None,
    created_before: Annotated[str | None, Field(description="Created before.")] = None,
    updated_since: Annotated[str | None, Field(description="Updated at or after.")] = None,
    updated_before: Annotated[str | None, Field(description="Updated before.")] = None,
    page_size: Annotated[int, Field(description="Rows per page, 1 to 200.")] = 100,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous call.")] = None,
) -> dict:
    """List agents (managed devices) one page per call; returns {items, has_more, next_cursor, total_count, ...}.
    Rows carry LocationId and WarrantyStartDate/WarrantyEndDate (renamed 2026-08-21). Deleted and client-inactivated devices are never listed. This server cannot delete or uninstall agents. Paging: pass next_cursor as cursor, same filters, until has_more is false.
    """
    params, shown = _shared_filters(client_ids, query, created_since, created_before, updated_since, updated_before)
    params["StatusIds"] = csv_ids("status_ids", positive_ids("status_ids", status_ids))
    shown["status_ids"] = status_ids
    token = non_empty("cursor", cursor)
    size = clamp_page_size(page_size)
    page = await client_of(ctx).get_page(
        LIST_AGENTS_OP, query=params, page_size=size, cursor=token, tool="list_agents"
    )
    return paged_result(page, shown)


@gorelo_tool(toolset="core", kind="read", ops=[GET_AGENT_OP], field_map=GET_AGENT_FIELD_MAP)
async def get_agent(
    ctx: Context,
    agent_id: Annotated[str, Field(description="Agent UUID: the Id of a list_agents row.")],
) -> dict:
    """Get one agent (managed device) by UUID; returns Gorelo's full device record."""
    aid = guid("agent_id", agent_id)
    record = await client_of(ctx).get_one(GET_AGENT_OP, path_params={"deviceId": aid}, tool="get_agent")
    return expect_object(record, GET_AGENT_OP, tool="get_agent")


@gorelo_tool(toolset="core", kind="read", ops=[LIST_CUSTOM_OP], field_map=CUSTOM_LIST_FIELD_MAP)
async def list_custom_assets(
    ctx: Context,
    client_ids: Annotated[list[StrictId] | None, Field(description="Only these clients' custom assets (list_clients).")] = None,
    query: Annotated[str | None, Field(description="Matches asset name, description and serial number.")] = None,
    created_since: Annotated[str | None, Field(description=f"Created at or after; {SINCE_HELP}")] = None,
    created_before: Annotated[str | None, Field(description="Created before.")] = None,
    updated_since: Annotated[str | None, Field(description="Updated at or after.")] = None,
    updated_before: Annotated[str | None, Field(description="Updated before.")] = None,
    page_size: Annotated[int, Field(description="Rows per page, 1 to 200.")] = 100,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous call.")] = None,
) -> dict:
    """List custom (non-agent) assets one page per call; returns {items, has_more, next_cursor, total_count, ...}.
    No status filter. This server cannot delete custom assets. Paging: pass next_cursor as cursor, same filters, until has_more is false.
    """
    params, shown = _shared_filters(client_ids, query, created_since, created_before, updated_since, updated_before)
    token = non_empty("cursor", cursor)
    size = clamp_page_size(page_size)
    page = await client_of(ctx).get_page(
        LIST_CUSTOM_OP, query=params, page_size=size, cursor=token, tool="list_custom_assets"
    )
    return paged_result(page, shown)
