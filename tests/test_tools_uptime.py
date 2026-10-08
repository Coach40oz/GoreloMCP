"""tools/uptime.py: uptime checks and maintenance windows (offline: MockGorelo and an in-process client)."""

import inspect
import json
import logging

import httpx
import pytest
from conftest import (
    TEST_TRACE_ID,
    call_tool,
    call_tool_error,
    envelope,
    error_envelope,
    in_order,
    list_tools,
    make_ctx,
    pagination,
    paged_envelope,
    uid,
)
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool

from tools import uptime
from tools._common import REGISTRY

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _quiet_fastmcp_tracebacks():
    """FastMCP logs every tool error with a Rich traceback (about 100 ms each). These tests read the error
    text the model would see instead, so the logger is silenced for the duration of each test."""
    logger = logging.getLogger("fastmcp")
    level = logger.level
    logger.setLevel(logging.CRITICAL)
    yield
    logger.setLevel(level)


UPTIME = "/v1/uptime"
CHECK_ID = uid(41)
CHECK_PATH = f"/v1/uptime/{CHECK_ID}"
GUID_ERROR = "check_id: expected a GUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b, got "
IP_ERROR = "ip: must be a literal IP address (IPv4 or IPv6), not a host name; ask the user for the address"
POSITIVE = "expected a positive whole number such as 123, got zero or a negative number"


def check(check_id=CHECK_ID, **extra):
    """One UptimeResponse as Gorelo returns it (an ICMP check by default)."""
    record = {
        "Id": check_id,
        "Description": "Main router",
        "Type": {"Id": 1, "Name": "ICMP"},
        "Target": {"Ip": "203.0.113.10", "Port": None, "Url": None},
        "AdoptClientAssets": False,
        "ClientId": 9102,
        "LocationId": 77,
        "Status": {"Id": 1, "Name": "Up"},
        "Frequency": 5,
        "NumberOfRetriesAfterFailure": 2,
        "RegionId": 1,
        "IspConnectionLink": None,
        "TagIds": [3],
        "MaintenanceMode": {"Enabled": False, "StartDateTime": None, "DurationInMinutes": None, "Reason": None},
        "CreatedOn": "2026-09-25T10:00:00Z",
        "UpdatedOn": None,
    }
    record.update(extra)
    return record


def http_check(check_id=CHECK_ID, **extra):
    """An HTTP check as Gorelo returns it."""
    record = {"Type": {"Id": 2, "Name": "HTTP"}, "Target": {"Ip": None, "Port": None, "Url": "https://example.com/health"}}
    return check(check_id, **{**record, **extra})


def tcp_check(check_id=CHECK_ID, **extra):
    """A TCP check as Gorelo returns it."""
    record = {"Type": {"Id": 3, "Name": "TCP"}, "Target": {"Ip": "198.51.100.7", "Port": 22, "Url": None}}
    return check(check_id, **{**record, **extra})


@pytest.fixture
def server(server_factory):
    """Every toolset on and delete tools enabled."""
    return server_factory(destructive=True)


def module_specs():
    return [spec for spec in REGISTRY.specs if spec.fn.__module__ == uptime.__name__]


def tool_def(name, tools):
    return next(tool for tool in tools if tool.name == name)


async def call_directly(client_factory, tool, **kwargs):
    """Call the decorated function itself (no pydantic in front of it): for the checks the schema normally pre-empts."""
    async with client_factory() as client:
        return await tool(make_ctx(client), **kwargs)


def route_create(mock, record=None):
    mock.on("POST", UPTIME, envelope({"Id": CHECK_ID}))
    mock.on("GET", CHECK_PATH, envelope(record if record is not None else check()))


def route_update(mock, record=None):
    mock.on("PATCH", CHECK_PATH, envelope({"Id": CHECK_ID}))
    mock.on("GET", CHECK_PATH, envelope(record if record is not None else check()))


# --------------------------------------------------------------------------
# What the module declares
# --------------------------------------------------------------------------

EXPECTED_TOOLS = {
    "list_uptime_checks": ("read", ["GET /v1/uptime"]),
    "get_uptime_check": ("read", ["GET /v1/uptime/{checkId}"]),
    "create_uptime_check": ("write", ["POST /v1/uptime", "GET /v1/uptime/{checkId}"]),
    "update_uptime_check": ("write", ["PATCH /v1/uptime/{checkId}", "GET /v1/uptime/{checkId}"]),
    "set_uptime_maintenance": ("write", ["PATCH /v1/uptime/{checkId}", "GET /v1/uptime/{checkId}"]),
    "delete_uptime_check": ("destructive", ["DELETE /v1/uptime/{checkId}"]),
}


def test_the_module_declares_exactly_these_tools():
    assert {spec.name: (spec.kind, spec.ops) for spec in module_specs()} == EXPECTED_TOOLS
    assert {spec.toolset for spec in module_specs()} == {"uptime"}


def test_the_fixtures_are_shaped_like_the_spec(spec_index):
    """The mocked answers carry exactly the fields the spec's response schemas define."""

    def fields(name):
        return set(spec_index.schema(name)["fields"])

    record = check()
    assert set(record) == fields("UptimeResponse")
    assert set(record["Target"]) == fields("UptimeTarget")
    assert set(record["MaintenanceMode"]) == fields("UptimeMaintenanceMode")
    assert set(record["Type"]) == set(record["Status"]) == fields("CodeModel")
    assert fields("CreateUptimeCheckResult") == fields("UpdateUptimeCheckResult") == fields("DeleteUptimeCheckResult") == {"Id"}


def test_the_module_docstring_names_every_op_the_tools_use():
    for _kind, ops in EXPECTED_TOOLS.values():
        for op in ops:
            assert op in uptime.__doc__, op


def body_paths(spec_index, op):
    paths = set()

    def walk(fields, prefix):
        for name, entry in fields.items():
            paths.add(f"{prefix}{name}")
            target = spec_index.schemas.get(entry.get("ref")) if entry.get("ref") else None
            nested = entry.get("fields") or (target or {}).get("fields")
            if nested:
                walk(nested, f"{prefix}{name}.")

    if op.body:
        walk(op.body.get("fields") or {}, "")
    return paths


def test_every_field_map_entry_is_a_name_the_spec_knows(spec_index):
    for spec in module_specs():
        known = set()
        for op_key in spec.ops:
            op = spec_index.op(op_key)
            known |= {name.lower() for name in op.query_params}
            known |= {name.lower() for name in op.path_placeholders}
            known |= {path.lower() for path in body_paths(spec_index, op)}
        for param, name in spec.field_map.items():
            assert name.lower() in known, f"{spec.name}: {param} maps to {name}, which no declared op has"


def test_the_body_maps_cover_exactly_the_documented_fields(spec_index):
    create = set(spec_index.op("POST /v1/uptime").body["fields"])
    update = set(spec_index.op("PATCH /v1/uptime/{checkId}").body["fields"])
    top = {path.split(".")[0] for path in uptime.CHECK_BODY.values()}
    assert top == create and top | {"MaintenanceMode"} == update
    assert {path.split(".")[0] for path in uptime.MAINTENANCE_BODY.values()} == {"MaintenanceMode"}
    assert {path for path in uptime.CHECK_BODY.values() if path.startswith("Target.")} == {"Target.Ip", "Target.Url", "Target.Port"}


def test_the_constants_match_the_contract():
    assert uptime.CHECK_TYPE_IDS == {"icmp": 1, "http": 2, "tcp": 3}
    assert uptime.REGION_IDS == {"seattle": 1, "sydney": 2, "uk": 3, "frankfurt": 4}
    assert uptime.TARGET_FIELDS_BY_TYPE == {"icmp": ("ip",), "http": ("url",), "tcp": ("ip", "port")}


def test_every_param_is_described_and_the_text_stays_within_the_concision_limits():
    for spec in module_specs():
        doc = inspect.getdoc(spec.fn)
        assert 40 < len(doc) <= 900, spec.name
        for name, definition in Tool.from_function(spec.fn).parameters["properties"].items():
            text = definition.get("description")
            assert text, f"{spec.name}.{name} needs a description"
            assert len(text) <= 160, f"{spec.name}.{name} description is {len(text)} characters"
    for name in ("create_uptime_check", "update_uptime_check", "set_uptime_maintenance"):
        assert "Side effects:" in inspect.getdoc(getattr(uptime, name)), name


def test_the_docstrings_state_the_facts_the_model_must_know():
    create = " ".join(inspect.getdoc(uptime.create_uptime_check).split())
    assert "monitoring starts immediately and failures can raise alerts" in create
    assert "adopt_client_assets=true moves unassigned devices with a matching public IP to the check's client" in create
    assert "{Id, warning}: the check exists, do not create it again" in create
    delete = " ".join(inspect.getdoc(uptime.delete_uptime_check).split())
    assert "a soft delete (deactivated, schedule cancelled)" in delete and "stops monitoring its target" in delete
    assert "Repeating it is safe" in delete
    assert "Ask the user first; needs confirm=true." in delete  # the consistency rule of every destructive tool
    maintenance = " ".join(inspect.getdoc(uptime.set_uptime_maintenance).split())
    assert "enabled=false ends it and takes no other field" in maintenance
    # a window needs its length (0 means it never expires); live 2026-10-02: Gorelo also refuses a window
    # without a start, so only the reason stays optional
    assert "with enabled=true start and duration_minutes are required (0 means the window never expires)" in maintenance
    assert "and reason is optional" in maintenance
    assert "Pass the current time as start only if the user wants maintenance now" in maintenance
    assert "start and reason are optional" not in maintenance and "start, duration_minutes and reason are optional" not in maintenance
    assert "the check's failures stop raising alerts" in maintenance and "one with no expiry (duration 0) hides outages" in maintenance
    assert "{Id, warning}: the change WAS applied, do not repeat it" in maintenance
    update = " ".join(inspect.getdoc(uptime.update_uptime_check).split())
    assert "tag_ids replaces the whole list" in update and "At least one change" in update
    assert "the check is read first and its complete target is sent" in update
    assert "monitoring and alerts change at once" in update and "a region change reschedules it" in update
    assert "{Id, warning}: the change WAS applied, do not repeat it" in update
    listing = " ".join(inspect.getdoc(uptime.list_uptime_checks).split())
    assert "Type 1 ICMP, 2 HTTP, 3 TCP" in listing and "RegionId 1 Seattle, 2 Sydney, 3 UK, 4 Frankfurt" in listing


async def test_the_parameter_descriptions_carry_the_id_sources_target_rules_and_the_confirm_rule(server):
    tools = await list_tools(server)

    def text(tool, param):
        return tool_def(tool, tools).inputSchema["properties"][param]["description"]

    assert "list_clients" in text("list_uptime_checks", "client_ids") and "same filters" in text("list_uptime_checks", "cursor")
    # the paging stop condition is said where the cursor is
    assert text("list_uptime_checks", "cursor") == (
        "next_cursor from the previous call; same filters; repeat until has_more is false."
    )
    assert "list_clients" in text("create_uptime_check", "client_id")
    assert "list_client_locations" in text("create_uptime_check", "location_id")
    assert "icmp needs ip, http needs url, tcp needs ip and port" in text("create_uptime_check", "check_type")
    for tool in ("create_uptime_check", "update_uptime_check"):
        assert "literal IP address, not a host name" in text(tool, "ip"), tool
    assert "No tag lookup" in text("create_uptime_check", "tag_ids")
    assert "COMPLETE new list" in text("update_uptime_check", "tag_ids")
    assert "complete target" in text("update_uptime_check", "check_type")
    assert text("set_uptime_maintenance", "duration_minutes") == (
        "Required with enabled=true: minutes the window lasts. 0 means no expiry: it never ends until you end it."
    )
    assert text("set_uptime_maintenance", "start") == (
        "Required with enabled=true: when the window begins (ISO 8601 with UTC offset). Use the current time only if "
        "the user wants maintenance now."
    )
    for tool in ("get_uptime_check", "update_uptime_check", "set_uptime_maintenance", "delete_uptime_check"):
        assert "list_uptime_checks" in text(tool, "check_id"), tool
    assert "Must be true" in text("delete_uptime_check", "confirm") and "user approves" in text("delete_uptime_check", "confirm")


