"""tools/assets.py: list_agents, get_agent, list_custom_assets (read only)."""

import json
from pathlib import Path

import pytest
from conftest import (
    TEST_TRACE_ID,
    call_tool,
    call_tool_error,
    envelope,
    error_envelope,
    list_tools,
    paged_envelope,
    paged_responder,
    pagination,
    uid,
)

import tools.assets as assets_module
from gorelo_client import FORBIDDEN_OPS
from tools._common import REGISTRY

pytestmark = pytest.mark.anyio

AGENTS = "/v1/assets/agents"
CUSTOM = "/v1/assets/custom"

FILTERS = ["query", "created_since", "created_before", "updated_since", "updated_before", "page_size", "cursor"]
TOOLS = {
    # name: (ops, parameters in order, required parameters)
    "list_agents": (["GET /v1/assets/agents"], ["status_ids", "client_ids", *FILTERS], []),
    "get_agent": (["GET /v1/assets/agents/{deviceId}"], ["agent_id"], ["agent_id"]),
    "list_custom_assets": (["GET /v1/assets/custom"], ["client_ids", *FILTERS], []),
}

ALL_DATES = {
    "created_since": "2026-01-01T00:00:00Z",
    "created_before": "2026-02-01T09:00:00-05:00",
    "updated_since": "2026-03-01T00:00:00+00:00",
    "updated_before": "2026-04-01T12:30:15.250000Z",
}
ALL_DATES_SENT = {
    "CreatedSince": "2026-01-01T00:00:00Z",
    "CreatedBefore": "2026-02-01T14:00:00Z",
    "UpdatedSince": "2026-03-01T00:00:00Z",
    "UpdatedBefore": "2026-04-01T12:30:15.250000Z",
}
ALL_DATES_SHOWN = {
    "created_since": "2026-01-01T00:00:00Z",
    "created_before": "2026-02-01T14:00:00Z",
    "updated_since": "2026-03-01T00:00:00Z",
    "updated_before": "2026-04-01T12:30:15.250000Z",
}


@pytest.fixture
def server(server_factory):
    return server_factory(toolsets={"core"})


def agent_record(n=1, **extra):
    record = {
        "Id": uid(n),
        "Name": f"HOST-{n:02d}",
        "DisplayName": f"Host {n}",
        "Status": {"Id": 1, "Name": "Online"},
        "ClientId": 9101,
        "LocationId": 9001,
        "CreatedOn": "2026-01-01T00:00:00Z",
        "UpdatedOn": None,
        "WarrantyStartDate": "2025-01-01T00:00:00Z",
        "WarrantyEndDate": "2028-01-01T00:00:00Z",
        "Os": "Windows 11 Pro",
        "SerialNo": "SN123",
        "SecurityPosture": {"Score": 80},
    }
    record.update(extra)
    return record


def custom_record(n=1, **extra):
    record = {
        "Id": uid(n),
        "Name": f"Switch {n}",
        "ClientId": 9101,
        "LocationId": None,
        "HardwareType": "Network",
        "Description": "Core switch",
        "IpAddress": "10.0.0.2",
        "SerialNumber": None,
        "Banner": "",
        "WarrantyEndDate": None,
        "CreatedOn": "2026-08-21T00:00:00Z",
        "UpdatedOn": None,
    }
    record.update(extra)
    return record


def trace(text):
    return f"{text} [trace {TEST_TRACE_ID}]"


def flat(text):
    return " ".join(text.split())


# --------------------------------------------------------------------------
# Declarations
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_each_tool_is_declared_as_a_core_read_tool(name, spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == name)
    assert (spec.toolset, spec.kind, spec.ops, spec.destructive_hint) == ("core", "read", TOOLS[name][0], False)
    assert all(op in spec_index.ops for op in spec.ops) and not set(spec.ops) & FORBIDDEN_OPS


