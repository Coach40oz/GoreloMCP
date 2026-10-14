"""tools/assets.py: list_agents, get_agent, list_custom_assets (read only), and the custom asset hardware types, create and update."""

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

WRITE_TOOLS = {
    "create_custom_asset": ["POST /v1/assets/custom", "GET /v1/assets/custom"],
    "update_custom_asset": ["PATCH /v1/assets/custom/{customAssetId}", "GET /v1/assets/custom"],
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


def test_the_single_agent_is_the_list_row_fields_plus_custom_properties_in_the_spec(spec_index):
    """The module text says the single record (DeviceResponse) is a list row (DeviceListItemResponse) plus CustomProperties,
    which the list deliberately leaves out. The two had exactly the same fields until contract 1b1c0cec0e9a (2026-10-14)."""
    assert spec_index.op("GET /v1/assets/agents").response["data"] == "[DeviceListItemResponse]"
    assert spec_index.op("GET /v1/assets/agents/{deviceId}").response["data"] == "DeviceResponse"
    row, record = spec_index.schema("DeviceListItemResponse")["fields"], spec_index.schema("DeviceResponse")["fields"]
    assert row != record and set(record) - set(row) == {"CustomProperties"} and set(row) < set(record)
    assert {name: entry for name, entry in record.items() if name != "CustomProperties"} == row  # nothing else differs
    assert {"LocationId", "WarrantyStartDate", "WarrantyEndDate"} <= set(row) and "ClientLocationId" not in row
    custom = record["CustomProperties"]
    assert (custom["type"], custom["items"]["ref"], custom["nullable"]) == ("array", "CustomPropertyValue", False)
    assert set(spec_index.schema("CustomPropertyValue")["fields"]) == {"Id", "Variable", "Name", "Type", "Value", "IsSet"}
    # no operation updates a device, so nothing in this module could write a custom property of one
    assert {op.method for op in spec_index.ops.values() if op.path == "/v1/assets/agents/{deviceId}"} == {"GET", "DELETE"}
    doc = flat(assets_module.__doc__)
    assert "DeviceListItemResponse" in doc and "DeviceResponse" in doc and "the same fields" not in doc
    assert "the list row's fields plus CustomProperties" in doc and "which the list deliberately leaves out" in doc
    assert "Gorelo has no operation that updates a device" in doc


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


def test_no_tool_can_delete_an_asset_and_agents_stay_read_only():
    agent_ops = {"GET /v1/assets/agents", "GET /v1/assets/agents/{deviceId}"}
    for spec in REGISTRY.specs:
        if set(spec.ops) & agent_ops:
            assert spec.kind == "read" and all(op.startswith("GET ") for op in spec.ops)
    mine = [s for s in REGISTRY.specs if any("/v1/assets/" in op for op in s.ops)]
    assert {s.name for s in mine} == set(TOOLS) | set(WRITE_TOOLS) | {"list_custom_asset_hardware_types"}
    assert not any(op.startswith("DELETE ") for s in mine for op in s.ops)
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


def test_the_agent_docstrings_send_the_model_from_the_list_to_the_single_record_for_custom_fields():
    # the list rows leave CustomProperties out and get_agent has it (contract 1b1c0cec0e9a); each pointer is a few words, because
    # the default tools/list is at its byte budget (tests/test_integration.py)
    assert "Rows omit custom fields: get_agent has them." in flat(assets_module.list_agents.__doc__)
    assert flat(assets_module.get_agent.__doc__).endswith("returns Gorelo's full device record, including CustomProperties.")


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


# The shape is CustomPropertyValue (contract 1b1c0cec0e9a); the values are invented, because the field types Gorelo uses
# (Type is {Id, Name}) and the formats of their values were not probed. Value is the effective value (the saved one, else the
# field's default, else null) and is always text; IsSet says whether a value is saved on the device.
CUSTOM_PROPERTIES = [
    {"Id": 41, "Variable": "warranty_vendor", "Name": "Warranty vendor", "Type": {"Id": 1, "Name": "Text"}, "Value": "Dell", "IsSet": True},
    {"Id": 42, "Variable": "asset_tag", "Name": "Asset tag", "Type": {"Id": 1, "Name": "Text"}, "Value": None, "IsSet": False},
    {"Id": 43, "Variable": "seats", "Name": "Seats", "Type": {"Id": 2, "Name": "Number"}, "Value": "5", "IsSet": False},
]


async def test_get_agent_passes_custom_properties_through_unchanged(server, mock_gorelo):
    """The single record carries every device custom field. The tool neither reads nor reshapes them: the order, the Type
    object, a null Value and a default shown with IsSet false all come back as Gorelo sent them."""
    record = agent_record(7, CustomProperties=CUSTOM_PROPERTIES)
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(7)}", envelope(record))
    result = await call_tool(server, "get_agent", {"agent_id": uid(7)})
    assert result == record
    assert result["CustomProperties"] == CUSTOM_PROPERTIES
    assert [entry["Id"] for entry in result["CustomProperties"]] == [41, 42, 43]
    assert [entry["Value"] for entry in result["CustomProperties"]] == ["Dell", None, "5"]
    assert [entry["IsSet"] for entry in result["CustomProperties"]] == [True, False, False]
    assert len(mock_gorelo.requests) == 1  # one GET: the custom fields are part of the single record