async def test_the_annotations_tell_a_client_what_each_tool_does(server):
    tools = {tool.name: tool for tool in await list_tools(server)}
    for name in ("list_uptime_checks", "get_uptime_check"):
        a = tools[name].annotations
        assert (a.readOnlyHint, a.destructiveHint, a.idempotentHint) == (True, False, True), name
    assert (tools["create_uptime_check"].annotations.readOnlyHint, tools["create_uptime_check"].annotations.destructiveHint) == (False, False)
    for name in ("update_uptime_check", "set_uptime_maintenance"):  # they overwrite what is stored
        assert (tools[name].annotations.readOnlyHint, tools[name].annotations.destructiveHint) == (False, True), name
    assert (tools["delete_uptime_check"].annotations.readOnlyHint, tools["delete_uptime_check"].annotations.destructiveHint) == (False, True)
    confirm = tools["delete_uptime_check"].inputSchema["properties"]["confirm"]
    assert confirm["type"] == "boolean" and confirm["default"] is False


async def test_update_does_not_take_maintenance_fields_and_maintenance_takes_nothing_else(server):
    tools = {tool.name: tool for tool in await list_tools(server)}
    assert not {"enabled", "start", "duration_minutes", "reason"} & set(tools["update_uptime_check"].inputSchema["properties"])
    assert set(tools["set_uptime_maintenance"].inputSchema["properties"]) == {"check_id", "enabled", "start", "duration_minutes", "reason"}
    assert tools["set_uptime_maintenance"].inputSchema["required"] == ["check_id", "enabled"]


# --------------------------------------------------------------------------
# list_uptime_checks
# --------------------------------------------------------------------------


async def test_list_uptime_checks_without_filters_sends_only_the_page_size(server, mock_gorelo):
    rows = [check(), check(uid(42), Type={"Id": 2, "Name": "HTTP"}, Target={"Ip": None, "Port": None, "Url": "https://example.com"})]
    mock_gorelo.on("GET", UPTIME, paged_envelope(rows, total_count=2))
    result = await call_tool(server, "list_uptime_checks")
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", UPTIME, {"PageSize": "50"}, None)
    assert result == {
        "items": rows,
        "count": 2,
        "total_count": 2,
        "has_more": False,
        "next_cursor": None,
        "page_size": 50,
        "filters": {},
    }


async def test_every_filter_goes_out_under_its_spec_name(server, mock_gorelo):
    mock_gorelo.on("GET", UPTIME, paged_envelope([check()]))
    args = {
        "client_ids": [9102, 9101],
        "check_types": ["icmp", "tcp"],
        "tag_ids": [3, 5],
        "query": "router",
        "page_size": 20,
        "cursor": "opaque-1",
    }
    result = await call_tool(server, "list_uptime_checks", args)
    assert mock_gorelo.last.query == {
        "ClientIds": "9102,9101",
        "TypeIds": "1,3",
        "TagIds": "3,5",
        "Query": "router",
        "PageSize": "20",
        "Cursor": "opaque-1",
    }
    assert result["filters"] == {"client_ids": [9102, 9101], "check_types": ["icmp", "tcp"], "tag_ids": [3, 5], "query": "router"}


@pytest.mark.parametrize(
    "types, wire",
    [(["icmp"], "1"), (["http"], "2"), (["tcp"], "3"), (["tcp", "icmp", "http"], "3,1,2"), (["icmp", "icmp", "http"], "1,2")],
)
async def test_check_type_names_become_gorelo_type_ids(server, mock_gorelo, types, wire):
    mock_gorelo.on("GET", UPTIME, paged_envelope([]))
    await call_tool(server, "list_uptime_checks", {"check_types": types})
    assert mock_gorelo.last.query == {"TypeIds": wire, "PageSize": "50"}


@pytest.mark.parametrize("given, used", [(0, 1), (-1, 1), (1, 1), (200, 200), (500, 200)])
async def test_page_size_is_clamped_and_reported(server, mock_gorelo, given, used):
    mock_gorelo.on("GET", UPTIME, paged_envelope([]))
    result = await call_tool(server, "list_uptime_checks", {"page_size": given})
    assert mock_gorelo.last.query["PageSize"] == str(used) and result["page_size"] == used


async def test_paging_follows_next_cursor_with_the_same_filters(server, mock_gorelo):
    mock_gorelo.on(
        "GET",
        UPTIME,
        in_order(
            envelope([check()], pagination("c-2", 2)),
            envelope([check(uid(42))], pagination(None, 2, has_more=False)),
        ),
    )
    args = {"check_types": ["http"], "page_size": 1}
    one = await call_tool(server, "list_uptime_checks", args)
    assert (one["has_more"], one["next_cursor"], one["total_count"]) == (True, "c-2", 2)
    two = await call_tool(server, "list_uptime_checks", {**args, "cursor": one["next_cursor"]})
    assert (two["has_more"], two["next_cursor"]) == (False, None)
    assert [r.query for r in mock_gorelo.requests] == [
        {"TypeIds": "2", "PageSize": "1"},
        {"TypeIds": "2", "PageSize": "1", "Cursor": "c-2"},
    ]
    assert one["filters"] == two["filters"] == {"check_types": ["http"]}


async def test_an_empty_page_is_reported_with_its_total(server, mock_gorelo):
    mock_gorelo.on("GET", UPTIME, envelope([], pagination(None, 0, has_more=False)))
    result = await call_tool(server, "list_uptime_checks", {"query": "nothing like this"})
    assert (result["items"], result["count"], result["total_count"]) == ([], 0, 0)


async def test_list_uptime_checks_refuses_an_answer_that_is_not_a_list(server, mock_gorelo):
    mock_gorelo.on("GET", UPTIME, envelope({"Id": CHECK_ID}, pagination(None, 1, has_more=False)))
    assert "expected Data to be a list" in await call_tool_error(server, "list_uptime_checks")


@pytest.mark.parametrize(
    "property_name, param",
    [("TypeIds", "check_types"), ("TagIds", "tag_ids"), ("ClientIds", "client_ids"), ("Query", "query"), ("Cursor", "cursor"), ("PageSize", "page_size")],
)
async def test_a_gorelo_400_names_the_snake_case_param(server, mock_gorelo, property_name, param):
    mock_gorelo.on("GET", UPTIME, error_envelope(400, [("070101", "Not a valid value.", property_name)]))
    text = await call_tool_error(server, "list_uptime_checks")
    assert text == f"Gorelo rejected list_uptime_checks (HTTP 400, code 070101): {param}: Not a valid value. [trace {TEST_TRACE_ID}]"


LOCAL_LIST_ERRORS = [
    pytest.param(
        {"client_ids": []},
        "client_ids: expected at least one id, got an empty list (omit client_ids if you have no ids to give)",
        id="empty-client-ids",
    ),
    pytest.param({"tag_ids": []}, "tag_ids: expected at least one id, got an empty list", id="empty-tag-ids"),
    pytest.param({"client_ids": [3, 0]}, f"client_ids[1]: {POSITIVE}", id="client-id-zero"),
    pytest.param({"tag_ids": [-1]}, f"tag_ids[0]: {POSITIVE}", id="tag-id-negative"),
    pytest.param({"tag_ids": [2**63]}, "tag_ids[0]: expected a positive whole number such as 123, got a number above 9223372036854775807", id="tag-id-beyond-int64"),
    pytest.param({"check_types": []}, "check_types: the list must contain at least one value", id="empty-check-types"),
    pytest.param({"query": "x" * 201}, "query: at most 200 characters", id="query-too-long"),
    pytest.param({"query": "  "}, "query: must not be empty or whitespace only", id="blank-query"),
    pytest.param({"cursor": ""}, "cursor: must not be empty or whitespace only", id="blank-cursor"),
]


@pytest.mark.parametrize("args, fragment", LOCAL_LIST_ERRORS)
async def test_list_local_errors_name_the_param_and_send_nothing(server, mock_gorelo, args, fragment):
    assert fragment in await call_tool_error(server, "list_uptime_checks", args)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("param", ["client_ids", "tag_ids"])
@pytest.mark.parametrize("bad", [True, "5", 5.0], ids=["json-true", "text-5", "decimal-5"])
async def test_an_id_list_is_strict_true_and_text_are_refused_instead_of_becoming_an_id(server, mock_gorelo, param, bad):
    text = await call_tool_error(server, "list_uptime_checks", {param: [bad]})
    assert param in text
    assert mock_gorelo.requests == []


async def test_a_check_type_gorelo_does_not_have_is_refused_not_dropped(server, client_factory, mock_gorelo):
    for bad in (["ping"], ["ICMP"], ["icmp", "udp"]):
        assert "check_types" in await call_tool_error(server, "list_uptime_checks", {"check_types": bad})
    with pytest.raises(ToolError, match="check_types: must be one of 'icmp', 'http', 'tcp', got 'udp'"):
        await call_directly(client_factory, uptime.list_uptime_checks, check_types=["icmp", "udp"])
    assert mock_gorelo.requests == []


# --------------------------------------------------------------------------
# get_uptime_check
# --------------------------------------------------------------------------


async def test_get_uptime_check_returns_the_record_unchanged(server, mock_gorelo):
    record = check(MaintenanceMode={"Enabled": True, "StartDateTime": "2026-10-02T22:00:00Z", "DurationInMinutes": 60, "Reason": "Firmware"})
    mock_gorelo.on("GET", CHECK_PATH, envelope(record))
    assert await call_tool(server, "get_uptime_check", {"check_id": CHECK_ID}) == record
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", CHECK_PATH, {}, None)


async def test_get_uptime_check_404_names_check_id(server, mock_gorelo):
    mock_gorelo.on("GET", CHECK_PATH, error_envelope(404, [("070401", "Uptime check not found.", "checkId")]))
    text = await call_tool_error(server, "get_uptime_check", {"check_id": CHECK_ID})
    assert text == f"Gorelo rejected get_uptime_check (HTTP 404, code 070401): check_id: Uptime check not found. [trace {TEST_TRACE_ID}]"


async def test_get_uptime_check_refuses_a_success_without_data(server, mock_gorelo):
    mock_gorelo.on("GET", CHECK_PATH, envelope(None))
    assert "Data is null; refusing to guess" in await call_tool_error(server, "get_uptime_check", {"check_id": CHECK_ID})


@pytest.mark.parametrize("data", [{}, [], [check()], True, "text"], ids=["empty-object", "empty-list", "list", "true", "text"])
async def test_get_uptime_check_refuses_an_answer_that_is_not_an_object_record(server, mock_gorelo, data):
    mock_gorelo.on("GET", CHECK_PATH, envelope(data))
    text = await call_tool_error(server, "get_uptime_check", {"check_id": CHECK_ID})
    assert text.startswith("Gorelo returned an unexpected response for get_uptime_check: GET /v1/uptime/{checkId}: expected Data to be a non-empty object but got ")
    assert text.endswith("refusing to guess")  # a read: nothing was written


@pytest.mark.parametrize("bad", ["", "41", "router", "../tickets", f"{CHECK_ID}/x", f" {CHECK_ID}", "{" + CHECK_ID + "}"])
async def test_get_needs_a_guid_and_the_error_names_check_id(server, mock_gorelo, bad):
    text = await call_tool_error(server, "get_uptime_check", {"check_id": bad})
    assert text.startswith(GUID_ERROR)
    assert mock_gorelo.requests == []


