"""Uptime checks (toolset "uptime").

Tools:

    list_uptime_checks      read         GET /v1/uptime
    get_uptime_check        read         GET /v1/uptime/{checkId}
    create_uptime_check     write        POST /v1/uptime, GET /v1/uptime/{checkId}
    update_uptime_check     write        PATCH /v1/uptime/{checkId}, GET /v1/uptime/{checkId}
    set_uptime_maintenance  write        PATCH /v1/uptime/{checkId}, GET /v1/uptime/{checkId}
    delete_uptime_check     destructive  DELETE /v1/uptime/{checkId}

Ops owned by this module:

    GET /v1/uptime
    GET /v1/uptime/{checkId}
    POST /v1/uptime
    PATCH /v1/uptime/{checkId}
    DELETE /v1/uptime/{checkId}

Check types: 1 ICMP (target ip), 2 HTTP (target url), 3 TCP (target ip and port). Regions: 1 Seattle,
2 Sydney, 3 UK, 4 Frankfurt. The ip of an ICMP or TCP check is a literal IP address, never a host name.
A new check starts monitoring at once. POST and PATCH answer with only the check Id, so the write tools
read the check back with reread_after_write. update_uptime_check never sends MaintenanceMode and
set_uptime_maintenance sends nothing else: they share PATCH /v1/uptime/{checkId} but change disjoint parts of
the check. TagIds replace the whole list; an empty list (every tag removed) is only sent through
clear_tags=true. The Target of a PATCH is always sent complete: when the caller changes only part of it
(for example the port of a TCP check) update_uptime_check reads the check first, validates the given
fields against its type and fills the rest from the stored target. set_uptime_maintenance with enabled=true
requires start and duration_minutes (0 is allowed and means the window never expires): a window nobody gave a
length would be an invented default, and one that never expires hides outages until somebody ends it. Gorelo
also refuses a window without a start (live, 2026-10-02: 400 "MaintenanceMode.StartDateTime is required when
enabling maintenance mode."), and the tool never reads the clock for the caller: for maintenance that should
begin now the model passes the current time, and only when the user wants it now. With enabled=false every
other field is refused.
"""

import ipaddress
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from fastmcp import Context
from pydantic import Field

from gorelo_client import GoreloAPIError
from tools._common import (
    StrictBool,
    StrictId,
    build_body,
    clamp_page_size,
    client_of,
    created_id,
    csv_ids,
    expect_object,
    gorelo_tool,
    guid,
    non_empty,
    paged_result,
    positive_id,
    positive_ids,
    reread_after_write,
    require_confirm,
    utc_iso,
)

LIST_OP = "GET /v1/uptime"
GET_OP = "GET /v1/uptime/{checkId}"
CREATE_OP = "POST /v1/uptime"
UPDATE_OP = "PATCH /v1/uptime/{checkId}"
DELETE_OP = "DELETE /v1/uptime/{checkId}"

CHECK_TYPE_IDS = {"icmp": 1, "http": 2, "tcp": 3}
CHECK_TYPE_NAMES = {type_id: name for name, type_id in CHECK_TYPE_IDS.items()}
REGION_IDS = {"seattle": 1, "sydney": 2, "uk": 3, "frankfurt": 4}
# The Target fields a check of each type takes. Gorelo rejects a field of another type ("rather than
# ignoring it"), so the tools refuse it before sending anything.
TARGET_FIELDS_BY_TYPE = {"icmp": ("ip",), "http": ("url",), "tcp": ("ip", "port")}
TARGET_HINTS = {
    "icmp": "ip is the IP address to ping",
    "http": "url is the address to request, such as https://example.com/health",
    "tcp": "ip is the IP address to connect to and port the TCP port",
}
CHECK_LABELS = {"icmp": "an ICMP check", "http": "an HTTP check", "tcp": "a TCP check"}

QUERY_MAX_CHARS = 200
DESCRIPTION_MAX_CHARS = 250
ISP_LINK_MAX_CHARS = 500
REASON_MAX_CHARS = 500
INT32_MAX = 2**31 - 1

