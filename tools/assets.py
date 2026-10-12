"""Assets: RMM agents (read only) and custom assets (read, create, update) (toolset "core").

Tools: list_agents, get_agent, list_custom_assets, list_custom_asset_hardware_types, create_custom_asset,
update_custom_asset.

Ops owned by this module:

    GET /v1/assets/agents
    GET /v1/assets/agents/{deviceId}
    GET /v1/assets/custom
    GET /v1/assets/custom/hardware-types
    POST /v1/assets/custom
    PATCH /v1/assets/custom/{customAssetId}

Gorelo has no GET for a single custom asset, so create_custom_asset and update_custom_asset read the record back
by scanning GET /v1/assets/custom for the asset's client and picking the row with the written Id. The read-back
never raises after a confirmed write: when the row is not found or the scan fails, the tool returns
{"Id": ..., "warning": ...}.

Agents carry LocationId (renamed from ClientLocationId on 2026-08-21) and WarrantyStartDate /
WarrantyEndDate (replaced WarrantyExpiryDate the same day). The agent list rows are DeviceListItemResponse
records and the single agent is a DeviceResponse; the published spec gives both the same fields. The agent
list takes StatusIds; the custom asset list has no StatusIds, so list_custom_assets does not offer it
(Gorelo rejects names an op does not declare).

There are no delete tools: DELETE /v1/assets/agents/{deviceId} uninstalls the RMM agent from the host and
DELETE /v1/assets/custom/{customAssetId} is not allowed; both are in FORBIDDEN_OPS.
"""

import ipaddress
import re
from datetime import date
from typing import Annotated, Any, Literal

from fastmcp import Context
from pydantic import Field

from gorelo_client import GoreloAPIError
from spec import SpecViolation
from tools._common import (
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
    list_result,
    non_empty,
    paged_result,
    positive_id,
    positive_ids,
    utc_iso,
)

LIST_AGENTS_OP = "GET /v1/assets/agents"
GET_AGENT_OP = "GET /v1/assets/agents/{deviceId}"
LIST_CUSTOM_OP = "GET /v1/assets/custom"
HARDWARE_TYPES_OP = "GET /v1/assets/custom/hardware-types"
CREATE_CUSTOM_OP = "POST /v1/assets/custom"
UPDATE_CUSTOM_OP = "PATCH /v1/assets/custom/{customAssetId}"