async def test_get_agent_keeps_an_empty_custom_properties_list(server, mock_gorelo):
    # "Empty only when no custom fields are defined": that is an answer, not a reason to drop the key or to refuse the record
    record = agent_record(7, CustomProperties=[])
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(7)}", envelope(record))
    result = await call_tool(server, "get_agent", {"agent_id": uid(7)})
    assert result == record and result["CustomProperties"] == []


async def test_list_agents_rows_have_no_custom_properties_and_the_tool_fetches_none(server, mock_gorelo):
    rows = [agent_record(1), agent_record(2)]
    assert all("CustomProperties" not in row for row in rows)  # the list shape leaves them out
    mock_gorelo.on("GET", AGENTS, paged_envelope(rows, total_count=2))
    result = await call_tool(server, "list_agents")
    assert result["items"] == rows and all("CustomProperties" not in row for row in result["items"])
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", AGENTS)]  # no get_agent call per row


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


# --------------------------------------------------------------------------
# list_custom_assets: the new fields
# --------------------------------------------------------------------------


def test_the_custom_asset_list_docstring_names_the_new_fields_and_the_write_tools():
    text = flat(assets_module.list_custom_assets.__doc__)
    for fragment in ("ContactId", "HardwareTypeId", "WarrantyEndDate", "calendar date", "create_custom_asset", "update_custom_asset"):
        assert fragment in text
    assert "cannot delete custom assets" in text


def test_the_spec_gives_the_custom_asset_row_the_new_fields(spec_index):
    fields = spec_index.schema("CustomAssetResponse")["fields"]
    assert {"ContactId", "HardwareTypeId", "HardwareType", "WarrantyEndDate"} <= set(fields)
    assert fields["WarrantyEndDate"]["format"] == "date"


# --------------------------------------------------------------------------
# list_custom_asset_hardware_types
# --------------------------------------------------------------------------

HW_TYPES = CUSTOM + "/hardware-types"


def hw_rows(n=3):
    return [{"Id": 100 + i, "Name": f"Type {i}", "CreatedOn": "2026-01-01T00:00:00Z", "UpdatedOn": None} for i in range(n)]


def test_the_hardware_type_tool_is_a_core_read_tool_with_no_parameters(spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == "list_custom_asset_hardware_types")
    assert (spec.toolset, spec.kind, spec.ops, spec.destructive_hint) == ("core", "read", ["GET /v1/assets/custom/hardware-types"], False)
    assert spec.ops[0] in spec_index.ops and spec_index.op(spec.ops[0]).paged is False
    doc = flat(spec.fn.__doc__)
    assert "hardware_type_id" in doc and "Side effects: None (read-only)" in doc


async def test_the_hardware_type_tool_exposes_no_parameters(server):
    tool = next(t for t in await list_tools(server) if t.name == "list_custom_asset_hardware_types")
    assert tool.inputSchema["properties"] == {}
    assert (tool.annotations.readOnlyHint, tool.annotations.destructiveHint) == (True, False)