# {tool param: Gorelo query name} for every filter of GET /v1/uptime. The maps below also turn the
# PropertyName of a Gorelo error back into the snake_case param in error messages.
FILTER_FIELDS = {
    "client_ids": "ClientIds",
    "check_types": "TypeIds",
    "tag_ids": "TagIds",
    "query": "Query",
}
LIST_FIELD_MAP = {**FILTER_FIELDS, "page_size": "PageSize", "cursor": "Cursor"}

# Body fields that CreateUptimeCheckCommand and UpdateUptimeCheckCommand share.
CHECK_BODY = {
    "check_type": "TypeId",
    "frequency_minutes": "Frequency",
    "region": "RegionId",
    "ip": "Target.Ip",
    "url": "Target.Url",
    "port": "Target.Port",
    "client_id": "ClientId",
    "location_id": "LocationId",
    "description": "Description",
    "retries_after_failure": "NumberOfRetriesAfterFailure",
    "isp_connection_link": "IspConnectionLink",
    "tag_ids": "TagIds",
    "adopt_client_assets": "AdoptClientAssets",
}
# set_uptime_maintenance sends only this nested object and update_uptime_check never does.
MAINTENANCE_BODY = {
    "enabled": "MaintenanceMode.Enabled",
    "start": "MaintenanceMode.StartDateTime",
    "duration_minutes": "MaintenanceMode.DurationInMinutes",
    "reason": "MaintenanceMode.Reason",
}

_HTTP_URL = re.compile(r"https?://\S+", re.IGNORECASE)


# --------------------------------------------------------------------------
# Local validation helpers (each raises ValueError naming the snake_case param)
# --------------------------------------------------------------------------


def _pick(param: str, value: str | None, table: Mapping[str, int]) -> int | None:
    """The Gorelo id for a named choice. An unknown name is an error, never a silently dropped value."""
    if value is None:
        return None
    if value not in table:
        raise ValueError(f"{param}: must be one of {', '.join(repr(name) for name in table)}, got {value!r}")
    return table[value]


def _pick_many(param: str, values: list[str] | None, table: Mapping[str, int]) -> list[int] | None:
    """Gorelo ids for a list of named choices, each once, in the order given."""
    if values is None:
        return None
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise ValueError(f"{param}: expected a list such as {list(table)[:2]}")
    if not values:
        raise ValueError(f"{param}: the list must contain at least one value (omit {param} to apply no filter)")
    return list(dict.fromkeys(_pick(param, value, table) for value in values))


def _limited_text(param: str, value: str | None, limit: int) -> str | None:
    value = non_empty(param, value)
    if value is not None and len(value) > limit:
        raise ValueError(f"{param}: at most {limit} characters")
    return value


