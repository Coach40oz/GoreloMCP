"""Contracts, read only (toolset "billing").

Two read tools, both in toolset "billing":

    list_contracts   read   GET /v1/contracts (paged; each contract carries its service lines)
    get_contract     read   GET /v1/contracts/{contractId} (service lines with labor detail and line items)

Ops owned by this module (each must exist in spec/spec_index.json, none may be in FORBIDDEN_OPS; keep
the list in step with the ops=[...] of the declarations below):

    GET /v1/contracts
    GET /v1/contracts/{contractId}

There is no create, update or delete tool. DELETE /v1/contracts/{contractId} is in FORBIDDEN_OPS (it
destroys billing configuration), so it can never be declared or called.

A service line's Id is the identifier time entries call ServiceLineId: the list tool's docstring says so
because create_time_entry and update_time_entry take it as service_line_id.

Integer ids are StrictId parameters (JSON true, "5" or 5.0 is refused by the schema) and are then
range-checked by positive_id / positive_ids; the single-record answer goes through expect_object.
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
    non_empty,
    paged_result,
    positive_id,
    positive_ids,
)

LIST_OP = "GET /v1/contracts"
GET_OP = "GET /v1/contracts/{contractId}"

# snake_case tool parameter -> Gorelo query name. The same map turns a Gorelo PropertyName back into the
# parameter to fix in error messages.
LIST_FIELDS = {"client_ids": "ClientIds", "page_size": "PageSize", "cursor": "Cursor"}


@gorelo_tool(toolset="billing", kind="read", ops=[LIST_OP], field_map=LIST_FIELDS)
async def list_contracts(
    ctx: Context,
    client_ids: Annotated[list[StrictId] | None, Field(description="Client ids (list_clients).")] = None,
    page_size: Annotated[int, Field(description="Rows per page, 1-200.")] = 50,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous page.")] = None,
) -> dict:
    """List billing contracts with their service lines, one page at a time.

    A service line's Id is the service_line_id of create_time_entry and update_time_entry. Expired and archived contracts are listed too: keep Status Active for live ones. get_contract has the line items and labor detail.
    Paging: pass next_cursor back as cursor with the SAME filters until has_more is false.
    """
    filters: dict[str, Any] = {"client_ids": positive_ids("client_ids", client_ids)}
    non_empty("cursor", cursor)
    size = clamp_page_size(page_size)
    params = {"ClientIds": csv_ids("client_ids", filters["client_ids"])}
    page = await client_of(ctx).get_page(LIST_OP, query=params, page_size=size, cursor=cursor, tool="list_contracts")
    return paged_result(page, filters)


@gorelo_tool(toolset="billing", kind="read", ops=[GET_OP], field_map={"contract_id": "contractId"})
async def get_contract(
    ctx: Context,
    contract_id: Annotated[StrictId, Field(description="Contract id (list_contracts).")],
) -> dict:
    """Get one contract in full: client, invoice schedule, contacts and every service line with labor detail and line items.

    A service line's Id is the service_line_id time entries use. An unknown id is a 404. No tool changes or deletes contracts.
    """
    number = positive_id("contract_id", contract_id)
    data = await client_of(ctx).get_one(GET_OP, path_params={"contractId": number}, tool="get_contract")
    return expect_object(data, GET_OP, tool="get_contract")