# --------------------------------------------------------------------------
# create_uptime_check
# --------------------------------------------------------------------------


async def test_an_icmp_check_sends_type_frequency_region_and_ip_then_rereads_it(server, mock_gorelo):
    route_create(mock_gorelo)
    args = {"check_type": "icmp", "frequency_minutes": 5, "region": "seattle", "ip": "203.0.113.10"}
    result = await call_tool(server, "create_uptime_check", args)
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("POST", UPTIME), ("GET", CHECK_PATH)]
    post = mock_gorelo.requests[0]
    assert post.json == {"TypeId": 1, "Frequency": 5, "RegionId": 1, "Target": {"Ip": "203.0.113.10"}} and post.query == {}
    assert result == check()


async def test_an_http_check_sends_its_url(server, mock_gorelo):
    route_create(mock_gorelo)
    args = {"check_type": "http", "frequency_minutes": 1, "region": "sydney", "url": "https://example.com/health"}
    await call_tool(server, "create_uptime_check", args)
    assert mock_gorelo.requests[0].json == {
        "TypeId": 2, "Frequency": 1, "RegionId": 2, "Target": {"Url": "https://example.com/health"},
    }


async def test_a_tcp_check_sends_ip_and_port(server, mock_gorelo):
    route_create(mock_gorelo)
    args = {"check_type": "tcp", "frequency_minutes": 15, "region": "uk", "ip": "198.51.100.7", "port": 443}
    await call_tool(server, "create_uptime_check", args)
    assert mock_gorelo.requests[0].json == {
        "TypeId": 3, "Frequency": 15, "RegionId": 3, "Target": {"Ip": "198.51.100.7", "Port": 443},
    }


@pytest.mark.parametrize("region, region_id", [("seattle", 1), ("sydney", 2), ("uk", 3), ("frankfurt", 4)])
async def test_region_names_become_gorelo_region_ids(server, mock_gorelo, region, region_id):
    route_create(mock_gorelo)
    await call_tool(server, "create_uptime_check", {"check_type": "icmp", "frequency_minutes": 5, "region": region, "ip": "10.0.0.1"})
    assert mock_gorelo.requests[0].json["RegionId"] == region_id


async def test_every_optional_field_is_sent_under_its_pascal_name(server, mock_gorelo):
    route_create(mock_gorelo)
    args = {
        "check_type": "icmp",
        "frequency_minutes": 5,
        "region": "frankfurt",
        "ip": "203.0.113.10",
        "client_id": 9102,
        "location_id": 77,
        "description": "Main router",
        "retries_after_failure": 3,
        "isp_connection_link": "https://status.isp.example/outages",
        "tag_ids": [3, 5, 3],
        "adopt_client_assets": True,
    }
    await call_tool(server, "create_uptime_check", args)
    assert mock_gorelo.requests[0].json == {
        "TypeId": 1,
        "Frequency": 5,
        "RegionId": 4,
        "Target": {"Ip": "203.0.113.10"},
        "ClientId": 9102,
        "LocationId": 77,
        "Description": "Main router",
        "NumberOfRetriesAfterFailure": 3,
        "IspConnectionLink": "https://status.isp.example/outages",
        "TagIds": [3, 5],  # each tag once
        "AdoptClientAssets": True,
    }


@pytest.mark.parametrize("adopt", [None, False])
async def test_adopt_client_assets_is_only_sent_when_true(server, mock_gorelo, adopt):
    route_create(mock_gorelo)
    args = {"check_type": "icmp", "frequency_minutes": 5, "region": "uk", "ip": "10.0.0.1"}
    if adopt is not None:
        args["adopt_client_assets"] = adopt
    await call_tool(server, "create_uptime_check", args)
    assert "AdoptClientAssets" not in mock_gorelo.requests[0].json


async def test_zero_retries_is_a_real_value_and_a_field_not_given_is_not_sent(server, mock_gorelo):
    route_create(mock_gorelo)
    args = {"check_type": "icmp", "frequency_minutes": 5, "region": "uk", "ip": "10.0.0.1", "retries_after_failure": 0}
    await call_tool(server, "create_uptime_check", args)
    assert mock_gorelo.requests[0].json == {
        "TypeId": 1, "Frequency": 5, "RegionId": 3, "Target": {"Ip": "10.0.0.1"}, "NumberOfRetriesAfterFailure": 0,
    }


async def test_a_location_without_a_client_is_sent_as_given(server, mock_gorelo):
    route_create(mock_gorelo)
    args = {"check_type": "icmp", "frequency_minutes": 5, "region": "uk", "ip": "10.0.0.1", "location_id": 77}
    await call_tool(server, "create_uptime_check", args)
    assert mock_gorelo.requests[0].json["LocationId"] == 77 and "ClientId" not in mock_gorelo.requests[0].json


BASE = {"frequency_minutes": 5, "region": "seattle"}

TARGET_ERRORS = [
    pytest.param({"check_type": "icmp"}, "ip: required for an ICMP check (ip is the IP address to ping)", id="icmp-needs-ip"),
    pytest.param({"check_type": "icmp", "ip": "10.0.0.1", "url": "https://x.example"}, "url: not valid for an ICMP check, which takes ip; omit it", id="icmp-refuses-url"),
    pytest.param({"check_type": "icmp", "ip": "10.0.0.1", "port": 80}, "port: not valid for an ICMP check, which takes ip; omit it", id="icmp-refuses-port"),
    pytest.param({"check_type": "icmp", "url": "https://x.example"}, "ip: required for an ICMP check", id="icmp-with-only-a-url"),
    pytest.param({"check_type": "http"}, "url: required for an HTTP check (url is the address to request, such as https://example.com/health)", id="http-needs-url"),
    pytest.param({"check_type": "http", "url": "https://x.example", "ip": "10.0.0.1"}, "ip: not valid for an HTTP check, which takes url; omit it", id="http-refuses-ip"),
    pytest.param({"check_type": "http", "url": "https://x.example", "port": 443}, "port: not valid for an HTTP check, which takes url; omit it", id="http-refuses-port"),
    pytest.param({"check_type": "http", "url": "https://x.example", "ip": "10.0.0.1", "port": 443}, "ip, port: not valid for an HTTP check", id="http-refuses-ip-and-port"),
    pytest.param({"check_type": "tcp", "port": 443}, "ip: required for a TCP check (ip is the IP address to connect to and port the TCP port)", id="tcp-needs-ip"),
    pytest.param({"check_type": "tcp", "ip": "10.0.0.1"}, "port: required for a TCP check", id="tcp-needs-port"),
    pytest.param({"check_type": "tcp"}, "ip, port: required for a TCP check", id="tcp-needs-both"),
    pytest.param({"check_type": "tcp", "ip": "10.0.0.1", "port": 443, "url": "https://x.example"}, "url: not valid for a TCP check, which takes ip and port; omit it", id="tcp-refuses-url"),
]


@pytest.mark.parametrize("args, fragment", TARGET_ERRORS)
async def test_target_fields_must_fit_the_check_type(server, mock_gorelo, args, fragment):
    text = await call_tool_error(server, "create_uptime_check", {**BASE, **args})
    assert fragment in text
    assert mock_gorelo.requests == []


LOCAL_CREATE_ERRORS = [
    pytest.param({"url": "example.com"}, "url: must be a full address starting with http:// or https://", id="url-without-scheme"),
    pytest.param({"url": "ftp://example.com"}, "url: must be a full address starting with http:// or https://", id="url-with-another-scheme"),
    pytest.param({"url": "https://"}, "url: must be a full address starting with http:// or https://", id="url-with-nothing-after-the-scheme"),
    pytest.param({"url": "https://exa mple.com"}, "url: must be a full address starting with http:// or https://", id="url-with-a-space"),
    pytest.param({"url": "  "}, "url: must not be empty or whitespace only", id="blank-url"),
]


@pytest.mark.parametrize("args, fragment", LOCAL_CREATE_ERRORS)
async def test_an_http_url_must_be_a_full_http_address(server, mock_gorelo, args, fragment):
    text = await call_tool_error(server, "create_uptime_check", {**BASE, "check_type": "http", **args})
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_the_http_scheme_is_accepted_in_any_case(server, mock_gorelo):
    route_create(mock_gorelo)
    for url in ("http://example.com", "HTTPS://Example.com/x?y=1#z"):
        await call_tool(server, "create_uptime_check", {**BASE, "check_type": "http", "url": url})
        assert mock_gorelo.calls("POST")[-1].json["Target"] == {"Url": url}


BAD_IPS = [
    pytest.param("router.example.com", IP_ERROR, id="host-name"),
    pytest.param("localhost", IP_ERROR, id="localhost"),
    pytest.param("example.com:443", IP_ERROR, id="host-and-port"),
    pytest.param("10.0.0", IP_ERROR, id="three-octets"),
    pytest.param("999.1.1.1", IP_ERROR, id="octet-out-of-range"),
    pytest.param("10.0.0.1/24", IP_ERROR, id="network-with-a-prefix"),
    pytest.param("010.0.0.1", IP_ERROR, id="leading-zero"),
    pytest.param("http://10.0.0.1", IP_ERROR, id="url"),
    pytest.param("10.0.0 .1", IP_ERROR, id="space-inside-the-address"),
    pytest.param("10.0.0.1 ", IP_ERROR, id="trailing-space"),
    pytest.param(" 10.0.0.1", IP_ERROR, id="leading-space"),
    pytest.param("   ", "ip: must not be empty or whitespace only", id="blank-ip"),
]


@pytest.mark.parametrize("ip, fragment", BAD_IPS)
async def test_an_icmp_ip_must_be_a_literal_address_never_a_host_name(server, mock_gorelo, ip, fragment):
    # Gorelo's ICMP and TCP checks take an address, so a name is refused here naming the param
    text = await call_tool_error(server, "create_uptime_check", {**BASE, "check_type": "icmp", "ip": ip})
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("ip, fragment", BAD_IPS)
async def test_a_tcp_ip_must_be_a_literal_address_never_a_host_name(server, mock_gorelo, ip, fragment):
    text = await call_tool_error(server, "create_uptime_check", {**BASE, "check_type": "tcp", "ip": ip, "port": 443})
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("ip", ["203.0.113.10", "2001:db8::1", "2001:DB8::A", "::1", "::ffff:192.0.2.1"])
@pytest.mark.parametrize("kind, extra", [("icmp", {}), ("tcp", {"port": 443})])
async def test_an_ipv4_or_ipv6_address_is_sent_as_given(server, mock_gorelo, ip, kind, extra):
    route_create(mock_gorelo)
    await call_tool(server, "create_uptime_check", {**BASE, "check_type": kind, "ip": ip, **extra})
    assert mock_gorelo.requests[0].json["Target"] == {"Ip": ip, **({"Port": 443} if extra else {})}


async def test_an_http_check_keeps_its_host_name_inside_the_url(server, mock_gorelo):
    # only icmp and tcp need a literal address; an http check names its host in the url
    route_create(mock_gorelo)
    await call_tool(server, "create_uptime_check", {**BASE, "check_type": "http", "url": "https://router.example.com/up"})
    assert mock_gorelo.requests[0].json["Target"] == {"Url": "https://router.example.com/up"}
    text = await call_tool_error(server, "create_uptime_check", {**BASE, "check_type": "http", "ip": "router.example.com", "url": "https://x.example"})
    assert text.startswith("ip: not valid for an HTTP check, which takes url; omit it")


@pytest.mark.parametrize("port", [0, -1, 65536, 100000])
async def test_a_tcp_port_must_be_1_to_65535(server, mock_gorelo, port):
    text = await call_tool_error(server, "create_uptime_check", {**BASE, "check_type": "tcp", "ip": "10.0.0.1", "port": port})
    assert text == "port: must be a whole number from 1 to 65535"
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("port", [1, 65535])
async def test_the_port_limits_themselves_are_valid(server, mock_gorelo, port):
    route_create(mock_gorelo)
    await call_tool(server, "create_uptime_check", {**BASE, "check_type": "tcp", "ip": "10.0.0.1", "port": port})
    assert mock_gorelo.requests[0].json["Target"] == {"Ip": "10.0.0.1", "Port": port}


