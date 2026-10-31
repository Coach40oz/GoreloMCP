"""Assets: RMM agents (read, update), monitoring policies and tags (read), asset types (read, create, rename, delete) and
custom assets (read, create, update, move) (toolset "core").

Tools: list_agents, get_agent, update_agent, list_agent_apps, get_agent_hardware, list_agent_patches,
list_agent_available_updates, list_agent_tags, list_policies, get_policy, list_asset_tags, list_asset_types,
create_asset_type, update_asset_type, delete_asset_type (gated), list_custom_assets, get_custom_asset,
list_custom_asset_hardware_types, create_custom_asset, update_custom_asset.

Ops owned by this module:

    GET /v1/assets/agents
    GET /v1/assets/agents/{agentAssetId}
    GET /v1/assets/agents/{agentAssetId}/apps
    GET /v1/assets/agents/{agentAssetId}/hardware
    GET /v1/assets/agents/{agentAssetId}/patches
    GET /v1/assets/agents/{agentAssetId}/patches/available
    GET /v1/assets/agents/{agentAssetId}/tags
    PATCH /v1/assets/agents/{agentAssetId}
    GET /v1/assets/policies
    GET /v1/assets/policies/{policyId}
    GET /v1/assets/tags
    GET /v1/assets/types
    POST /v1/assets/types
    PATCH /v1/assets/types/{assetTypeId}
    DELETE /v1/assets/types/{assetTypeId}
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
still reported. get_agent passes CustomProperties through unchanged; nothing writes them: update_agent (contract 6c30770dffc0) takes only
Description, HardwareTypeId, ContactId, WarrantyStartDate and WarrantyEndDate. The agent list takes StatusIds, checked locally against Gorelo's scale (1 Unreachable, 2 Online,
3 Offline, 4 Pending Delete, 6 Pending client inactive; up to 100 ids; any other value is a 400); the custom asset list
has no StatusIds, so list_custom_assets does not offer it (Gorelo rejects names an op does not declare).

Contract 05200951c89e (2026-10-22) added six reads. The five per-agent ones are unpaged and each takes the agent UUID: apps,
hardware and patches answer one object ({LastScannedOn, Items}; hardware has Processors, Disks, TotalMemoryBytes and
HardwareType), available updates add WindowsUpdateStatus (Windows only; a macOS agent is a 400 on DeviceId) and tags answer a
list. GET /v1/assets/types is the {Id, Name} list of asset types: the Type.Id of agent and custom assets and the id that
create_custom_asset and update_custom_asset take as hardware_type_id. The same contract added LocalIPAddress, Manufacturer, Model
and WarrantyStartDate to the custom asset body (IpAddress is deprecated for LocalIPAddress), made GET
/v1/assets/custom/hardware-types deprecated for GET /v1/assets/types, and renamed device statuses 2 and 3 to Online and Offline.

An unknown agent id (measured live with read-only GETs on 2026-10-24): GET /v1/assets/agents/{agentAssetId} answers HTTP 200
with IsSuccess true and Data null, where every other single-record GET answers 404 ("The requested resource was not
found."), the agent sub-resources apps, hardware and tags included. The client reads that answer as the shape error "Gorelo
reported success but Data is null; refusing to guess" (for an id taken from elsewhere or mistyped), so get_agent sends the GET with request() and turns a Data that is null or missing, the very condition
under which get_one raised that error, into a plain refusal: it names agent_id, says Gorelo has no agent with that id (unknown
or deleted) and points to list_agents and get_custom_asset. It is a read, so nothing was written. Every other outcome of the
call is unchanged: an envelope or HTTP error, an answer that is no envelope, and a Data that is not a non-empty object. The
other per-agent reads keep get_one, because their unknown ids are 404s.

Contract 6c30770dffc0 (2026-10-29) added PATCH /v1/assets/agents/{agentAssetId} (UpdateAgentCommand, every field optional,
an empty body is a 400; Description, ContactId, WarrantyStartDate and WarrantyEndDate clear with null, HardwareTypeId cannot be
cleared; a cleared warranty date is filled in again by Gorelo's warranty lookup and a date set here is kept; ContactId must be
an active contact of the agent's client, so an agent with no client takes none; the answer is {Id}). update_agent sends only
what changed, clears through clear_fields (sent as null), refuses an empty change locally and reads the agent back with
GET /v1/assets/agents/{agentAssetId}. HardwareTypeId is "one of the active types GET /v1/assets/types lists", so
list_asset_types is the list that applies (not list_custom_asset_hardware_types, which is deprecated).

It also added the monitoring policies (GET /v1/assets/policies paged, highest Priority first, Os filter Windows, MacOS or Linux
without regard to case, a cursor only continues the Os it was issued for; GET /v1/assets/policies/{policyId}), the asset tags
(GET /v1/assets/tags, unpaged, New first then by name; the ids are the ones a policy's Tags carry) and writes on asset types:
POST /v1/assets/types (Name, max 100; a name already used is a 409 ResourceAlreadyExists whose DataContext.ExistingId is the
id of that type), PATCH /v1/assets/types/{assetTypeId} (rename; a built-in type is a 409 ResourceUpdateBlocked, a name used by
another type a 400 on Name) and DELETE /v1/assets/types/{assetTypeId} (a 409 ResourceDeleteBlocked while any agent or custom
asset has the type, a 409 ResourceNotDeletable for a default type, a 404 the second time). All three answer {Id}.
GoreloAPIError keeps no DataContext, so create_asset_type finds the existing type itself: on a 409 it reads the type list and
names the Id of the type with that name. The write tools read the new or changed type back from GET /v1/assets/types
(there is no GET of one type) and return that row, or {Id, warning} when the list cannot show it.

There is no delete tool for assets: DELETE /v1/assets/agents/{agentAssetId} uninstalls the RMM agent from the host and
DELETE /v1/assets/custom/{customAssetId} is not allowed; both are in FORBIDDEN_OPS. DELETE /v1/assets/types/{assetTypeId}
removes an asset type, not an asset, and is the gated delete_asset_type.
"""