@pytest.mark.parametrize("name", sorted(TOOLS))
async def test_each_tool_exposes_exactly_the_documented_parameters(name, server):
    _ops, params, required = TOOLS[name]
    tool = next(t for t in await list_tools(server) if t.name == name)
    assert list(tool.inputSchema["properties"]) == params
    assert tool.inputSchema.get("required", []) == required
    assert all(p.get("description") for p in tool.inputSchema["properties"].values())
    assert (tool.annotations.readOnlyHint, tool.annotations.destructiveHint) == (True, False)


@pytest.mark.parametrize("name, default", [("list_agents", 100), ("list_custom_assets", 100)])
async def test_the_page_size_default_is_declared_in_the_schema(server, name, default):
    tool = next(t for t in await list_tools(server) if t.name == name)
    assert tool.inputSchema["properties"]["page_size"]["default"] == default


@pytest.mark.parametrize("name", ["list_agents", "list_custom_assets"])
def test_every_query_name_of_a_list_tool_is_a_query_param_of_its_op(name, spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == name)
    op = spec_index.op(spec.ops[0])
    assert {path.lower() for path in spec.field_map.values()} == {q.lower() for q in op.query_params}


def test_the_field_map_of_get_agent_names_the_path_placeholder(spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == "get_agent")
    assert spec.field_map == {"agent_id": "deviceId"}
    assert set(spec.field_map.values()) == set(spec_index.op("GET /v1/assets/agents/{deviceId}").path_placeholders)


def test_the_agent_list_rows_and_the_single_agent_are_the_same_fields_in_the_spec(spec_index):
    """The tool text says a list row (DeviceListItemResponse) has the fields of the single record (DeviceResponse)."""
    assert spec_index.op("GET /v1/assets/agents").response["data"] == "[DeviceListItemResponse]"
    assert spec_index.op("GET /v1/assets/agents/{deviceId}").response["data"] == "DeviceResponse"
    row, record = spec_index.schema("DeviceListItemResponse")["fields"], spec_index.schema("DeviceResponse")["fields"]
    assert row == record
    assert {"LocationId", "WarrantyStartDate", "WarrantyEndDate"} <= set(row) and "ClientLocationId" not in row
    doc = flat(assets_module.__doc__)
    assert "DeviceListItemResponse" in doc and "DeviceResponse" in doc and "the same fields" in doc


LATEST_SPEC = Path(__file__).resolve().parent.parent / "backups" / "swagger-latest.json"


@pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")
def test_the_deleted_and_inactivated_device_promise_is_the_one_in_the_spec_text():
    """spec/spec_index.json keeps no description text: what list_agents promises about devices that are never listed is
    pinned to the StatusIds text of the full OpenAPI snapshot (unlike the client list, the agent list still excludes them
    whatever StatusIds says, in contract e15cb5a18ec2)."""
    parameters = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))["paths"]["/v1/assets/agents"]["get"]["parameters"]
    text = " ".join(next(p for p in parameters if p["name"] == "StatusIds")["description"].split())
    assert "Deleted and client-inactivated devices are excluded either way" in text
    assert "Deleted and client-inactivated devices are never listed" in flat(assets_module.list_agents.__doc__)


def test_there_is_no_tool_that_can_delete_or_change_an_asset():
    mine = [s for s in REGISTRY.specs if set(s.ops) & {
        "GET /v1/assets/agents", "GET /v1/assets/agents/{deviceId}", "GET /v1/assets/custom"
    }]
    assert {s.name for s in mine} == set(TOOLS)
    for spec in mine:
        assert spec.kind == "read" and all(op.startswith("GET ") for op in spec.ops)
    assert "DELETE /v1/assets/agents/{deviceId}" in FORBIDDEN_OPS and "DELETE /v1/assets/custom/{customAssetId}" in FORBIDDEN_OPS


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_docstrings_follow_the_template(name):
    doc = next(s for s in REGISTRY.specs if s.name == name).fn.__doc__
    assert "Side effects:" not in doc  # read only: nothing to state
    assert ("Paging:" in doc) == (name != "get_agent")