@pytest.mark.parametrize("frequency", [0, -5])
async def test_the_frequency_must_be_a_positive_whole_number_of_minutes(server, mock_gorelo, frequency):
    args = {"check_type": "icmp", "region": "uk", "ip": "10.0.0.1", "frequency_minutes": frequency}
    assert await call_tool_error(server, "create_uptime_check", args) == "frequency_minutes: must be a whole number from 1 to 2147483647"
    assert mock_gorelo.requests == []


async def test_a_missing_frequency_is_refused_by_name(server, client_factory, mock_gorelo):
    assert "frequency_minutes" in await call_tool_error(server, "create_uptime_check", {"check_type": "icmp", "region": "uk", "ip": "10.0.0.1"})
    with pytest.raises(ToolError, match="frequency_minutes: is required"):
        await call_directly(client_factory, uptime.create_uptime_check, check_type="icmp", frequency_minutes=None, region="uk", ip="10.0.0.1")
    assert mock_gorelo.requests == []


async def test_negative_retries_are_refused(server, mock_gorelo):
    args = {**BASE, "check_type": "icmp", "ip": "10.0.0.1", "retries_after_failure": -1}
    assert await call_tool_error(server, "create_uptime_check", args) == "retries_after_failure: must be a whole number from 0 to 2147483647"
    assert mock_gorelo.requests == []


async def test_a_client_needs_its_location(server, mock_gorelo):
    args = {**BASE, "check_type": "icmp", "ip": "10.0.0.1", "client_id": 9102}
    text = await call_tool_error(server, "create_uptime_check", args)
    assert text.startswith("location_id: required together with client_id")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("field", ["client_id", "location_id"])
@pytest.mark.parametrize("value", [0, -1])
async def test_ids_must_be_positive(server, mock_gorelo, field, value):
    args = {**BASE, "check_type": "icmp", "ip": "10.0.0.1", "client_id": 9102, "location_id": 77, field: value}
    assert await call_tool_error(server, "create_uptime_check", args) == f"{field}: {POSITIVE}"
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("field", ["client_id", "location_id"])
async def test_an_id_beyond_int64_is_refused(server, mock_gorelo, field):
    args = {**BASE, "check_type": "icmp", "ip": "10.0.0.1", "client_id": 9102, "location_id": 77, field: 2**63}
    text = await call_tool_error(server, "create_uptime_check", args)
    assert text.startswith(f"{field}: expected a positive whole number such as 123, got a number above 9223372036854775807")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("field", ["client_id", "location_id"])
@pytest.mark.parametrize("bad", [True, "5", 5.0], ids=["json-true", "text-5", "decimal-5"])
@pytest.mark.parametrize("tool", ["create_uptime_check", "update_uptime_check"])
async def test_an_id_is_strict_true_and_text_never_become_an_id(server, mock_gorelo, tool, field, bad):
    args = {"check_type": "icmp", "ip": "10.0.0.1", **BASE} if tool == "create_uptime_check" else {"check_id": CHECK_ID}
    args.update({"client_id": 9102, "location_id": 77, field: bad})
    assert field in await call_tool_error(server, tool, args)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", [True, "5", 5.0], ids=["json-true", "text-5", "decimal-5"])
@pytest.mark.parametrize("tool", ["create_uptime_check", "update_uptime_check"])
async def test_a_tag_id_is_strict_true_and_text_never_become_an_id(server, mock_gorelo, tool, bad):
    args = {"check_type": "icmp", "ip": "10.0.0.1", **BASE} if tool == "create_uptime_check" else {"check_id": CHECK_ID}
    assert "tag_ids" in await call_tool_error(server, tool, {**args, "tag_ids": [3, bad]})
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("field", ["client_id", "location_id"])
async def test_calling_create_directly_with_a_bool_or_text_id_is_refused_by_the_helper(client_factory, mock_gorelo, field):
    for bad in (True, "5", 5.0):
        with pytest.raises(ToolError, match=f"{field}: expected a positive whole number such as 123, got "):
            ids = {"client_id": 9102, "location_id": 77, field: bad}
            await call_directly(
                client_factory, uptime.create_uptime_check, check_type="icmp", frequency_minutes=5, region="uk",
                ip="10.0.0.1", **ids,
            )
    with pytest.raises(ToolError) as info:  # what was given is described, never quoted
        await call_directly(
            client_factory, uptime.create_uptime_check, check_type="icmp", frequency_minutes=5, region="uk",
            ip="10.0.0.1", **{"client_id": 9102, "location_id": 77, field: "secret-text"},
        )
    assert "secret-text" not in str(info.value) and str(info.value).endswith("got a string")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "field, limit",
    [("description", 250), ("isp_connection_link", 500)],
)
async def test_text_longer_than_gorelos_column_is_refused_and_the_limit_itself_is_accepted(server, mock_gorelo, field, limit):
    route_create(mock_gorelo)
    args = {**BASE, "check_type": "icmp", "ip": "10.0.0.1"}
    assert await call_tool_error(server, "create_uptime_check", {**args, field: "x" * (limit + 1)}) == f"{field}: at most {limit} characters"
    assert mock_gorelo.requests == []
    await call_tool(server, "create_uptime_check", {**args, field: "x" * limit})
    assert len(mock_gorelo.requests[0].json[uptime.CHECK_BODY[field]]) == limit


@pytest.mark.parametrize("field", ["description", "isp_connection_link"])
async def test_blank_text_is_refused_on_create(server, mock_gorelo, field):
    args = {**BASE, "check_type": "icmp", "ip": "10.0.0.1", field: " "}
    assert (await call_tool_error(server, "create_uptime_check", args)).startswith(f"{field}: must not be empty or whitespace only")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "tags, fragment",
    [
        pytest.param([], "tag_ids: the list must contain at least one tag id; omit it for no tags", id="empty"),
        pytest.param([0], f"tag_ids[0]: {POSITIVE}", id="zero"),
        pytest.param([3, -1], f"tag_ids[1]: {POSITIVE}", id="negative"),
        pytest.param([2**63], "tag_ids[0]: expected a positive whole number such as 123, got a number above 9223372036854775807", id="beyond-int64"),
    ],
)
async def test_tag_ids_must_be_real_ids(server, mock_gorelo, tags, fragment):
    args = {**BASE, "check_type": "icmp", "ip": "10.0.0.1", "tag_ids": tags}
    assert fragment in await call_tool_error(server, "create_uptime_check", args)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "args, param",
    [
        ({"check_type": "ping", "frequency_minutes": 5, "region": "uk", "ip": "10.0.0.1"}, "check_type"),
        ({"frequency_minutes": 5, "region": "uk", "ip": "10.0.0.1"}, "check_type"),
        ({"check_type": "icmp", "frequency_minutes": 5, "region": "mars", "ip": "10.0.0.1"}, "region"),
        ({"check_type": "icmp", "frequency_minutes": 5, "ip": "10.0.0.1"}, "region"),
        ({"check_type": "tcp", "frequency_minutes": 5, "region": "uk", "ip": "10.0.0.1", "port": "https"}, "port"),
        ({"check_type": "icmp", "frequency_minutes": "often", "region": "uk", "ip": "10.0.0.1"}, "frequency_minutes"),
    ],
)
async def test_a_value_outside_the_schema_is_refused_with_the_param_name(server, mock_gorelo, args, param):
    assert param in await call_tool_error(server, "create_uptime_check", args)
    assert mock_gorelo.requests == []


async def test_calling_create_directly_with_an_unknown_name_is_refused(client_factory, mock_gorelo):
    with pytest.raises(ToolError, match="check_type: must be one of 'icmp', 'http', 'tcp', got 'ping'"):
        await call_directly(client_factory, uptime.create_uptime_check, check_type="ping", frequency_minutes=5, region="uk", ip="x")
    with pytest.raises(ToolError, match="region: must be one of 'seattle', 'sydney', 'uk', 'frankfurt', got 'mars'"):
        await call_directly(client_factory, uptime.create_uptime_check, check_type="icmp", frequency_minutes=5, region="mars", ip="x")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "property_name, param",
    [("Ip", "ip"), ("Url", "url"), ("Port", "port"), ("Target.Ip", "ip"), ("Target.Port", "port"), ("Frequency", "frequency_minutes"),
     ("RegionId", "region"), ("TypeId", "check_type"), ("NumberOfRetriesAfterFailure", "retries_after_failure"),
     ("TagIds", "tag_ids"), ("LocationId", "location_id"), ("ClientId", "client_id"), ("IspConnectionLink", "isp_connection_link"),
     ("Description", "description"), ("AdoptClientAssets", "adopt_client_assets")],
)
async def test_a_gorelo_400_names_the_snake_case_param_on_create(server, mock_gorelo, property_name, param):
    mock_gorelo.on("POST", UPTIME, error_envelope(400, [("070101", "Rejected by Gorelo.", property_name)]))
    args = {**BASE, "check_type": "icmp", "ip": "10.0.0.1"}
    text = await call_tool_error(server, "create_uptime_check", args)
    assert text == f"Gorelo rejected create_uptime_check (HTTP 400, code 070101): {param}: Rejected by Gorelo. [trace {TEST_TRACE_ID}]"
    assert mock_gorelo.calls("GET") == []


async def test_a_failed_reread_after_a_successful_create_returns_the_id_and_a_warning(server, mock_gorelo):
    mock_gorelo.on("POST", UPTIME, envelope({"Id": CHECK_ID}))
    mock_gorelo.on("GET", CHECK_PATH, error_envelope(404, [("070401", "Uptime check not found.")]))
    result = await call_tool(server, "create_uptime_check", {**BASE, "check_type": "icmp", "ip": "10.0.0.1"})
    assert set(result) == {"Id", "warning"} and result["Id"] == CHECK_ID
    assert result["warning"].startswith("the write succeeded; re-reading it failed: GET /v1/uptime/{checkId} answered HTTP 404")
    assert "Do not repeat the write" in result["warning"]
    assert len(mock_gorelo.calls("POST")) == 1 and len(mock_gorelo.calls("GET")) == 1


@pytest.mark.parametrize(
    "data, problem",
    [
        pytest.param(None, "Data is null, not an object with an Id", id="null"),
        pytest.param(True, "Data is a boolean, not an object with an Id", id="true"),
        pytest.param(False, "Data is a boolean, not an object with an Id", id="false"),
        pytest.param({}, "Data is an object without an Id", id="empty-object"),
        pytest.param({"Ok": True}, "Data is an object without an Id", id="object-without-id"),
        pytest.param({"Id": ""}, "Data.Id is blank", id="blank-id"),
        pytest.param({"Id": None}, "Data.Id is null", id="null-id"),
        pytest.param({"Id": 0}, "Data.Id is zero or negative", id="zero-id"),
        pytest.param({"Id": True}, "Data.Id is a boolean", id="boolean-id"),
        pytest.param([{"Id": CHECK_ID}], "Data is a list of 1 item, not an object with an Id", id="list"),
    ],
)
async def test_a_create_answer_without_an_id_is_not_taken_for_a_clean_success(server, mock_gorelo, data, problem):
    mock_gorelo.on("POST", UPTIME, envelope(data))
    text = await call_tool_error(server, "create_uptime_check", {**BASE, "check_type": "icmp", "ip": "10.0.0.1"})
    assert text.startswith(
        "Gorelo returned an unexpected response for create_uptime_check: POST /v1/uptime: Gorelo reported success "
        f"but the answer carries no usable Id for the record ({problem})"
    )
    assert "the write may have been applied, so verify it with a read before repeating it" in text
    assert mock_gorelo.calls("GET") == [] and len(mock_gorelo.calls("POST")) == 1