def _whole(param: str, value: Any, *, minimum: int, maximum: int = INT32_MAX) -> int | None:
    """A whole number in a range; a bool is not a number here."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise ValueError(f"{param}: must be a whole number from {minimum} to {maximum}")
    return value


def _required_whole(param: str, value: Any, *, minimum: int) -> int:
    """A whole number that must be given."""
    if value is None:
        raise ValueError(f"{param}: is required")
    return _whole(param, value, minimum=minimum)  # type: ignore[return-value]


def _opt_id(param: str, value: Any) -> int | None:
    """An id that may be left out: None stays None, anything else must be a positive whole number."""
    return None if value is None else positive_id(param, value)


def _tag_ids(param: str, value: Any, *, if_empty: str) -> list[int] | None:
    """Tag ids as a list of positive ints, each once. [] is refused with `if_empty` as the reason."""
    if isinstance(value, (list, tuple)) and not value:
        raise ValueError(f"{param}: {if_empty}")
    ids = positive_ids(param, value)
    return None if ids is None else list(dict.fromkeys(ids))


def _ip_address(value: Any) -> str | None:
    """The ip of an icmp or tcp check: a literal IPv4 or IPv6 address, sent as given. Host names are refused."""
    if value is None:
        return None
    text = non_empty("ip", value)
    try:
        ipaddress.ip_address(text)
    except ValueError:
        raise ValueError(
            "ip: must be a literal IP address (IPv4 or IPv6), not a host name; ask the user for the address"
        ) from None
    return text


def _http_url(value: Any) -> str | None:
    if value is None:
        return None
    text = non_empty("url", value)
    if not _HTTP_URL.fullmatch(text):
        raise ValueError("url: must be a full address starting with http:// or https://, such as https://example.com/health")
    return text


def _type_name(value: Any, *, required: bool) -> str | None:
    """A check type name that Gorelo knows (icmp, http, tcp)."""
    if value is None:
        if required:
            raise ValueError("check_type: is required")
        return None
    if value not in CHECK_TYPE_IDS:
        raise ValueError(f"check_type: must be one of {', '.join(repr(name) for name in CHECK_TYPE_IDS)}, got {value!r}")
    return value


def _loose_target(*, ip: Any, url: Any, port: Any) -> None:
    """Without check_type the stored type is unknown, but a URL never shares a target with an address or port."""
    if url is not None and (ip is not None or port is not None):
        other = " and ".join(name for name, value in (("ip", ip), ("port", port)) if value is not None)
        raise ValueError(
            f"url: cannot be combined with {other}; a check targets either a URL (http) or an address (icmp, tcp)"
        )


def _check_target(check_type: str, *, ip: Any, url: Any, port: Any) -> None:
    """Every Target field the check type needs is present and none of another type's is."""
    takes = TARGET_FIELDS_BY_TYPE[check_type]
    given = {"ip": ip, "url": url, "port": port}
    missing = [name for name in takes if given[name] is None]
    if missing:
        raise ValueError(f"{', '.join(missing)}: required for {CHECK_LABELS[check_type]} ({TARGET_HINTS[check_type]})")
    extra = [name for name, value in given.items() if value is not None and name not in takes]
    if extra:
        raise ValueError(
            f"{', '.join(extra)}: not valid for {CHECK_LABELS[check_type]}, which takes {' and '.join(takes)}; omit it"
        )


def _client_needs_location(client_id: Any, location_id: Any) -> None:
    if client_id is not None and location_id is None:
        raise ValueError(
            "location_id: required together with client_id (a location belongs to a client, and Gorelo refuses "
            "a client without its location)"
        )


def _stored_type(record: Mapping[str, Any]) -> str:
    """The type name (icmp, http, tcp) of a check as read from Gorelo, matched on Type.Id and never on Name."""
    type_info = record.get("Type")
    type_id = type_info.get("Id") if isinstance(type_info, Mapping) else None
    name = None if isinstance(type_id, bool) or not isinstance(type_id, int) else CHECK_TYPE_NAMES.get(type_id)
    if name is None:
        raise GoreloAPIError(
            f"{GET_OP}: the check has no readable Type.Id, so its target cannot be completed; "
            "refusing to guess. Nothing was changed",
            status=200,
            op_key=GET_OP,
            kind="shape",
        )
    return name


def _complete_target(
    check_type: str, stored: Any, *, ip: str | None, url: str | None, port: int | None
) -> dict[str, Any]:
    """The complete Target of a check of `check_type`: the fields the caller gave, the rest from `stored`.

    The given fields must belong to the type (a local error naming them otherwise). A field the check type
    needs that is neither given nor stored is a local error too: nothing is guessed.
    """
    takes = TARGET_FIELDS_BY_TYPE[check_type]
    given = {"ip": ip, "url": url, "port": port}
    extra = [name for name, value in given.items() if value is not None and name not in takes]
    if extra:
        raise ValueError(
            f"{', '.join(extra)}: not valid for {CHECK_LABELS[check_type]}, which takes {' and '.join(takes)}; omit "
            "it, or pass check_type with the complete target to change the type"
        )
    keys = {"ip": "Ip", "url": "Url", "port": "Port"}
    current = stored if isinstance(stored, Mapping) else {}
    target: dict[str, Any] = {}
    for name in takes:
        value = given[name] if given[name] is not None else current.get(keys[name])
        if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
            raise ValueError(
                f"{name}: the check has no stored {name} to keep; give {' and '.join(takes)} together"
            )
        target[name] = value
    return target


