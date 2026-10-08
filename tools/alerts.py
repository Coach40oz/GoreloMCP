"""Alerts: post an alert, list alerts (toolset "core").

Tools:

    post_alert   write  POST /v1/alerts
    list_alerts  read   GET /v1/alerts

Ops owned by this module:

    POST /v1/alerts
    GET /v1/alerts

POST /v1/alerts (no trailing slash; the legacy tool used /alerts/) answers 200 with Data true. Data is
a boolean, so post_alert returns {"ok": true} instead of the bare boolean that used to break the MCP
serialization. Resource is required here as in the spec (the legacy tool made it optional), and so is
Severity: the spec only requires ClientId, Name and Resource, but a severity nobody chose would be an
invented default, so the tool asks for one. Severity is a STRICT integer: JSON true or "2" is refused
instead of becoming a level, because an alert cannot be taken back.

Severity is Gorelo's AlertSeverity (contract e15cb5a18ec2, 2026-10-02): 1 Critical, 2 Error, 3 Warning,
4 Information. It used to be an AlertLevel enum of the same four numbers without names. Alerts cannot be
deleted through the API, so a mistaken alert can only be dealt with in the Gorelo app.

GET /v1/alerts (new in the same contract, paged like the other 18) lists the alerts of the provider, including
the ones post_alert raised, so a post whose outcome is unknown CAN now be checked by a read. Until 2026-10-02 it
could not, which is why every such outcome (timeout, connection failure, 5xx, an unusable answer, Data other than
true) is still reported with alert-specific text instead of the generic "verify with a read" advice: the alert may
already exist, so it must not be posted again until list_alerts has been looked at, and the text says how to look.
One trap is named in that text: a check for an alert that post_alert raised must not filter by client. The spec
says an alert type that carries no client id (Uptime, External and Script alerts) never matches the ClientIds
filter. Production disagrees (live, 2026-10-04): a Script alert whose ClientId was null matched through its device's
client. Nothing was observed for Uptime or External alerts, and an alert raised by post_alert is sent with no device
id, so the advice stays as it is. The client_ids text of list_alerts gives the model both facts: Uptime and External
alerts have no ClientId, so the filter may leave them out (a hedge, not a promise: nothing was observed), and a Script
alert without one matched through its device (the spec text is only pinned in the tests as a fact about the spec).
The filters are StatusIds (new, ignored, ticketed), TypeIds (the scale published in the parameter text
of the spec, 0 for a type it cannot name), ClientIds, DeviceIds, CreatedSince (at or after), CreatedBefore
(strictly before) and SortOrder (desc, newest first, is the default). DismissedOn and DismissedBy are set only
while an alert is dismissed.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field, Strict

from gorelo_client import GoreloAPIError
from tools._common import (
    StrictId,
    build_body,
    clamp_page_size,
    client_of,
    csv_ids,
    describe_value,
    gorelo_tool,
    guids,
    non_empty,
    ok_result,
    paged_result,
    positive_id,
    positive_ids,
    utc_iso,
)

POST_OP = "POST /v1/alerts"
LIST_OP = "GET /v1/alerts"

# snake_case tool parameter -> PostAlertRequest field.
FIELD_MAP = {
    "name": "Name",
    "client_id": "ClientId",
    "resource": "Resource",
    "severity": "Severity",
    "description": "Description",
}

# AlertSeverity: the four values Gorelo accepts, with the names it publishes.
SEVERITY_NAMES = {1: "Critical", 2: "Error", 3: "Warning", 4: "Information"}
SEVERITIES = tuple(SEVERITY_NAMES)
SEVERITY_HELP = "1 Critical, 2 Error, 3 Warning, 4 Information"

# GET /v1/alerts StatusIds: "New=1, Ignored=2, Ticketed=3. 0 (Unknown) matches any other status." The tool offers the
# three named statuses as words; 0 is not offered, because it only matches a status this scale cannot name.
ALERT_STATUS_IDS = {"new": 1, "ignored": 2, "ticketed": 3}
STATUS_NAMES = ", ".join(repr(name) for name in ALERT_STATUS_IDS)
# GET /v1/alerts TypeIds, as the spec text of that parameter lists them. "7-17 are device-check alerts, identified by
# their check type rather than by how the alert was raised. 18-20 carry no device or check; Contract and Domain carry a
# subject id, Warranty does not. 0 (Unknown) matches an alert this scale cannot name."
ALERT_TYPES = {
    0: "Unknown",
    1: "Uptime",
    2: "API",
    3: "Script",
    4: "External",
    5: "Slide",
    6: "Huntress",
    7: "Error Event Log",
    8: "Disk Usage",
    9: "Connectivity",
    10: "Process",
    11: "Service",
    12: "Antivirus",
    13: "Redfish",
    14: "CPU",
    15: "Memory",
    16: "Ping",
    17: "Windows Updates",
    18: "Warranty Expiration",
    19: "Domain Expiration",
    20: "Contract Expiration",
}
TYPE_HELP = ", ".join(f"{name}={key}" for key, name in ALERT_TYPES.items() if key)

# {tool param: Gorelo query name} for every filter of GET /v1/alerts, plus the paging names. The map also turns the
# PropertyName of a Gorelo 400 back into the snake_case param in error messages.
FILTER_FIELDS = {
    "status": "StatusIds",
    "type_ids": "TypeIds",
    "client_ids": "ClientIds",
    "device_ids": "DeviceIds",
    "created_since": "CreatedSince",
    "created_before": "CreatedBefore",
    "sort_order": "SortOrder",
}
LIST_FIELD_MAP = {**FILTER_FIELDS, "page_size": "PageSize", "cursor": "Cursor"}


@gorelo_tool(toolset="core", kind="write", ops=[POST_OP], field_map=FIELD_MAP)
async def post_alert(
    ctx: Context,
    name: Annotated[str, Field(description="Alert title.")],
    client_id: Annotated[StrictId, Field(description="Client id (list_clients).")],
    resource: Annotated[str, Field(description="What the alert is about, e.g. a host name.")],
    severity: Annotated[int, Strict(), Field(description=f"{SEVERITY_HELP}.")],
    description: Annotated[str | None, Field(description="Free-text details.")] = None,
) -> dict:
    """Post an external alert for a client's resource; returns {"ok": true} when Gorelo accepts it.
    Severity: 1 Critical, 2 Error, 3 Warning, 4 Information. Side effects: per the tenant's alert rules it may open a ticket or notify technicians; posting twice may notify twice. Alerts cannot be deleted through the API, so a mistake is fixed in the Gorelo app. If a call fails or times out, do not post it again until list_alerts (newest first, no client_ids filter: external alerts carry no client id) shows whether it arrived; ask the user first.
    """
    non_empty("name", name)
    non_empty("resource", resource)
    cid = positive_id("client_id", client_id)
    level = _severity(severity)
    body = build_body(
        {"name": name, "client_id": cid, "resource": resource, "severity": level, "description": description},
        FIELD_MAP,
    )
    try:
        data = await client_of(ctx).post(POST_OP, json_body=body, tool="post_alert")
    except GoreloAPIError as err:
        if err.write_unconfirmed:
            raise _not_confirmed(_how_it_failed(err), err.trace_id) from err
        raise
    if data is not True:
        raise _not_confirmed(f"Gorelo answered success but its Data was {describe_value(data)}, not true", None)
    return ok_result(data)


def _severity(value: Any) -> int:
    """The alert severity: 1 Critical, 2 Error, 3 Warning or 4 Information (AlertSeverity)."""
    if isinstance(value, bool) or value not in SEVERITIES:
        shown = value if isinstance(value, int) and not isinstance(value, bool) else describe_value(value)
        raise ValueError(f"severity: must be a whole number from 1 to 4 ({SEVERITY_HELP}), got {shown}")
    return value


def _how_it_failed(err: GoreloAPIError) -> str:
    """One phrase for what happened to a post whose outcome is unknown (never quotes the request)."""
    if err.kind == "timeout":
        return "the request timed out"
    if err.kind == "transport":
        return "the connection failed"
    if err.status is not None and err.status >= 500:
        notes = [str(note["message"]) for note in err.notifications[:3] if note.get("message")]
        return f"Gorelo answered HTTP {err.status}" + (f": {'; '.join(notes)}" if notes else "")
    return "its answer could not be used"


def _not_confirmed(how: str, trace_id: str | None) -> ToolError:
    """The alert-specific error for a post that may or may not have been applied.

    The generic text says "verify with a read before retrying" and names no read. Here the read is list_alerts, and
    it has a trap the generic text cannot warn about: an external alert carries no client id, and the spec says the
    client_ids filter never matches such an alert (live, a Script alert without a ClientId did match through its
    device's client on 2026-10-04, but an alert raised by post_alert is sent with no device id, and nothing was
    observed for External alerts). So the text says the alert may already be posted, which read to use and how (newest
    first, no client filter), and that the user is asked before the post is repeated.
    """
    trace = f" [trace {trace_id}]" if trace_id else ""
    return ToolError(
        f"Gorelo did not confirm post_alert ({how}). The alert may already be posted. Before posting it again, check "
        "list_alerts (newest first, created_since a few minutes before this call, no client_ids filter: external "
        f"alerts carry no client id) and ask the user first.{trace}"
    )


# --------------------------------------------------------------------------
# list_alerts
# --------------------------------------------------------------------------


def _status_filter(values: Any) -> list[str] | None:
    """status as a list of distinct names in the caller's order, or None when not given."""
    if values is None:
        return None
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise ValueError(
            f"status: expected a list such as ['new', 'ticketed'], got {describe_value(values)}; statuses are {STATUS_NAMES}"
        )
    if not values:
        raise ValueError(f"status: must contain at least one of {STATUS_NAMES} (omit it for every status)")
    for item in values:
        if not isinstance(item, str) or item not in ALERT_STATUS_IDS:
            raise ValueError(f"status: expected one of {STATUS_NAMES}, got {describe_value(item)}")
    return list(dict.fromkeys(values))