async def test_a_create_answer_with_an_id_that_cannot_be_read_back_returns_a_warning_not_a_second_create(server, mock_gorelo):
    # an Id that is not a GUID passes the answer check but cannot name the check to re-read: the write is not repeated
    mock_gorelo.on("POST", UPTIME, envelope({"Id": 7}))
    result = await call_tool(server, "create_uptime_check", {**BASE, "check_type": "icmp", "ip": "10.0.0.1"})
    assert result["Id"] == 7 and result["warning"].startswith("the write succeeded; re-reading it failed: ")
    assert "Do not repeat the write" in result["warning"]
    assert mock_gorelo.calls("GET") == [] and len(mock_gorelo.calls("POST")) == 1


async def test_a_create_that_times_out_is_not_retried_and_says_to_verify(server, mock_gorelo):
    mock_gorelo.on("POST", UPTIME, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "create_uptime_check", {**BASE, "check_type": "icmp", "ip": "10.0.0.1"})
    assert text.startswith("Gorelo did not confirm create_uptime_check (the request timed out).")
    assert "Verify with a read before retrying." in text
    assert len(mock_gorelo.requests) == 1


# --------------------------------------------------------------------------
# update_uptime_check
# --------------------------------------------------------------------------


async def test_an_update_sends_only_the_given_field_and_rereads_the_check(server, mock_gorelo):
    route_update(mock_gorelo, check(Frequency=10))
    result = await call_tool(server, "update_uptime_check", {"check_id": CHECK_ID, "frequency_minutes": 10})
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("PATCH", CHECK_PATH), ("GET", CHECK_PATH)]
    patch = mock_gorelo.requests[0]
    assert patch.json == {"Frequency": 10} and patch.query == {}
    assert result["Frequency"] == 10


@pytest.mark.parametrize(
    "args, body",
    [
        ({"description": "Backup router"}, {"Description": "Backup router"}),
        ({"retries_after_failure": 0}, {"NumberOfRetriesAfterFailure": 0}),
        ({"retries_after_failure": 4}, {"NumberOfRetriesAfterFailure": 4}),
        ({"region": "frankfurt"}, {"RegionId": 4}),
        ({"isp_connection_link": "https://status.isp.example"}, {"IspConnectionLink": "https://status.isp.example"}),
        ({"adopt_client_assets": True}, {"AdoptClientAssets": True}),
        ({"adopt_client_assets": False}, {"AdoptClientAssets": False}),
        ({"tag_ids": [3, 5, 5]}, {"TagIds": [3, 5]}),
        ({"client_id": 9102, "location_id": 77}, {"ClientId": 9102, "LocationId": 77}),
        ({"location_id": 78}, {"LocationId": 78}),
        ({"frequency_minutes": 30, "region": "uk"}, {"Frequency": 30, "RegionId": 3}),
    ],
)
async def test_every_updatable_field_goes_out_under_its_pascal_name(server, mock_gorelo, args, body):
    route_update(mock_gorelo)
    await call_tool(server, "update_uptime_check", {"check_id": CHECK_ID, **args})
    assert mock_gorelo.requests[0].json == body


def route_target_update(mock, stored, after=None):
    """The stored check for the read before the PATCH, then `after` (default: the same record) for the re-read."""
    mock.on("PATCH", CHECK_PATH, envelope({"Id": CHECK_ID}))
    mock.on("GET", CHECK_PATH, in_order(envelope(stored), envelope(stored if after is None else after)))


# Gorelo takes the Target as a whole, so a change to part of it is completed from the stored check
@pytest.mark.parametrize(
    "stored, args, target",
    [
        pytest.param(check(), {"ip": "203.0.113.20"}, {"Ip": "203.0.113.20"}, id="ip-only-on-icmp"),
        pytest.param(http_check(), {"url": "https://example.com/up"}, {"Url": "https://example.com/up"}, id="url-on-http"),
        pytest.param(tcp_check(), {"port": 8443}, {"Ip": "198.51.100.7", "Port": 8443}, id="port-only-on-tcp-keeps-the-stored-ip"),
        pytest.param(tcp_check(), {"ip": "198.51.100.9"}, {"Ip": "198.51.100.9", "Port": 22}, id="ip-only-on-tcp-keeps-the-stored-port"),
        pytest.param(tcp_check(), {"ip": "198.51.100.9", "port": 8443}, {"Ip": "198.51.100.9", "Port": 8443}, id="ip-and-port-on-tcp"),
        pytest.param(
            tcp_check(Target={"Ip": "2001:db8::7", "Port": 22, "Url": None}), {"port": 23}, {"Ip": "2001:db8::7", "Port": 23},
            id="a-stored-ipv6-address-is-kept",
        ),
        pytest.param(
            check(Type={"Id": 3, "Name": "ICMP"}, Target={"Ip": "198.51.100.7", "Port": 22, "Url": None}),
            {"port": 8443}, {"Ip": "198.51.100.7", "Port": 8443},
            id="the-type-is-matched-on-its-id-not-its-name",
        ),
    ],
)
async def test_a_target_change_reads_the_check_first_and_sends_its_complete_target(server, mock_gorelo, stored, args, target):
    route_target_update(mock_gorelo, stored)
    result = await call_tool(server, "update_uptime_check", {"check_id": CHECK_ID, **args})
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", CHECK_PATH), ("PATCH", CHECK_PATH), ("GET", CHECK_PATH)]
    read, patch, _reread = mock_gorelo.requests
    assert read.query == {} and read.json is None
    assert patch.json == {"Target": target} and patch.query == {}  # the complete target and nothing else
    assert result == stored


async def test_a_target_change_can_ride_along_with_other_fields(server, mock_gorelo):
    route_target_update(mock_gorelo, tcp_check())
    await call_tool(server, "update_uptime_check", {"check_id": CHECK_ID, "port": 8443, "frequency_minutes": 10, "region": "uk"})
    assert mock_gorelo.calls("PATCH")[0].json == {"Frequency": 10, "RegionId": 3, "Target": {"Ip": "198.51.100.7", "Port": 8443}}


@pytest.mark.parametrize(
    "stored, args, fragment",
    [
        pytest.param(check(), {"url": "https://x.example"}, "url: not valid for an ICMP check, which takes ip; omit it, or pass check_type with the complete target to change the type", id="url-on-icmp"),
        pytest.param(check(), {"port": 80}, "port: not valid for an ICMP check, which takes ip; omit it", id="port-on-icmp"),
        pytest.param(check(), {"ip": "10.0.0.1", "port": 80}, "port: not valid for an ICMP check, which takes ip; omit it", id="ip-and-port-on-icmp"),
        pytest.param(http_check(), {"ip": "10.0.0.1"}, "ip: not valid for an HTTP check, which takes url; omit it", id="ip-on-http"),
        pytest.param(http_check(), {"port": 443}, "port: not valid for an HTTP check, which takes url; omit it", id="port-on-http"),
        pytest.param(tcp_check(), {"url": "https://x.example"}, "url: not valid for a TCP check, which takes ip and port; omit it", id="url-on-tcp"),
        pytest.param(check(Type={"Id": 2, "Name": "ICMP"}), {"ip": "10.0.0.1"}, "ip: not valid for an HTTP check", id="the-type-is-matched-on-its-id-not-its-name"),
    ],
)
async def test_a_target_field_that_does_not_fit_the_stored_type_is_refused_and_nothing_is_written(server, mock_gorelo, stored, args, fragment):
    mock_gorelo.on("GET", CHECK_PATH, envelope(stored))
    text = await call_tool_error(server, "update_uptime_check", {"check_id": CHECK_ID, **args})
    assert fragment in text
    assert [r.method for r in mock_gorelo.requests] == ["GET"]  # the one read; no PATCH


@pytest.mark.parametrize(
    "stored, args, message",
    [
        pytest.param(
            tcp_check(Target={"Ip": "198.51.100.7", "Port": None, "Url": None}), {"ip": "198.51.100.9"},
            "port: the check has no stored port to keep; give ip and port together", id="no-stored-port",
        ),
        pytest.param(
            tcp_check(Target={"Ip": None, "Port": 22, "Url": None}), {"port": 23},
            "ip: the check has no stored ip to keep; give ip and port together", id="no-stored-ip",
        ),
        pytest.param(
            tcp_check(Target=None), {"port": 23},
            "ip: the check has no stored ip to keep; give ip and port together", id="no-stored-target",
        ),
        pytest.param(
            tcp_check(Target={"Ip": "  ", "Port": 22, "Url": None}), {"port": 23},
            "ip: the check has no stored ip to keep; give ip and port together", id="blank-stored-ip",
        ),
    ],
)
async def test_a_stored_target_that_cannot_complete_the_change_is_not_guessed(server, mock_gorelo, stored, args, message):
    mock_gorelo.on("GET", CHECK_PATH, envelope(stored))
    assert message in await call_tool_error(server, "update_uptime_check", {"check_id": CHECK_ID, **args})
    assert [r.method for r in mock_gorelo.requests] == ["GET"]


@pytest.mark.parametrize(
    "failure, expected",
    [
        pytest.param(
            error_envelope(404, [("070401", "Uptime check not found.", "checkId")]),
            "Gorelo rejected update_uptime_check (HTTP 404, code 070401): check_id: Uptime check not found.",
            id="404",
        ),
        pytest.param(
            error_envelope(500, [("070001", "Boom.")]), "Gorelo rejected update_uptime_check (HTTP 500, code 070001): Boom.", id="500"
        ),
        pytest.param(
            httpx.ReadTimeout("slow"),
            "Gorelo did not answer update_uptime_check (the request timed out). This was a read, so retrying is safe.",
            id="timeout",
        ),
        pytest.param(
            httpx.ConnectError("refused"),
            "Gorelo did not answer update_uptime_check (the connection failed). This was a read, so retrying is safe.",
            id="connection-lost",
        ),
    ],
)
async def test_a_failed_read_before_a_target_change_sends_no_patch(server, mock_gorelo, failure, expected):
    mock_gorelo.on("GET", CHECK_PATH, failure)
    text = await call_tool_error(server, "update_uptime_check", {"check_id": CHECK_ID, "port": 8443})
    assert expected in text
    assert mock_gorelo.calls("PATCH") == [] and len(mock_gorelo.calls("GET")) == 1  # nothing written, nothing retried


@pytest.mark.parametrize(
    "record",
    [
        pytest.param(check(Type=None), id="no-type"),
        pytest.param(check(Type={"Name": "TCP"}), id="type-without-an-id"),
        pytest.param(check(Type={"Id": 9, "Name": "SCTP"}), id="a-type-gorelo-added"),
        pytest.param(check(Type={"Id": True, "Name": "ICMP"}), id="boolean-type-id"),
        pytest.param(check(Type={"Id": "1", "Name": "ICMP"}), id="text-type-id"),
        pytest.param(check(Type="ICMP"), id="type-is-not-an-object"),
    ],
)
async def test_a_stored_type_that_cannot_be_read_stops_the_update_before_the_patch(server, mock_gorelo, record):
    mock_gorelo.on("GET", CHECK_PATH, envelope(record))
    text = await call_tool_error(server, "update_uptime_check", {"check_id": CHECK_ID, "ip": "10.0.0.2"})
    assert text.startswith(
        "Gorelo returned an unexpected response for update_uptime_check: GET /v1/uptime/{checkId}: the check has no "
        "readable Type.Id, so its target cannot be completed; refusing to guess. Nothing was changed"
    )
    assert [r.method for r in mock_gorelo.requests] == ["GET"]