async def test_list_custom_asset_hardware_types_returns_an_unpaged_list(server, mock_gorelo):
    rows = hw_rows(12)
    mock_gorelo.on("GET", HW_TYPES, envelope(rows))
    result = await call_tool(server, "list_custom_asset_hardware_types")
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", HW_TYPES, {}, b"")
    assert result == {"items": rows, "count": 12}


async def test_list_custom_asset_hardware_types_refuses_a_non_list_answer(server, mock_gorelo):
    mock_gorelo.on("GET", HW_TYPES, envelope({"Id": 1}))
    text = await call_tool_error(server, "list_custom_asset_hardware_types")
    assert text.startswith("Gorelo returned an unexpected response for list_custom_asset_hardware_types")


async def test_list_custom_asset_hardware_types_maps_a_gorelo_error(server, mock_gorelo):
    mock_gorelo.on("GET", HW_TYPES, error_envelope(500, [("070500", "Boom.")]))
    text = await call_tool_error(server, "list_custom_asset_hardware_types")
    assert "list_custom_asset_hardware_types" in text and "Boom." in text


# --------------------------------------------------------------------------
# create_custom_asset and update_custom_asset: declarations
# --------------------------------------------------------------------------

CREATE_PARAMS = [
    "name", "client_id", "location_id", "hardware_type_id", "description", "contact_id", "warranty_end_date",
    "ip_address", "serial_number",
]
UPDATE_PARAMS = [
    "custom_asset_id", "client_id", "name", "location_id", "hardware_type_id", "description", "contact_id",
    "warranty_end_date", "ip_address", "serial_number", "clear_fields",
]
CLEARABLE = ["description", "contact_id", "warranty_end_date", "ip_address", "serial_number"]
ASSET = uid(5)
ASSET_UP = ASSET.upper()
PATCH_PATH = f"{CUSTOM}/{ASSET}"
CREATE_MIN = {"name": "Reception printer", "client_id": 9101, "location_id": 4410, "hardware_type_id": 118}
CREATE_ALL = {
    **CREATE_MIN, "description": "HP LaserJet", "contact_id": 7962, "warranty_end_date": "2027-03-31",
    "ip_address": "10.0.0.20", "serial_number": "CNB1K23456",
}
CREATE_ALL_BODY = {
    "Name": "Reception printer", "ClientId": 9101, "LocationId": 4410, "HardwareTypeId": 118,
    "Description": "HP LaserJet", "ContactId": 7962, "WarrantyEndDate": "2027-03-31",
    "IpAddress": "10.0.0.20", "SerialNumber": "CNB1K23456",
}


def test_the_write_tools_are_declared_as_core_writes(spec_index):
    for name, ops in WRITE_TOOLS.items():
        spec = next(s for s in REGISTRY.specs if s.name == name)
        assert (spec.toolset, spec.kind, spec.ops) == ("core", "write", ops)
        assert spec.destructive_hint is (name == "update_custom_asset")
        assert all(op in spec_index.ops for op in spec.ops) and not set(spec.ops) & FORBIDDEN_OPS


def test_the_body_field_maps_match_the_spec_commands(spec_index):
    create = next(s for s in REGISTRY.specs if s.name == "create_custom_asset")
    update = next(s for s in REGISTRY.specs if s.name == "update_custom_asset")
    assert set(create.field_map.values()) == set(spec_index.op("POST /v1/assets/custom").body["fields"])
    body_fields = set(spec_index.op("PATCH /v1/assets/custom/{customAssetId}").body["fields"])
    assert set(update.field_map.values()) - {"customAssetId"} == body_fields
    assert "customAssetId" in spec_index.op("PATCH /v1/assets/custom/{customAssetId}").path_placeholders


async def test_create_and_update_expose_exactly_the_documented_parameters(server):
    tools = {t.name: t for t in await list_tools(server)}
    create, update = tools["create_custom_asset"], tools["update_custom_asset"]
    assert list(create.inputSchema["properties"]) == CREATE_PARAMS
    assert create.inputSchema["required"] == ["name", "client_id", "location_id", "hardware_type_id"]
    assert list(update.inputSchema["properties"]) == UPDATE_PARAMS
    assert update.inputSchema["required"] == ["custom_asset_id", "client_id"]
    for tool in (create, update):
        assert all(p.get("description") for p in tool.inputSchema["properties"].values())
        assert tool.annotations.readOnlyHint is False
    assert (create.annotations.destructiveHint, update.annotations.destructiveHint) == (False, True)
    clear_items = update.inputSchema["properties"]["clear_fields"]
    assert clear_items["items"]["enum"] == CLEARABLE