# The read-back scans one client's custom assets for the written Id (there is no GET for a single custom asset).
READBACK_PAGE_SIZE = 200
READBACK_MAX_ITEMS = 1000
NAME_MAX = 300
DESCRIPTION_MAX = 1000
SERIAL_MAX = 250

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
# snake_case tool parameter -> body field of CreateCustomAssetCommand / UpdateCustomAssetCommand.
CUSTOM_BODY_MAP = {
    "name": "Name",
    "client_id": "ClientId",
    "location_id": "LocationId",
    "hardware_type_id": "HardwareTypeId",
    "description": "Description",
    "contact_id": "ContactId",
    "warranty_end_date": "WarrantyEndDate",
    "ip_address": "IpAddress",
    "serial_number": "SerialNumber",
}
UPDATE_CUSTOM_MAP = {**CUSTOM_BODY_MAP, "custom_asset_id": "customAssetId"}
# Fields that Gorelo clears with a JSON null (name, location_id and hardware_type_id cannot be cleared).
CUSTOM_CLEARABLE = ("description", "contact_id", "warranty_end_date", "ip_address", "serial_number")
CUSTOM_CLEAR_VALUES = {param: None for param in CUSTOM_CLEARABLE}
CustomClearable = Literal["description", "contact_id", "warranty_end_date", "ip_address", "serial_number"]
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
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
    Rows carry ContactId (null when unassigned), HardwareType (name, null when the type no longer exists), HardwareTypeId (the value create_custom_asset and update_custom_asset take as hardware_type_id; list_custom_asset_hardware_types lists them) and WarrantyEndDate (a calendar date, YYYY-MM-DD). There is no way to read one custom asset by id: find it here with client_ids and query. No status filter. Create and update exist (create_custom_asset, update_custom_asset); this server still cannot delete custom assets. Paging: pass next_cursor as cursor, same filters, until has_more is false.
    """
    params, shown = _shared_filters(client_ids, query, created_since, created_before, updated_since, updated_before)
    token = non_empty("cursor", cursor)
    size = clamp_page_size(page_size)
    page = await client_of(ctx).get_page(
        LIST_CUSTOM_OP, query=params, page_size=size, cursor=token, tool="list_custom_assets"
    )
    return paged_result(page, shown)


# --------------------------------------------------------------------------
# Custom assets: hardware types, create, update
# --------------------------------------------------------------------------


@gorelo_tool(toolset="core", kind="read", ops=[HARDWARE_TYPES_OP])
async def list_custom_asset_hardware_types(ctx: Context) -> dict:
    """List the hardware types a custom asset can be given; returns {items, count}, each item {Id, Name, CreatedOn, UpdatedOn}. Not paged.
    These Ids are the hardware_type_id values create_custom_asset and update_custom_asset accept. Call it first when you need a hardware type.
    Side effects: None (read-only).
    """
    rows = await client_of(ctx).get_list(HARDWARE_TYPES_OP, tool="list_custom_asset_hardware_types")
    return list_result(rows)


def _bounded_text(param: str, value: Any, limit: int) -> Any:
    """Text of 1 to `limit` characters that is not blank; None passes (not given)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{param}: expected text, got {describe_value(value)}")
    if not value.strip():
        raise ValueError(f"{param}: must not be empty or whitespace only")
    if len(value) > limit:
        raise ValueError(f"{param}: at most {limit} characters, got {len(value)}")
    return value


def _calendar_date(param: str, value: Any) -> Any:
    """A real calendar date written YYYY-MM-DD, sent as given; None passes."""
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError(f"{param}: expected a calendar date written YYYY-MM-DD, such as 2027-03-31")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{param}: {value} is not a real calendar date; write it YYYY-MM-DD, such as 2027-03-31") from None
    return value