@pytest.mark.parametrize("data", [{}, [], [check()], True, "text"], ids=["empty-object", "empty-list", "list", "true", "text"])
async def test_a_read_that_is_not_an_object_stops_the_update_before_the_patch(server, mock_gorelo, data):
    mock_gorelo.on("GET", CHECK_PATH, envelope(data))
    text = await call_tool_error(server, "update_uptime_check", {"check_id": CHECK_ID, "ip": "10.0.0.2"})
    assert text.startswith(
        "Gorelo returned an unexpected response for update_uptime_check: GET /v1/uptime/{checkId}: "
        "expected Data to be a non-empty object but got "
    )
    assert text.endswith("refusing to guess")  # a read: it says nothing about a write
    assert [r.method for r in mock_gorelo.requests] == ["GET"]


async def test_a_read_without_data_stops_the_update_before_the_patch(server, mock_gorelo):
    mock_gorelo.on("GET", CHECK_PATH, envelope(None))
    assert "Data is null; refusing to guess" in await call_tool_error(server, "update_uptime_check", {"check_id": CHECK_ID, "url": "https://x.example"})
    assert [r.method for r in mock_gorelo.requests] == ["GET"]


@pytest.mark.parametrize(
    "args",
    [
        {"ip": "router.example.com"},
        {"ip": "10.0.0.1 "},
        {"url": "ftp://x.example"},
        {"url": "example.com"},
        {"port": 0},
        {"port": 65536},
        {"url": "https://x.example", "ip": "10.0.0.1"},
        {"url": "https://x.example", "port": 80},
    ],
)
async def test_a_bad_target_value_is_refused_before_the_check_is_even_read(server, mock_gorelo, args):
    await call_tool_error(server, "update_uptime_check", {"check_id": CHECK_ID, **args})
    assert mock_gorelo.requests == []  # no GET and no PATCH: a mock without routes would fail the test


async def test_the_read_goes_to_the_check_in_canonical_form(server, mock_gorelo):
    route_target_update(mock_gorelo, check())
    await call_tool(server, "update_uptime_check", {"check_id": CHECK_ID.upper(), "ip": "203.0.113.20"})
    assert [r.path for r in mock_gorelo.requests] == [CHECK_PATH, CHECK_PATH, CHECK_PATH]


async def test_a_change_that_leaves_the_target_alone_reads_nothing_before_the_patch(server, mock_gorelo):
    route_update(mock_gorelo)
    await call_tool(server, "update_uptime_check", {"check_id": CHECK_ID, "description": "Backup router", "clear_tags": True})
    assert [r.method for r in mock_gorelo.requests] == ["PATCH", "GET"]
    assert "Target" not in mock_gorelo.requests[0].json


async def test_a_gorelo_error_on_the_patch_after_the_read_still_names_the_snake_case_param(server, mock_gorelo):
    mock_gorelo.on("GET", CHECK_PATH, envelope(tcp_check()))
    mock_gorelo.on("PATCH", CHECK_PATH, error_envelope(400, [("070101", "Rejected by Gorelo.", "Target.Port")]))
    text = await call_tool_error(server, "update_uptime_check", {"check_id": CHECK_ID, "port": 8443})
    assert text == f"Gorelo rejected update_uptime_check (HTTP 400, code 070101): port: Rejected by Gorelo. [trace {TEST_TRACE_ID}]"
    assert len(mock_gorelo.calls("PATCH")) == 1 and len(mock_gorelo.calls("GET")) == 1  # no re-read of a failed write


async def test_a_timeout_on_the_patch_after_the_read_says_to_verify_before_retrying(server, mock_gorelo):
    mock_gorelo.on("GET", CHECK_PATH, envelope(tcp_check()))
    mock_gorelo.on("PATCH", CHECK_PATH, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "update_uptime_check", {"check_id": CHECK_ID, "port": 8443})
    assert text.startswith("Gorelo did not confirm update_uptime_check (the request timed out).")
    assert "Verify with a read before retrying." in text
    assert len(mock_gorelo.calls("PATCH")) == 1


@pytest.mark.parametrize(
    "args, body",
    [
        ({"check_type": "icmp", "ip": "10.0.0.2"}, {"TypeId": 1, "Target": {"Ip": "10.0.0.2"}}),
        ({"check_type": "http", "url": "https://example.com"}, {"TypeId": 2, "Target": {"Url": "https://example.com"}}),
        ({"check_type": "tcp", "ip": "10.0.0.2", "port": 22}, {"TypeId": 3, "Target": {"Ip": "10.0.0.2", "Port": 22}}),
    ],
)
async def test_changing_the_type_sends_the_matching_target(server, mock_gorelo, args, body):
    route_update(mock_gorelo)
    await call_tool(server, "update_uptime_check", {"check_id": CHECK_ID, **args})
    assert mock_gorelo.requests[0].json == body
    # the complete target is given, so there is nothing to complete: PATCH first, then the re-read
    assert [r.method for r in mock_gorelo.requests] == ["PATCH", "GET"]


@pytest.mark.parametrize(
    "args, fragment",
    [
        pytest.param({"check_type": "icmp"}, "ip: required for an ICMP check", id="icmp-without-ip"),
        pytest.param({"check_type": "http"}, "url: required for an HTTP check", id="http-without-url"),
        pytest.param({"check_type": "tcp", "ip": "10.0.0.1"}, "port: required for a TCP check", id="tcp-without-port"),
        pytest.param({"check_type": "tcp", "port": 22}, "ip: required for a TCP check", id="tcp-without-ip"),
        pytest.param({"check_type": "icmp", "ip": "10.0.0.1", "url": "https://x.example"}, "url: not valid for an ICMP check", id="icmp-with-a-url"),
        pytest.param({"check_type": "http", "url": "https://x.example", "port": 80}, "port: not valid for an HTTP check", id="http-with-a-port"),
        pytest.param({"url": "https://x.example", "ip": "10.0.0.1"}, "url: cannot be combined with ip; a check targets either a URL (http) or an address (icmp, tcp)", id="url-and-ip"),
        pytest.param({"url": "https://x.example", "port": 80}, "url: cannot be combined with port;", id="url-and-port"),
        pytest.param({"url": "https://x.example", "ip": "10.0.0.1", "port": 80}, "url: cannot be combined with ip and port;", id="url-ip-and-port"),
        pytest.param({"url": "example.com"}, "url: must be a full address starting with http:// or https://", id="url-without-scheme"),
        pytest.param({"port": 0}, "port: must be a whole number from 1 to 65535", id="port-zero"),
        pytest.param({"ip": "a b"}, IP_ERROR, id="space-in-ip"),
        pytest.param({"ip": "router.example.com"}, IP_ERROR, id="host-name-without-a-type"),
        pytest.param({"check_type": "icmp", "ip": "router.example.com"}, IP_ERROR, id="icmp-host-name"),
        pytest.param({"check_type": "tcp", "ip": "localhost", "port": 22}, IP_ERROR, id="tcp-host-name"),
        pytest.param({"check_type": "icmp", "ip": "10.0.0.1/24"}, IP_ERROR, id="icmp-network"),
        pytest.param({"frequency_minutes": 0}, "frequency_minutes: must be a whole number from 1 to 2147483647", id="frequency-zero"),
        pytest.param({"retries_after_failure": -1}, "retries_after_failure: must be a whole number from 0 to 2147483647", id="negative-retries"),
        pytest.param({"client_id": 9102}, "location_id: required together with client_id", id="client-without-location"),
        pytest.param({"client_id": 0, "location_id": 77}, f"client_id: {POSITIVE}", id="client-id-zero"),
        pytest.param({"client_id": 5, "location_id": -1}, f"location_id: {POSITIVE}", id="location-id-negative"),
        pytest.param({"description": ""}, "description: must not be empty or whitespace only", id="blank-description"),
        pytest.param({"description": "x" * 251}, "description: at most 250 characters", id="description-too-long"),
        pytest.param({"isp_connection_link": "  "}, "isp_connection_link: must not be empty or whitespace only", id="blank-isp-link"),
        pytest.param({"isp_connection_link": "x" * 501}, "isp_connection_link: at most 500 characters", id="isp-link-too-long"),
        pytest.param({"tag_ids": []}, "tag_ids: an empty list is not accepted here; to remove every tag pass clear_tags=true", id="empty-tags-need-clear-tags"),
        pytest.param({"tag_ids": [0]}, f"tag_ids[0]: {POSITIVE}", id="tag-id-zero"),
        pytest.param({"tag_ids": [3, 2**63]}, "tag_ids[1]: expected a positive whole number such as 123, got a number above 9223372036854775807", id="tag-id-beyond-int64"),
        pytest.param({"tag_ids": [3], "clear_tags": True}, "tag_ids: cannot be combined with clear_tags=true", id="tags-and-clear-tags"),
    ],
)
async def test_update_local_errors_name_the_param_and_send_nothing(server, mock_gorelo, args, fragment):
    assert fragment in await call_tool_error(server, "update_uptime_check", {"check_id": CHECK_ID, **args})
    assert mock_gorelo.requests == []


async def test_clear_tags_sends_an_empty_tag_list_and_nothing_else(server, mock_gorelo):
    route_update(mock_gorelo, check(TagIds=[]))
    result = await call_tool(server, "update_uptime_check", {"check_id": CHECK_ID, "clear_tags": True})
    sent = mock_gorelo.requests[0].json
    assert sent == {"TagIds": []} and isinstance(sent["TagIds"], list)
    assert result["TagIds"] == []


async def test_clear_tags_can_ride_along_with_other_changes(server, mock_gorelo):
    route_update(mock_gorelo)
    await call_tool(server, "update_uptime_check", {"check_id": CHECK_ID, "clear_tags": True, "frequency_minutes": 15})
    assert mock_gorelo.requests[0].json == {"Frequency": 15, "TagIds": []}


async def test_clear_tags_false_is_the_same_as_not_asking(server, mock_gorelo):
    route_update(mock_gorelo)
    await call_tool(server, "update_uptime_check", {"check_id": CHECK_ID, "clear_tags": False, "frequency_minutes": 15})
    assert mock_gorelo.requests[0].json == {"Frequency": 15}


@pytest.mark.parametrize("args", [{}, {"clear_tags": False}, {"adopt_client_assets": None}])
async def test_an_update_without_any_change_is_refused_and_sends_nothing(server, mock_gorelo, args):
    text = await call_tool_error(server, "update_uptime_check", {"check_id": CHECK_ID, **args})
    assert text == "no change requested: give at least one field to change, or clear_tags=true"
    assert mock_gorelo.requests == []


async def test_an_update_can_never_carry_a_maintenance_window(server, mock_gorelo):
    route_update(mock_gorelo)
    everything = {
        "check_id": CHECK_ID, "check_type": "tcp", "ip": "10.0.0.2", "port": 22, "client_id": 1, "location_id": 2,
        "description": "d", "frequency_minutes": 5, "retries_after_failure": 1, "region": "uk",
        "isp_connection_link": "https://isp.example", "tag_ids": [1], "adopt_client_assets": True,
    }
    await call_tool(server, "update_uptime_check", everything)
    assert "MaintenanceMode" not in mock_gorelo.requests[0].json


@pytest.mark.parametrize("bad", ["", "41", "not-a-guid", "../clients", f"{CHECK_ID} "])
async def test_update_needs_a_guid_and_names_check_id(server, mock_gorelo, bad):
    text = await call_tool_error(server, "update_uptime_check", {"check_id": bad, "frequency_minutes": 5})
    assert text.startswith(GUID_ERROR)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "args, param",
    [({"check_type": "ping"}, "check_type"), ({"region": "mars"}, "region"), ({"frequency_minutes": "fast"}, "frequency_minutes"), ({"clear_tags": "maybe"}, "clear_tags")],
)
async def test_update_refuses_a_value_outside_the_schema_with_the_param_name(server, mock_gorelo, args, param):
    assert param in await call_tool_error(server, "update_uptime_check", {"check_id": CHECK_ID, **args})
    assert mock_gorelo.requests == []