@pytest.mark.parametrize(
    "name, param, resolver",
    [
        ("list_agents", "client_ids", "list_clients"),
        ("list_custom_assets", "client_ids", "list_clients"),
        ("get_agent", "agent_id", "list_agents"),
    ],
)
async def test_each_id_description_names_the_tool_that_resolves_it(server, name, param, resolver):
    tool = next(t for t in await list_tools(server) if t.name == name)
    assert resolver in tool.inputSchema["properties"][param]["description"]


def test_the_agent_list_docstring_names_the_renamed_fields():
    text = flat(assets_module.list_agents.__doc__)
    assert "LocationId" in text and "WarrantyStartDate" in text and "WarrantyEndDate" in text
    assert "2026-08-21" in text


def test_the_asset_docstrings_say_this_server_cannot_delete():
    assert "cannot delete or uninstall agents" in flat(assets_module.list_agents.__doc__)
    assert "cannot delete custom assets" in flat(assets_module.list_custom_assets.__doc__)


def test_the_custom_asset_docstring_says_there_is_no_status_filter():
    assert "No status filter" in flat(assets_module.list_custom_assets.__doc__)


# --------------------------------------------------------------------------
# list_agents
# --------------------------------------------------------------------------


async def test_list_agents_with_no_filters_sends_only_the_default_page_size_of_100(server, mock_gorelo):
    rows = [agent_record(1), agent_record(2)]
    mock_gorelo.on("GET", AGENTS, paged_envelope(rows, total_count=120))
    result = await call_tool(server, "list_agents")
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", AGENTS, {"PageSize": "100"}, b"")
    assert result == {
        "items": rows, "count": 2, "total_count": 120, "has_more": False, "next_cursor": None,
        "page_size": 100, "filters": {},
    }


async def test_list_agents_passes_the_renamed_fields_through_untouched(server, mock_gorelo):
    row = agent_record(1)
    assert "LocationId" in row and "WarrantyStartDate" in row and "WarrantyEndDate" in row
    mock_gorelo.on("GET", AGENTS, paged_envelope([row], total_count=1))
    result = await call_tool(server, "list_agents")
    assert result["items"] == [row]
    assert "ClientLocationId" not in result["items"][0] and "WarrantyExpiryDate" not in result["items"][0]


async def test_list_agents_sends_every_filter_under_its_spec_name(server, mock_gorelo):
    mock_gorelo.on("GET", AGENTS, paged_envelope([agent_record()], next_cursor="c2", total_count=120))
    result = await call_tool(
        server,
        "list_agents",
        {"status_ids": [1, 2], "client_ids": [9101, 9102], "query": "HOST", **ALL_DATES, "page_size": 40, "cursor": "c1"},
    )
    assert mock_gorelo.last.query == {
        "StatusIds": "1,2",
        "ClientIds": "9101,9102",
        "Query": "HOST",
        **ALL_DATES_SENT,
        "PageSize": "40",
        "Cursor": "c1",
    }
    assert result["has_more"] is True and result["next_cursor"] == "c2" and result["page_size"] == 40
    assert result["filters"] == {"status_ids": [1, 2], "client_ids": [9101, 9102], "query": "HOST", **ALL_DATES_SHOWN}


async def test_list_agents_follows_the_cursor_with_the_same_filters(server, mock_gorelo):
    mock_gorelo.on("GET", AGENTS, paged_responder([[agent_record(1)], [agent_record(2)]], total_count=2))
    first = await call_tool(server, "list_agents", {"client_ids": [9101], "page_size": 1})
    second = await call_tool(server, "list_agents", {"client_ids": [9101], "page_size": 1, "cursor": first["next_cursor"]})
    assert first["has_more"] is True and second["has_more"] is False and second["items"] == [agent_record(2)]
    assert [r.query for r in mock_gorelo.requests] == [
        {"ClientIds": "9101", "PageSize": "1"},
        {"ClientIds": "9101", "PageSize": "1", "Cursor": "c1"},
    ]