def _type_filter(values: Any) -> list[int] | None:
    """type_ids on the published scale (0 to 20), distinct, in the caller's order. An id off the scale would come back
    as a silent empty page, so it is an error naming the scale."""
    if values is None:
        return None
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise ValueError(f"type_ids: expected a list of ids such as [1, 4], got {describe_value(values)}")
    if not values:
        raise ValueError("type_ids: expected at least one id, got an empty list (omit type_ids if you have none to give)")
    for position, item in enumerate(values):
        if isinstance(item, bool) or not isinstance(item, int) or item not in ALERT_TYPES:
            shown = item if isinstance(item, int) and not isinstance(item, bool) else describe_value(item)
            raise ValueError(
                f"type_ids[{position}]: {shown} is not an alert type id; valid ids: 0 Unknown, "
                + ", ".join(f"{key} {name}" for key, name in ALERT_TYPES.items() if key)
            )
    return list(dict.fromkeys(values))


def _sort_order(value: Any) -> str:
    if value not in ("desc", "asc"):
        raise ValueError(f"sort_order: must be 'desc' (newest first) or 'asc' (oldest first), got {describe_value(value)}")
    return value


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@gorelo_tool(toolset="core", kind="read", ops=[LIST_OP], field_map=LIST_FIELD_MAP)
async def list_alerts(
    ctx: Context,
    status: Annotated[
        list[Literal["new", "ignored", "ticketed"]] | None, Field(description="Omit for every status.")
    ] = None,
    type_ids: Annotated[
        list[StrictId] | None, Field(description="Alert type ids, named in the tool text. 0 matches a type with no name.")
    ] = None,
    client_ids: Annotated[
        list[StrictId] | None,
        Field(
            description="From list_clients. Uptime and External alerts have no ClientId, so this filter may leave them out; "
            "a Script alert without one matched through its device."
        ),
    ] = None,
    device_ids: Annotated[list[str] | None, Field(description="Device GUIDs (list_agents).")] = None,
    created_since: Annotated[str | None, Field(description="At or after this instant (UTC offset required).")] = None,
    created_before: Annotated[str | None, Field(description="Strictly before this instant (UTC offset required).")] = None,
    sort_order: Annotated[Literal["desc", "asc"], Field(description="desc: newest first. asc: oldest first.")] = "desc",
    page_size: Annotated[int, Field(description="1-200, clamped.")] = 50,
    cursor: Annotated[
        str | None, Field(description="next_cursor from the previous call; same filters and sort; repeat until has_more is false.")
    ] = None,
) -> dict:
    """List alerts raised in Gorelo, including those sent with post_alert, one page at a time: newest first unless sort_order is asc.
    Alerts cannot be deleted. DismissedOn and DismissedBy are set only while an alert is dismissed.
    type_ids: Uptime=1, API=2, Script=3, External=4, Slide=5, Huntress=6, Error Event Log=7, Disk Usage=8, Connectivity=9, Process=10, Service=11, Antivirus=12, Redfish=13, CPU=14, Memory=15, Ping=16, Windows Updates=17, Warranty Expiration=18, Domain Expiration=19, Contract Expiration=20 (7-17 are device checks; 0 matches a type with no name).
    """
    names = _status_filter(status)
    types = _type_filter(type_ids)
    since = utc_iso("created_since", created_since)
    before = utc_iso("created_before", created_before)
    if since is not None and before is not None and _instant(since) >= _instant(before):
        raise ValueError(
            "created_since: must be earlier than created_before (created_since is inclusive, created_before is "
            "exclusive), so no alert could match"
        )
    filters = {
        "status": names,
        "type_ids": types,
        "client_ids": positive_ids("client_ids", client_ids),
        "device_ids": guids("device_ids", device_ids),
        "created_since": since,
        "created_before": before,
        "sort_order": _sort_order(sort_order),
    }
    wire = {
        "StatusIds": None if names is None else csv_ids("status", [ALERT_STATUS_IDS[name] for name in names]),
        "TypeIds": csv_ids("type_ids", types),
        "ClientIds": csv_ids("client_ids", filters["client_ids"]),
        "DeviceIds": csv_ids("device_ids", filters["device_ids"]),
        "CreatedSince": since,
        "CreatedBefore": before,
        "SortOrder": filters["sort_order"],
    }
    page = await client_of(ctx).get_page(
        LIST_OP,
        query=wire,
        page_size=clamp_page_size(page_size),
        cursor=non_empty("cursor", cursor),
        tool="list_alerts",
    )
    return paged_result(page, filters)