def test_the_write_docstrings_say_what_the_user_needs_to_know():
    create = flat(assets_module.create_custom_asset.__doc__)
    assert "timeline" in create and "cannot delete custom assets" in create and "Gorelo app" in create
    assert "list_custom_asset_hardware_types" in create
    update = flat(assets_module.update_custom_asset.__doc__)
    assert "client cannot be changed" in update.lower() and "clear_fields" in update and "not cleared" in update
    for name in WRITE_TOOLS:
        assert "Side effects:" in flat(getattr(assets_module, name).__doc__)


# --------------------------------------------------------------------------
# create_custom_asset
# --------------------------------------------------------------------------


def row_for(asset_id, **extra):
    return custom_record(5, Id=asset_id, ClientId=9101, LocationId=4410, HardwareTypeId=118, ContactId=None, **extra)


async def test_create_sends_the_pascal_case_body_then_reads_the_row_back_by_client(server, mock_gorelo):
    row = row_for(ASSET)
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", CUSTOM, paged_envelope([custom_record(1), row]))
    result = await call_tool(server, "create_custom_asset", CREATE_ALL)
    post, get = mock_gorelo.requests
    assert (post.method, post.path, post.query, post.json) == ("POST", CUSTOM, {}, CREATE_ALL_BODY)
    assert (get.method, get.path) == ("GET", CUSTOM)
    assert get.query == {"ClientIds": "9101", "PageSize": "200"}
    assert len(mock_gorelo.requests) == 2
    assert result == row


async def test_create_with_only_the_required_fields_omits_every_optional_key(server, mock_gorelo):
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", CUSTOM, paged_envelope([row_for(ASSET)]))
    await call_tool(server, "create_custom_asset", CREATE_MIN)
    assert mock_gorelo.requests[0].json == {
        "Name": "Reception printer", "ClientId": 9101, "LocationId": 4410, "HardwareTypeId": 118
    }


async def test_create_matches_the_written_id_ignoring_case(server, mock_gorelo):
    row = row_for(ASSET_UP)
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", CUSTOM, paged_envelope([row]))
    assert await call_tool(server, "create_custom_asset", CREATE_MIN) == row


async def test_create_scans_further_pages_for_the_new_row(server, mock_gorelo):
    row = row_for(ASSET)
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", CUSTOM, paged_responder([[custom_record(1)], [row]]))
    assert await call_tool(server, "create_custom_asset", CREATE_MIN) == row
    assert len(mock_gorelo.calls("GET", CUSTOM)) == 2


async def test_create_returns_a_warning_when_the_row_is_not_in_the_list(server, mock_gorelo):
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", CUSTOM, paged_envelope([custom_record(1)]))
    result = await call_tool(server, "create_custom_asset", CREATE_MIN)
    assert set(result) == {"Id", "warning"} and result["Id"] == ASSET
    assert "the write succeeded" in result["warning"] and "Do not repeat the write" in result["warning"]
    assert "not among" in result["warning"]


async def test_create_returns_a_warning_when_the_scan_hits_its_cap_without_the_row(server, mock_gorelo, monkeypatch):
    monkeypatch.setattr(assets_module, "READBACK_MAX_ITEMS", 2)
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", CUSTOM, paged_responder([[custom_record(1), custom_record(2)], [row_for(ASSET)]]))
    result = await call_tool(server, "create_custom_asset", CREATE_MIN)
    assert set(result) == {"Id", "warning"} and "scan stopped" in result["warning"]


async def test_create_returns_a_warning_not_an_error_when_the_read_back_fails(server, mock_gorelo):
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", CUSTOM, error_envelope(500, [("070500", "Boom.")]))
    result = await call_tool(server, "create_custom_asset", CREATE_MIN)
    assert set(result) == {"Id", "warning"} and result["Id"] == ASSET
    assert "re-reading it failed" in result["warning"] and "HTTP 500" in result["warning"]