def _to_query(filters: dict[str, Any]) -> dict[str, Any]:
    """Gorelo's query names; a list becomes one comma separated value (csv_ids names the param on error).

    Unset filters stay in the dict as None on purpose: the client checks every NAME before it drops them.
    """
    return {
        FILTER_FIELDS[param]: csv_ids(param, value) if isinstance(value, (list, tuple)) else value
        for param, value in filters.items()
    }


# --------------------------------------------------------------------------
# list_uptime_checks and get_uptime_check
# --------------------------------------------------------------------------


@gorelo_tool(toolset="uptime", kind="read", ops=[LIST_OP], field_map=LIST_FIELD_MAP)
async def list_uptime_checks(
    ctx: Context,
    client_ids: Annotated[list[StrictId] | None, Field(description="From list_clients.")] = None,
    check_types: Annotated[list[Literal["icmp", "http", "tcp"]] | None, Field(description="Types to include.")] = None,
    tag_ids: Annotated[list[StrictId] | None, Field(description="Any of these; see TagIds of checks.")] = None,
    query: Annotated[str | None, Field(description="Description keyword.")] = None,
    page_size: Annotated[int, Field(description="1-200, clamped.")] = 50,
    cursor: Annotated[
        str | None, Field(description="next_cursor from the previous call; same filters; repeat until has_more is false.")
    ] = None,
) -> dict:
    """List uptime checks, one page at a time. Rows: Type 1 ICMP, 2 HTTP, 3 TCP; RegionId 1 Seattle,
    2 Sydney, 3 UK, 4 Frankfurt.
    """
    filters = {
        "client_ids": positive_ids("client_ids", client_ids),
        "check_types": check_types,
        "tag_ids": positive_ids("tag_ids", tag_ids),
        "query": _limited_text("query", query, QUERY_MAX_CHARS),
    }
    wire = _to_query({**filters, "check_types": _pick_many("check_types", check_types, CHECK_TYPE_IDS)})
    page = await client_of(ctx).get_page(
        LIST_OP,
        query=wire,
        page_size=clamp_page_size(page_size),
        cursor=non_empty("cursor", cursor),
        tool="list_uptime_checks",
    )
    return paged_result(page, filters)


@gorelo_tool(toolset="uptime", kind="read", ops=[GET_OP], field_map={"check_id": "checkId"})
async def get_uptime_check(
    ctx: Context,
    check_id: Annotated[str, Field(description="GUID (list_uptime_checks).")],
) -> dict:
    """Get one uptime check by Id: target, status, region, tags and maintenance window."""
    data = await client_of(ctx).get_one(
        GET_OP, path_params={"checkId": guid("check_id", check_id)}, tool="get_uptime_check"
    )
    return expect_object(data, GET_OP, tool="get_uptime_check")


# --------------------------------------------------------------------------
# create_uptime_check
# --------------------------------------------------------------------------


