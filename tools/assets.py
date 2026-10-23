"""Assets: RMM agents (read only) and custom assets (read, create, update, move) (toolset "core").

Tools: list_agents, get_agent, list_agent_apps, get_agent_hardware, list_agent_patches, list_agent_available_updates,
list_agent_tags, list_asset_types, list_custom_assets, get_custom_asset, list_custom_asset_hardware_types,
create_custom_asset, update_custom_asset.

Ops owned by this module:

    GET /v1/assets/agents
    GET /v1/assets/agents/{agentAssetId}
    GET /v1/assets/agents/{agentAssetId}/apps
    GET /v1/assets/agents/{agentAssetId}/hardware
    GET /v1/assets/agents/{agentAssetId}/patches
    GET /v1/assets/agents/{agentAssetId}/patches/available
    GET /v1/assets/agents/{agentAssetId}/tags
    GET /v1/assets/types
    GET /v1/assets/custom
    GET /v1/assets/custom/{customAssetId}
    GET /v1/assets/custom/hardware-types
    POST /v1/assets/custom
    PATCH /v1/assets/custom/{customAssetId}

Since the 2026-10-17 contract (a7954b24844b) Gorelo has a GET for one custom asset. get_custom_asset is it, and
create_custom_asset and update_custom_asset read the record back with it by id (reread_after_write). The read-back does
not raise after a confirmed write: when it fails, or answers no usable record of the asset, the tool returns
{"Id": ..., "warning": ...}; the move check below is the one exception, and a PATCH answer with another Id is refused
before any read-back.

update_custom_asset can move an asset: PATCH text "A new ClientId moves the asset to that client. The asset's location and
contact must belong to the new client, so send a LocationId of the new client, and a ContactId of it or null". client_id is
optional and means the NEW client. When it is given the tool reads the asset first (GET) and, for a client other than the
one the asset holds, refuses locally, before any PATCH, unless location_id is given and, when the asset holds a contact,
contact_id is given or "contact_id" is in clear_fields. The current client is no move. After a move the read-back must show
the new client, else the tool raises a write-unconfirmed shape error. ClientId is sent iff client_id was given.

Agents carry LocationId (renamed from ClientLocationId on 2026-08-21) and WarrantyStartDate /
WarrantyEndDate (replaced WarrantyExpiryDate the same day). The agent list rows are AgentAssetListItemResponse (DeviceListItemResponse before contract 05200951c89e)
records and the single agent is an AgentAssetResponse (DeviceResponse before). Since contract 1b1c0cec0e9a (2026-10-14) the single agent is
the list row's fields plus CustomProperties, which the list deliberately leaves out: one entry (Id, Variable,
Name, Type, Value, IsSet, IsHidden) for every custom field defined for devices. Value is the effective value (the saved
one, else the field's default, else null), always text, and IsSet is true when a value is saved on the device. Since
2026-10-17 IsHidden is true when the provider hid the field's value from the API: Value is then always null, IsSet is
still reported. get_agent passes CustomProperties through unchanged; nothing writes them, because Gorelo has no operation
that updates a device. The agent list takes StatusIds, checked locally against Gorelo's scale (1 Unreachable, 2 Online,
3 Offline, 4 Pending Delete, 6 Pending client inactive; up to 100 ids; any other value is a 400); the custom asset list
has no StatusIds, so list_custom_assets does not offer it (Gorelo rejects names an op does not declare).

Contract 05200951c89e (2026-10-22) added six reads. The five per-agent ones are unpaged and each takes the agent UUID: apps,
hardware and patches answer one object ({LastScannedOn, Items}; hardware has Processors, Disks, TotalMemoryBytes and
HardwareType), available updates add WindowsUpdateStatus (Windows only; a macOS agent is a 400 on DeviceId) and tags answer a
list. GET /v1/assets/types is the {Id, Name} list of asset types: the Type.Id of agent and custom assets and the id that
create_custom_asset and update_custom_asset take as hardware_type_id. The same contract added LocalIPAddress, Manufacturer, Model
and WarrantyStartDate to the custom asset body (IpAddress is deprecated for LocalIPAddress), made GET
/v1/assets/custom/hardware-types deprecated for GET /v1/assets/types, and renamed device statuses 2 and 3 to Online and Offline.

There are no delete tools: DELETE /v1/assets/agents/{agentAssetId} uninstalls the RMM agent from the host and
DELETE /v1/assets/custom/{customAssetId} is not allowed; both are in FORBIDDEN_OPS.
"""