async def test_create_returns_a_warning_when_the_read_back_answers_something_odd(server, mock_gorelo):
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", CUSTOM, {"data": [], "nextCursor": None, "hasMore": False})
    result = await call_tool(server, "create_custom_asset", CREATE_MIN)
    assert set(result) == {"Id", "warning"}


async def test_create_without_an_id_in_the_answer_is_an_unconfirmed_write_and_reads_nothing(server, mock_gorelo):
    mock_gorelo.on("POST", CUSTOM, envelope({}))
    text = await call_tool_error(server, "create_custom_asset", CREATE_MIN)
    assert "create_custom_asset" in text and "Id" in text
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "property_name, param",
    [
        ("Name", "name"), ("ClientId", "client_id"), ("LocationId", "location_id"),
        ("HardwareTypeId", "hardware_type_id"), ("Description", "description"), ("ContactId", "contact_id"),
        ("WarrantyEndDate", "warranty_end_date"), ("IpAddress", "ip_address"), ("SerialNumber", "serial_number"),
    ],
)
async def test_create_maps_a_gorelo_400_to_the_snake_case_parameter(server, mock_gorelo, property_name, param):
    mock_gorelo.on("POST", CUSTOM, error_envelope(400, [("070101", "Rejected value.", property_name)]))
    text = await call_tool_error(server, "create_custom_asset", CREATE_ALL)
    assert text == trace(f"Gorelo rejected create_custom_asset (HTTP 400, code 070101): {param}: Rejected value.")
    assert len(mock_gorelo.requests) == 1  # no read-back after a failed write


@pytest.mark.parametrize(
    "extra, fragment",
    [
        ({"name": ""}, "name: must not be empty"),
        ({"name": "   "}, "name: must not be empty"),
        ({"name": "x" * 301}, "name: at most 300 characters"),
        ({"description": "x" * 1001}, "description: at most 1000 characters"),
        ({"description": " "}, "description: must not be empty"),
        ({"serial_number": "x" * 251}, "serial_number: at most 250 characters"),
        ({"serial_number": ""}, "serial_number: must not be empty"),
        ({"warranty_end_date": "2027-02-30"}, "warranty_end_date: 2027-02-30 is not a real calendar date"),
        ({"warranty_end_date": "31/03/2027"}, "warranty_end_date: expected a calendar date written YYYY-MM-DD"),
        ({"warranty_end_date": "2027-03-31T00:00:00Z"}, "warranty_end_date: expected a calendar date"),
        ({"warranty_end_date": "20270331"}, "warranty_end_date: expected a calendar date"),
        ({"ip_address": "::1"}, "ip_address: expected an IPv4 address"),
        ({"ip_address": "2001:db8::1"}, "IPv6 is not accepted"),
        ({"ip_address": "300.1.1.1"}, "ip_address: expected an IPv4 address"),
        ({"ip_address": "192.168.1"}, "ip_address: expected an IPv4 address"),
        ({"ip_address": ""}, "ip_address: expected an IPv4 address"),
        ({"client_id": 0}, "client_id"),
        ({"location_id": -1}, "location_id"),
        ({"contact_id": 0}, "contact_id"),
        ({"client_id": True}, "client_id"),
        ({"client_id": "9101"}, "client_id"),
        ({"hardware_type_id": 1.5}, "hardware_type_id"),
    ],
)
async def test_create_refuses_bad_values_before_any_http_call(server, mock_gorelo, extra, fragment):
    text = await call_tool_error(server, "create_custom_asset", {**CREATE_MIN, **extra})
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("missing", ["name", "client_id", "location_id", "hardware_type_id"])
async def test_create_requires_the_four_ids_and_the_name(server, mock_gorelo, missing):
    arguments = {k: v for k, v in CREATE_MIN.items() if k != missing}
    text = await call_tool_error(server, "create_custom_asset", arguments)
    assert missing in text
    assert mock_gorelo.requests == []