@gorelo_tool(toolset="uptime", kind="write", ops=[CREATE_OP, GET_OP], field_map=CHECK_BODY)
async def create_uptime_check(
    ctx: Context,
    check_type: Annotated[
        Literal["icmp", "http", "tcp"], Field(description="icmp needs ip, http needs url, tcp needs ip and port.")
    ],
    frequency_minutes: Annotated[int, Field(description="Minutes, 1 or more.")],
    region: Annotated[Literal["seattle", "sydney", "uk", "frankfurt"], Field(description="Where it runs.")],
    ip: Annotated[str | None, Field(description="icmp, tcp: literal IP address, not a host name.")] = None,
    url: Annotated[str | None, Field(description="http: http:// or https:// address.")] = None,
    port: Annotated[int | None, Field(description="tcp: 1-65535.")] = None,
    client_id: Annotated[StrictId | None, Field(description="From list_clients; send location_id too.")] = None,
    location_id: Annotated[
        StrictId | None, Field(description="From list_client_locations; needs client_id.")
    ] = None,
    description: Annotated[str | None, Field(description="Free text.")] = None,
    retries_after_failure: Annotated[int | None, Field(description="0 or more.")] = None,
    isp_connection_link: Annotated[str | None, Field(description="ISP status page URL.")] = None,
    tag_ids: Annotated[
        list[StrictId] | None, Field(description="No tag lookup: copy TagIds of other checks.")
    ] = None,
    adopt_client_assets: Annotated[
        bool | None, Field(description="true: move matching unassigned devices to the client.")
    ] = None,
) -> dict:
    """Create an uptime check and return it.
    Side effects: monitoring starts immediately and failures can raise alerts; adopt_client_assets=true
    moves unassigned devices with a matching public IP to the check's client. A failed re-read returns
    {Id, warning}: the check exists, do not create it again.
    """
    kind = _type_name(check_type, required=True)
    _check_target(kind, ip=ip, url=url, port=port)
    _client_needs_location(client_id, location_id)
    body = build_body(
        {
            "check_type": CHECK_TYPE_IDS[kind],
            "frequency_minutes": _required_whole("frequency_minutes", frequency_minutes, minimum=1),
            "region": _pick("region", region, REGION_IDS),
            "ip": _ip_address(ip),
            "url": _http_url(url),
            "port": _whole("port", port, minimum=1, maximum=65535),
            "client_id": _opt_id("client_id", client_id),
            "location_id": _opt_id("location_id", location_id),
            "description": _limited_text("description", description, DESCRIPTION_MAX_CHARS),
            "retries_after_failure": _whole("retries_after_failure", retries_after_failure, minimum=0),
            "isp_connection_link": _limited_text("isp_connection_link", isp_connection_link, ISP_LINK_MAX_CHARS),
            "tag_ids": _tag_ids("tag_ids", tag_ids, if_empty="the list must contain at least one tag id; omit it for no tags"),
            "adopt_client_assets": True if adopt_client_assets is True else None,
        },
        CHECK_BODY,
    )
    written = await client_of(ctx).post(CREATE_OP, json_body=body, tool="create_uptime_check")
    check_id = created_id(written, CREATE_OP, tool="create_uptime_check")
    return await reread_after_write(
        ctx, GET_OP, path_params={"checkId": check_id}, tool="create_uptime_check", written_id=check_id
    )


# --------------------------------------------------------------------------
# update_uptime_check
# --------------------------------------------------------------------------