import ipaddress
import re
from datetime import date
from typing import Annotated, Any, Literal

from fastmcp import Context
from pydantic import Field

from gorelo_client import GoreloAPIError
from tools._common import (
    MAX_ID,
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
    reread_after_write,
    utc_iso,
)

LIST_AGENTS_OP = "GET /v1/assets/agents"
GET_AGENT_OP = "GET /v1/assets/agents/{agentAssetId}"
LIST_CUSTOM_OP = "GET /v1/assets/custom"
GET_CUSTOM_OP = "GET /v1/assets/custom/{customAssetId}"
HARDWARE_TYPES_OP = "GET /v1/assets/custom/hardware-types"
APPS_OP = "GET /v1/assets/agents/{agentAssetId}/apps"
HARDWARE_OP = "GET /v1/assets/agents/{agentAssetId}/hardware"
PATCHES_OP = "GET /v1/assets/agents/{agentAssetId}/patches"
AVAILABLE_OP = "GET /v1/assets/agents/{agentAssetId}/patches/available"
TAGS_OP = "GET /v1/assets/agents/{agentAssetId}/tags"
TYPES_OP = "GET /v1/assets/types"
CREATE_CUSTOM_OP = "POST /v1/assets/custom"
UPDATE_CUSTOM_OP = "PATCH /v1/assets/custom/{customAssetId}"

NAME_MAX = 300
DESCRIPTION_MAX = 1000
SERIAL_MAX = 250
MANUFACTURER_MAX = 300
MODEL_MAX = 300
# The client_id parameter of update_custom_asset (a move, see _is_move) and the refusal of a call that would change nothing.
MOVE_CLIENT_HELP = (
    "Only to MOVE the asset: the new client (list_clients; must be active). Then also give location_id of that client, "
    "and contact_id of it or clear_fields contact_id if the asset has a contact."
)
NOTHING_TO_CHANGE = (
    "nothing to change: give at least one of name, location_id, hardware_type_id, description, contact_id, "
    "warranty_start_date, warranty_end_date, local_ip_address, ip_address, manufacturer, model, serial_number or clear_fields (client_id alone changes nothing; it moves the "
    "asset only together with a location_id of the new client)"
)

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
# The device status scale of the StatusIds filter of GET /v1/assets/agents ("1 Unreachable, 2 Active, 3 Inactive, 4 Pending
# Delete, 6 Pending client inactive. Up to 100 ids; any other value is a 400", spec of 2026-10-17; contract 05200951c89e
# calls 2 Online and 3 Offline, the ids are unchanged). There is no status 5.
AGENT_STATUS_LABELS = {1: "Unreachable", 2: "Online", 3: "Offline", 4: "Pending Delete", 6: "Pending client inactive"}
AGENT_STATUS_SCALE = ", ".join(f"{status} {label}" for status, label in AGENT_STATUS_LABELS.items())
AGENT_STATUS_IDS_MAX = 100
AGENT_STATUS_HELP = f"Device status ids: {AGENT_STATUS_SCALE}. Up to {AGENT_STATUS_IDS_MAX} ids; any other id is refused."
# snake_case tool parameter -> body field of CreateCustomAssetCommand / UpdateCustomAssetCommand.
CUSTOM_BODY_MAP = {
    "name": "Name",
    "client_id": "ClientId",
    "location_id": "LocationId",
    "hardware_type_id": "HardwareTypeId",
    "description": "Description",
    "contact_id": "ContactId",
    "warranty_start_date": "WarrantyStartDate",
    "warranty_end_date": "WarrantyEndDate",
    "local_ip_address": "LocalIPAddress",
    "ip_address": "IpAddress",
    "manufacturer": "Manufacturer",
    "model": "Model",
    "serial_number": "SerialNumber",
}
UPDATE_CUSTOM_MAP = {**CUSTOM_BODY_MAP, "custom_asset_id": "customAssetId"}
# Fields that Gorelo clears with a JSON null (name, location_id and hardware_type_id cannot be cleared).
CUSTOM_CLEARABLE = (
    "description", "contact_id", "warranty_start_date", "warranty_end_date", "local_ip_address", "ip_address", "manufacturer",
    "model", "serial_number",
)
CUSTOM_CLEAR_VALUES = {param: None for param in CUSTOM_CLEARABLE}
CustomClearable = Literal[
    "description", "contact_id", "warranty_start_date", "warranty_end_date", "local_ip_address", "ip_address", "manufacturer",
    "model", "serial_number",
]
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# The path placeholder of get_agent, so that a Gorelo error about the id names agent_id.
GET_AGENT_FIELD_MAP = {"agent_id": "agentAssetId"}
AGENT_ID_HELP = "Agent asset UUID: the Id of a list_agents row."
# The path placeholder of get_custom_asset, so that a Gorelo error about the id names custom_asset_id.
GET_CUSTOM_FIELD_MAP = {"custom_asset_id": "customAssetId"}


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