async def test_calling_update_directly_with_an_unknown_type_is_refused(client_factory, mock_gorelo):
    with pytest.raises(ToolError, match="check_type: must be one of 'icmp', 'http', 'tcp', got 'ping'"):
        await call_directly(client_factory, uptime.update_uptime_check, check_id=CHECK_ID, check_type="ping", ip="x")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "property_name, param",
    [("Ip", "ip"), ("Url", "url"), ("Port", "port"), ("Frequency", "frequency_minutes"), ("RegionId", "region"), ("TypeId", "check_type"),
     ("TagIds", "tag_ids"), ("LocationId", "location_id"), ("ClientId", "client_id"), ("checkId", "check_id"), ("CheckId", "check_id"), ("MaintenanceMode", "MaintenanceMode")],
)
async def test_a_gorelo_error_on_update_names_the_snake_case_param(server, mock_gorelo, property_name, param):
    # MaintenanceMode is not a parameter of update_uptime_check: Gorelo's own name stays
    mock_gorelo.on("PATCH", CHECK_PATH, error_envelope(400, [("070101", "Rejected by Gorelo.", property_name)]))
    text = await call_tool_error(server, "update_uptime_check", {"check_id": CHECK_ID, "frequency_minutes": 5})
    assert text == f"Gorelo rejected update_uptime_check (HTTP 400, code 070101): {param}: Rejected by Gorelo. [trace {TEST_TRACE_ID}]"
    assert mock_gorelo.calls("GET") == []


async def test_a_gorelo_400_for_an_empty_patch_is_shown_as_it_is(server, mock_gorelo):
    mock_gorelo.on("PATCH", CHECK_PATH, error_envelope(400, [("070101", "The request must contain at least one field to update.")]))
    text = await call_tool_error(server, "update_uptime_check", {"check_id": CHECK_ID, "frequency_minutes": 5})
    assert "The request must contain at least one field to update." in text


@pytest.mark.parametrize(
    "data", [None, True, False, {}, [], [{"Id": CHECK_ID}], "ok"], ids=["null", "true", "false", "empty-object", "empty-list", "list", "text"]
)
async def test_an_update_answer_that_is_not_an_object_is_not_taken_for_a_clean_success(server, mock_gorelo, data):
    mock_gorelo.on("PATCH", CHECK_PATH, envelope(data))
    text = await call_tool_error(server, "update_uptime_check", {"check_id": CHECK_ID, "frequency_minutes": 5})
    assert text.startswith(
        "Gorelo returned an unexpected response for update_uptime_check: PATCH /v1/uptime/{checkId}: "
        "expected Data to be a non-empty object but got "
    )
    assert "the write may have been applied, so verify it with a read before repeating it" in text
    assert mock_gorelo.calls("GET") == [] and len(mock_gorelo.calls("PATCH")) == 1  # nothing re-read, nothing repeated


async def test_a_failed_reread_after_a_successful_update_returns_a_warning(server, mock_gorelo):
    mock_gorelo.on("PATCH", CHECK_PATH, envelope({"Id": CHECK_ID}))
    mock_gorelo.on("GET", CHECK_PATH, httpx.ReadTimeout("slow"))
    result = await call_tool(server, "update_uptime_check", {"check_id": CHECK_ID, "frequency_minutes": 5})
    assert set(result) == {"Id", "warning"} and result["Id"] == CHECK_ID
    assert "the write succeeded; re-reading it failed: GET /v1/uptime/{checkId} timed out" in result["warning"]
    assert len(mock_gorelo.calls("PATCH")) == 1 and len(mock_gorelo.calls("GET")) == 1


async def test_an_update_that_times_out_is_not_retried_and_says_to_verify(server, mock_gorelo):
    mock_gorelo.on("PATCH", CHECK_PATH, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "update_uptime_check", {"check_id": CHECK_ID, "frequency_minutes": 5})
    assert text.startswith("Gorelo did not confirm update_uptime_check (the request timed out).")
    assert "Verify with a read before retrying." in text
    assert len(mock_gorelo.requests) == 1


# --------------------------------------------------------------------------
# set_uptime_maintenance
# --------------------------------------------------------------------------


START = "2026-10-02T22:00:00Z"  # Gorelo refuses a window without a start (live, 2026-10-02)


async def test_starting_a_window_sends_only_maintenance_mode_then_rereads_the_check(server, mock_gorelo):
    window = {"Enabled": True, "StartDateTime": "2026-10-03T03:00:00Z", "DurationInMinutes": 90, "Reason": "Router firmware"}
    route_update(mock_gorelo, check(MaintenanceMode=window))
    args = {
        "check_id": CHECK_ID,
        "enabled": True,
        "start": "2026-10-02T22:00:00-05:00",
        "duration_minutes": 90,
        "reason": "Router firmware",
    }
    result = await call_tool(server, "set_uptime_maintenance", args)
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("PATCH", CHECK_PATH), ("GET", CHECK_PATH)]
    patch = mock_gorelo.requests[0]
    assert patch.json == {  # nothing but MaintenanceMode, and the start is converted to UTC
        "MaintenanceMode": {
            "Enabled": True,
            "StartDateTime": "2026-10-03T03:00:00Z",
            "DurationInMinutes": 90,
            "Reason": "Router firmware",
        }
    }
    assert patch.query == {}
    assert result["MaintenanceMode"] == window


async def test_a_window_with_a_start_a_duration_and_a_reason_sends_all_three(server, mock_gorelo):
    route_update(mock_gorelo)
    args = {"check_id": CHECK_ID, "enabled": True, "start": START, "duration_minutes": 30, "reason": "Patching"}
    await call_tool(server, "set_uptime_maintenance", args)
    assert mock_gorelo.requests[0].json == {
        "MaintenanceMode": {"Enabled": True, "StartDateTime": START, "DurationInMinutes": 30, "Reason": "Patching"}
    }


async def test_a_duration_of_zero_is_sent_because_it_means_the_window_never_expires(server, mock_gorelo):
    route_update(mock_gorelo)
    args = {"check_id": CHECK_ID, "enabled": True, "start": START, "duration_minutes": 0, "reason": "Decommissioning"}
    await call_tool(server, "set_uptime_maintenance", args)
    sent = mock_gorelo.requests[0].json["MaintenanceMode"]
    assert sent == {"Enabled": True, "StartDateTime": START, "DurationInMinutes": 0, "Reason": "Decommissioning"}
    assert "DurationInMinutes" in sent


async def test_ending_a_window_sends_enabled_false_and_nothing_else(server, mock_gorelo):
    route_update(mock_gorelo)
    await call_tool(server, "set_uptime_maintenance", {"check_id": CHECK_ID, "enabled": False})
    assert mock_gorelo.requests[0].json == {"MaintenanceMode": {"Enabled": False}}


@pytest.mark.parametrize(
    "extra, names",
    [
        ({"start": "2026-10-02T22:00:00Z"}, "start"),
        ({"duration_minutes": 0}, "duration_minutes"),
        ({"reason": "done"}, "reason"),
        ({"start": "2026-10-02T22:00:00Z", "duration_minutes": 5, "reason": "x"}, "start, duration_minutes, reason"),
    ],
)
async def test_ending_a_window_takes_no_other_field(server, mock_gorelo, extra, names):
    text = await call_tool_error(server, "set_uptime_maintenance", {"check_id": CHECK_ID, "enabled": False, **extra})
    assert text == f"{names}: only valid with enabled=true; ending a window takes no other field"
    assert mock_gorelo.requests == []


DURATION_REQUIRED = (
    "duration_minutes: required when enabled=true; ask the user how long the window should last, in minutes. "
    "0 is allowed and means the window never expires (it hides outages until it is ended with enabled=false)"
)
START_REQUIRED = (
    "start: required when enabled=true; ask the user when the window should begin. Pass the current time "
    "(ISO 8601 with a UTC offset, for example 2026-10-02T14:30:00Z) only if the user wants maintenance to "
    "begin now. Gorelo refuses a window without a start"
)


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"start": START}, id="start-only"),
        pytest.param({"start": START, "reason": "Patching"}, id="start-and-reason"),
        pytest.param({"start": START, "duration_minutes": None}, id="explicit-null-duration"),
    ],
)
async def test_beginning_a_window_without_a_duration_is_refused_and_nothing_is_sent(server, mock_gorelo, extra):
    # no default length: a window nobody gave a length would be invented, and one that never expires hides outages
    text = await call_tool_error(server, "set_uptime_maintenance", {"check_id": CHECK_ID, "enabled": True, **extra})
    assert text == DURATION_REQUIRED
    assert mock_gorelo.requests == []  # not even the re-read


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"duration_minutes": 30}, id="duration-only"),
        pytest.param({"duration_minutes": 0}, id="zero-duration"),
        pytest.param({"duration_minutes": 30, "reason": "Patching"}, id="duration-and-reason"),
        pytest.param({"duration_minutes": 30, "start": None}, id="explicit-null-start"),
    ],
)
async def test_beginning_a_window_without_a_start_is_refused_and_nothing_is_sent(server, mock_gorelo, extra):
    # live, 2026-10-02: Gorelo answers 400 "MaintenanceMode.StartDateTime is required when enabling maintenance mode."
    # The tool never reads the clock: the model passes the current time, and only when the user wants it now
    text = await call_tool_error(server, "set_uptime_maintenance", {"check_id": CHECK_ID, "enabled": True, **extra})
    assert text == START_REQUIRED
    assert "only if the user wants maintenance to begin now" in text and "ask the user when the window should begin" in text
    assert mock_gorelo.requests == []  # no HTTP at all, not even the re-read


@pytest.mark.parametrize(
    "extra", [pytest.param({}, id="enabled-alone"), pytest.param({"reason": "Patching"}, id="reason-only"), pytest.param({"start": None, "duration_minutes": None}, id="both-null")]
)
async def test_beginning_a_window_without_a_start_or_a_duration_names_both(server, mock_gorelo, extra):
    text = await call_tool_error(server, "set_uptime_maintenance", {"check_id": CHECK_ID, "enabled": True, **extra})
    assert text == f"{START_REQUIRED}; {DURATION_REQUIRED}"
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("minutes", [0, 1, 90, 2**31 - 1])
async def test_a_window_with_any_duration_from_zero_up_is_sent_and_only_the_reason_stays_optional(server, mock_gorelo, minutes):
    route_update(mock_gorelo)
    await call_tool(server, "set_uptime_maintenance", {"check_id": CHECK_ID, "enabled": True, "start": START, "duration_minutes": minutes})
    assert mock_gorelo.requests[0].json == {
        "MaintenanceMode": {"Enabled": True, "StartDateTime": START, "DurationInMinutes": minutes}
    }
    assert "Reason" not in mock_gorelo.requests[0].json["MaintenanceMode"]  # nothing is invented for the reason


async def test_the_start_and_duration_rules_leave_the_schema_alone_because_ending_a_window_needs_neither(server):
    # the requirement depends on enabled, which a JSON schema cannot say: the schema keeps only the always-needed pair
    tools = {tool.name: tool for tool in await list_tools(server)}
    assert tools["set_uptime_maintenance"].inputSchema["required"] == ["check_id", "enabled"]
    assert {"start", "duration_minutes"} <= set(tools["set_uptime_maintenance"].inputSchema["properties"])


async def test_ending_a_window_still_refuses_a_duration_even_zero(server, mock_gorelo):
    text = await call_tool_error(server, "set_uptime_maintenance", {"check_id": CHECK_ID, "enabled": False, "duration_minutes": 0})
    assert text == "duration_minutes: only valid with enabled=true; ending a window takes no other field"
    assert mock_gorelo.requests == []