@gorelo_tool(
    toolset="uptime",
    kind="write",
    ops=[UPDATE_OP, GET_OP],
    field_map={**CHECK_BODY, "check_id": "checkId"},
    destructive_hint=True,
)
async def update_uptime_check(
    ctx: Context,
    check_id: Annotated[str, Field(description="GUID (list_uptime_checks).")],
    check_type: Annotated[
        Literal["icmp", "http", "tcp"] | None, Field(description="Change the type; send its complete target too.")
    ] = None,
    ip: Annotated[str | None, Field(description="icmp, tcp: literal IP address, not a host name.")] = None,
    url: Annotated[str | None, Field(description="http: http:// or https:// address.")] = None,
    port: Annotated[int | None, Field(description="tcp: 1-65535.")] = None,
    client_id: Annotated[StrictId | None, Field(description="From list_clients; send location_id too.")] = None,
    location_id: Annotated[
        StrictId | None, Field(description="From list_client_locations; needs client_id.")
    ] = None,
    description: Annotated[str | None, Field(description="Free text.")] = None,
    frequency_minutes: Annotated[int | None, Field(description="Minutes, 1 or more.")] = None,
    retries_after_failure: Annotated[int | None, Field(description="0 or more.")] = None,
    region: Annotated[Literal["seattle", "sydney", "uk", "frankfurt"] | None, Field(description="Where it runs.")] = None,
    isp_connection_link: Annotated[str | None, Field(description="ISP status page URL.")] = None,
    tag_ids: Annotated[
        list[StrictId] | None, Field(description="The COMPLETE new list; copy TagIds of other checks.")
    ] = None,
    clear_tags: Annotated[bool, Field(description="true: remove every tag (not with tag_ids).")] = False,
    adopt_client_assets: Annotated[
        bool | None, Field(description="true: move matching unassigned devices to the client.")
    ] = None,
) -> dict:
    """Change fields of an uptime check (partial update) and return it; tag_ids replaces the whole list.
    To change the target pass ip, url or port: the check is read first and its complete target is sent.
    At least one change.
    Side effects: overwrites the running check's settings, so monitoring and alerts change at once; a
    region change reschedules it; adopt_client_assets=true moves unassigned devices with a matching public
    IP to the check's client. A failed re-read returns {Id, warning}: the change WAS applied, do not
    repeat it.
    """
    check_guid = guid("check_id", check_id)
    kind = _type_name(check_type, required=False)
    if kind is not None:
        _check_target(kind, ip=ip, url=url, port=port)
    else:
        _loose_target(ip=ip, url=url, port=port)
    _client_needs_location(client_id, location_id)
    if clear_tags and tag_ids is not None:
        raise ValueError("tag_ids: cannot be combined with clear_tags=true; give the new list or clear every tag, not both")
    values = {
        "check_type": _pick("check_type", kind, CHECK_TYPE_IDS),
        "frequency_minutes": _whole("frequency_minutes", frequency_minutes, minimum=1),
        "region": _pick("region", region, REGION_IDS),
        "ip": _ip_address(ip),
        "url": _http_url(url),
        "port": _whole("port", port, minimum=1, maximum=65535),
        "client_id": _opt_id("client_id", client_id),
        "location_id": _opt_id("location_id", location_id),
        "description": _limited_text("description", description, DESCRIPTION_MAX_CHARS),
        "retries_after_failure": _whole("retries_after_failure", retries_after_failure, minimum=0),
        "isp_connection_link": _limited_text("isp_connection_link", isp_connection_link, ISP_LINK_MAX_CHARS),
        "tag_ids": _tag_ids(
            "tag_ids",
            tag_ids,
            if_empty="an empty list is not accepted here; to remove every tag pass clear_tags=true",
        ),
        "adopt_client_assets": adopt_client_assets,
    }
    client = client_of(ctx)
    if kind is None and any(values[name] is not None for name in ("ip", "url", "port")):
        # Gorelo takes the Target as a whole, so a partial change (the port of a TCP check) needs the rest of
        # it. Read the check first: its type says which fields may be given, the stored target fills the rest.
        record = expect_object(
            await client.get_one(GET_OP, path_params={"checkId": check_guid}, tool="update_uptime_check"),
            GET_OP,
            tool="update_uptime_check",
        )
        target = _complete_target(
            _stored_type(record), record.get("Target"), ip=values["ip"], url=values["url"], port=values["port"]
        )
        values.update({"ip": None, "url": None, "port": None, **target})
    body = build_body(
        values,
        CHECK_BODY,
        clear=["tag_ids"] if clear_tags is True else None,
        clear_values={"tag_ids": []},
    )
    if not body:
        raise ValueError("no change requested: give at least one field to change, or clear_tags=true")
    written = await client.patch(UPDATE_OP, path_params={"checkId": check_guid}, json_body=body, tool="update_uptime_check")
    expect_object(written, UPDATE_OP, tool="update_uptime_check")
    return await reread_after_write(
        ctx, GET_OP, path_params={"checkId": check_guid}, tool="update_uptime_check", written_id=check_guid
    )


# --------------------------------------------------------------------------
# set_uptime_maintenance
# --------------------------------------------------------------------------