def _agent_status(param: str, value: Any) -> int:
    """One device status on Gorelo's scale (AGENT_STATUS_LABELS). Every whole number outside the scale is refused with the
    scale, 0 and the negative numbers included (positive_id's "expected a positive whole number such as 123" would suggest
    that any id will do); whatever else is not an id (a boolean, a decimal, text, a number beyond int64) keeps the message
    of positive_id."""
    if isinstance(value, int) and not isinstance(value, bool) and -MAX_ID <= value < 1:
        status = int(value)
    else:
        status = positive_id(param, value)
    if status not in AGENT_STATUS_LABELS:
        raise ValueError(f"{param}: expected {AGENT_STATUS_SCALE}, got {status}")
    return status


def _agent_statuses(param: str, values: Any) -> list[int] | None:
    """The status_ids filter of the agent list: every item on the scale, a bad one named by its index, at most 100 ids.

    None, an empty list and a value that is not a list keep the messages of positive_ids."""
    if isinstance(values, (list, tuple)) and values:
        checked = [_agent_status(f"{param}[{index}]", item) for index, item in enumerate(values)]
        if len(checked) > AGENT_STATUS_IDS_MAX:
            raise ValueError(f"{param}: at most {AGENT_STATUS_IDS_MAX} ids, got {len(checked)}")
        return checked
    return positive_ids(param, values)


async def _agent_object(ctx: Context, agent_id: Any, op: str, tool: str) -> dict:
    """GET one per-agent object ({LastScannedOn, Items} and kin) by agent UUID, returned as Gorelo sent it."""
    aid = guid("agent_id", agent_id)
    record = await client_of(ctx).get_one(op, path_params={"agentAssetId": aid}, tool=tool)
    return expect_object(record, op, tool=tool)