async def test_create_accepts_the_boundary_lengths_and_a_leap_day(server, mock_gorelo):
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", CUSTOM, paged_envelope([row_for(ASSET)]))
    await call_tool(
        server, "create_custom_asset",
        {**CREATE_MIN, "name": "n" * 300, "description": "d" * 1000, "serial_number": "s" * 250, "warranty_end_date": "2028-02-29"},
    )
    body = mock_gorelo.requests[0].json
    assert (len(body["Name"]), len(body["Description"]), len(body["SerialNumber"]), body["WarrantyEndDate"]) == (300, 1000, 250, "2028-02-29")


# --------------------------------------------------------------------------
# update_custom_asset
# --------------------------------------------------------------------------


def update_args(**extra):
    return {"custom_asset_id": ASSET, "client_id": 9101, **extra}


async def test_update_sends_only_the_changed_fields_plus_client_id_and_reads_the_row_back(server, mock_gorelo):
    row = row_for(ASSET, Name="New name")
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", CUSTOM, paged_envelope([row]))
    result = await call_tool(server, "update_custom_asset", update_args(name="New name"))
    patch, get = mock_gorelo.requests
    assert (patch.method, patch.path, patch.query, patch.json) == ("PATCH", PATCH_PATH, {}, {"ClientId": 9101, "Name": "New name"})
    assert (get.method, get.path, get.query) == ("GET", CUSTOM, {"ClientIds": "9101", "PageSize": "200"})
    assert result == row


async def test_update_sends_every_field_under_its_pascal_case_name(server, mock_gorelo):
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", CUSTOM, paged_envelope([row_for(ASSET)]))
    await call_tool(server, "update_custom_asset", update_args(**CREATE_ALL))
    assert mock_gorelo.requests[0].json == CREATE_ALL_BODY


async def test_update_clear_fields_are_sent_as_json_null(server, mock_gorelo):
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", CUSTOM, paged_envelope([row_for(ASSET)]))
    await call_tool(server, "update_custom_asset", update_args(clear_fields=CLEARABLE))
    assert mock_gorelo.requests[0].json == {
        "ClientId": 9101, "Description": None, "ContactId": None, "WarrantyEndDate": None,
        "IpAddress": None, "SerialNumber": None,
    }
    assert b'"Description":null' in mock_gorelo.requests[0].content.replace(b" ", b"")


async def test_update_can_set_one_field_and_clear_another_in_one_call(server, mock_gorelo):
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", CUSTOM, paged_envelope([row_for(ASSET)]))
    await call_tool(server, "update_custom_asset", update_args(name="N", clear_fields=["ip_address", "ip_address"]))
    assert mock_gorelo.requests[0].json == {"ClientId": 9101, "Name": "N", "IpAddress": None}


async def test_update_normalizes_an_uppercase_asset_id_and_matches_the_row(server, mock_gorelo):
    row = row_for(ASSET)
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", CUSTOM, paged_envelope([row]))
    assert await call_tool(server, "update_custom_asset", update_args(custom_asset_id=ASSET_UP, serial_number="S")) == row
    assert mock_gorelo.requests[0].path == PATCH_PATH


async def test_update_read_back_not_found_returns_a_warning(server, mock_gorelo):
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", CUSTOM, paged_envelope([]))
    result = await call_tool(server, "update_custom_asset", update_args(name="N"))
    assert set(result) == {"Id", "warning"} and result["Id"] == ASSET and "Do not repeat the write" in result["warning"]


async def test_update_read_back_failure_returns_a_warning_not_an_error(server, mock_gorelo):
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", CUSTOM, error_envelope(500, [("070500", "Boom.")]))
    result = await call_tool(server, "update_custom_asset", update_args(name="N"))
    assert set(result) == {"Id", "warning"} and "HTTP 500" in result["warning"]


async def test_update_without_an_id_in_the_answer_is_an_unconfirmed_write(server, mock_gorelo):
    mock_gorelo.on("PATCH", PATCH_PATH, envelope(None))
    text = await call_tool_error(server, "update_custom_asset", update_args(name="N"))
    assert "update_custom_asset" in text
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "property_name, param",
    [
        ("Name", "name"), ("ClientId", "client_id"), ("LocationId", "location_id"),
        ("HardwareTypeId", "hardware_type_id"), ("Description", "description"), ("ContactId", "contact_id"),
        ("WarrantyEndDate", "warranty_end_date"), ("IpAddress", "ip_address"), ("SerialNumber", "serial_number"),
        ("customAssetId", "custom_asset_id"),
    ],
)
async def test_update_maps_a_gorelo_400_to_the_snake_case_parameter(server, mock_gorelo, property_name, param):
    mock_gorelo.on("PATCH", PATCH_PATH, error_envelope(400, [("070101", "Rejected value.", property_name)]))
    text = await call_tool_error(server, "update_custom_asset", update_args(name="N"))
    assert text == trace(f"Gorelo rejected update_custom_asset (HTTP 400, code 070101): {param}: Rejected value.")
    assert len(mock_gorelo.requests) == 1