@gorelo_tool(
    toolset="uptime",
    kind="write",
    ops=[UPDATE_OP, GET_OP],
    field_map={**MAINTENANCE_BODY, "check_id": "checkId"},
    destructive_hint=True,
)
async def set_uptime_maintenance(
    ctx: Context,
    check_id: Annotated[str, Field(description="GUID (list_uptime_checks).")],
    enabled: Annotated[bool, Field(description="true: begin a window; false: end it.")],
    start: Annotated[
        str | None,
        Field(
            description="Required with enabled=true: when the window begins (ISO 8601 with UTC offset). Use the "
            "current time only if the user wants maintenance now."
        ),
    ] = None,
    duration_minutes: Annotated[
        int | None,
        Field(
            description="Required with enabled=true: minutes the window lasts. 0 means no expiry: it never ends "
            "until you end it."
        ),
    ] = None,
    reason: Annotated[str | None, Field(description="Why the check is in maintenance.")] = None,
) -> dict:
    """Start or end a maintenance window on an uptime check and return it. enabled=false ends it and takes
    no other field; with enabled=true start and duration_minutes are required (0 means the window never
    expires) and reason is optional. Pass the current time as start only if the user wants maintenance now.
    Side effects: while a window is active the check's failures stop raising alerts, and one with no
    expiry (duration 0) hides outages until it is ended. A failed re-read returns {Id, warning}: the
    change WAS applied, do not repeat it.
    """
    check_guid = guid("check_id", check_id)
    if not isinstance(enabled, bool):
        raise ValueError("enabled: must be true (begin a window) or false (end it)")
    if not enabled:
        extra = [
            name
            for name, value in (("start", start), ("duration_minutes", duration_minutes), ("reason", reason))
            if value is not None
        ]
        if extra:
            raise ValueError(
                f"{', '.join(extra)}: only valid with enabled=true; ending a window takes no other field"
            )
    elif start is None or duration_minutes is None:
        # No defaults. A window nobody gave a length would be an invented one, and a window that never expires hides
        # outages until somebody ends it, so the choice (including 0) is the caller's, and the user's. Gorelo refuses
        # a window without a start (400 "MaintenanceMode.StartDateTime is required when enabling maintenance mode."),
        # and the tool does not read the clock: the model passes the current time when the user wants it now.
        missing = []
        if start is None:
            missing.append(
                "start: required when enabled=true; ask the user when the window should begin. Pass the current time "
                "(ISO 8601 with a UTC offset, for example 2026-10-02T14:30:00Z) only if the user wants maintenance to "
                "begin now. Gorelo refuses a window without a start"
            )
        if duration_minutes is None:
            missing.append(
                "duration_minutes: required when enabled=true; ask the user how long the window should last, in "
                "minutes. 0 is allowed and means the window never expires (it hides outages until it is ended with "
                "enabled=false)"
            )
        raise ValueError("; ".join(missing))
    body = build_body(
        {
            "enabled": enabled,
            "start": utc_iso("start", start),
            "duration_minutes": _whole("duration_minutes", duration_minutes, minimum=0),
            "reason": _limited_text("reason", reason, REASON_MAX_CHARS),
        },
        MAINTENANCE_BODY,
    )
    written = await client_of(ctx).patch(
        UPDATE_OP, path_params={"checkId": check_guid}, json_body=body, tool="set_uptime_maintenance"
    )
    expect_object(written, UPDATE_OP, tool="set_uptime_maintenance")
    return await reread_after_write(
        ctx, GET_OP, path_params={"checkId": check_guid}, tool="set_uptime_maintenance", written_id=check_guid
    )


# --------------------------------------------------------------------------
# delete_uptime_check
# --------------------------------------------------------------------------


@gorelo_tool(toolset="uptime", kind="destructive", ops=[DELETE_OP], field_map={"check_id": "checkId"})
async def delete_uptime_check(
    ctx: Context,
    check_id: Annotated[str, Field(description="GUID (list_uptime_checks).")],
    confirm: Annotated[StrictBool, Field(description="Must be true; only after the user approves.")] = False,
) -> dict:
    """Delete one uptime check by Id: a soft delete (deactivated, schedule cancelled) that stops monitoring
    its target. Repeating it is safe. Ask the user first; needs confirm=true.
    """
    check_guid = guid("check_id", check_id)
    require_confirm(
        confirm,
        action=f"delete uptime check {check_guid}",
        effect=(
            "The check is deactivated and its monitoring schedule is cancelled, so the target is no longer "
            "monitored. Nothing has been sent to Gorelo."
        ),
    )
    data = await client_of(ctx).delete(DELETE_OP, path_params={"checkId": check_guid}, tool="delete_uptime_check")
    return expect_object(data, DELETE_OP, tool="delete_uptime_check")