import ipaddress
import re
from datetime import date
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from gorelo_client import GoreloAPIError
from spec import SpecViolation
from tools._common import (
    MAX_ID,
    StrictBool,
    StrictId,
    build_body,
    clamp_page_size,
    client_of,
    created_id,
    csv_ids,
    describe_value,
    expect_object,
    format_gorelo_error,
    gorelo_tool,
    guid,
    list_result,
    non_empty,
    paged_result,
    positive_id,
    positive_ids,
    require_confirm,
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
UPDATE_AGENT_OP = "PATCH /v1/assets/agents/{agentAssetId}"
LIST_POLICIES_OP = "GET /v1/assets/policies"
GET_POLICY_OP = "GET /v1/assets/policies/{policyId}"
ASSET_TAGS_OP = "GET /v1/assets/tags"
CREATE_TYPE_OP = "POST /v1/assets/types"
UPDATE_TYPE_OP = "PATCH /v1/assets/types/{assetTypeId}"
DELETE_TYPE_OP = "DELETE /v1/assets/types/{assetTypeId}"
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
# What get_agent says when Gorelo answers success with no record. For an unknown agent id Gorelo answers HTTP 200, IsSuccess true
# and Data null (measured live on 2026-10-24), not the 404 of every other single-record read. The text names the parameter, says
# what the answer means and where to look next; it is a plain refusal of a read, so it says nothing about a write.
NO_AGENT_TEXT = (
    "agent_id: Gorelo has no agent with this id (unknown or deleted). For an unknown agent id Gorelo answers success with no "
    "record, unlike other records, which answer 404. Find agents with list_agents; read a custom asset with get_custom_asset."
)
# snake_case tool parameter -> body field of UpdateAgentCommand (every field optional; null clears the four clearable ones).
AGENT_BODY_MAP = {
    "description": "Description",
    "hardware_type_id": "HardwareTypeId",
    "contact_id": "ContactId",
    "warranty_start_date": "WarrantyStartDate",
    "warranty_end_date": "WarrantyEndDate",
}
UPDATE_AGENT_MAP = {**AGENT_BODY_MAP, "agent_id": "agentAssetId"}
# HardwareTypeId "cannot be cleared; null leaves it unchanged".
AGENT_CLEARABLE = ("description", "contact_id", "warranty_start_date", "warranty_end_date")
AGENT_CLEAR_VALUES = {param: None for param in AGENT_CLEARABLE}
AgentClearable = Literal["description", "contact_id", "warranty_start_date", "warranty_end_date"]
AGENT_DESCRIPTION_MAX = 2000
NOTHING_TO_CHANGE_AGENT = (
    "nothing to change: give at least one of description, hardware_type_id, contact_id, warranty_start_date, "
    "warranty_end_date or clear_fields"
)
# list_policies: the Os filter ("Windows, MacOS or Linux, without regard to case. Any other value is a 400.") and the paging names.
POLICY_LIST_FIELD_MAP = {"os": "Os", "page_size": "PageSize", "cursor": "Cursor"}
POLICY_ID_FIELD_MAP = {"policy_id": "policyId"}
# Asset types: Name is "max 100 characters". A 409 on the create is a name that is already used.
TYPE_NAME_MAX = 100
HTTP_CONFLICT = 409
CREATE_TYPE_MAP = {"name": "Name"}
UPDATE_TYPE_MAP = {"asset_type_id": "assetTypeId", "name": "Name"}
DELETE_TYPE_MAP = {"asset_type_id": "assetTypeId"}
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
    # request() instead of get_one(): get_one refuses a null Data as a shape error, and for this operation a null Data is
    # Gorelo's answer for an agent id it does not know. Everything else (errors, odd bodies) is get_one's behavior unchanged.
    answer = await client_of(ctx).request(GET_AGENT_OP, path_params={"agentAssetId": aid}, tool="get_agent")
    record = answer.get("Data")
    if record is None:
        raise ValueError(NO_AGENT_TEXT)
    return expect_object(record, GET_AGENT_OP, tool="get_agent")


@gorelo_tool(
    toolset="core",
    kind="write",
    ops=[UPDATE_AGENT_OP, GET_AGENT_OP],
    field_map=UPDATE_AGENT_MAP,
    destructive_hint=True,
)
async def update_agent(
    ctx: Context,
    agent_id: Annotated[str, Field(description=AGENT_ID_HELP)],
    description: Annotated[str | None, Field(description=f"New description, at most {AGENT_DESCRIPTION_MAX} characters. To empty it use clear_fields.")] = None,
    hardware_type_id: Annotated[StrictId | None, Field(description="New asset type id (list_asset_types). Cannot be cleared.")] = None,
    contact_id: Annotated[StrictId | None, Field(description="Contact to assign (list_contacts): an active contact of the agent's client. To unassign use clear_fields.")] = None,
    warranty_start_date: Annotated[str | None, Field(description="New warranty start, YYYY-MM-DD (no time).")] = None,
    warranty_end_date: Annotated[str | None, Field(description="New warranty end, YYYY-MM-DD (no time).")] = None,
    clear_fields: Annotated[
        list[AgentClearable] | None,
        Field(description="Fields to empty (sent as null). Not together with a value for the same field."),
    ] = None,
) -> dict:
    """Change fields of one agent (managed device); returns the updated record (read back by id), or {Id, warning} if the read-back fails. Send only what changes.
    clear_fields empties description, contact_id and the two warranty dates; hardware_type_id (an asset type) cannot be cleared. A cleared warranty date is filled in again by Gorelo's warranty lookup; a date set here is kept. The contact must be an active contact of the agent's client, so an agent awaiting adoption (no client) takes none. Custom fields cannot be written.
    Resolve ids first: agent_id -> list_agents, hardware_type_id -> list_asset_types, contact_id -> list_contacts.
    Side effects: overwrites or clears values, recorded on the agent's timeline; the contact is also in its related items (assigning adds it, replacing or clearing removes the previous one). This server cannot delete or uninstall agents.
    """
    aid = guid("agent_id", agent_id)
    clear = _clear_names(clear_fields, AGENT_CLEARABLE)
    values = {
        "description": _bounded_text("description", description, AGENT_DESCRIPTION_MAX),
        "hardware_type_id": _optional_id("hardware_type_id", hardware_type_id),
        "contact_id": _optional_id("contact_id", contact_id),
        "warranty_start_date": _calendar_date("warranty_start_date", warranty_start_date),
        "warranty_end_date": _calendar_date("warranty_end_date", warranty_end_date),
    }
    for param in clear:
        if values[param] is not None:
            raise ValueError(f"{param}: cannot be given a value and listed in clear_fields in the same call")
    body = build_body(values, AGENT_BODY_MAP, clear=clear, clear_values=AGENT_CLEAR_VALUES)
    if not body:
        raise ValueError(NOTHING_TO_CHANGE_AGENT)
    data = await client_of(ctx).patch(
        UPDATE_AGENT_OP, path_params={"agentAssetId": aid}, json_body=body, tool="update_agent"
    )
    answered = created_id(data, UPDATE_AGENT_OP, tool="update_agent")
    if str(answered).lower() != aid:
        raise GoreloAPIError(
            f"{UPDATE_AGENT_OP}: Gorelo reported success for agent {aid} but answered with Id {answered}; "
            "check both with get_agent",
            status=200,
            op_key=UPDATE_AGENT_OP,
            kind="shape",
            write_unconfirmed=True,
        )
    row = await reread_after_write(
        ctx, GET_AGENT_OP, path_params={"agentAssetId": aid}, tool="update_agent", written_id=aid
    )
    return _written_row(row, aid, noun="agent", reader="get_agent")


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


@gorelo_tool(toolset="core", kind="read", ops=[LIST_POLICIES_OP], field_map=POLICY_LIST_FIELD_MAP)
async def list_policies(
    ctx: Context,
    os: Annotated[
        Literal["Windows", "MacOS", "Linux"] | None,
        Field(description="Only the policies for this operating system. A cursor only continues the os it was issued for."),
    ] = None,
    page_size: Annotated[int, Field(description="Rows per page, 1 to 200.")] = 50,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous call.")] = None,
) -> dict:
    """List the monitoring policies one page per call, highest Priority first (the app's order); returns {items, has_more, next_cursor, total_count, ...}.
    Rows carry Id, Name, Os, Priority, Status ({Id, Name}), DistributedOn, Checks, Scripts and Plugins (each enabled or switched off) and Tags (AllOfTagIds, AnyOfTagIds, ExcludedTagIds: ids from list_asset_tags, they decide which agents get the policy). Policies set on a single agent as an override are not listed. get_policy reads one by id.
    Resolve ids first: none.
    Side effects: None (read-only).
    Paging: pass next_cursor as cursor, same filters (os), until has_more is false.
    """
    token = non_empty("cursor", cursor)
    size = clamp_page_size(page_size)
    page = await client_of(ctx).get_page(
        LIST_POLICIES_OP, query={"Os": os}, page_size=size, cursor=token, tool="list_policies"
    )
    return paged_result(page, {"os": os})


@gorelo_tool(toolset="core", kind="read", ops=[GET_POLICY_OP], field_map=POLICY_ID_FIELD_MAP)
async def get_policy(
    ctx: Context,
    policy_id: Annotated[StrictId, Field(description="Policy id: the Id of a list_policies row.")],
) -> dict:
    """Get one monitoring policy by id; returns the same fields as a list_policies row (Checks, Scripts, Plugins and Tags included).
    An unknown id is a 404.
    Resolve ids first: policy_id -> list_policies.
    Side effects: None (read-only).
    """
    pid = positive_id("policy_id", policy_id)
    record = await client_of(ctx).get_one(GET_POLICY_OP, path_params={"policyId": pid}, tool="get_policy")
    return expect_object(record, GET_POLICY_OP, tool="get_policy")


@gorelo_tool(toolset="core", kind="read", ops=[ASSET_TAGS_OP])
async def list_asset_tags(ctx: Context) -> dict:
    """List the account's asset tags; returns {items, count}, each item {Id, Name, Type, Conditions, Description, CreatedOn, UpdatedOn}. Not paged; New first, then by name.
    These ids are the ones the Tags of a policy carry (list_policies, get_policy); list_agent_tags gives the tags of one agent. A client or location tag is named after its client.
    Resolve ids first: none.
    Side effects: None (read-only).
    """
    rows = await client_of(ctx).get_list(ASSET_TAGS_OP, tool="list_asset_tags")
    return list_result(rows)


@gorelo_tool(toolset="core", kind="read", ops=[TYPES_OP])
async def list_asset_types(ctx: Context) -> dict:
    """List the asset types; returns {items, count}, each item {Id, Name}. Not paged.
    The Id is the Type.Id of agent and custom assets and the hardware_type_id that create_custom_asset and update_custom_asset take.
    """
    rows = await client_of(ctx).get_list(TYPES_OP, tool="list_asset_types")
    return list_result(rows)


def _type_read_failure(err: Exception) -> str:
    """Why the read of the type list failed, worded about the READ (the write it follows has already succeeded)."""
    if isinstance(err, GoreloAPIError):
        if err.kind == "timeout":
            return f"{TYPES_OP} timed out"
        if err.kind == "transport":
            return f"the connection to Gorelo failed during {TYPES_OP}"
        if err.status is not None:
            return f"{TYPES_OP} answered HTTP {err.status}"
    return str(err)


async def _type_row(ctx: Context, type_id: Any, tool: str) -> dict[str, Any]:
    """The row of a type that was just written, found by Id in GET /v1/assets/types (there is no GET of one type).

    A list that cannot be read, or that does not show the type, is returned as {Id, warning}, never raised: the write is done."""
    try:
        rows = await client_of(ctx).get_list(TYPES_OP, tool=tool)
    except (GoreloAPIError, SpecViolation) as err:
        reason = f"re-reading it failed: {_type_read_failure(err)}"
    else:
        for row in rows:
            if isinstance(row, dict) and row.get("Id") == type_id:
                return row
        reason = f"the list of asset types does not show type {type_id}"
    return {
        "Id": type_id,
        "warning": f"the write succeeded; {reason}. Do not repeat the write; check with list_asset_types.",
    }


def _type_answer(data: Any, op_key: str, tool: str, expected: int | None = None) -> int:
    """The Id of an asset type write's answer ({Id}); when `expected` is given the answer must carry that same Id."""
    answered = created_id(data, op_key, tool=tool)
    if expected is not None and answered != expected:
        raise GoreloAPIError(
            f"{op_key}: Gorelo reported success for asset type {expected} but answered with Id {answered}; "
            "check both with list_asset_types",
            status=200,
            op_key=op_key,
            kind="shape",
            write_unconfirmed=True,
        )
    return answered


async def _name_taken_error(ctx: Context, err: GoreloAPIError, name: str) -> ToolError:
    """The error of a create that answered 409: Gorelo's own text plus the Id of the type that already has the name.

    Gorelo puts that Id in DataContext.ExistingId, which GoreloAPIError does not keep, so the type list is read instead."""
    text = format_gorelo_error(err, "create_asset_type", CREATE_TYPE_MAP)
    trace = f" [trace {err.trace_id}]" if err.trace_id else ""
    head = text[: -len(trace)] if trace and text.endswith(trace) else text
    try:
        rows = await client_of(ctx).get_list(TYPES_OP, tool="create_asset_type")
    except (GoreloAPIError, SpecViolation):
        extra = "A type with this name exists already; find its Id with list_asset_types"
    else:
        wanted = name.strip().casefold()
        same = [
            row for row in rows
            if isinstance(row, dict) and isinstance(row.get("Name"), str) and row["Name"].strip().casefold() == wanted
            and isinstance(row.get("Id"), int)
        ]
        if len(same) == 1:
            extra = (
                f"The asset type with this name already exists: Id {same[0]['Id']} ({same[0]['Name']}); use that id as "
                "hardware_type_id, or choose another name"
            )
        else:
            extra = "No listed asset type has exactly this name; look for it with list_asset_types"
    return ToolError(f"{head.rstrip('. ')}. {extra}.{trace}")


@gorelo_tool(toolset="core", kind="write", ops=[CREATE_TYPE_OP, TYPES_OP], field_map=CREATE_TYPE_MAP)
async def create_asset_type(
    ctx: Context,
    name: Annotated[str, Field(description=f"Type name, not blank, at most {TYPE_NAME_MAX} characters; not the name of another asset type.")],
) -> dict:
    """Create an asset type; returns its row {Id, Name} (read back from the type list), or {Id, warning} if the read-back fails: then do not repeat the create; check with list_asset_types.
    The new type can be given to agents (update_agent) and to custom assets (create_custom_asset, update_custom_asset) as hardware_type_id. A name already used by another type is a 409 and the error names the Id of the existing type. update_asset_type renames one.
    Side effects: adds a type to the account's list of asset types.
    """
    label = _bounded_text("name", name, TYPE_NAME_MAX)
    if label is None:
        raise ValueError("name: required")
    body = build_body({"name": label}, CREATE_TYPE_MAP)
    try:
        data = await client_of(ctx).post(CREATE_TYPE_OP, json_body=body, tool="create_asset_type")
    except GoreloAPIError as err:
        if err.status == HTTP_CONFLICT and not err.write_unconfirmed:
            raise await _name_taken_error(ctx, err, label) from err
        raise
    new_id = _type_answer(data, CREATE_TYPE_OP, "create_asset_type")
    return await _type_row(ctx, new_id, "create_asset_type")


@gorelo_tool(
    toolset="core", kind="write", ops=[UPDATE_TYPE_OP, TYPES_OP], field_map=UPDATE_TYPE_MAP, destructive_hint=True
)
async def update_asset_type(
    ctx: Context,
    asset_type_id: Annotated[StrictId, Field(description="Asset type id (list_asset_types).")],
    name: Annotated[str, Field(description=f"New name, not blank, at most {TYPE_NAME_MAX} characters; not the name of another asset type.")],
) -> dict:
    """Rename one asset type; returns its row {Id, Name} (read back from the type list), or {Id, warning} if the read-back fails.
    A built-in (default) type cannot be renamed (409), a name used by another type is a 400 on name, and sending the name it already has is a no-op. The name cannot be cleared.
    Resolve ids first: asset_type_id -> list_asset_types.
    Side effects: overwrites the type's name.
    """
    type_id = positive_id("asset_type_id", asset_type_id)
    label = _bounded_text("name", name, TYPE_NAME_MAX)
    if label is None:
        raise ValueError("name: required")
    body = build_body({"name": label}, CREATE_TYPE_MAP)
    data = await client_of(ctx).patch(
        UPDATE_TYPE_OP, path_params={"assetTypeId": type_id}, json_body=body, tool="update_asset_type"
    )
    _type_answer(data, UPDATE_TYPE_OP, "update_asset_type", expected=type_id)
    return await _type_row(ctx, type_id, "update_asset_type")


@gorelo_tool(toolset="core", kind="destructive", ops=[DELETE_TYPE_OP], field_map=DELETE_TYPE_MAP)
async def delete_asset_type(
    ctx: Context,
    asset_type_id: Annotated[StrictId, Field(description="Asset type id (list_asset_types).")],
    confirm: Annotated[StrictBool, Field(description="Must be true to delete; ask the user first.")] = False,
) -> dict:
    """Delete one asset type by Id; returns Gorelo's {Id}.
    Refused (409) while any agent or custom asset still has the type: give them another type first (update_agent, update_custom_asset). Gorelo's default types can never be deleted (409). A deleted type is no longer listed and cannot be given to an asset; deleting it again is a 404.
    Side effects: removes the type from the account; no tool restores it.
    Ask the user first; needs confirm=true.
    """
    type_id = positive_id("asset_type_id", asset_type_id)
    require_confirm(
        confirm,
        action=f"delete asset type {type_id}",
        effect=(
            "The type is removed from the account's asset types; Gorelo refuses (409) while an agent or custom asset "
            "has it and never deletes a default type. Nothing has been sent to Gorelo."
        ),
    )
    data = await client_of(ctx).delete(DELETE_TYPE_OP, path_params={"assetTypeId": type_id}, tool="delete_asset_type")
    _type_answer(data, DELETE_TYPE_OP, "delete_asset_type", expected=type_id)
    return {"Id": type_id}


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


def _written_row(
    row: dict[str, Any], written_id: Any, *, noun: str = "asset", reader: str = "get_custom_asset"
) -> dict[str, Any]:
    """The read-back of a write, checked against the id that was written.

    A {Id, warning} pair (the read-back failed) is returned unchanged, and so is a record whose Id, compared as lower-case
    text, is the written id. An empty record, or a record whose Id is missing or is another record's, is not the one that
    was written: it becomes {Id, warning} naming what came back, so the tool never presents it as the result of the write.
    noun and reader name the record and the tool that reads it ("agent" and "get_agent" for update_agent)."""
    if set(row) == {"Id", "warning"}:
        return row
    if row and str(row.get("Id")).lower() == str(written_id).lower():
        return row
    if not row:
        found = "an empty object"
    elif row.get("Id") is None:
        found = "a record without an Id"
    else:
        found = f"the record of {noun} {row['Id']}"
    return {
        "Id": written_id,
        "warning": (
            f"the write succeeded; re-reading it returned no usable record of this {noun} ({found}). "
            f"Do not repeat the write; check with {reader}."
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


def _clear_names(clear_fields: list[str] | None, allowed: tuple[str, ...] = CUSTOM_CLEARABLE) -> list[str]:
    """The distinct names in clear_fields, each one a field Gorelo clears with null. [] is an error."""
    if clear_fields is None:
        return []
    non_empty("clear_fields", clear_fields)
    for index, name in enumerate(clear_fields):
        if not isinstance(name, str):
            raise ValueError(f"clear_fields[{index}]: expected a field name such as \"description\", got {describe_value(name)}")
    names = list(dict.fromkeys(clear_fields))
    for name in names:
        if name not in allowed:
            raise ValueError(f"clear_fields: {name!r} cannot be cleared; allowed: {', '.join(allowed)}")
    return names