async def test_update_maps_a_404_without_reading_back(server, mock_gorelo):
    mock_gorelo.on("PATCH", PATCH_PATH, error_envelope(404, [("070404", "Custom asset not found.")]))
    text = await call_tool_error(server, "update_custom_asset", update_args(name="N"))
    assert "update_custom_asset" in text and "HTTP 404" in text
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"custom_asset_id": "abc", "name": "N"}, "custom_asset_id"),
        ({"custom_asset_id": 5, "name": "N"}, "custom_asset_id"),
        ({"client_id": 0, "name": "N"}, "client_id"),
        ({"client_id": "9101", "name": "N"}, "client_id"),
        ({"name": " "}, "name: must not be empty"),
        ({"name": "x" * 301}, "name: at most 300 characters"),
        ({"description": "x" * 1001}, "description: at most 1000"),
        ({"description": ""}, "description: must not be empty"),
        ({"serial_number": "x" * 251}, "serial_number: at most 250"),
        ({"warranty_end_date": "2027-13-01"}, "warranty_end_date"),
        ({"warranty_end_date": "tomorrow"}, "warranty_end_date: expected a calendar date"),
        ({"ip_address": "fe80::1"}, "ip_address: expected an IPv4 address"),
        ({"ip_address": "1.2.3.256"}, "ip_address: expected an IPv4 address"),
        ({"location_id": 0}, "location_id"),
        ({"hardware_type_id": False}, "hardware_type_id"),
        ({"contact_id": 0}, "contact_id"),
        ({"clear_fields": []}, "clear_fields: must not be an empty list"),
        ({"clear_fields": ["name"]}, "clear_fields"),
        ({"clear_fields": ["location_id"]}, "clear_fields"),
        ({"clear_fields": ["hardware_type_id"]}, "clear_fields"),
        ({"clear_fields": ["client_id"]}, "clear_fields"),
        ({"clear_fields": "description"}, "clear_fields"),
        ({"clear_fields": [3]}, "clear_fields"),
    ],
)
async def test_update_refuses_bad_input_before_any_http_call(server, mock_gorelo, arguments, fragment):
    text = await call_tool_error(server, "update_custom_asset", update_args(**arguments))
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("field, value", [
    ("description", "text"), ("contact_id", 7962), ("warranty_end_date", "2027-03-31"),
    ("ip_address", "10.0.0.1"), ("serial_number", "SN"),
])
async def test_update_refuses_a_value_together_with_a_clear_of_the_same_field(server, mock_gorelo, field, value):
    text = await call_tool_error(server, "update_custom_asset", update_args(**{field: value}, clear_fields=[field]))
    assert field in text and "cannot be given a value and listed in clear_fields" in text
    assert mock_gorelo.requests == []


async def test_update_with_nothing_to_change_besides_client_id_sends_nothing(server, mock_gorelo):
    text = await call_tool_error(server, "update_custom_asset", update_args())
    assert "nothing to change" in text and "client_id alone changes nothing" in text
    assert mock_gorelo.requests == []


async def test_update_requires_client_id_and_the_asset_id(server, mock_gorelo):
    for arguments in ({"custom_asset_id": ASSET, "name": "N"}, {"client_id": 9101, "name": "N"}):
        text = await call_tool_error(server, "update_custom_asset", arguments)
        assert ("client_id" in text) or ("custom_asset_id" in text)
    assert mock_gorelo.requests == []


def test_the_new_asset_modules_text_has_no_dashes():
    source = Path(assets_module.__file__).read_text(encoding="utf-8")
    assert chr(0x2014) not in source and chr(0x2013) not in source