@gorelo_tool(toolset="core", kind="read", ops=[LIST_AGENTS_OP], field_map=AGENT_LIST_FIELD_MAP)
async def list_agents(
    ctx: Context,
    status_ids: Annotated[list[StrictId] | None, Field(description=AGENT_STATUS_HELP)] = None,
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
    Rows carry LocationId, WarrantyStartDate/WarrantyEndDate, DisplayName, SerialNumber, Type ({Id, Name}), ContactId and Banner; SerialNo and HardwareType are deprecated. Rows omit custom fields: get_agent has them. Deleted and client-inactivated devices are never listed. This server cannot delete or uninstall agents. Paging: pass next_cursor as cursor, same filters, until has_more is false.
    """
    params, shown = _shared_filters(client_ids, query, created_since, created_before, updated_since, updated_before)
    params["StatusIds"] = csv_ids("status_ids", _agent_statuses("status_ids", status_ids))
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
    """Get one agent (managed device) by UUID; returns Gorelo's full device record, including CustomProperties (a field with IsHidden true has a null Value; IsSet still says whether one is saved).
    Like list rows it has DisplayName, SerialNumber, Type ({Id, Name}), ContactId and Banner; SerialNo and HardwareType are deprecated.
    """
    aid = guid("agent_id", agent_id)
    record = await client_of(ctx).get_one(GET_AGENT_OP, path_params={"agentAssetId": aid}, tool="get_agent")
    return expect_object(record, GET_AGENT_OP, tool="get_agent")


@gorelo_tool(toolset="core", kind="read", ops=[APPS_OP], field_map=GET_AGENT_FIELD_MAP)
async def list_agent_apps(
    ctx: Context,
    agent_id: Annotated[str, Field(description=AGENT_ID_HELP)],
) -> dict:
    """List the apps installed on one agent, from its last scan; returns {LastScannedOn, Items}. Not paged.
    LastScannedOn is null and Items empty when the agent never reported.
    """
    return await _agent_object(ctx, agent_id, APPS_OP, "list_agent_apps")


@gorelo_tool(toolset="core", kind="read", ops=[HARDWARE_OP], field_map=GET_AGENT_FIELD_MAP)
async def get_agent_hardware(
    ctx: Context,
    agent_id: Annotated[str, Field(description=AGENT_ID_HELP)],
) -> dict:
    """Get the hardware of one agent from its last scan; returns {LastScannedOn, HardwareType, TotalMemoryBytes, Processors, Disks (each with its partitions)}.
    Sizes are in bytes.
    """
    return await _agent_object(ctx, agent_id, HARDWARE_OP, "get_agent_hardware")


@gorelo_tool(toolset="core", kind="read", ops=[PATCHES_OP], field_map=GET_AGENT_FIELD_MAP)
async def list_agent_patches(
    ctx: Context,
    agent_id: Annotated[str, Field(description=AGENT_ID_HELP)],
) -> dict:
    """List the patches installed on one agent, newest first (Windows and macOS); returns {LastScannedOn, Items}. Not paged.
    For pending updates use list_agent_available_updates.
    """
    return await _agent_object(ctx, agent_id, PATCHES_OP, "list_agent_patches")


@gorelo_tool(toolset="core", kind="read", ops=[AVAILABLE_OP], field_map=GET_AGENT_FIELD_MAP)
async def list_agent_available_updates(
    ctx: Context,
    agent_id: Annotated[str, Field(description=AGENT_ID_HELP)],
) -> dict:
    """List the updates still pending on one agent, from its last scan (no new scan is started); returns {LastScannedOn, WindowsUpdateStatus, Items}.
    Windows agents only: for a macOS agent Gorelo answers 400 on DeviceId, which means agent_id (use a Windows agent).
    """
    return await _agent_object(ctx, agent_id, AVAILABLE_OP, "list_agent_available_updates")


@gorelo_tool(toolset="core", kind="read", ops=[TAGS_OP], field_map=GET_AGENT_FIELD_MAP)
async def list_agent_tags(
    ctx: Context,
    agent_id: Annotated[str, Field(description=AGENT_ID_HELP)],
) -> dict:
    """List the tags of one agent; returns {items, count}, each item {Id, Name, Type, Conditions}. Not paged."""
    aid = guid("agent_id", agent_id)
    rows = await client_of(ctx).get_list(TAGS_OP, path_params={"agentAssetId": aid}, tool="list_agent_tags")
    return list_result(rows)


@gorelo_tool(toolset="core", kind="read", ops=[TYPES_OP])
async def list_asset_types(ctx: Context) -> dict:
    """List the asset types; returns {items, count}, each item {Id, Name}. Not paged.
    The Id is the Type.Id of agent and custom assets and the hardware_type_id that create_custom_asset and update_custom_asset take.
    """
    rows = await client_of(ctx).get_list(TYPES_OP, tool="list_asset_types")
    return list_result(rows)


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
    Rows carry ContactId (null when unassigned), DisplayName, Type ({Id, Name}; list_asset_types lists them), LocalIPAddress, Manufacturer, Model and WarrantyStartDate/WarrantyEndDate (calendar dates, YYYY-MM-DD); HardwareType, HardwareTypeId and IpAddress are deprecated. get_custom_asset reads one by UUID. No status filter. Create and update exist (create_custom_asset, update_custom_asset); this server still cannot delete custom assets. Paging: pass next_cursor as cursor, same filters, until has_more is false.
    """
    params, shown = _shared_filters(client_ids, query, created_since, created_before, updated_since, updated_before)
    token = non_empty("cursor", cursor)
    size = clamp_page_size(page_size)
    page = await client_of(ctx).get_page(
        LIST_CUSTOM_OP, query=params, page_size=size, cursor=token, tool="list_custom_assets"
    )
    return paged_result(page, shown)


@gorelo_tool(toolset="core", kind="read", ops=[GET_CUSTOM_OP], field_map=GET_CUSTOM_FIELD_MAP)
async def get_custom_asset(
    ctx: Context,
    custom_asset_id: Annotated[str, Field(description="Custom asset UUID: the Id of a list_custom_assets row.")],
) -> dict:
    """Get one custom (non-agent) asset by UUID; returns the same fields as a list_custom_assets row (Description, SerialNumber, LocalIPAddress and ContactId are null when the asset has none; Type replaces the deprecated HardwareType, HardwareTypeId and IpAddress).
    Use it to see the asset's ClientId, LocationId and ContactId before update_custom_asset moves or changes it, and to re-read it after a change.
    """
    aid = guid("custom_asset_id", custom_asset_id)
    record = await client_of(ctx).get_one(GET_CUSTOM_OP, path_params={"customAssetId": aid}, tool="get_custom_asset")
    return expect_object(record, GET_CUSTOM_OP, tool="get_custom_asset")


# --------------------------------------------------------------------------
# Custom assets: hardware types, create, update
# --------------------------------------------------------------------------


@gorelo_tool(toolset="core", kind="read", ops=[HARDWARE_TYPES_OP])
async def list_custom_asset_hardware_types(ctx: Context) -> dict:
    """DEPRECATED by Gorelo: use list_asset_types. Lists the hardware types a custom asset can be given; returns {items, count}, each item {Id, Name, CreatedOn, UpdatedOn}. Not paged.
    These Ids are the hardware_type_id values create_custom_asset and update_custom_asset accept.
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
    warranty_start_date: Any,
    warranty_end_date: Any,
    local_ip_address: Any,
    ip_address: Any,
    manufacturer: Any,
    model: Any,
    serial_number: Any,
) -> dict[str, Any]:
    """The validated body values of create and update (None means not given). ip_address and local_ip_address are one
    field to Gorelo (IpAddress is deprecated for LocalIPAddress), so both together are refused here."""
    if ip_address is not None and local_ip_address is not None:
        raise ValueError(
            "ip_address and local_ip_address: give only one; ip_address is deprecated, use local_ip_address"
        )
    return {
        "name": _bounded_text("name", name, NAME_MAX),
        "client_id": _optional_id("client_id", client_id),
        "location_id": _optional_id("location_id", location_id),
        "hardware_type_id": _optional_id("hardware_type_id", hardware_type_id),
        "description": _bounded_text("description", description, DESCRIPTION_MAX),
        "contact_id": _optional_id("contact_id", contact_id),
        "warranty_start_date": _calendar_date("warranty_start_date", warranty_start_date),
        "warranty_end_date": _calendar_date("warranty_end_date", warranty_end_date),
        "local_ip_address": _ipv4("local_ip_address", local_ip_address),
        "ip_address": _ipv4("ip_address", ip_address),
        "manufacturer": _bounded_text("manufacturer", manufacturer, MANUFACTURER_MAX),
        "model": _bounded_text("model", model, MODEL_MAX),
        "serial_number": _bounded_text("serial_number", serial_number, SERIAL_MAX),
    }


def _held_id(key: str, row: dict[str, Any]) -> int | None:
    """ClientId or ContactId of the asset as the pre-read showed it: a positive whole number, or None (not set)."""
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"custom_asset_id: {GET_CUSTOM_OP} returned a {key} that is {describe_value(value)}, not an id; "
            "refusing to guess whether client_id moves the asset. Nothing was changed."
        )
    return value


async def _is_move(
    ctx: Context, *, asset_id: str, client_id: int, location_id: Any, contact_id: Any, clears_contact: bool
) -> bool:
    """GET the asset before a PATCH that names client_id; True when client_id is a different client (a move).

    A move that lacks what Gorelo needs is refused here, before any write: a location_id of the new client always, and
    a contact_id of the new client or the contact clear when the asset holds a contact. A missing ClientId counts as a
    different client. A read that fails stops the call with the PATCH unsent."""
    data = await client_of(ctx).get_one(
        GET_CUSTOM_OP, path_params={"customAssetId": asset_id}, tool="update_custom_asset"
    )
    row = expect_object(data, GET_CUSTOM_OP, tool="update_custom_asset")
    held_client = _held_id("ClientId", row)
    if held_client == client_id:
        return False
    now = f"it is at client {held_client} now" if held_client is not None else "it has no client now"
    if location_id is None:
        raise ValueError(
            f"location_id: required when client_id moves the asset ({now}); give a location of client {client_id} "
            "(list_client_locations). Nothing was changed."
        )
    held_contact = _held_id("ContactId", row)
    if held_contact is not None and contact_id is None and not clears_contact:
        raise ValueError(
            f"contact_id: the asset holds contact {held_contact}; when client_id moves it, give contact_id (a contact of "
            f"client {client_id}, which may be {held_contact} if it already belongs to that client) or put contact_id in "
            "clear_fields to unassign it. Nothing was changed."
        )
    return True


def _written_row(row: dict[str, Any], written_id: Any) -> dict[str, Any]:
    """The read-back of a write, checked against the id that was written.

    A {Id, warning} pair (the read-back failed) is returned unchanged, and so is a record whose Id, compared as lower-case
    text, is the written id. An empty record, or a record whose Id is missing or is another asset's, is not the asset that
    was written: it becomes {Id, warning} naming what came back, so the tool never presents it as the result of the write."""
    if set(row) == {"Id", "warning"}:
        return row
    if row and str(row.get("Id")).lower() == str(written_id).lower():
        return row
    if not row:
        found = "an empty object"
    elif row.get("Id") is None:
        found = "a record without an Id"
    else:
        found = f"the record of asset {row['Id']}"
    return {
        "Id": written_id,
        "warning": (
            f"the write succeeded; re-reading it returned no usable record of this asset ({found}). "
            "Do not repeat the write; check with get_custom_asset."
        ),
    }


def _check_moved(row: dict[str, Any], client_id: int) -> None:
    """After a move the read-back must show the new client. A read-back that failed ({Id, warning}) is left alone."""
    if set(row) == {"Id", "warning"}:
        return
    shown = row.get("ClientId")
    if shown == client_id:
        return
    where = f"client {shown}" if shown is not None else "no client"
    raise GoreloAPIError(
        f"{UPDATE_CUSTOM_OP}: Gorelo reported success but the asset reads back at {where}, not at client {client_id}; "
        "the move may or may not have been applied, so verify it with a read before repeating it",
        status=200,
        op_key=UPDATE_CUSTOM_OP,
        kind="shape",
        write_unconfirmed=True,
    )


@gorelo_tool(
    toolset="core", kind="write", ops=[CREATE_CUSTOM_OP, GET_CUSTOM_OP], field_map=CUSTOM_BODY_MAP
)
async def create_custom_asset(
    ctx: Context,
    name: Annotated[str, Field(description=f"Display name, not blank, at most {NAME_MAX} characters.")],
    client_id: Annotated[StrictId, Field(description="Active client that owns the asset (list_clients).")],
    location_id: Annotated[StrictId, Field(description="Active location of the client (list_client_locations).")],
    hardware_type_id: Annotated[
        StrictId, Field(description="Asset type id (list_asset_types).")
    ],
    description: Annotated[str | None, Field(description=f"Free text, at most {DESCRIPTION_MAX} characters.")] = None,
    contact_id: Annotated[StrictId | None, Field(description="Contact to assign (list_contacts), active, of client_id.")] = None,
    warranty_start_date: Annotated[str | None, Field(description="Warranty start, YYYY-MM-DD.")] = None,
    warranty_end_date: Annotated[str | None, Field(description="Warranty end, YYYY-MM-DD.")] = None,
    local_ip_address: Annotated[str | None, Field(description="IPv4 address such as 10.0.0.20. Not together with ip_address.")] = None,
    ip_address: Annotated[str | None, Field(description="Deprecated, use local_ip_address. IPv4 address such as 10.0.0.20; IPv6 is not accepted.")] = None,
    manufacturer: Annotated[str | None, Field(description=f"Manufacturer, at most {MANUFACTURER_MAX} characters.")] = None,
    model: Annotated[str | None, Field(description=f"Model, at most {MODEL_MAX} characters.")] = None,
    serial_number: Annotated[str | None, Field(description=f"Hardware serial number, at most {SERIAL_MAX} characters.")] = None,
) -> dict:
    """Create a custom (non-agent) asset at a location of a client; returns the new row (read back by id), or {Id, warning} if the read-back fails: then do not repeat the create; check with get_custom_asset.
    Resolve ids first: client_id -> list_clients, location_id -> list_client_locations, hardware_type_id -> list_asset_types.
    Side effects: the creation is recorded on the asset's timeline, as in the app. This server cannot delete custom assets (DELETE is forbidden), so a created asset can only be removed in the Gorelo app.
    """
    values = _custom_values(
        name=name, client_id=client_id, location_id=location_id, hardware_type_id=hardware_type_id,
        description=description, contact_id=contact_id, warranty_start_date=warranty_start_date,
        warranty_end_date=warranty_end_date, local_ip_address=local_ip_address, ip_address=ip_address,
        manufacturer=manufacturer, model=model, serial_number=serial_number,
    )
    for param in ("name", "client_id", "location_id", "hardware_type_id"):
        if values[param] is None:
            raise ValueError(f"{param}: required")
    body = build_body(values, CUSTOM_BODY_MAP)
    data = await client_of(ctx).post(CREATE_CUSTOM_OP, json_body=body, tool="create_custom_asset")
    new_id = created_id(data, CREATE_CUSTOM_OP, tool="create_custom_asset")
    row = await reread_after_write(
        ctx, GET_CUSTOM_OP, path_params={"customAssetId": new_id}, tool="create_custom_asset", written_id=new_id
    )
    return _written_row(row, new_id)


@gorelo_tool(
    toolset="core",
    kind="write",
    ops=[UPDATE_CUSTOM_OP, GET_CUSTOM_OP],
    field_map=UPDATE_CUSTOM_MAP,
    destructive_hint=True,
)
async def update_custom_asset(
    ctx: Context,
    custom_asset_id: Annotated[str, Field(description="Custom asset UUID: the Id of a list_custom_assets row.")],
    client_id: Annotated[StrictId | None, Field(description=MOVE_CLIENT_HELP)] = None,
    name: Annotated[str | None, Field(description=f"New name, not blank, at most {NAME_MAX} characters.")] = None,
    location_id: Annotated[
        StrictId | None,
        Field(description="New location (list_client_locations); of the new client when client_id moves the asset."),
    ] = None,
    hardware_type_id: Annotated[StrictId | None, Field(description="New asset type id (list_asset_types).")] = None,
    description: Annotated[str | None, Field(description=f"New description, at most {DESCRIPTION_MAX} characters.")] = None,
    contact_id: Annotated[StrictId | None, Field(description="Contact to assign (list_contacts), active, of the asset's client (of the new client when moving). To unassign use clear_fields.")] = None,
    warranty_start_date: Annotated[str | None, Field(description="New warranty start, YYYY-MM-DD.")] = None,
    warranty_end_date: Annotated[str | None, Field(description="New warranty end, YYYY-MM-DD.")] = None,
    local_ip_address: Annotated[str | None, Field(description="New IPv4 address, such as 10.0.0.20. Not together with ip_address.")] = None,
    ip_address: Annotated[str | None, Field(description="Deprecated, use local_ip_address. New IPv4 address, such as 10.0.0.20.")] = None,
    manufacturer: Annotated[str | None, Field(description=f"New manufacturer, at most {MANUFACTURER_MAX} characters.")] = None,
    model: Annotated[str | None, Field(description=f"New model, at most {MODEL_MAX} characters.")] = None,
    serial_number: Annotated[str | None, Field(description=f"New serial number, at most {SERIAL_MAX} characters.")] = None,
    clear_fields: Annotated[
        list[CustomClearable] | None,
        Field(description="Fields to empty (sent as null). Not together with a value for the same field."),
    ] = None,
) -> dict:
    """Change fields of one custom asset, or move it to another client; returns the updated row (read back by id), or {Id, warning} if the read-back fails. Send only what changes.
    Name, location_id and hardware_type_id can be changed but not cleared; clear_fields empties the other nine.
    To move it give client_id (the new client) with a location_id of that client and, if get_custom_asset shows a ContactId, a contact_id of that client or clear_fields contact_id. It is the same asset (same Id), not a copy. The current client moves nothing.
    Resolve ids first: custom_asset_id -> list_custom_assets; the others as their parameter texts say.
    Side effects: overwrites or clears values; a move changes the owning client. The contact is also in the asset's related items: assigning one adds it there, replacing or clearing it removes the previous one. This server cannot delete custom assets.
    """
    asset_id = guid("custom_asset_id", custom_asset_id)
    clear = _clear_names(clear_fields)
    values = _custom_values(
        name=name, client_id=client_id, location_id=location_id, hardware_type_id=hardware_type_id,
        description=description, contact_id=contact_id, warranty_start_date=warranty_start_date,
        warranty_end_date=warranty_end_date, local_ip_address=local_ip_address, ip_address=ip_address,
        manufacturer=manufacturer, model=model, serial_number=serial_number,
    )
    for param in clear:
        if values[param] is not None:
            raise ValueError(f"{param}: cannot be given a value and listed in clear_fields in the same call")
    for given, other in (("local_ip_address", "ip_address"), ("ip_address", "local_ip_address")):
        if values[given] is not None and other in clear:
            # both names are one Gorelo field: a value next to a null for the other is "different values", a 400
            raise ValueError(
                f"{given} with {other} in clear_fields: {other} is the deprecated twin of the same field, so a value and a "
                f"clear of it contradict each other (Gorelo answers 400). Give {given} alone, or clear "
                "both names to remove the address"
            )
    body = build_body(values, CUSTOM_BODY_MAP, clear=clear, clear_values=CUSTOM_CLEAR_VALUES)
    if not set(body) - {"ClientId"}:
        raise ValueError(NOTHING_TO_CHANGE)
    moves = False
    if values["client_id"] is not None:
        moves = await _is_move(
            ctx, asset_id=asset_id, client_id=values["client_id"], location_id=values["location_id"],
            contact_id=values["contact_id"], clears_contact="contact_id" in clear,
        )
    data = await client_of(ctx).patch(
        UPDATE_CUSTOM_OP, path_params={"customAssetId": asset_id}, json_body=body, tool="update_custom_asset"
    )
    answered = created_id(data, UPDATE_CUSTOM_OP, tool="update_custom_asset")
    if str(answered).lower() != asset_id:
        raise GoreloAPIError(
            f"{UPDATE_CUSTOM_OP}: Gorelo reported success for custom asset {asset_id} but answered with Id {answered}; "
            "check both with get_custom_asset",
            status=200,
            op_key=UPDATE_CUSTOM_OP,
            kind="shape",
            write_unconfirmed=True,
        )
    row = await reread_after_write(
        ctx, GET_CUSTOM_OP, path_params={"customAssetId": asset_id}, tool="update_custom_asset", written_id=asset_id
    )
    row = _written_row(row, asset_id)
    if moves:
        _check_moved(row, values["client_id"])
    return row


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
