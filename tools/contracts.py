"""Contracts, read only (toolset "billing").

Two read tools, both in toolset "billing":

    list_contracts   read   GET /v1/contracts (paged; each contract carries its service lines; filters client_ids and status_ids)
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

status_ids is checked against 1 Active, 2 Expired, 3 Archived before any HTTP call (the spec says any other id
is a 400); omitted, Gorelo returns Active and Expired.
"""

from typing import Annotated, Any

from fastmcp import Context
from pydantic import Field

from tools._common import (
    MAX_ID,
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
LIST_FIELDS = {"client_ids": "ClientIds", "status_ids": "StatusIds", "page_size": "PageSize", "cursor": "Cursor"}

# The contract status scale of GET /v1/contracts (StatusIds). Gorelo derives the status (a contract has none stored);
# omitted, it returns Active and Expired, so archived contracts are left out.
CONTRACT_STATUS_LABELS = {1: "Active", 2: "Expired", 3: "Archived"}
CONTRACT_STATUS_SCALE = ", ".join(f"{status} {label}" for status, label in CONTRACT_STATUS_LABELS.items())
CONTRACT_STATUSES = (
    f"{CONTRACT_STATUS_SCALE}. Omitted: Active and Expired (archived left out). [1] gives live contracts only."
)


# --------------------------------------------------------------------------
# Private helpers (shared helpers live in tools/_common.py only)
# --------------------------------------------------------------------------


def _status(param: str, value: Any) -> int:
    """One contract status: 1 Active, 2 Expired or 3 Archived.

    Every whole number outside the scale is refused here, before any HTTP call, and answered with the scale and quoted
    back, 0 and the negative numbers included (positive_id's "expected a positive whole number such as 123" would
    suggest that any id will do). Whatever else is not an id (a boolean, a decimal, text, a list, a number beyond
    int64) keeps the message of positive_id."""
    if isinstance(value, int) and not isinstance(value, bool) and -MAX_ID <= value < 1:
        status = int(value)
    else:
        status = positive_id(param, value)
    if status not in CONTRACT_STATUS_LABELS:
        raise ValueError(f"{param}: expected {CONTRACT_STATUS_SCALE}, got {status}")
    return status


def _statuses(param: str, values: Any) -> list[int] | None:
    """The status_ids filter: every item on the contract scale, a bad one named by its index.

    None, an empty list and a value that is not a list keep the messages of positive_ids."""
    if isinstance(values, (list, tuple)) and values:
        return [_status(f"{param}[{index}]", item) for index, item in enumerate(values)]
    return positive_ids(param, values)


@gorelo_tool(toolset="billing", kind="read", ops=[LIST_OP], field_map=LIST_FIELDS)
async def list_contracts(
    ctx: Context,
    client_ids: Annotated[list[StrictId] | None, Field(description="Client ids (list_clients).")] = None,
    status_ids: Annotated[list[StrictId] | None, Field(description=CONTRACT_STATUSES)] = None,
    page_size: Annotated[int, Field(description="Rows per page, 1-200.")] = 50,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous page.")] = None,
) -> dict:
    """List billing contracts with their service lines, one page at a time.

    Without status_ids, Active and Expired contracts are listed and Archived ones are left out: pass status_ids=[1] for live contracts only, or include 3 to see archived ones. A service line's Id is the service_line_id of create_time_entry and update_time_entry. get_contract has the line items and labor detail.
    Paging: pass next_cursor back as cursor with the SAME filters until has_more is false.
    """
    filters: dict[str, Any] = {
        "client_ids": positive_ids("client_ids", client_ids),
        "status_ids": _statuses("status_ids", status_ids),
    }
    non_empty("cursor", cursor)
    size = clamp_page_size(page_size)
    params = {
        "ClientIds": csv_ids("client_ids", filters["client_ids"]),
        "StatusIds": csv_ids("status_ids", filters["status_ids"]),
    }
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