@pytest.mark.parametrize("asked, sent", [(500, 200), (201, 200), (0, 1), (-1, 1), (100, 100)])
async def test_list_agents_clamps_page_size_and_reports_the_size_used(server, mock_gorelo, asked, sent):
    mock_gorelo.on("GET", AGENTS, paged_envelope([agent_record()]))
    result = await call_tool(server, "list_agents", {"page_size": asked})
    assert mock_gorelo.last.query["PageSize"] == str(sent) and result["page_size"] == sent


async def test_list_agents_reports_an_empty_page_as_an_empty_page(server, mock_gorelo):
    mock_gorelo.on("GET", AGENTS, envelope([], pagination(None, 0, has_more=False)))
    result = await call_tool(server, "list_agents", {"query": "nothing"})
    assert result["items"] == [] and result["count"] == 0 and result["total_count"] == 0
    assert result["filters"] == {"query": "nothing"}


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"status_ids": []}, "status_ids: expected at least one id, got an empty list"),
        ({"status_ids": [2, 0]}, "status_ids[1]: expected a positive whole number such as 123, got zero or a negative number"),
        ({"client_ids": []}, "client_ids: expected at least one id, got an empty list"),
        ({"client_ids": [9101, 0]}, "client_ids[1]: expected a positive whole number such as 123, got zero or a negative number"),
        ({"client_ids": [-5]}, "client_ids[0]: expected a positive whole number such as 123, got zero or a negative number"),
        ({"query": " "}, "query: must not be empty or whitespace only"),
        ({"created_since": "2026-01-01T00:00:00"}, "created_since: '2026-01-01T00:00:00' has no UTC offset"),
        ({"created_before": "soon"}, "created_before: 'soon' is not an ISO 8601 datetime"),
        ({"updated_since": ""}, "updated_since: expected an ISO 8601 datetime with a UTC offset"),
        ({"updated_before": "2026-04-01"}, "updated_before: '2026-04-01' has no UTC offset"),
        ({"cursor": ""}, "cursor: must not be empty or whitespace only"),
    ],
)
async def test_list_agents_local_errors_name_the_parameter_and_send_nothing(server, mock_gorelo, arguments, fragment):
    assert fragment in await call_tool_error(server, "list_agents", arguments)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "property_name, param",
    [
        ("StatusIds", "status_ids"),
        ("ClientIds", "client_ids"),
        ("Query", "query"),
        ("CreatedSince", "created_since"),
        ("UpdatedBefore", "updated_before"),
        ("PageSize", "page_size"),
        ("Cursor", "cursor"),
    ],
)
async def test_list_agents_maps_a_gorelo_error_to_the_snake_case_parameter(server, mock_gorelo, property_name, param):
    mock_gorelo.on("GET", AGENTS, error_envelope(400, [("070101", "Rejected value.", property_name)]))
    text = await call_tool_error(server, "list_agents", {})
    assert text == trace(f"Gorelo rejected list_agents (HTTP 400, code 070101): {param}: Rejected value.")


async def test_list_agents_refuses_the_legacy_lowercase_body_instead_of_returning_zero_rows(server, mock_gorelo):
    mock_gorelo.on("GET", AGENTS, {"data": [{"id": "x"}], "nextCursor": None, "hasMore": False})
    text = await call_tool_error(server, "list_agents", {})
    assert text.startswith("Gorelo returned an unexpected response for list_agents")


# --------------------------------------------------------------------------
# get_agent
# --------------------------------------------------------------------------


async def test_get_agent_reads_one_agent_and_returns_the_record_unchanged(server, mock_gorelo):
    record = agent_record(7)
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(7)}", envelope(record))
    result = await call_tool(server, "get_agent", {"agent_id": uid(7)})
    assert result == record
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", f"/v1/assets/agents/{uid(7)}", {}, b"")
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "given",
    [uid(7).upper(), uid(7).replace("-", ""), uid(7).upper().replace("-", "")],
)
async def test_get_agent_accepts_any_case_and_form_of_the_uuid_and_sends_it_canonically(server, mock_gorelo, given):
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(7)}", envelope(agent_record(7)))
    await call_tool(server, "get_agent", {"agent_id": given})
    assert mock_gorelo.last.path == f"/v1/assets/agents/{uid(7)}"