async def test_a_window_without_a_reason_sends_none(server, mock_gorelo):
    route_update(mock_gorelo)
    await call_tool(server, "set_uptime_maintenance", {"check_id": CHECK_ID, "enabled": True, "start": START, "duration_minutes": 30})
    assert mock_gorelo.requests[0].json == {
        "MaintenanceMode": {"Enabled": True, "StartDateTime": START, "DurationInMinutes": 30}
    }


async def test_only_the_maintenance_params_that_are_given_are_sent(server, mock_gorelo):
    route_update(mock_gorelo)
    await call_tool(
        server, "set_uptime_maintenance",
        {"check_id": CHECK_ID, "enabled": True, "duration_minutes": 15, "start": "2026-10-02T22:00:00Z"},
    )  # start and duration are required; the reason is the only optional field and is not invented
    assert mock_gorelo.requests[0].json == {
        "MaintenanceMode": {"Enabled": True, "StartDateTime": "2026-10-02T22:00:00Z", "DurationInMinutes": 15}
    }


@pytest.mark.parametrize(
    "extra, fragment",
    [
        pytest.param({"start": "2026-10-02T22:00:00"}, "start: '2026-10-02T22:00:00' has no UTC offset", id="naive-start"),
        pytest.param({"start": "tonight"}, "start: 'tonight' is not an ISO 8601 datetime", id="garbage-start"),
        pytest.param({"duration_minutes": -1}, "duration_minutes: must be a whole number from 0 to 2147483647", id="negative-duration"),
        pytest.param({"duration_minutes": 2**31}, "duration_minutes: must be a whole number from 0 to 2147483647", id="duration-beyond-int32"),
        pytest.param({"reason": "   "}, "reason: must not be empty or whitespace only", id="blank-reason"),
        pytest.param({"reason": "x" * 501}, "reason: at most 500 characters", id="reason-too-long"),
    ],
)
async def test_maintenance_local_errors_name_the_param_and_send_nothing(server, mock_gorelo, extra, fragment):
    args = {"check_id": CHECK_ID, "enabled": True, "start": START, "duration_minutes": 30, "reason": "Patching", **extra}
    assert fragment in await call_tool_error(server, "set_uptime_maintenance", args)
    assert mock_gorelo.requests == []


async def test_a_reason_of_exactly_500_characters_is_accepted(server, mock_gorelo):
    route_update(mock_gorelo)
    await call_tool(server, "set_uptime_maintenance", {"check_id": CHECK_ID, "enabled": True, "start": START, "duration_minutes": 30, "reason": "x" * 500})
    assert len(mock_gorelo.requests[0].json["MaintenanceMode"]["Reason"]) == 500


@pytest.mark.parametrize("bad", ["", "41", "not-a-guid", f" {CHECK_ID}"])
async def test_maintenance_needs_a_guid_and_names_check_id(server, mock_gorelo, bad):
    text = await call_tool_error(server, "set_uptime_maintenance", {"check_id": bad, "enabled": False})
    assert text.startswith(GUID_ERROR)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "args, param",
    [({"check_id": CHECK_ID}, "enabled"), ({"check_id": CHECK_ID, "enabled": "maybe"}, "enabled"), ({"enabled": False}, "check_id")],
)
async def test_maintenance_refuses_a_missing_or_malformed_value_with_the_param_name(server, mock_gorelo, args, param):
    assert param in await call_tool_error(server, "set_uptime_maintenance", args)
    assert mock_gorelo.requests == []


async def test_calling_maintenance_directly_without_a_real_boolean_is_refused(client_factory, mock_gorelo):
    for bad in (None, "yes", 1):
        with pytest.raises(ToolError, match="enabled: must be true"):
            await call_directly(client_factory, uptime.set_uptime_maintenance, check_id=CHECK_ID, enabled=bad)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "property_name, param",
    [("Enabled", "enabled"), ("StartDateTime", "start"), ("DurationInMinutes", "duration_minutes"), ("Reason", "reason"),
     ("MaintenanceMode.Reason", "reason"), ("checkId", "check_id")],
)
async def test_a_gorelo_error_on_maintenance_names_the_snake_case_param(server, mock_gorelo, property_name, param):
    mock_gorelo.on("PATCH", CHECK_PATH, error_envelope(400, [("070101", "Rejected by Gorelo.", property_name)]))
    args = {"check_id": CHECK_ID, "enabled": True, "start": START, "duration_minutes": 30, "reason": "Patching"}
    text = await call_tool_error(server, "set_uptime_maintenance", args)
    assert text == f"Gorelo rejected set_uptime_maintenance (HTTP 400, code 070101): {param}: Rejected by Gorelo. [trace {TEST_TRACE_ID}]"


@pytest.mark.parametrize(
    "data", [None, True, False, {}, [], [{"Id": CHECK_ID}], "ok"], ids=["null", "true", "false", "empty-object", "empty-list", "list", "text"]
)
async def test_a_maintenance_answer_that_is_not_an_object_is_not_taken_for_a_clean_success(server, mock_gorelo, data):
    mock_gorelo.on("PATCH", CHECK_PATH, envelope(data))
    text = await call_tool_error(server, "set_uptime_maintenance", {"check_id": CHECK_ID, "enabled": False})
    assert text.startswith(
        "Gorelo returned an unexpected response for set_uptime_maintenance: PATCH /v1/uptime/{checkId}: "
        "expected Data to be a non-empty object but got "
    )
    assert "the write may have been applied, so verify it with a read before repeating it" in text
    assert mock_gorelo.calls("GET") == [] and len(mock_gorelo.calls("PATCH")) == 1


async def test_a_failed_reread_after_a_successful_maintenance_change_returns_a_warning(server, mock_gorelo):
    mock_gorelo.on("PATCH", CHECK_PATH, envelope({"Id": CHECK_ID}))
    mock_gorelo.on("GET", CHECK_PATH, error_envelope(500, [("070001", "Boom.")]))
    result = await call_tool(server, "set_uptime_maintenance", {"check_id": CHECK_ID, "enabled": False})
    assert set(result) == {"Id", "warning"} and "the change" not in result["warning"] and "Do not repeat the write" in result["warning"]
    assert len(mock_gorelo.calls("PATCH")) == 1 and len(mock_gorelo.calls("GET")) == 1


async def test_a_maintenance_change_that_times_out_is_not_retried_and_says_to_verify(server, mock_gorelo):
    mock_gorelo.on("PATCH", CHECK_PATH, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "set_uptime_maintenance", {"check_id": CHECK_ID, "enabled": False})
    assert text.startswith("Gorelo did not confirm set_uptime_maintenance (the request timed out).")
    assert "Verify with a read before retrying." in text
    assert len(mock_gorelo.requests) == 1


async def test_the_maintenance_body_never_carries_anything_but_maintenance_mode(server, mock_gorelo):
    route_update(mock_gorelo)
    for args in (
        {"enabled": False},
        {"enabled": True, "start": START, "duration_minutes": 0},
        {"enabled": True, "start": START, "duration_minutes": 0, "reason": "r"},
        {"enabled": True, "duration_minutes": 5, "reason": "r", "start": "2026-10-02T22:00:00Z"},
    ):
        await call_tool(server, "set_uptime_maintenance", {"check_id": CHECK_ID, **args})
        assert set(mock_gorelo.calls("PATCH")[-1].json) == {"MaintenanceMode"}
    assert json.dumps(mock_gorelo.calls("PATCH")[-1].json).count("Target") == 0


# --------------------------------------------------------------------------
# delete_uptime_check
# --------------------------------------------------------------------------


async def test_delete_refuses_without_confirm_and_makes_no_http_call(server, mock_gorelo):
    for args in ({"check_id": CHECK_ID}, {"check_id": CHECK_ID, "confirm": False}):
        text = await call_tool_error(server, "delete_uptime_check", args)
        assert text.startswith(f"confirm: refusing to delete uptime check {CHECK_ID} without confirm=true.")
        assert "the target is no longer monitored" in text
        assert f"Call again with confirm=true if you really want to delete uptime check {CHECK_ID}." in text
    assert mock_gorelo.requests == []


async def test_a_confirmed_delete_sends_one_delete_and_returns_gorelos_record(server, mock_gorelo):
    mock_gorelo.on("DELETE", CHECK_PATH, envelope({"Id": CHECK_ID}))
    result = await call_tool(server, "delete_uptime_check", {"check_id": CHECK_ID, "confirm": True})
    assert result == {"Id": CHECK_ID}
    request = mock_gorelo.last
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("DELETE", CHECK_PATH)]
    assert request.query == {} and request.json is None


@pytest.mark.parametrize(
    "data", [None, True, False, {}, [], [{"Id": CHECK_ID}], "ok", 5], ids=["null", "true", "false", "empty-object", "empty-list", "list", "text", "number"]
)
async def test_a_delete_answer_that_is_not_an_object_raises_instead_of_reporting_success(server, mock_gorelo, data):
    mock_gorelo.on("DELETE", CHECK_PATH, envelope(data))
    text = await call_tool_error(server, "delete_uptime_check", {"check_id": CHECK_ID, "confirm": True})
    assert text.startswith(
        "Gorelo returned an unexpected response for delete_uptime_check: DELETE /v1/uptime/{checkId}: "
        "expected Data to be a non-empty object but got "
    )
    assert "the write may have been applied, so verify it with a read before repeating it" in text
    assert len(mock_gorelo.calls("DELETE")) == 1  # never repeated


async def test_delete_404_names_check_id(server, mock_gorelo):
    mock_gorelo.on("DELETE", CHECK_PATH, error_envelope(404, [("070401", "Uptime check not found.", "checkId")]))
    text = await call_tool_error(server, "delete_uptime_check", {"check_id": CHECK_ID, "confirm": True})
    assert text == f"Gorelo rejected delete_uptime_check (HTTP 404, code 070401): check_id: Uptime check not found. [trace {TEST_TRACE_ID}]"


async def test_a_delete_that_times_out_is_not_retried_and_says_to_verify(server, mock_gorelo):
    mock_gorelo.on("DELETE", CHECK_PATH, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "delete_uptime_check", {"check_id": CHECK_ID, "confirm": True})
    assert text.startswith("Gorelo did not confirm delete_uptime_check (the request timed out).")
    assert "Verify with a read before retrying." in text
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize("bad", ["", "5", "router", f"{CHECK_ID}/../clients", f" {CHECK_ID}"])
async def test_delete_needs_a_guid_and_names_check_id_even_without_confirm(server, mock_gorelo, bad):
    text = await call_tool_error(server, "delete_uptime_check", {"check_id": bad})
    assert text.startswith(GUID_ERROR)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", ["true", "yes", 1, "1"], ids=["text-true", "text-yes", "number-1", "text-1"])
async def test_confirm_is_a_strict_boolean_so_nothing_but_true_deletes(server, mock_gorelo, bad):
    text = await call_tool_error(server, "delete_uptime_check", {"check_id": CHECK_ID, "confirm": bad})
    assert "confirm" in text
    assert mock_gorelo.requests == []


async def test_delete_uptime_check_does_not_exist_unless_delete_tools_are_enabled(server_factory, mock_gorelo):
    off = server_factory()
    names = {tool.name for tool in await list_tools(off)}
    assert set(EXPECTED_TOOLS) - {"delete_uptime_check"} <= names and "delete_uptime_check" not in names
    assert "unknown tool" in (await call_tool_error(off, "delete_uptime_check", {"check_id": CHECK_ID, "confirm": True})).lower()
    assert mock_gorelo.requests == []


async def test_the_uptime_tools_belong_to_the_uptime_toolset(server_factory):
    names = set(EXPECTED_TOOLS)
    assert not names & {t.name for t in await list_tools(server_factory(toolsets={"core", "billing"}, destructive=True))}
    assert names <= {t.name for t in await list_tools(server_factory(toolsets={"uptime"}, destructive=True))}