def _ipv4(param: str, value: Any) -> Any:
    """One IPv4 address such as 10.0.0.20, sent as given. IPv6 is refused; None passes."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{param}: expected an IPv4 address such as 10.0.0.20, got {describe_value(value)}")
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        raise ValueError(
            f"{param}: expected an IPv4 address such as 10.0.0.20 (IPv6 is not accepted)"
        ) from None
    return value


def _optional_id(param: str, value: Any) -> Any:
    return None if value is None else positive_id(param, value)


def _custom_values(
    *,
    name: Any,
    client_id: Any,
    location_id: Any,
    hardware_type_id: Any,
    description: Any,
    contact_id: Any,
    warranty_end_date: Any,
    ip_address: Any,
    serial_number: Any,
) -> dict[str, Any]:
    """The validated body values of create and update (None means not given)."""
    return {
        "name": _bounded_text("name", name, NAME_MAX),
        "client_id": _optional_id("client_id", client_id),
        "location_id": _optional_id("location_id", location_id),
        "hardware_type_id": _optional_id("hardware_type_id", hardware_type_id),
        "description": _bounded_text("description", description, DESCRIPTION_MAX),
        "contact_id": _optional_id("contact_id", contact_id),
        "warranty_end_date": _calendar_date("warranty_end_date", warranty_end_date),
        "ip_address": _ipv4("ip_address", ip_address),
        "serial_number": _bounded_text("serial_number", serial_number, SERIAL_MAX),
    }


async def _read_back_custom(ctx: Context, *, tool: str, client_id: int, asset_id: Any) -> dict[str, Any]:
    """The custom asset row with `asset_id` among the client's assets, or {"Id", "warning"}. Never raises.

    Called only after Gorelo confirmed the write, so no failure of the read may look like a failed write."""

    def warn(reason: str) -> dict[str, Any]:
        return {
            "Id": asset_id,
            "warning": f"the write succeeded; re-reading it failed: {reason.rstrip('. ')}. "
            "Do not repeat the write; find it with list_custom_assets (client_ids) later.",
        }

    try:
        scan = await client_of(ctx).get_all(
            LIST_CUSTOM_OP,
            query={"ClientIds": str(client_id)},
            page_size=READBACK_PAGE_SIZE,
            max_items=READBACK_MAX_ITEMS,
            tool=tool,
        )
    except GoreloAPIError as err:
        where = f"HTTP {err.status}" if err.status is not None else err.kind
        return warn(f"{LIST_CUSTOM_OP} failed ({where})")
    except SpecViolation as err:
        return warn(str(err))
    wanted = str(asset_id).lower()
    for row in scan.items:
        if isinstance(row, dict) and str(row.get("Id", "")).lower() == wanted:
            return row
    if scan.complete:
        return warn(f"the asset was not among the {len(scan.items)} custom assets {LIST_CUSTOM_OP} listed for client_id {client_id}")
    return warn(f"the asset was not among the first {len(scan.items)} custom assets of client_id {client_id}, and the scan stopped there")


@gorelo_tool(
    toolset="core", kind="write", ops=[CREATE_CUSTOM_OP, LIST_CUSTOM_OP], field_map=CUSTOM_BODY_MAP
)
async def create_custom_asset(
    ctx: Context,
    name: Annotated[str, Field(description=f"Display name, not blank, at most {NAME_MAX} characters.")],
    client_id: Annotated[StrictId, Field(description="Active client that owns the asset (list_clients).")],
    location_id: Annotated[StrictId, Field(description="Active location of the client (list_client_locations).")],
    hardware_type_id: Annotated[
        StrictId, Field(description="Hardware type (list_custom_asset_hardware_types).")
    ],
    description: Annotated[str | None, Field(description=f"Free text, at most {DESCRIPTION_MAX} characters.")] = None,
    contact_id: Annotated[StrictId | None, Field(description="Contact to assign (list_contacts), active, of client_id.")] = None,
    warranty_end_date: Annotated[str | None, Field(description="Warranty end, YYYY-MM-DD.")] = None,
    ip_address: Annotated[str | None, Field(description="IPv4 address such as 10.0.0.20. IPv6 is not accepted.")] = None,
    serial_number: Annotated[str | None, Field(description=f"Hardware serial number, at most {SERIAL_MAX} characters.")] = None,
) -> dict:
    """Create a custom (non-agent) asset at a location of a client; returns the new row (read back from the client's list), or {Id, warning} if the read-back fails: then do not repeat the create.
    Resolve ids first: client_id -> list_clients, location_id -> list_client_locations, hardware_type_id -> list_custom_asset_hardware_types.
    Side effects: the creation is recorded on the asset's timeline, as in the app. This server cannot delete custom assets (DELETE is forbidden), so a created asset can only be removed in the Gorelo app.
    """
    values = _custom_values(
        name=name, client_id=client_id, location_id=location_id, hardware_type_id=hardware_type_id,
        description=description, contact_id=contact_id, warranty_end_date=warranty_end_date,
        ip_address=ip_address, serial_number=serial_number,
    )
    for param in ("name", "client_id", "location_id", "hardware_type_id"):
        if values[param] is None:
            raise ValueError(f"{param}: required")
    body = build_body(values, CUSTOM_BODY_MAP)
    data = await client_of(ctx).post(CREATE_CUSTOM_OP, json_body=body, tool="create_custom_asset")
    new_id = created_id(data, CREATE_CUSTOM_OP, tool="create_custom_asset")
    return await _read_back_custom(ctx, tool="create_custom_asset", client_id=values["client_id"], asset_id=new_id)


@gorelo_tool(
    toolset="core",
    kind="write",
    ops=[UPDATE_CUSTOM_OP, LIST_CUSTOM_OP],
    field_map=UPDATE_CUSTOM_MAP,
    destructive_hint=True,
)
async def update_custom_asset(
    ctx: Context,
    custom_asset_id: Annotated[str, Field(description="Custom asset UUID: the Id of a list_custom_assets row.")],
    client_id: Annotated[
        StrictId,
        Field(
            description="The asset's CURRENT client (list_custom_assets): a safety check, since the client cannot be changed."
        ),
    ],
    name: Annotated[str | None, Field(description=f"New name, not blank, at most {NAME_MAX} characters.")] = None,
    location_id: Annotated[StrictId | None, Field(description="New location of the client (list_client_locations).")] = None,
    hardware_type_id: Annotated[StrictId | None, Field(description="New hardware type (list_custom_asset_hardware_types).")] = None,
    description: Annotated[str | None, Field(description=f"New description, at most {DESCRIPTION_MAX} characters.")] = None,
    contact_id: Annotated[StrictId | None, Field(description="Contact to assign (list_contacts), active, of client_id. To unassign use clear_fields.")] = None,
    warranty_end_date: Annotated[str | None, Field(description="New warranty end, YYYY-MM-DD.")] = None,
    ip_address: Annotated[str | None, Field(description="New IPv4 address, such as 10.0.0.20.")] = None,
    serial_number: Annotated[str | None, Field(description=f"New serial number, at most {SERIAL_MAX} characters.")] = None,
    clear_fields: Annotated[
        list[CustomClearable] | None,
        Field(description="Fields to empty (sent as null). Not together with a value for the same field."),
    ] = None,
) -> dict:
    """Change fields of one custom asset; returns the updated row (read back from the client's list), or {Id, warning} if the read-back fails. Send only what changes; at least one change besides client_id is required.
    Name, location_id and hardware_type_id can be changed but not cleared; clear_fields empties the other five. The client cannot be changed: client_id must be its current client, so an asset with no client cannot be updated here.
    Resolve ids first: custom_asset_id and client_id -> list_custom_assets, location_id -> list_client_locations, hardware_type_id -> list_custom_asset_hardware_types.
    Side effects: overwrites or clears stored values. This server cannot delete custom assets.
    """
    asset_id = guid("custom_asset_id", custom_asset_id)
    clear = _clear_names(clear_fields)
    values = _custom_values(
        name=name, client_id=client_id, location_id=location_id, hardware_type_id=hardware_type_id,
        description=description, contact_id=contact_id, warranty_end_date=warranty_end_date,
        ip_address=ip_address, serial_number=serial_number,
    )
    for param in clear:
        if values[param] is not None:
            raise ValueError(f"{param}: cannot be given a value and listed in clear_fields in the same call")
    body = build_body(values, CUSTOM_BODY_MAP, clear=clear, clear_values=CUSTOM_CLEAR_VALUES)
    if not set(body) - {"ClientId"}:
        raise ValueError(
            "nothing to change: give at least one of name, location_id, hardware_type_id, description, "
            "contact_id, warranty_end_date, ip_address, serial_number or clear_fields (client_id alone changes nothing)"
        )
    data = await client_of(ctx).patch(
        UPDATE_CUSTOM_OP, path_params={"customAssetId": asset_id}, json_body=body, tool="update_custom_asset"
    )
    created_id(data, UPDATE_CUSTOM_OP, tool="update_custom_asset")
    return await _read_back_custom(ctx, tool="update_custom_asset", client_id=values["client_id"], asset_id=asset_id)


def _clear_names(clear_fields: list[str] | None) -> list[str]:
    """The distinct names in clear_fields, each one a field Gorelo clears with null. [] is an error."""
    if clear_fields is None:
        return []
    non_empty("clear_fields", clear_fields)
    for index, name in enumerate(clear_fields):
        if not isinstance(name, str):
            raise ValueError(f"clear_fields[{index}]: expected a field name such as \"description\", got {describe_value(name)}")
    names = list(dict.fromkeys(clear_fields))
    for name in names:
        if name not in CUSTOM_CLEARABLE:
            raise ValueError(f"clear_fields: {name!r} cannot be cleared; allowed: {', '.join(CUSTOM_CLEARABLE)}")
    return names