@pytest.mark.parametrize(
    "bad",
    [
        " ",
        "abc",
        "123",
        "00000001-aaaa-4bbb-8ccc-00000000000z",
        "00000001-aaaa-4bbb-8ccc",
        "..%2F..%2Fassets%2Fagents",
        "../agents",
        f"{uid(7)}/extra",
        f"{uid(7)}?x=1",
        f"  {uid(7)}  ",  # nothing is trimmed or guessed
        f"{uid(7)}\n",
        "{" + uid(7) + "}",
        "urn:uuid:" + uid(7),
    ],
)
async def test_get_agent_rejects_anything_that_is_not_a_uuid_naming_agent_id_and_sends_nothing(server, mock_gorelo, bad):
    text = await call_tool_error(server, "get_agent", {"agent_id": bad})
    assert text == "agent_id: expected a GUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b, got text that is not a GUID"
    assert mock_gorelo.requests == []


async def test_get_agent_rejects_an_empty_string_without_echoing_it(server, mock_gorelo):
    text = await call_tool_error(server, "get_agent", {"agent_id": ""})
    assert text == "agent_id: expected a GUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b, got an empty string"
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", [7, 7.5, True, None, ["x"]])
async def test_get_agent_rejects_a_value_that_is_not_text_before_any_http_call(server, mock_gorelo, bad):
    text = await call_tool_error(server, "get_agent", {"agent_id": bad})
    assert "agent_id" in text
    assert mock_gorelo.requests == []


async def test_get_agent_maps_a_missing_agent_to_a_tool_error(server, mock_gorelo):
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(9)}", error_envelope(404, [("070404", "Device not found.")]))
    text = await call_tool_error(server, "get_agent", {"agent_id": uid(9)})
    assert text == trace("Gorelo rejected get_agent (HTTP 404, code 070404): Device not found.")


@pytest.mark.parametrize("property_name", ["deviceId", "DeviceId"])
async def test_get_agent_maps_a_gorelo_error_about_the_id_to_agent_id(server, mock_gorelo, property_name):
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(9)}", error_envelope(400, [("070101", "The id is not valid.", property_name)]))
    text = await call_tool_error(server, "get_agent", {"agent_id": uid(9)})
    assert text == trace("Gorelo rejected get_agent (HTTP 400, code 070101): agent_id: The id is not valid.")


async def test_get_agent_refuses_a_success_without_data(server, mock_gorelo):
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(7)}", envelope(None))
    assert "Data is null" in await call_tool_error(server, "get_agent", {"agent_id": uid(7)})


@pytest.mark.parametrize("data, found", [({}, "an empty object"), ([agent_record(7)], "a list of 1 item"), ("x", "a string")])
async def test_get_agent_refuses_a_record_that_is_not_an_object(server, mock_gorelo, data, found):
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(7)}", envelope(data))
    text = await call_tool_error(server, "get_agent", {"agent_id": uid(7)})
    assert text.startswith("Gorelo returned an unexpected response for get_agent: GET /v1/assets/agents/{deviceId}: expected Data to be a non-empty object")
    assert f"but got {found}" in text and "refusing to guess" in text


async def test_get_agent_never_sends_a_delete_even_for_a_well_formed_id(server, mock_gorelo):
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(7)}", envelope(agent_record(7)))
    await call_tool(server, "get_agent", {"agent_id": uid(7)})
    assert {r.method for r in mock_gorelo.requests} == {"GET"}


# --------------------------------------------------------------------------
# list_custom_assets
# --------------------------------------------------------------------------


async def test_list_custom_assets_with_no_filters_sends_only_the_default_page_size(server, mock_gorelo):
    rows = [custom_record(1), custom_record(2)]
    mock_gorelo.on("GET", CUSTOM, paged_envelope(rows, total_count=2))
    result = await call_tool(server, "list_custom_assets")
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", CUSTOM, {"PageSize": "100"}, b"")
    assert result == {
        "items": rows, "count": 2, "total_count": 2, "has_more": False, "next_cursor": None,
        "page_size": 100, "filters": {},
    }


async def test_list_custom_assets_sends_every_filter_under_its_spec_name_and_no_status_ids(server, mock_gorelo):
    mock_gorelo.on("GET", CUSTOM, paged_envelope([custom_record()], next_cursor="c2", total_count=60))
    result = await call_tool(
        server, "list_custom_assets",
        {"client_ids": [9101], "query": "switch", **ALL_DATES, "page_size": 10, "cursor": "c1"},
    )
    assert mock_gorelo.last.query == {
        "ClientIds": "9101", "Query": "switch", **ALL_DATES_SENT, "PageSize": "10", "Cursor": "c1",
    }
    assert "StatusIds" not in mock_gorelo.last.query
    assert result["filters"] == {"client_ids": [9101], "query": "switch", **ALL_DATES_SHOWN}
    assert result["page_size"] == 10 and result["next_cursor"] == "c2"


async def test_list_custom_assets_has_no_status_filter_so_one_is_refused_before_any_http_call(server, mock_gorelo):
    text = await call_tool_error(server, "list_custom_assets", {"status_ids": [1]})
    assert "status_ids" in text
    assert mock_gorelo.requests == []


async def test_an_empty_custom_asset_list_is_an_empty_page_not_an_error(server, mock_gorelo):
    # with no custom assets Gorelo answers an empty page
    mock_gorelo.on("GET", CUSTOM, envelope([], pagination(None, 0, has_more=False)))
    result = await call_tool(server, "list_custom_assets")
    assert result["items"] == [] and result["count"] == 0 and result["total_count"] == 0
    assert result["has_more"] is False and result["next_cursor"] is None


async def test_list_custom_assets_follows_the_cursor_with_the_same_filters(server, mock_gorelo):
    mock_gorelo.on("GET", CUSTOM, paged_responder([[custom_record(1)], [custom_record(2)]], total_count=2))
    first = await call_tool(server, "list_custom_assets", {"query": "sw", "page_size": 1})
    second = await call_tool(server, "list_custom_assets", {"query": "sw", "page_size": 1, "cursor": first["next_cursor"]})
    assert first["has_more"] is True and second["items"] == [custom_record(2)]
    assert [r.query for r in mock_gorelo.requests] == [
        {"Query": "sw", "PageSize": "1"},
        {"Query": "sw", "PageSize": "1", "Cursor": "c1"},
    ]


@pytest.mark.parametrize("asked, sent", [(500, 200), (0, 1), (-3, 1), (7, 7)])
async def test_list_custom_assets_clamps_page_size_and_reports_the_size_used(server, mock_gorelo, asked, sent):
    mock_gorelo.on("GET", CUSTOM, paged_envelope([custom_record()]))
    result = await call_tool(server, "list_custom_assets", {"page_size": asked})
    assert mock_gorelo.last.query["PageSize"] == str(sent) and result["page_size"] == sent


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"client_ids": []}, "client_ids: expected at least one id, got an empty list"),
        ({"client_ids": [0]}, "client_ids[0]: expected a positive whole number such as 123, got zero or a negative number"),
        ({"query": ""}, "query: must not be empty or whitespace only"),
        ({"created_since": "2026-01-01T00:00:00"}, "created_since: '2026-01-01T00:00:00' has no UTC offset"),
        ({"updated_before": "next week"}, "updated_before: 'next week' is not an ISO 8601 datetime"),
        ({"cursor": "  "}, "cursor: must not be empty or whitespace only"),
    ],
)
async def test_list_custom_assets_local_errors_name_the_parameter_and_send_nothing(server, mock_gorelo, arguments, fragment):
    assert fragment in await call_tool_error(server, "list_custom_assets", arguments)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "property_name, param",
    [
        ("ClientIds", "client_ids"),
        ("Query", "query"),
        ("CreatedBefore", "created_before"),
        ("UpdatedSince", "updated_since"),
        ("PageSize", "page_size"),
    ],
)
async def test_list_custom_assets_maps_a_gorelo_error_to_the_snake_case_parameter(server, mock_gorelo, property_name, param):
    mock_gorelo.on("GET", CUSTOM, error_envelope(400, [("070101", "Rejected value.", property_name)]))
    text = await call_tool_error(server, "list_custom_assets", {})
    assert text == trace(f"Gorelo rejected list_custom_assets (HTTP 400, code 070101): {param}: Rejected value.")


async def test_list_custom_assets_refuses_the_legacy_lowercase_body_instead_of_returning_zero_rows(server, mock_gorelo):
    mock_gorelo.on("GET", CUSTOM, {"data": [], "nextCursor": None, "hasMore": False})
    text = await call_tool_error(server, "list_custom_assets", {})
    assert text.startswith("Gorelo returned an unexpected response for list_custom_assets")


# --------------------------------------------------------------------------
# Strict ids: JSON true, "5" and 5.0 are refused before the tool runs
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, param", [("list_agents", "status_ids"), ("list_agents", "client_ids"), ("list_custom_assets", "client_ids")]
)
@pytest.mark.parametrize("bad", [[True], ["5"], [5.0], [1, True], [None], "5", 5])
async def test_the_id_lists_are_strict_item_by_item(server, mock_gorelo, name, param, bad):
    text = await call_tool_error(server, name, {param: bad})
    assert param in text and ("valid integer" in text or "valid list" in text)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("name", ["list_agents", "list_custom_assets"])
async def test_an_id_above_the_largest_gorelo_id_is_refused_naming_the_item(server, mock_gorelo, name):
    text = await call_tool_error(server, name, {"client_ids": [1, 2**63]})
    assert text.startswith("client_ids[1]: expected a positive whole number such as 123, got a number above ")
    assert mock_gorelo.requests == []


async def test_an_explicit_null_for_every_optional_parameter_is_still_accepted(server, mock_gorelo):
    # the schema no longer advertises null for optional parameters, but validation still accepts it
    mock_gorelo.on("GET", AGENTS, paged_envelope([agent_record()]))
    mock_gorelo.on("GET", CUSTOM, paged_envelope([custom_record()]))
    nulls = {name: None for name in FILTERS if name not in ("page_size",)}
    await call_tool(server, "list_agents", {"status_ids": None, "client_ids": None, **nulls})
    assert mock_gorelo.last.query == {"PageSize": "100"}
    await call_tool(server, "list_custom_assets", {"client_ids": None, **nulls})
    assert mock_gorelo.last.query == {"PageSize": "100"}


async def test_the_optional_parameters_default_to_null_without_advertising_a_null_branch(server):
    # The server compacts every advertised schema (server.compact_input_schema, see test_schema_compaction.py): an
    # optional parameter shows only its real type and description, with no null branch and no "default": null.
    # It is optional because it is not in "required"; the declared type is a plain `X | None`, so null is accepted.
    listed = {t.name: t for t in await list_tools(server)}
    tools = {name: tool.inputSchema["properties"] for name, tool in listed.items()}
    for name in ("list_agents", "list_custom_assets"):
        assert all("anyOf" not in p for p in tools[name].values()), name
        assert [p for p, definition in tools[name].items() if "default" in definition] == ["page_size"], name
        assert not listed[name].inputSchema.get("required"), name
        assert tools[name]["client_ids"]["type"] == "array" and tools[name]["client_ids"]["items"] == {"type": "integer"}
        assert tools[name]["query"]["type"] == "string"
    assert tools["get_agent"]["agent_id"]["type"] == "string"
