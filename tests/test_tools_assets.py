"""tools/assets.py: agents (read, update_agent), policies, asset tags, asset types (read, create, rename, delete), custom assets (read,
hardware types, create, update)."""

import inspect
import json
import re
from pathlib import Path

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
# The per-agent reads of contract 05200951c89e: tool -> path suffix. list_agent_tags answers a list, the others one object.
AGENT_SUB_TOOLS = {
    "list_agent_apps": "/apps",
    "get_agent_hardware": "/hardware",
    "list_agent_patches": "/patches",
    "list_agent_available_updates": "/patches/available",
    "list_agent_tags": "/tags",
}
RAW_INDEX = json.loads((Path(__file__).resolve().parent.parent / "spec" / "spec_index.json").read_text(encoding="utf-8"))
TOOLS = {
    # name: (ops, parameters in order, required parameters)
    "list_agents": (["GET /v1/assets/agents"], ["status_ids", "client_ids", *FILTERS], []),
    "get_agent": (["GET /v1/assets/agents/{agentAssetId}"], ["agent_id"], ["agent_id"]),
    **{
        name: ([f"GET /v1/assets/agents/{{agentAssetId}}{suffix}"], ["agent_id"], ["agent_id"])
        for name, suffix in AGENT_SUB_TOOLS.items()
    },
    "list_asset_types": (["GET /v1/assets/types"], [], []),
    "list_policies": (["GET /v1/assets/policies"], ["os", "page_size", "cursor"], []),
    "get_policy": (["GET /v1/assets/policies/{policyId}"], ["policy_id"], ["policy_id"]),
    "list_asset_tags": (["GET /v1/assets/tags"], [], []),
    "list_custom_assets": (["GET /v1/assets/custom"], ["client_ids", *FILTERS], []),
    "get_custom_asset": (["GET /v1/assets/custom/{customAssetId}"], ["custom_asset_id"], ["custom_asset_id"]),
}

WRITE_TOOLS = {
    "create_custom_asset": ["POST /v1/assets/custom", "GET /v1/assets/custom/{customAssetId}"],
    "update_custom_asset": ["PATCH /v1/assets/custom/{customAssetId}", "GET /v1/assets/custom/{customAssetId}"],
}
# The writes of contract 6c30770dffc0: tool -> (kind, ops, destructive_hint). delete_asset_type is the one gated tool.
NEW_WRITE_TOOLS = {
    "update_agent": ("write", ["PATCH /v1/assets/agents/{agentAssetId}", "GET /v1/assets/agents/{agentAssetId}"], True),
    "create_asset_type": ("write", ["POST /v1/assets/types", "GET /v1/assets/types"], False),
    "update_asset_type": ("write", ["PATCH /v1/assets/types/{assetTypeId}", "GET /v1/assets/types"], True),
    "delete_asset_type": ("destructive", ["DELETE /v1/assets/types/{assetTypeId}"], True),
}

# The device status scale of list_agents: the StatusIds text of the spec (pinned by test_the_device_status_scale_is_the_one_in_the_spec_text).
AGENT_SCALE = "1 Unreachable, 2 Online, 3 Offline, 4 Pending Delete, 6 Pending client inactive"

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
        "Status": {"Id": 2, "Name": "Online"},
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
    assert spec.field_map == {"agent_id": "agentAssetId"}
    assert set(spec.field_map.values()) == set(spec_index.op("GET /v1/assets/agents/{agentAssetId}").path_placeholders)


def test_the_field_map_of_get_custom_asset_names_the_path_placeholder(spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == "get_custom_asset")
    assert spec.field_map == {"custom_asset_id": "customAssetId"}
    assert set(spec.field_map.values()) == set(spec_index.op("GET /v1/assets/custom/{customAssetId}").path_placeholders)


def test_the_single_agent_is_the_list_row_fields_plus_custom_properties_in_the_spec(spec_index):
    """The module text says the single record (AgentAssetResponse) is a list row (AgentAssetListItemResponse) plus CustomProperties,
    which the list deliberately leaves out. The two had exactly the same fields until contract 1b1c0cec0e9a (2026-10-14).
    IsHidden joined CustomPropertyValue on 2026-10-17."""
    assert spec_index.op("GET /v1/assets/agents").response["data"] == "[AgentAssetListItemResponse]"
    assert spec_index.op("GET /v1/assets/agents/{agentAssetId}").response["data"] == "AgentAssetResponse"
    row, record = spec_index.schema("AgentAssetListItemResponse")["fields"], spec_index.schema("AgentAssetResponse")["fields"]
    assert row != record and set(record) - set(row) == {"CustomProperties"} and set(row) < set(record)
    assert {name: entry for name, entry in record.items() if name != "CustomProperties"} == row  # nothing else differs
    assert {"LocationId", "WarrantyStartDate", "WarrantyEndDate"} <= set(row) and "ClientLocationId" not in row
    custom = record["CustomProperties"]
    assert (custom["type"], custom["items"]["ref"], custom["nullable"]) == ("array", "CustomPropertyValue", False)
    assert set(spec_index.schema("CustomPropertyValue")["fields"]) == {"Id", "Variable", "Name", "Type", "Value", "IsSet", "IsHidden"}
    hidden = spec_index.schema("CustomPropertyValue")["fields"]["IsHidden"]
    assert (hidden["type"], hidden["nullable"]) == ("boolean", False)
    # since contract 6c30770dffc0 a device has a PATCH, but its body (UpdateAgentCommand) has no CustomProperties, so nothing in
    # this module can write a custom property of one
    assert {op.method for op in spec_index.ops.values() if op.path == "/v1/assets/agents/{agentAssetId}"} == {"GET", "DELETE", "PATCH"}
    assert set(spec_index.op("PATCH /v1/assets/agents/{agentAssetId}").body["fields"]) == {
        "Description", "HardwareTypeId", "ContactId", "WarrantyStartDate", "WarrantyEndDate"
    }
    doc = flat(assets_module.__doc__)
    assert "DeviceListItemResponse" in doc and "DeviceResponse" in doc and "the same fields" not in doc
    assert "the list row's fields plus CustomProperties" in doc and "which the list deliberately leaves out" in doc
    assert "nothing writes them: update_agent" in doc and "Gorelo has no operation that updates a device" not in doc
    assert "IsHidden" in doc and "Value is then always null" in doc


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


def test_no_tool_can_delete_an_asset_and_the_only_agent_write_is_update_agent():
    for spec in REGISTRY.specs:
        assert "DELETE /v1/assets/agents/{agentAssetId}" not in spec.ops
        if any(op.startswith("GET /v1/assets/agents") or op.startswith("PATCH /v1/assets/agents") for op in spec.ops):
            assert spec.kind == ("write" if spec.name == "update_agent" else "read")
            assert all(op.startswith("GET ") for op in spec.ops) or spec.name == "update_agent"
    mine = [s for s in REGISTRY.specs if any("/v1/assets/" in op for op in s.ops)]
    assert {s.name for s in mine} == set(TOOLS) | set(WRITE_TOOLS) | set(NEW_WRITE_TOOLS) | {"list_custom_asset_hardware_types"}
    # the one DELETE is the asset TYPE (gated), never an agent or a custom asset
    assert [op for s in mine for op in s.ops if op.startswith("DELETE ")] == ["DELETE /v1/assets/types/{assetTypeId}"]
    assert "DELETE /v1/assets/agents/{agentAssetId}" in FORBIDDEN_OPS and "DELETE /v1/assets/custom/{customAssetId}" in FORBIDDEN_OPS


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_docstrings_follow_the_template(name):
    doc = next(s for s in REGISTRY.specs if s.name == name).fn.__doc__
    if name in TEMPLATE_READS:  # the 1.5.0 reads state their ids and that they change nothing
        assert "Resolve ids first:" in doc and "Side effects: None (read-only)." in doc
    else:
        assert "Side effects:" not in doc  # read only: nothing to state
    assert ("Paging:" in doc) == (name in ("list_agents", "list_custom_assets", "list_policies"))


TEMPLATE_READS = {"list_policies", "get_policy", "list_asset_tags"}


def test_the_template_reads_of_the_policies_name_the_tool_that_resolves_their_id():
    assert "policy_id -> list_policies" in assets_module.get_policy.__doc__


@pytest.mark.parametrize(
    "name, param, resolver",
    [
        ("list_agents", "client_ids", "list_clients"),
        ("list_custom_assets", "client_ids", "list_clients"),
        ("get_agent", "agent_id", "list_agents"),
        *[(name, "agent_id", "list_agents") for name in AGENT_SUB_TOOLS],
        ("get_custom_asset", "custom_asset_id", "list_custom_assets"),
    ],
)
async def test_each_id_description_names_the_tool_that_resolves_it(server, name, param, resolver):
    tool = next(t for t in await list_tools(server) if t.name == name)
    assert resolver in tool.inputSchema["properties"][param]["description"]


def test_the_agent_list_docstring_names_the_renamed_fields():
    text = flat(assets_module.list_agents.__doc__)
    assert "LocationId" in text and "WarrantyStartDate" in text and "WarrantyEndDate" in text
    for fragment in ("SerialNumber", "Type ({Id, Name})", "ContactId", "Banner", "SerialNo and HardwareType are deprecated"):
        assert fragment in text, fragment
    single = flat(assets_module.get_agent.__doc__)
    assert "SerialNumber" in single and "Banner" in single and "SerialNo and HardwareType are deprecated" in single


def test_the_agent_docstrings_send_the_model_from_the_list_to_the_single_record_for_custom_fields():
    # the list rows leave CustomProperties out and get_agent has it (contract 1b1c0cec0e9a); each pointer is a few words, because
    # the default tools/list is at its byte budget (tests/test_integration.py)
    assert "Rows omit custom fields: get_agent has them." in flat(assets_module.list_agents.__doc__)
    assert "returns Gorelo's full device record, including CustomProperties (a field with IsHidden true has a null Value; IsSet still says whether one is saved)." in flat(assets_module.get_agent.__doc__)


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
        ({"status_ids": [2, 0]}, f"status_ids[1]: expected {AGENT_SCALE}, got 0"),
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


@pytest.mark.parametrize("status", [1, 2, 3, 4, 6])
async def test_every_device_status_of_the_scale_is_sent(server, mock_gorelo, status):
    mock_gorelo.on("GET", AGENTS, paged_envelope([agent_record()]))
    result = await call_tool(server, "list_agents", {"status_ids": [status]})
    assert mock_gorelo.last.query == {"StatusIds": str(status), "PageSize": "100"}
    assert result["filters"] == {"status_ids": [status]}


async def test_the_whole_device_scale_goes_in_one_filter_in_the_order_given(server, mock_gorelo):
    mock_gorelo.on("GET", AGENTS, paged_envelope([]))
    result = await call_tool(server, "list_agents", {"status_ids": [6, 1, 2, 3, 4]})
    assert mock_gorelo.last.query == {"StatusIds": "6,1,2,3,4", "PageSize": "100"}
    assert result["filters"] == {"status_ids": [6, 1, 2, 3, 4]}


@pytest.mark.parametrize("bad", [0, -1, -7, 5, 7, 99, 100000])
async def test_a_device_status_outside_the_scale_is_refused_with_the_scale_and_sends_nothing(server, mock_gorelo, bad):
    text = await call_tool_error(server, "list_agents", {"status_ids": [2, bad]})
    assert text == f"status_ids[1]: expected {AGENT_SCALE}, got {bad}"
    assert mock_gorelo.requests == []


async def test_a_device_status_above_int64_keeps_the_id_message(server, mock_gorelo):
    text = await call_tool_error(server, "list_agents", {"status_ids": [2**63]})
    assert text.startswith("status_ids[0]: expected a positive whole number such as 123, got a number above ")
    assert mock_gorelo.requests == []


async def test_up_to_100_device_status_ids_are_sent_and_101_are_refused(server, mock_gorelo):
    mock_gorelo.on("GET", AGENTS, paged_envelope([]))
    await call_tool(server, "list_agents", {"status_ids": [2] * 100})
    assert mock_gorelo.last.query["StatusIds"] == ",".join(["2"] * 100)
    mock_gorelo.reset()
    text = await call_tool_error(server, "list_agents", {"status_ids": [2] * 101})
    assert text == "status_ids: at most 100 ids, got 101"
    assert mock_gorelo.requests == []


async def test_the_status_ids_text_the_model_reads_is_built_from_the_table_the_check_uses(server):
    assert assets_module.AGENT_STATUS_SCALE == AGENT_SCALE and assets_module.AGENT_STATUS_IDS_MAX == 100
    tool = next(t for t in await list_tools(server) if t.name == "list_agents")
    text = tool.inputSchema["properties"]["status_ids"]["description"]
    assert AGENT_SCALE in text and "Up to 100 ids" in text and "any other id is refused" in text


@pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")
def test_the_device_status_scale_is_the_one_in_the_spec_text():
    parameters = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))["paths"]["/v1/assets/agents"]["get"]["parameters"]
    text = flat(next(p for p in parameters if p["name"] == "StatusIds")["description"])
    found = re.search(r"to include: (.*?)\. Up to (\d+) ids; any other value is a 400", text)
    assert found, text
    scale = {int(n): label for n, label in re.findall(r"(\d+) ([A-Za-z ]+?)(?:, |$)", found.group(1))}
    # contract 05200951c89e renamed two labels in the text (2 Active -> Online, 3 Inactive -> Offline); the ids are the same.
    assert scale == assets_module.AGENT_STATUS_LABELS and int(found.group(2)) == assets_module.AGENT_STATUS_IDS_MAX


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
# (Type is {Id, Name}) and the formats of their values are not pinned here. Value is the effective value (the saved one, else the
# field's default, else null) and is always text; IsSet says whether a value is saved on the device.
# IsHidden (contract a7954b24844b): Value is always null when it is true; IsSet is still reported.
CUSTOM_PROPERTIES = [
    {"Id": 41, "Variable": "warranty_vendor", "Name": "Warranty vendor", "Type": {"Id": 1, "Name": "Text"}, "Value": "Dell", "IsSet": True, "IsHidden": False},
    {"Id": 42, "Variable": "asset_tag", "Name": "Asset tag", "Type": {"Id": 1, "Name": "Text"}, "Value": None, "IsSet": False, "IsHidden": False},
    {"Id": 43, "Variable": "seats", "Name": "Seats", "Type": {"Id": 2, "Name": "Number"}, "Value": "5", "IsSet": False, "IsHidden": False},
    {"Id": 44, "Variable": "licence_key", "Name": "Licence key", "Type": {"Id": 1, "Name": "Text"}, "Value": None, "IsSet": True, "IsHidden": True},
]


async def test_get_agent_passes_custom_properties_through_unchanged(server, mock_gorelo):
    """The single record carries every device custom field. The tool neither reads nor reshapes them: the order, the Type
    object, a null Value and a default shown with IsSet false all come back as Gorelo sent them."""
    record = agent_record(7, CustomProperties=CUSTOM_PROPERTIES)
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(7)}", envelope(record))
    result = await call_tool(server, "get_agent", {"agent_id": uid(7)})
    assert result == record
    assert result["CustomProperties"] == CUSTOM_PROPERTIES
    assert [entry["Id"] for entry in result["CustomProperties"]] == [41, 42, 43, 44]
    assert [entry["Value"] for entry in result["CustomProperties"]] == ["Dell", None, "5", None]
    assert [entry["IsSet"] for entry in result["CustomProperties"]] == [True, False, False, True]
    assert [entry["IsHidden"] for entry in result["CustomProperties"]] == [False, False, False, True]
    assert len(mock_gorelo.requests) == 1  # one GET: the custom fields are part of the single record


async def test_get_agent_does_not_hide_or_reshape_a_hidden_field(server, mock_gorelo):
    """A hidden field (IsHidden true) has a null Value whatever is saved, and IsSet is still reported: the entry comes back as sent."""
    hidden = {
        "Id": 44, "Variable": "licence_key", "Name": "Licence key", "Type": {"Id": 1, "Name": "Text"},
        "Value": None, "IsSet": True, "IsHidden": True,
    }
    record = agent_record(7, CustomProperties=[hidden])
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(7)}", envelope(record))
    result = await call_tool(server, "get_agent", {"agent_id": uid(7)})
    assert result == record
    assert result["CustomProperties"] == [hidden] and result["CustomProperties"][0]["IsSet"] is True


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


@pytest.mark.parametrize("property_name", ["agentAssetId", "AgentAssetId"])
async def test_get_agent_maps_a_gorelo_error_about_the_id_to_agent_id(server, mock_gorelo, property_name):
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(9)}", error_envelope(400, [("070101", "The id is not valid.", property_name)]))
    text = await call_tool_error(server, "get_agent", {"agent_id": uid(9)})
    assert text == trace("Gorelo rejected get_agent (HTTP 400, code 070101): agent_id: The id is not valid.")


# What the model reads when Gorelo answers success with no record for an agent id (the text is pinned word for word).
NO_AGENT = (
    "agent_id: Gorelo has no agent with this id (unknown or deleted). For an unknown agent id Gorelo answers success with no "
    "record, unlike other records, which answer 404. Find agents with list_agents; read a custom asset with get_custom_asset."
)
# Measured live on 2026-10-24: an agent id Gorelo does not know answers HTTP 200, IsSuccess true and Data null (the first body).
# The second body has no Data key at all: get_one never told the two apart (the shape error it raised cannot say which one
# Gorelo wrote), so get_agent does not either, and both are the unknown agent.
NO_RECORD_BODIES = [
    pytest.param(envelope(None), id="data-null"),
    pytest.param({"StatusCode": 200, "IsSuccess": True, "Notifications": []}, id="data-missing"),
]


@pytest.mark.parametrize("body", NO_RECORD_BODIES)
async def test_get_agent_says_gorelo_has_no_such_agent_when_it_answers_success_with_no_record(server, mock_gorelo, body):
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(9)}", body)
    text = await call_tool_error(server, "get_agent", {"agent_id": uid(9)})
    assert text == NO_AGENT == assets_module.NO_AGENT_TEXT
    # it names the parameter, says what the answer means and sends the model on: list_agents for agents, get_custom_asset for custom assets
    assert text.startswith("agent_id: Gorelo has no agent with this id (unknown or deleted)")
    assert "Gorelo answers success with no record, unlike other records" in text
    assert "Find agents with list_agents" in text and "read a custom asset with get_custom_asset" in text
    # a plain refusal of a read: not the shape error it used to be, and no word about a write that may have been applied
    for old in ("unexpected response", "refusing to guess", "Data is null", "verify", "may or may not", "Gorelo returned", "Gorelo rejected"):
        assert old not in text, old
    assert [(r.method, r.path, r.query, r.content) for r in mock_gorelo.requests] == [("GET", f"/v1/assets/agents/{uid(9)}", {}, b"")]


def test_the_no_agent_text_names_only_tools_and_parameters_that_exist():
    tools_by_name = {s.name: s for s in REGISTRY.specs}
    names = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", NO_AGENT))
    assert names == {"agent_id", "list_agents", "get_custom_asset"}
    assert "agent_id" in inspect.signature(tools_by_name["get_agent"].fn).parameters  # the parameter the text names
    assert names - {"agent_id"} <= set(tools_by_name) and tools_by_name["get_custom_asset"].kind == "read"
    assert chr(0x2014) not in NO_AGENT and chr(0x2013) not in NO_AGENT


async def test_get_agent_still_returns_a_record_next_to_an_unknown_id(server, mock_gorelo):
    record = agent_record(7, CustomProperties=CUSTOM_PROPERTIES)
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(7)}", envelope(record))
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(9)}", envelope(None))
    assert await call_tool(server, "get_agent", {"agent_id": uid(7)}) == record
    assert await call_tool_error(server, "get_agent", {"agent_id": uid(9)}) == NO_AGENT
    assert await call_tool(server, "get_agent", {"agent_id": uid(7)}) == record
    assert [r.path for r in mock_gorelo.requests] == [f"/v1/assets/agents/{uid(n)}" for n in (7, 9, 7)]


async def test_get_agent_maps_gorelos_404_for_an_unknown_id_as_before(server, mock_gorelo):
    # every other single-record GET, and the agent sub-resources (apps, hardware, tags), answer this (measured live on 2026-10-24)
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(9)}", error_envelope(404, [("070202", "The requested resource was not found.")]))
    text = await call_tool_error(server, "get_agent", {"agent_id": uid(9)})
    assert text == trace("Gorelo rejected get_agent (HTTP 404, code 070202): The requested resource was not found.")
    assert text != NO_AGENT and len(mock_gorelo.requests) == 1


async def test_get_agent_does_not_mistake_a_failure_envelope_for_an_unknown_agent(server, mock_gorelo):
    # a failure envelope has Data null too: only a SUCCESS with no record is the unknown agent
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(9)}", error_envelope(200, [("070500", "Something broke.")]))
    text = await call_tool_error(server, "get_agent", {"agent_id": uid(9)})
    assert text == trace("Gorelo rejected get_agent (HTTP 200, code 070500): Something broke.")
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "response, expected",
    [
        pytest.param(
            error_envelope(500, [("070500", "Boom.")]),
            trace("Gorelo rejected get_agent (HTTP 500, code 070500): Boom."),
            id="http-500",
        ),
        pytest.param(
            error_envelope(403, [("080203", "API key does not have 'Assets' scope")]),
            trace("Gorelo rejected get_agent (HTTP 403, code 080203): the API key does not have the 'Assets' scope. Grant it on the API key in Gorelo, then retry"),
            id="missing-scope",
        ),
        pytest.param(
            httpx.ReadTimeout("slow"),
            "Gorelo did not answer get_agent (the request timed out). This was a read, so retrying is safe.",
            id="timeout",
        ),
    ],
)
async def test_get_agent_keeps_every_other_failure_as_it_was(server, mock_gorelo, response, expected):
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(9)}", response)
    text = await call_tool_error(server, "get_agent", {"agent_id": uid(9)})
    assert text == expected and len(mock_gorelo.requests) == 1


async def test_get_agent_keeps_the_shape_error_for_an_answer_that_is_no_envelope(server, mock_gorelo):
    page = httpx.Response(200, text="<html>gateway</html>", headers={"content-type": "text/html"})
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(9)}", page)
    text = await call_tool_error(server, "get_agent", {"agent_id": uid(9)})
    assert text.startswith("Gorelo returned an unexpected response for get_agent: GET /v1/assets/agents/{agentAssetId}: unexpected response shape; refusing to guess")
    assert "text/html" in text and text != NO_AGENT


@pytest.mark.parametrize("data, found", [({}, "an empty object"), ([agent_record(7)], "a list of 1 item"), ("x", "a string")])
async def test_get_agent_refuses_a_record_that_is_not_an_object(server, mock_gorelo, data, found):
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(7)}", envelope(data))
    text = await call_tool_error(server, "get_agent", {"agent_id": uid(7)})
    assert text.startswith("Gorelo returned an unexpected response for get_agent: GET /v1/assets/agents/{agentAssetId}: expected Data to be a non-empty object")
    assert f"but got {found}" in text and "refusing to guess" in text


@pytest.mark.parametrize(
    "data, found",
    [
        ([], "an empty list"),
        ([agent_record(7), agent_record(8)], "a list of 2 items"),
        ("", "an empty string"),
        (0, "a number"),
        (7, "a number"),
        (False, "a boolean"),
        (True, "a boolean"),
    ],
)
async def test_get_agent_takes_only_a_null_data_for_an_unknown_agent_not_other_empty_looking_data(server, mock_gorelo, data, found):
    # falsy is not null: an empty list, an empty string, 0 and false are shapes Gorelo does not promise, and stay the shape error
    mock_gorelo.on("GET", f"/v1/assets/agents/{uid(7)}", envelope(data))
    text = await call_tool_error(server, "get_agent", {"agent_id": uid(7)})
    assert text.startswith("Gorelo returned an unexpected response for get_agent: GET /v1/assets/agents/{agentAssetId}: expected Data to be a non-empty object")
    assert f"but got {found}" in text and "refusing to guess" in text and text != NO_AGENT
    assert "verify" not in text.lower()  # a read: nothing may have been applied


def test_the_single_agent_get_is_a_plain_unpaged_json_get_so_request_can_stand_in_for_get_one(spec_index):
    # get_agent sends it with GoreloClient.request, which skips the method, paging and download checks of get_one
    op = spec_index.op("GET /v1/assets/agents/{agentAssetId}")
    assert (op.method, op.paged, op.is_binary) == ("GET", False, False)
    assert op.path_placeholders == ("agentAssetId",) and not op.query_params and not op.body


def test_the_module_text_records_the_unknown_agent_answer_and_the_error_that_replaced_the_shape_error():
    doc = flat(assets_module.__doc__)
    assert "An unknown agent id (measured live with read-only GETs on 2026-10-24)" in doc
    assert "answers HTTP 200 with IsSuccess true and Data null" in doc
    assert 'every other single-record GET answers 404 ("The requested resource was not found."), the agent sub-resources apps, hardware and tags included' in doc
    assert "(for an id taken from elsewhere or mistyped)" in doc
    assert "in production" not in doc
    assert "turns a Data that is null or missing, the very condition under which get_one raised that error, into a plain refusal" in doc
    assert "names agent_id" in doc and "points to list_agents and get_custom_asset" in doc
    assert "The other per-agent reads keep get_one" in doc


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
# get_custom_asset
# --------------------------------------------------------------------------

GET_ASSET_PATH = f"{CUSTOM}/{uid(7)}"


async def test_get_custom_asset_reads_one_asset_and_returns_the_record_unchanged(server, mock_gorelo):
    record = custom_record(7, ContactId=7962, HardwareTypeId=118)
    mock_gorelo.on("GET", GET_ASSET_PATH, envelope(record))
    result = await call_tool(server, "get_custom_asset", {"custom_asset_id": uid(7)})
    assert result == record
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", GET_ASSET_PATH, {}, b"")
    assert len(mock_gorelo.requests) == 1


async def test_get_custom_asset_keeps_the_null_fields_as_gorelo_sent_them(server, mock_gorelo):
    # a null Description, SerialNumber, IpAddress or ContactId is an answer, not a reason to refuse or to drop the key
    record = custom_record(
        7, Description=None, SerialNumber=None, IpAddress=None, ContactId=None, WarrantyEndDate=None, HardwareType=None,
    )
    mock_gorelo.on("GET", GET_ASSET_PATH, envelope(record))
    result = await call_tool(server, "get_custom_asset", {"custom_asset_id": uid(7)})
    assert result == record
    assert all(result[key] is None for key in ("Description", "SerialNumber", "IpAddress", "ContactId"))


@pytest.mark.parametrize("given", [uid(7).upper(), uid(7).replace("-", ""), uid(7).upper().replace("-", "")])
async def test_get_custom_asset_accepts_any_case_and_form_of_the_uuid_and_sends_it_canonically(server, mock_gorelo, given):
    mock_gorelo.on("GET", GET_ASSET_PATH, envelope(custom_record(7)))
    await call_tool(server, "get_custom_asset", {"custom_asset_id": given})
    assert mock_gorelo.last.path == GET_ASSET_PATH


@pytest.mark.parametrize(
    "bad",
    [
        " ",
        "abc",
        "123",
        "00000001-aaaa-4bbb-8ccc-00000000000z",
        "00000001-aaaa-4bbb-8ccc",
        "..%2F..%2Fassets%2Fcustom",
        "../custom",
        f"{uid(7)}/extra",
        f"{uid(7)}?x=1",
        f"  {uid(7)}  ",  # nothing is trimmed or guessed
        f"{uid(7)}\n",
        "{" + uid(7) + "}",
        "urn:uuid:" + uid(7),
    ],
)
async def test_get_custom_asset_rejects_anything_that_is_not_a_uuid_naming_custom_asset_id_and_sends_nothing(server, mock_gorelo, bad):
    text = await call_tool_error(server, "get_custom_asset", {"custom_asset_id": bad})
    assert text == "custom_asset_id: expected a GUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b, got text that is not a GUID"
    assert mock_gorelo.requests == []


async def test_get_custom_asset_rejects_an_empty_string_without_echoing_it(server, mock_gorelo):
    text = await call_tool_error(server, "get_custom_asset", {"custom_asset_id": ""})
    assert text == "custom_asset_id: expected a GUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b, got an empty string"
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", [7, 7.5, True, None, ["x"]])
async def test_get_custom_asset_rejects_a_value_that_is_not_text_before_any_http_call(server, mock_gorelo, bad):
    text = await call_tool_error(server, "get_custom_asset", {"custom_asset_id": bad})
    assert "custom_asset_id" in text
    assert mock_gorelo.requests == []


async def test_get_custom_asset_maps_a_missing_asset_to_a_tool_error(server, mock_gorelo):
    mock_gorelo.on("GET", f"{CUSTOM}/{uid(9)}", error_envelope(404, [("070202", "The requested resource was not found.")]))
    text = await call_tool_error(server, "get_custom_asset", {"custom_asset_id": uid(9)})
    assert text == trace("Gorelo rejected get_custom_asset (HTTP 404, code 070202): The requested resource was not found.")


@pytest.mark.parametrize("property_name", ["customAssetId", "CustomAssetId"])
async def test_get_custom_asset_maps_a_gorelo_error_about_the_id_to_custom_asset_id(server, mock_gorelo, property_name):
    mock_gorelo.on("GET", f"{CUSTOM}/{uid(9)}", error_envelope(400, [("070101", "The id is not valid.", property_name)]))
    text = await call_tool_error(server, "get_custom_asset", {"custom_asset_id": uid(9)})
    assert text == trace("Gorelo rejected get_custom_asset (HTTP 400, code 070101): custom_asset_id: The id is not valid.")


async def test_get_custom_asset_refuses_a_success_without_data(server, mock_gorelo):
    mock_gorelo.on("GET", GET_ASSET_PATH, envelope(None))
    assert "Data is null" in await call_tool_error(server, "get_custom_asset", {"custom_asset_id": uid(7)})


@pytest.mark.parametrize("data, found", [({}, "an empty object"), ([custom_record(7)], "a list of 1 item"), ("x", "a string")])
async def test_get_custom_asset_refuses_a_record_that_is_not_an_object(server, mock_gorelo, data, found):
    mock_gorelo.on("GET", GET_ASSET_PATH, envelope(data))
    text = await call_tool_error(server, "get_custom_asset", {"custom_asset_id": uid(7)})
    assert text.startswith(
        "Gorelo returned an unexpected response for get_custom_asset: GET /v1/assets/custom/{customAssetId}: "
        "expected Data to be a non-empty object"
    )
    assert f"but got {found}" in text and "refusing to guess" in text


async def test_get_custom_asset_never_sends_a_delete_even_for_a_well_formed_id(server, mock_gorelo):
    mock_gorelo.on("GET", GET_ASSET_PATH, envelope(custom_record(7)))
    await call_tool(server, "get_custom_asset", {"custom_asset_id": uid(7)})
    assert {r.method for r in mock_gorelo.requests} == {"GET"}


def test_the_get_custom_asset_text_points_at_the_move_and_the_nulls():
    text = flat(assets_module.get_custom_asset.__doc__)
    assert "Description, SerialNumber, LocalIPAddress and ContactId are null" in text
    assert "update_custom_asset" in text


def test_the_custom_asset_list_docstring_points_at_get_custom_asset():
    text = flat(assets_module.list_custom_assets.__doc__)
    assert "get_custom_asset reads one by UUID" in text
    assert "There is no way to read one custom asset" not in text


@pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")
def test_the_single_custom_asset_get_is_the_one_the_spec_text_describes():
    get = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))["paths"]["/v1/assets/custom/{customAssetId}"]["get"]
    assert "Returns the same fields as an item of `GET /v1/assets/custom`." in flat(get["description"])
    assert "404" in get["responses"]


def test_the_single_custom_asset_get_answers_one_unpaged_custom_asset_row(spec_index):
    op = spec_index.op("GET /v1/assets/custom/{customAssetId}")
    assert op.response["data"] == "CustomAssetResponse" and op.paged is False
    assert set(op.path_placeholders) == {"customAssetId"}


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
    for fragment in (
        "ContactId", "DisplayName", "Type ({Id, Name}", "LocalIPAddress", "Manufacturer", "Model", "WarrantyStartDate",
        "WarrantyEndDate", "calendar dates", "HardwareType, HardwareTypeId and IpAddress are deprecated",
        "create_custom_asset", "update_custom_asset",
    ):
        assert fragment in text
    assert "cannot delete custom assets" in text


def test_the_spec_gives_the_custom_asset_row_the_new_fields(spec_index):
    fields = spec_index.schema("CustomAssetResponse")["fields"]
    assert {"ContactId", "HardwareTypeId", "HardwareType", "WarrantyEndDate", "WarrantyStartDate", "LocalIPAddress", "Manufacturer", "Model", "Type", "DisplayName"} <= set(fields)
    assert fields["WarrantyEndDate"]["format"] == "date"
    assert fields["Description"]["nullable"] is True


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
    assert "DEPRECATED by Gorelo: use list_asset_types" in doc and RAW_INDEX["ops"][spec.ops[0]].get("deprecated") is True


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
    "name", "client_id", "location_id", "hardware_type_id", "description", "contact_id", "warranty_start_date",
    "warranty_end_date", "local_ip_address", "ip_address", "manufacturer", "model", "serial_number",
]
UPDATE_PARAMS = [
    "custom_asset_id", "client_id", "name", "location_id", "hardware_type_id", "description", "contact_id",
    "warranty_start_date", "warranty_end_date", "local_ip_address", "ip_address", "manufacturer", "model", "serial_number",
    "clear_fields",
]
CLEARABLE = [
    "description", "contact_id", "warranty_start_date", "warranty_end_date", "local_ip_address", "ip_address", "manufacturer",
    "model", "serial_number",
]
ASSET = uid(5)
ASSET_UP = ASSET.upper()
PATCH_PATH = f"{CUSTOM}/{ASSET}"
CREATE_MIN = {"name": "Reception printer", "client_id": 9101, "location_id": 4410, "hardware_type_id": 118}
CREATE_ALL = {
    **CREATE_MIN, "description": "HP LaserJet", "contact_id": 7962, "warranty_start_date": "2024-03-31",
    "warranty_end_date": "2027-03-31", "local_ip_address": "10.0.0.20", "manufacturer": "HP", "model": "M404dn",
    "serial_number": "CNB1K23456",
}
CREATE_ALL_BODY = {
    "Name": "Reception printer", "ClientId": 9101, "LocationId": 4410, "HardwareTypeId": 118,
    "Description": "HP LaserJet", "ContactId": 7962, "WarrantyStartDate": "2024-03-31", "WarrantyEndDate": "2027-03-31",
    "LocalIPAddress": "10.0.0.20", "Manufacturer": "HP", "Model": "M404dn", "SerialNumber": "CNB1K23456",
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
    assert update.inputSchema["required"] == ["custom_asset_id"]
    props = update.inputSchema["properties"]
    assert "MOVE" in props["client_id"]["description"] and "location_id" in props["client_id"]["description"]
    assert "new client" in props["location_id"]["description"]
    assert "(of the new client when moving)" in props["contact_id"]["description"]
    for tool in (create, update):
        assert all(p.get("description") for p in tool.inputSchema["properties"].values())
        assert tool.annotations.readOnlyHint is False
    assert (create.annotations.destructiveHint, update.annotations.destructiveHint) == (False, True)
    clear_items = update.inputSchema["properties"]["clear_fields"]
    assert clear_items["items"]["enum"] == CLEARABLE


def test_the_write_docstrings_say_what_the_user_needs_to_know():
    create = flat(assets_module.create_custom_asset.__doc__)
    assert "timeline" in create and "cannot delete custom assets" in create and "Gorelo app" in create
    assert "list_asset_types" in create
    assert "read back by id" in create and "get_custom_asset" in create
    update = flat(assets_module.update_custom_asset.__doc__)
    for fragment in ("or move it to another client", "same Id", "not a copy", "clear_fields contact_id", "get_custom_asset",
                     "related items", "assigning one adds it there", "replacing or clearing it removes the previous one",
                     "not cleared", "cannot delete custom assets"):
        assert fragment in update, fragment
    assert "client cannot be changed" not in update.lower()
    for name in WRITE_TOOLS:
        assert "Side effects:" in flat(getattr(assets_module, name).__doc__)


# --------------------------------------------------------------------------
# create_custom_asset
# --------------------------------------------------------------------------


def row_for(asset_id, **extra):
    return custom_record(
        5, **{"Id": asset_id, "ClientId": 9101, "LocationId": 4410, "HardwareTypeId": 118, "ContactId": None, **extra}
    )


# The single GET of one custom asset: the same path as the PATCH of update_custom_asset.
GET_ROW = PATCH_PATH
OTHER_ASSET = uid(6)
# Read-backs that are no usable record of the asset written: an empty object, another asset, a record without an Id.
UNUSABLE_READ_BACKS = [
    (envelope({}), "an empty object"),
    (envelope(row_for(OTHER_ASSET)), f"the record of asset {OTHER_ASSET}"),
    (envelope({"Name": "Switch 6"}), "a record without an Id"),
]


def unusable_read_back(found, written=ASSET):
    """What a write returns when its read-back answers no usable record of the asset: {Id, warning}."""
    return {
        "Id": written,
        "warning": (
            f"the write succeeded; re-reading it returned no usable record of this asset ({found}). "
            "Do not repeat the write; check with get_custom_asset."
        ),
    }


async def test_create_sends_the_pascal_case_body_then_reads_the_row_back_by_id(server, mock_gorelo):
    row = row_for(ASSET)
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", GET_ROW, envelope(row))
    result = await call_tool(server, "create_custom_asset", CREATE_ALL)
    post, get = mock_gorelo.requests
    assert (post.method, post.path, post.query, post.json) == ("POST", CUSTOM, {}, CREATE_ALL_BODY)
    assert (get.method, get.path, get.query, get.content) == ("GET", GET_ROW, {}, b"")
    assert len(mock_gorelo.requests) == 2
    assert result == row


async def test_create_with_only_the_required_fields_omits_every_optional_key(server, mock_gorelo):
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", GET_ROW, envelope(row_for(ASSET)))
    await call_tool(server, "create_custom_asset", CREATE_MIN)
    assert mock_gorelo.requests[0].json == {
        "Name": "Reception printer", "ClientId": 9101, "LocationId": 4410, "HardwareTypeId": 118
    }


async def test_create_reads_back_with_the_canonical_path_when_the_answer_id_is_upper_case(server, mock_gorelo):
    row = row_for(ASSET)
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET_UP}))
    mock_gorelo.on("GET", GET_ROW, envelope(row))
    assert await call_tool(server, "create_custom_asset", CREATE_MIN) == row
    assert mock_gorelo.last.path == GET_ROW


async def test_create_returns_a_warning_not_an_error_when_the_read_back_fails(server, mock_gorelo):
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", GET_ROW, error_envelope(500, [("070500", "Boom.")]))
    result = await call_tool(server, "create_custom_asset", CREATE_MIN)
    assert set(result) == {"Id", "warning"} and result["Id"] == ASSET
    warning = result["warning"]
    assert "the write succeeded" in warning and "re-reading it failed" in warning and "HTTP 500" in warning
    assert "Do not repeat the write" in warning and "GET /v1/assets/custom/{customAssetId}" in warning


async def test_create_returns_a_warning_when_the_read_back_is_a_404(server, mock_gorelo):
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", GET_ROW, error_envelope(404, [("070202", "The requested resource was not found.")]))
    result = await call_tool(server, "create_custom_asset", CREATE_MIN)
    assert set(result) == {"Id", "warning"} and result["Id"] == ASSET
    assert "HTTP 404" in result["warning"] and "Do not repeat the write" in result["warning"]


@pytest.mark.parametrize("answer", [envelope(None), envelope([row_for(ASSET)])])
async def test_create_returns_a_warning_when_the_read_back_answers_something_odd(server, mock_gorelo, answer):
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", GET_ROW, answer)
    result = await call_tool(server, "create_custom_asset", CREATE_MIN)
    assert set(result) == {"Id", "warning"}


@pytest.mark.parametrize("answer, found", UNUSABLE_READ_BACKS)
async def test_create_returns_a_warning_when_the_read_back_is_not_a_usable_record_of_the_asset(server, mock_gorelo, answer, found):
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", GET_ROW, answer)
    assert await call_tool(server, "create_custom_asset", CREATE_MIN) == unusable_read_back(found)
    assert [r.method for r in mock_gorelo.requests] == ["POST", "GET"]


async def test_create_keeps_a_read_back_whose_id_differs_from_the_written_one_only_in_case(server, mock_gorelo):
    row = row_for(ASSET)
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET_UP}))
    mock_gorelo.on("GET", GET_ROW, envelope(row))
    assert await call_tool(server, "create_custom_asset", CREATE_MIN) == row


async def test_create_returns_a_warning_when_the_answer_id_is_not_a_uuid(server, mock_gorelo):
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": 7}))
    result = await call_tool(server, "create_custom_asset", CREATE_MIN)
    assert set(result) == {"Id", "warning"} and result["Id"] == 7
    assert "the write succeeded" in result["warning"]
    assert mock_gorelo.calls("GET") == []  # the path check refuses the id before any HTTP call


def test_create_declares_no_list_scan():
    create = next(s for s in REGISTRY.specs if s.name == "create_custom_asset")
    assert create.ops == ["POST /v1/assets/custom", "GET /v1/assets/custom/{customAssetId}"]


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
        ("WarrantyStartDate", "warranty_start_date"), ("LocalIPAddress", "local_ip_address"),
        ("Manufacturer", "manufacturer"), ("Model", "model"),
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
        ({"warranty_end_date": "2027-03-31+02:00"}, "warranty_end_date: expected a calendar date"),
        ({"warranty_end_date": "2027-03-31 10:00"}, "warranty_end_date: expected a calendar date"),
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
    mock_gorelo.on("GET", GET_ROW, envelope(row_for(ASSET)))
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
    return {"custom_asset_id": ASSET, **extra}


SECOND = 9102
SECOND_LOCATION = 4420


def moving(**extra):
    """A call that moves the asset from client 9101 (what row_for holds) to client 9102."""
    return update_args(client_id=SECOND, location_id=SECOND_LOCATION, **extra)


def after_move(**extra):
    return row_for(ASSET, ClientId=SECOND, LocationId=SECOND_LOCATION, **extra)


async def test_update_without_client_id_sends_only_the_changed_fields_then_reads_the_row_back(server, mock_gorelo):
    row = row_for(ASSET, Name="New name")
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", PATCH_PATH, envelope(row))
    result = await call_tool(server, "update_custom_asset", update_args(name="New name"))
    patch, get = mock_gorelo.requests
    assert (patch.method, patch.path, patch.query, patch.json) == ("PATCH", PATCH_PATH, {}, {"Name": "New name"})
    assert (get.method, get.path, get.query) == ("GET", PATCH_PATH, {})
    assert result == row


async def test_update_sends_every_field_under_its_pascal_case_name(server, mock_gorelo):
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", PATCH_PATH, envelope(row_for(ASSET)))
    await call_tool(server, "update_custom_asset", update_args(**{k: v for k, v in CREATE_ALL.items() if k != "client_id"}))
    assert mock_gorelo.requests[0].json == {k: v for k, v in CREATE_ALL_BODY.items() if k != "ClientId"}
    assert len(mock_gorelo.requests) == 2


async def test_update_clear_fields_are_sent_as_json_null(server, mock_gorelo):
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", PATCH_PATH, envelope(row_for(ASSET)))
    await call_tool(server, "update_custom_asset", update_args(clear_fields=CLEARABLE))
    assert mock_gorelo.requests[0].json == {
        "Description": None, "ContactId": None, "WarrantyStartDate": None, "WarrantyEndDate": None, "LocalIPAddress": None,
        "IpAddress": None, "Manufacturer": None, "Model": None, "SerialNumber": None,
    }
    assert b'"Description":null' in mock_gorelo.requests[0].content.replace(b" ", b"")


async def test_update_can_set_one_field_and_clear_another_in_one_call(server, mock_gorelo):
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", PATCH_PATH, envelope(row_for(ASSET)))
    await call_tool(server, "update_custom_asset", update_args(name="N", clear_fields=["ip_address", "ip_address"]))
    assert mock_gorelo.requests[0].json == {"Name": "N", "IpAddress": None}


async def test_update_normalizes_an_uppercase_asset_id_and_matches_the_row(server, mock_gorelo):
    row = row_for(ASSET)
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", PATCH_PATH, envelope(row))
    assert await call_tool(server, "update_custom_asset", update_args(custom_asset_id=ASSET_UP, serial_number="S")) == row
    assert [r.path for r in mock_gorelo.requests] == [PATCH_PATH, PATCH_PATH]


async def test_update_read_back_not_found_returns_a_warning(server, mock_gorelo):
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", PATCH_PATH, error_envelope(404, [("070202", "The requested resource was not found.")]))
    result = await call_tool(server, "update_custom_asset", update_args(name="N"))
    assert set(result) == {"Id", "warning"} and result["Id"] == ASSET
    assert "HTTP 404" in result["warning"] and "Do not repeat the write" in result["warning"]


async def test_update_read_back_failure_returns_a_warning_not_an_error(server, mock_gorelo):
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", PATCH_PATH, error_envelope(500, [("070500", "Boom.")]))
    result = await call_tool(server, "update_custom_asset", update_args(name="N"))
    assert set(result) == {"Id", "warning"} and "HTTP 500" in result["warning"]


@pytest.mark.parametrize("answer, found", UNUSABLE_READ_BACKS)
async def test_update_returns_a_warning_when_the_read_back_is_not_a_usable_record_of_the_asset(server, mock_gorelo, answer, found):
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", PATCH_PATH, answer)
    assert await call_tool(server, "update_custom_asset", update_args(name="N")) == unusable_read_back(found)
    assert [r.method for r in mock_gorelo.requests] == ["PATCH", "GET"]


async def test_update_without_an_id_in_the_answer_is_an_unconfirmed_write(server, mock_gorelo):
    mock_gorelo.on("PATCH", PATCH_PATH, envelope(None))
    text = await call_tool_error(server, "update_custom_asset", update_args(name="N"))
    assert "update_custom_asset" in text
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize("move, sent", [(False, ["PATCH"]), (True, ["GET", "PATCH"])])
async def test_a_patch_that_answers_another_id_is_an_unconfirmed_write_naming_both_ids_and_reads_nothing_back(
    server, mock_gorelo, move, sent
):
    if move:
        mock_gorelo.on("GET", PATCH_PATH, envelope(row_for(ASSET)))
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": OTHER_ASSET}))
    text = await call_tool_error(server, "update_custom_asset", moving() if move else update_args(name="N"))
    assert text == (
        "Gorelo returned an unexpected response for update_custom_asset: PATCH /v1/assets/custom/{customAssetId}: "
        f"Gorelo reported success for custom asset {ASSET} but answered with Id {OTHER_ASSET}; check both with "
        "get_custom_asset. The change may or may not have been applied. Verify with a read before retrying."
    )
    assert text.count("may or may not have been applied") == 1 and text.lower().count("verify") == 1  # said once, not twice
    assert [r.method for r in mock_gorelo.requests] == sent


async def test_a_patch_that_answers_the_same_id_in_upper_case_is_the_same_asset(server, mock_gorelo):
    row = row_for(ASSET)
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET_UP}))
    mock_gorelo.on("GET", PATCH_PATH, envelope(row))
    assert await call_tool(server, "update_custom_asset", update_args(name="N")) == row


@pytest.mark.parametrize(
    "property_name, param",
    [
        ("Name", "name"), ("ClientId", "client_id"), ("LocationId", "location_id"),
        ("HardwareTypeId", "hardware_type_id"), ("Description", "description"), ("ContactId", "contact_id"),
        ("WarrantyEndDate", "warranty_end_date"), ("IpAddress", "ip_address"), ("SerialNumber", "serial_number"),
        ("WarrantyStartDate", "warranty_start_date"), ("LocalIPAddress", "local_ip_address"),
        ("Manufacturer", "manufacturer"), ("Model", "model"),
        ("customAssetId", "custom_asset_id"),
    ],
)
async def test_update_maps_a_gorelo_400_to_the_snake_case_parameter(server, mock_gorelo, property_name, param):
    mock_gorelo.on("PATCH", PATCH_PATH, error_envelope(400, [("070101", "Rejected value.", property_name)]))
    text = await call_tool_error(server, "update_custom_asset", update_args(name="N"))
    assert text == trace(f"Gorelo rejected update_custom_asset (HTTP 400, code 070101): {param}: Rejected value.")
    assert len(mock_gorelo.requests) == 1


async def test_update_maps_a_404_without_reading_back(server, mock_gorelo):
    mock_gorelo.on("PATCH", PATCH_PATH, error_envelope(404, [("070202", "The requested resource was not found.")]))
    text = await call_tool_error(server, "update_custom_asset", update_args(name="N"))
    assert "update_custom_asset" in text and "HTTP 404" in text
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "property_name, param",
    [("ClientId", "client_id"), ("LocationId", "location_id"), ("ContactId", "contact_id")],
)
async def test_update_maps_a_gorelo_400_on_a_move_to_the_snake_case_parameter(server, mock_gorelo, property_name, param):
    mock_gorelo.on("GET", PATCH_PATH, envelope(row_for(ASSET)))
    mock_gorelo.on("PATCH", PATCH_PATH, error_envelope(400, [("070101", "Rejected value.", property_name)]))
    text = await call_tool_error(server, "update_custom_asset", moving())
    assert text == trace(f"Gorelo rejected update_custom_asset (HTTP 400, code 070101): {param}: Rejected value.")
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH"]


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
        ({"warranty_end_date": "2027-03-31T00:00:00Z"}, "warranty_end_date: expected a calendar date"),
        ({"warranty_end_date": "2027-03-31+02:00"}, "warranty_end_date: expected a calendar date"),
        ({"client_id": True}, "client_id"),
        ({"client_id": -1, "location_id": 4410}, "client_id"),
    ],
)
async def test_update_refuses_bad_input_before_any_http_call(server, mock_gorelo, arguments, fragment):
    text = await call_tool_error(server, "update_custom_asset", update_args(**arguments))
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("field, value", [
    ("description", "text"), ("contact_id", 7962), ("warranty_end_date", "2027-03-31"),
    ("ip_address", "10.0.0.1"), ("serial_number", "SN"), ("warranty_start_date", "2024-03-31"),
    ("local_ip_address", "10.0.0.1"), ("manufacturer", "HP"), ("model", "M404"),
])
async def test_update_refuses_a_value_together_with_a_clear_of_the_same_field(server, mock_gorelo, field, value):
    text = await call_tool_error(server, "update_custom_asset", update_args(**{field: value}, clear_fields=[field]))
    assert field in text and "cannot be given a value and listed in clear_fields" in text
    assert mock_gorelo.requests == []


async def test_update_with_client_id_alone_sends_nothing(server, mock_gorelo):
    for client_id in (9101, SECOND):
        text = await call_tool_error(server, "update_custom_asset", update_args(client_id=client_id))
        assert "nothing to change" in text and "client_id alone changes nothing" in text and "location_id" in text
    assert mock_gorelo.requests == []


async def test_update_with_nothing_at_all_sends_nothing(server, mock_gorelo):
    text = await call_tool_error(server, "update_custom_asset", update_args())
    assert "nothing to change" in text and "client_id alone changes nothing" in text and "location_id" in text
    assert mock_gorelo.requests == []


async def test_update_requires_only_the_asset_id(server, mock_gorelo):
    text = await call_tool_error(server, "update_custom_asset", {"name": "N"})
    assert "custom_asset_id" in text
    assert mock_gorelo.requests == []


async def test_update_naming_the_current_client_moves_nothing_and_still_sends_client_id(server, mock_gorelo):
    after = row_for(ASSET, Name="New name")
    mock_gorelo.on("GET", PATCH_PATH, in_order(envelope(row_for(ASSET)), envelope(after)))
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    result = await call_tool(server, "update_custom_asset", update_args(client_id=9101, name="New name"))
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", PATCH_PATH), ("PATCH", PATCH_PATH), ("GET", PATCH_PATH)]
    assert mock_gorelo.requests[1].json == {"ClientId": 9101, "Name": "New name"}
    assert result == after


async def test_update_naming_the_current_client_needs_no_location_or_contact_even_when_the_asset_has_a_contact(server, mock_gorelo):
    before = row_for(ASSET, ContactId=7962)
    after = row_for(ASSET, ContactId=7962, Name="N")
    mock_gorelo.on("GET", PATCH_PATH, in_order(envelope(before), envelope(after)))
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    result = await call_tool(server, "update_custom_asset", update_args(client_id=9101, name="N"))
    assert mock_gorelo.requests[1].json == {"ClientId": 9101, "Name": "N"}
    assert result == after


async def test_update_moves_the_asset_to_another_client_after_reading_it(server, mock_gorelo):
    mock_gorelo.on("GET", PATCH_PATH, in_order(envelope(row_for(ASSET)), envelope(after_move())))
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    result = await call_tool(server, "update_custom_asset", moving())
    pre, patch, back = mock_gorelo.requests
    for get in (pre, back):
        assert (get.method, get.path, get.query, get.content) == ("GET", PATCH_PATH, {}, b"")
    assert patch.json == {"ClientId": SECOND, "LocationId": SECOND_LOCATION}
    assert result == after_move()


@pytest.mark.parametrize("extra, expected", [({"contact_id": 7970}, 7970), ({"clear_fields": ["contact_id"]}, None)])
async def test_update_moves_an_asset_that_holds_a_contact_when_a_contact_is_given_or_cleared(server, mock_gorelo, extra, expected):
    held = row_for(ASSET, ContactId=7962)
    mock_gorelo.on("GET", PATCH_PATH, in_order(envelope(held), envelope(after_move(ContactId=expected))))
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    result = await call_tool(server, "update_custom_asset", moving(**extra))
    assert mock_gorelo.requests[1].json == {"ClientId": SECOND, "LocationId": SECOND_LOCATION, "ContactId": expected}
    assert result == after_move(ContactId=expected)


async def test_update_moves_an_asset_that_has_no_client(server, mock_gorelo):
    before = row_for(ASSET, ClientId=None, LocationId=None)
    mock_gorelo.on("GET", PATCH_PATH, in_order(envelope(before), envelope(after_move())))
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    result = await call_tool(server, "update_custom_asset", moving())
    assert mock_gorelo.requests[1].json == {"ClientId": SECOND, "LocationId": SECOND_LOCATION}
    assert result == after_move()


async def test_update_can_change_other_fields_in_the_same_call_as_a_move(server, mock_gorelo):
    after = after_move(Name="Moved printer", SerialNumber="S1")
    mock_gorelo.on("GET", PATCH_PATH, in_order(envelope(row_for(ASSET)), envelope(after)))
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    await call_tool(server, "update_custom_asset", moving(name="Moved printer", serial_number="S1"))
    assert mock_gorelo.requests[1].json == {
        "ClientId": SECOND, "LocationId": SECOND_LOCATION, "Name": "Moved printer", "SerialNumber": "S1",
    }


async def test_update_refuses_a_move_without_location_id(server, mock_gorelo):
    mock_gorelo.on("GET", PATCH_PATH, envelope(row_for(ASSET)))
    text = await call_tool_error(server, "update_custom_asset", update_args(client_id=SECOND, name="N"))
    assert text == (
        "location_id: required when client_id moves the asset (it is at client 9101 now); "
        "give a location of client 9102 (list_client_locations). Nothing was changed."
    )
    assert [r.method for r in mock_gorelo.requests] == ["GET"]
    assert mock_gorelo.calls("PATCH") == []


async def test_update_refuses_a_move_of_an_asset_with_no_client_without_location_id(server, mock_gorelo):
    mock_gorelo.on("GET", PATCH_PATH, envelope(row_for(ASSET, ClientId=None)))
    text = await call_tool_error(server, "update_custom_asset", update_args(client_id=SECOND, name="N"))
    assert "location_id: required when client_id moves the asset (it has no client now)" in text
    assert [r.method for r in mock_gorelo.requests] == ["GET"]
    assert mock_gorelo.calls("PATCH") == []


# The refusal of a move that leaves the asset's contact unsaid (_is_move), for contact 7962 and the new client 9102.
MOVE_CONTACT_REFUSAL = (
    "contact_id: the asset holds contact 7962; when client_id moves it, give contact_id (a contact of client 9102, "
    "which may be 7962 if it already belongs to that client) or put contact_id in clear_fields to unassign it. "
    "Nothing was changed."
)


async def test_update_refuses_a_move_that_leaves_the_contact_unsaid(server, mock_gorelo):
    mock_gorelo.on("GET", PATCH_PATH, envelope(row_for(ASSET, ContactId=7962)))
    text = await call_tool_error(server, "update_custom_asset", moving())
    assert text == MOVE_CONTACT_REFUSAL
    assert [r.method for r in mock_gorelo.requests] == ["GET"]
    assert mock_gorelo.calls("PATCH") == []


async def test_update_refuses_a_move_of_an_asset_with_no_client_that_holds_a_contact_with_the_same_text(server, mock_gorelo):
    mock_gorelo.on("GET", PATCH_PATH, envelope(row_for(ASSET, ClientId=None, LocationId=None, ContactId=7962)))
    text = await call_tool_error(server, "update_custom_asset", moving())
    assert text == MOVE_CONTACT_REFUSAL
    assert [r.method for r in mock_gorelo.requests] == ["GET"]
    assert mock_gorelo.calls("PATCH") == []


@pytest.mark.parametrize(
    "held",
    [row_for(ASSET, ClientId="x"), row_for(ASSET, ClientId=True), row_for(ASSET, ClientId=0), row_for(ASSET, ContactId="y")],
)
async def test_update_refuses_a_pre_read_it_cannot_use(server, mock_gorelo, held):
    mock_gorelo.on("GET", PATCH_PATH, envelope(held))
    text = await call_tool_error(server, "update_custom_asset", moving())
    assert text.startswith("custom_asset_id: GET /v1/assets/custom/{customAssetId} returned a ")
    assert text.endswith("refusing to guess whether client_id moves the asset. Nothing was changed.")
    assert [r.method for r in mock_gorelo.requests] == ["GET"]
    assert mock_gorelo.calls("PATCH") == []


async def test_update_stops_before_the_patch_when_the_pre_read_is_not_found(server, mock_gorelo):
    mock_gorelo.on("GET", PATCH_PATH, error_envelope(404, [("070202", "The requested resource was not found.")]))
    text = await call_tool_error(server, "update_custom_asset", moving())
    assert text == trace("Gorelo rejected update_custom_asset (HTTP 404, code 070202): The requested resource was not found.")
    assert [r.method for r in mock_gorelo.requests] == ["GET"]


@pytest.mark.parametrize("answer, fragment", [(error_envelope(500, [("070500", "Boom.")]), "Boom."), (envelope(None), "Data is null")])
async def test_update_stops_before_the_patch_when_the_pre_read_fails(server, mock_gorelo, answer, fragment):
    mock_gorelo.on("GET", PATCH_PATH, answer)
    text = await call_tool_error(server, "update_custom_asset", moving())
    assert "update_custom_asset" in text and fragment in text
    assert [r.method for r in mock_gorelo.requests] == ["GET"]
    assert mock_gorelo.calls("PATCH") == []


async def test_update_raises_a_write_unconfirmed_error_when_the_asset_reads_back_at_the_old_client(server, mock_gorelo):
    mock_gorelo.on("GET", PATCH_PATH, in_order(envelope(row_for(ASSET)), envelope(row_for(ASSET))))
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    text = await call_tool_error(server, "update_custom_asset", moving())
    assert text.startswith(
        "Gorelo returned an unexpected response for update_custom_asset: PATCH /v1/assets/custom/{customAssetId}: "
        "Gorelo reported success but the asset reads back at client 9101, not at client 9102;"
    )
    assert "verify it with a read before repeating it" in text
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH", "GET"]


async def test_update_raises_the_same_error_when_the_asset_reads_back_with_no_client(server, mock_gorelo):
    mock_gorelo.on("GET", PATCH_PATH, in_order(envelope(row_for(ASSET)), envelope(row_for(ASSET, ClientId=None))))
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    text = await call_tool_error(server, "update_custom_asset", moving())
    assert "reads back at no client" in text and "verify it with a read before repeating it" in text


async def test_update_returns_a_warning_when_the_read_back_of_a_move_fails(server, mock_gorelo):
    mock_gorelo.on("GET", PATCH_PATH, in_order(envelope(row_for(ASSET)), error_envelope(500, [("070500", "Boom.")])))
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    result = await call_tool(server, "update_custom_asset", moving())
    assert set(result) == {"Id", "warning"} and result["Id"] == ASSET


@pytest.mark.parametrize("answer, found", UNUSABLE_READ_BACKS)
async def test_a_move_whose_read_back_is_not_a_usable_record_of_the_asset_returns_a_warning_and_does_not_raise(
    server, mock_gorelo, answer, found
):
    mock_gorelo.on("GET", PATCH_PATH, in_order(envelope(row_for(ASSET)), answer))
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    assert await call_tool(server, "update_custom_asset", moving()) == unusable_read_back(found)
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH", "GET"]


@pytest.mark.parametrize(
    "failure, text",
    [
        (
            httpx.ReadTimeout("slow"),
            "Gorelo did not confirm update_custom_asset (the request timed out). The change may or may not have been "
            "applied. Verify with a read before retrying.",
        ),
        (
            httpx.ConnectError("refused"),
            "Gorelo did not confirm update_custom_asset (the connection failed). The change may or may not have been "
            "applied. Verify with a read before retrying.",
        ),
        (
            httpx.Response(502, text="<html>Bad Gateway</html>"),
            "Gorelo returned an unexpected response for update_custom_asset: PATCH /v1/assets/custom/{customAssetId}: "
            "unexpected response shape; refusing to guess (HTTP 502, a body that is not JSON, content-type text/plain, "
            "24 bytes). The change may or may not have been applied. Verify with a read before retrying.",
        ),
    ],
    ids=["read-timeout", "connection-error", "502-gateway-page"],
)
async def test_a_move_whose_patch_times_out_loses_the_connection_or_answers_502_is_unconfirmed_and_not_repeated(
    server, mock_gorelo, failure, text
):
    mock_gorelo.on("GET", PATCH_PATH, envelope(row_for(ASSET)))
    mock_gorelo.on("PATCH", PATCH_PATH, failure)
    assert await call_tool_error(server, "update_custom_asset", moving()) == text
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH"]


async def test_update_checks_the_new_client_only_for_a_move(server, mock_gorelo):
    mock_gorelo.on("GET", PATCH_PATH, in_order(envelope(row_for(ASSET)), envelope(row_for(ASSET, ClientId=9999))))
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    result = await call_tool(server, "update_custom_asset", update_args(client_id=9101, name="N"))
    assert result == row_for(ASSET, ClientId=9999)


def test_update_declares_the_single_get_and_no_list():
    spec = next(s for s in REGISTRY.specs if s.name == "update_custom_asset")
    assert spec.ops == ["PATCH /v1/assets/custom/{customAssetId}", "GET /v1/assets/custom/{customAssetId}"]
    assert spec.destructive_hint is True and spec.kind == "write"


def test_the_patch_takes_a_nullable_client_id_in_the_spec_index(spec_index):
    assert spec_index.op("PATCH /v1/assets/custom/{customAssetId}").body["fields"]["ClientId"]["nullable"] is True


@pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")
def test_the_move_rules_the_update_tool_states_are_in_the_spec_text():
    raw = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))
    patch = flat(raw["paths"]["/v1/assets/custom/{customAssetId}"]["patch"]["description"])
    assert "A new `ClientId` moves the asset to that client." in patch
    assert "send a `LocationId` of the new client, and a `ContactId` of it or null" in patch
    assert "assigning a contact adds it there, and replacing or clearing it removes the previous one" in patch
    schemas = raw["components"]["schemas"]
    client = flat(schemas["UpdateCustomAssetCommand"]["properties"]["ClientId"]["description"])
    assert "must be an active client" in client and "A different client moves the asset" in client
    for command in ("CreateCustomAssetCommand", "UpdateCustomAssetCommand"):
        assert "A value with a time or a time zone is rejected" in flat(schemas[command]["properties"]["WarrantyEndDate"]["description"])


# --------------------------------------------------------------------------
# contract 05200951c89e: the per-agent reads and list_asset_types
# --------------------------------------------------------------------------

SUB_BODIES = {
    "list_agent_apps": {"LastScannedOn": "2026-10-20T08:00:00Z", "Items": [{"Name": "7-Zip", "Version": "23.01"}]},
    "get_agent_hardware": {
        "LastScannedOn": "2026-10-20T08:00:00Z", "HardwareType": "Laptop", "TotalMemoryBytes": 17179869184,
        "Processors": [{"Name": "Intel i7"}], "Disks": [{"Name": "C:", "Partitions": [{"Name": "C:"}]}],
    },
    "list_agent_patches": {"LastScannedOn": "2026-10-20T08:00:00Z", "Items": [{"Title": "KB5031356"}]},
    "list_agent_available_updates": {
        "LastScannedOn": "2026-10-20T08:00:00Z", "WindowsUpdateStatus": {"Id": 1, "Name": "Up to date"}, "Items": [],
    },
}
OBJECT_SUB_TOOLS = sorted(SUB_BODIES)


def sub_path(name, n=7):
    return f"{AGENTS}/{uid(n)}{AGENT_SUB_TOOLS[name]}"


def test_the_new_agent_reads_are_unpaged_envelopes_in_the_spec_and_say_what_the_brief_says(spec_index):
    for name, suffix in AGENT_SUB_TOOLS.items():
        op = spec_index.op(f"GET /v1/assets/agents/{{agentAssetId}}{suffix}")
        assert op.paged is False and set(op.path_placeholders) == {"agentAssetId"} and not op.query_params
    spec = next(s for s in REGISTRY.specs if s.name == "list_agent_tags")
    assert spec.field_map == {"agent_id": "agentAssetId"}
    assert "macOS" in flat(assets_module.list_agent_available_updates.__doc__)
    assert "Sizes are in bytes" in flat(assets_module.get_agent_hardware.__doc__)
    assert "list_agent_available_updates" in flat(assets_module.list_agent_patches.__doc__)
    for name in (*AGENT_SUB_TOOLS, "list_asset_types"):
        assert len(next(s for s in REGISTRY.specs if s.name == name).fn.__doc__) < 400, name


@pytest.mark.parametrize("name", OBJECT_SUB_TOOLS)
async def test_the_agent_object_reads_return_the_record_as_gorelo_sent_it(server, mock_gorelo, name):
    body = SUB_BODIES[name]
    mock_gorelo.on("GET", sub_path(name), envelope(body))
    result = await call_tool(server, name, {"agent_id": uid(7)})
    assert result == body
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", sub_path(name), {}, b"")
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize("name", OBJECT_SUB_TOOLS)
async def test_the_agent_object_reads_pass_an_agent_that_never_reported_through(server, mock_gorelo, name):
    body = {"LastScannedOn": None, "Items": []}
    mock_gorelo.on("GET", sub_path(name), envelope(body))
    assert await call_tool(server, name, {"agent_id": uid(7)}) == body


async def test_list_agent_tags_returns_the_envelope_list_as_items_and_count(server, mock_gorelo):
    rows = [{"Id": 3, "Name": "Servers", "Type": {"Id": 1, "Name": "Manual"}, "Conditions": {}}, {"Id": 4, "Name": "VIP"}]
    mock_gorelo.on("GET", sub_path("list_agent_tags"), envelope(rows))
    result = await call_tool(server, "list_agent_tags", {"agent_id": uid(7)})
    assert result == {"items": rows, "count": 2}
    assert mock_gorelo.last.query == {} and len(mock_gorelo.requests) == 1


async def test_list_agent_tags_reports_an_agent_without_tags_as_an_empty_list(server, mock_gorelo):
    mock_gorelo.on("GET", sub_path("list_agent_tags"), envelope([]))
    assert await call_tool(server, "list_agent_tags", {"agent_id": uid(7)}) == {"items": [], "count": 0}


@pytest.mark.parametrize("name", sorted(AGENT_SUB_TOOLS))
@pytest.mark.parametrize("given", [uid(7).upper(), uid(7).replace("-", "")])
async def test_the_agent_reads_send_the_uuid_canonically(server, mock_gorelo, name, given):
    mock_gorelo.on("GET", sub_path(name), envelope([] if name == "list_agent_tags" else SUB_BODIES.get(name, {})))
    await call_tool(server, name, {"agent_id": given})
    assert mock_gorelo.last.path == sub_path(name)


@pytest.mark.parametrize("name", sorted(AGENT_SUB_TOOLS))
@pytest.mark.parametrize("bad", ["", "abc", f"{uid(7)}/extra", "../agents", 7, True, None])
async def test_the_agent_reads_reject_a_bad_agent_id_naming_agent_id_and_send_nothing(server, mock_gorelo, name, bad):
    text = await call_tool_error(server, name, {"agent_id": bad})
    assert "agent_id" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("name", sorted(AGENT_SUB_TOOLS))
async def test_the_agent_reads_name_agent_id_when_gorelo_rejects_the_path_id(server, mock_gorelo, name):
    mock_gorelo.on("GET", sub_path(name), error_envelope(400, [("070101", "The id is not valid.", "agentAssetId")]))
    text = await call_tool_error(server, name, {"agent_id": uid(7)})
    assert text == trace(f"Gorelo rejected {name} (HTTP 400, code 070101): agent_id: The id is not valid.")


@pytest.mark.parametrize("name", sorted(AGENT_SUB_TOOLS))
async def test_the_agent_reads_map_a_missing_agent_to_a_tool_error(server, mock_gorelo, name):
    mock_gorelo.on("GET", sub_path(name), error_envelope(404, [("070404", "Agent not found.")]))
    text = await call_tool_error(server, name, {"agent_id": uid(7)})
    assert text == trace(f"Gorelo rejected {name} (HTTP 404, code 070404): Agent not found.")


async def test_list_agent_available_updates_shows_gorelos_400_for_a_macos_agent(server, mock_gorelo):
    mock_gorelo.on(
        "GET", sub_path("list_agent_available_updates"),
        error_envelope(400, [("070101", "Windows updates are not available for this device.", "DeviceId")]),
    )
    text = await call_tool_error(server, "list_agent_available_updates", {"agent_id": uid(7)})
    assert "HTTP 400" in text and "Windows updates are not available for this device." in text
    assert len(mock_gorelo.requests) == 1


async def test_the_available_updates_text_says_a_deviceid_error_means_agent_id(server):
    tool = next(t for t in await list_tools(server) if t.name == "list_agent_available_updates")
    assert "DeviceId, which means agent_id" in tool.description


@pytest.mark.parametrize("name", OBJECT_SUB_TOOLS)
@pytest.mark.parametrize("data, found", [(None, "Data is null"), ({}, "an empty object"), ([{"Name": "x"}], "a list of 1 item"), ("x", "a string")])
async def test_the_agent_object_reads_refuse_an_answer_that_is_not_an_object(server, mock_gorelo, name, data, found):
    mock_gorelo.on("GET", sub_path(name), envelope(data))
    text = await call_tool_error(server, name, {"agent_id": uid(7)})
    assert text.startswith(f"Gorelo returned an unexpected response for {name}") or found in text
    assert found in text


@pytest.mark.parametrize("data", [{"Id": 1}, "x", None])
async def test_list_agent_tags_refuses_an_answer_that_is_not_a_list(server, mock_gorelo, data):
    mock_gorelo.on("GET", sub_path("list_agent_tags"), envelope(data))
    text = await call_tool_error(server, "list_agent_tags", {"agent_id": uid(7)})
    assert text.startswith("Gorelo returned an unexpected response for list_agent_tags")


TYPES_PATH = "/v1/assets/types"


def test_list_asset_types_is_a_core_read_tool_with_no_parameters_and_the_hardware_type_tool_points_to_it(spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == "list_asset_types")
    assert (spec.toolset, spec.kind, spec.ops, spec.destructive_hint) == ("core", "read", ["GET /v1/assets/types"], False)
    assert spec_index.op(spec.ops[0]).paged is False
    doc = flat(spec.fn.__doc__)
    assert "Type.Id" in doc and "hardware_type_id" in doc


async def test_list_asset_types_returns_an_unpaged_list(server, mock_gorelo):
    rows = [{"Id": 1, "Name": "Desktop"}, {"Id": 2, "Name": "Laptop"}]
    mock_gorelo.on("GET", TYPES_PATH, envelope(rows))
    result = await call_tool(server, "list_asset_types")
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", TYPES_PATH, {}, b"")
    assert result == {"items": rows, "count": 2}


async def test_list_asset_types_refuses_a_non_list_answer(server, mock_gorelo):
    mock_gorelo.on("GET", TYPES_PATH, envelope({"Id": 1}))
    text = await call_tool_error(server, "list_asset_types")
    assert text.startswith("Gorelo returned an unexpected response for list_asset_types")


async def test_list_asset_types_maps_a_gorelo_error(server, mock_gorelo):
    mock_gorelo.on("GET", TYPES_PATH, error_envelope(500, [("070500", "Boom.")]))
    text = await call_tool_error(server, "list_asset_types")
    assert "list_asset_types" in text and "Boom." in text


# --------------------------------------------------------------------------
# contract 05200951c89e: the new custom asset fields
# --------------------------------------------------------------------------

NEW_FIELDS = {
    "warranty_start_date": ("WarrantyStartDate", "2024-03-31"),
    "local_ip_address": ("LocalIPAddress", "10.0.0.20"),
    "manufacturer": ("Manufacturer", "Ubiquiti"),
    "model": ("Model", "UniFi Dream Machine Pro"),
}


@pytest.mark.parametrize("param", sorted(NEW_FIELDS))
async def test_create_sends_each_new_field_under_its_spec_name(server, mock_gorelo, param):
    gorelo_name, value = NEW_FIELDS[param]
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", GET_ROW, envelope(row_for(ASSET)))
    await call_tool(server, "create_custom_asset", {**CREATE_MIN, param: value})
    assert mock_gorelo.requests[0].json == {
        "Name": "Reception printer", "ClientId": 9101, "LocationId": 4410, "HardwareTypeId": 118, gorelo_name: value
    }


@pytest.mark.parametrize("param", sorted(NEW_FIELDS))
async def test_update_sends_each_new_field_alone_and_clears_it_with_null(server, mock_gorelo, param):
    gorelo_name, value = NEW_FIELDS[param]
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", PATCH_PATH, envelope(row_for(ASSET)))
    await call_tool(server, "update_custom_asset", update_args(**{param: value}))
    await call_tool(server, "update_custom_asset", update_args(clear_fields=[param]))
    sent = [r.json for r in mock_gorelo.requests if r.method == "PATCH"]
    assert sent == [{gorelo_name: value}, {gorelo_name: None}]


async def test_update_refuses_a_new_field_together_with_its_clear(server, mock_gorelo):
    for param, (_gorelo_name, value) in NEW_FIELDS.items():
        text = await call_tool_error(server, "update_custom_asset", update_args(**{param: value}, clear_fields=[param]))
        assert f"{param}: cannot be given a value and listed in clear_fields" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("given, other", [("local_ip_address", "ip_address"), ("ip_address", "local_ip_address")])
async def test_update_refuses_an_address_value_together_with_a_clear_of_its_twin(server, mock_gorelo, given, other):
    text = await call_tool_error(server, "update_custom_asset", update_args(**{given: "10.0.0.9"}, clear_fields=[other]))
    assert f"{given} with {other} in clear_fields" in text and "400" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("fields", [["ip_address", "local_ip_address"], ["local_ip_address"], ["ip_address"]])
async def test_update_clearing_the_addresses_without_a_value_is_sent(server, mock_gorelo, fields):
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", PATCH_PATH, envelope(row_for(ASSET)))
    await call_tool(server, "update_custom_asset", update_args(clear_fields=fields))
    sent = next(r.json for r in mock_gorelo.requests if r.method == "PATCH")
    assert sent == {assets_module.CUSTOM_BODY_MAP[f]: None for f in fields}


async def test_a_new_field_alone_is_enough_for_an_update(server, mock_gorelo):
    mock_gorelo.on("PATCH", PATCH_PATH, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", PATCH_PATH, envelope(row_for(ASSET)))
    await call_tool(server, "update_custom_asset", update_args(model="X1"))
    assert mock_gorelo.requests[0].json == {"Model": "X1"}


@pytest.mark.parametrize("tool, base", [("create_custom_asset", CREATE_MIN), ("update_custom_asset", update_args())])
async def test_both_ip_parameters_are_refused_locally_naming_both(server, mock_gorelo, tool, base):
    text = await call_tool_error(server, tool, {**base, "ip_address": "10.0.0.1", "local_ip_address": "10.0.0.1"})
    assert "ip_address" in text and "local_ip_address" in text and "give only one" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("tool, base", [("create_custom_asset", CREATE_MIN), ("update_custom_asset", update_args())])
@pytest.mark.parametrize(
    "extra, fragment",
    [
        ({"warranty_start_date": "2027-02-30"}, "warranty_start_date: 2027-02-30 is not a real calendar date"),
        ({"warranty_start_date": "31/03/2027"}, "warranty_start_date: expected a calendar date written YYYY-MM-DD"),
        ({"warranty_start_date": "2027-03-31T00:00:00Z"}, "warranty_start_date: expected a calendar date"),
        ({"warranty_start_date": "20270331"}, "warranty_start_date: expected a calendar date"),
        ({"warranty_start_date": ""}, "warranty_start_date: expected a calendar date"),
        ({"local_ip_address": "::1"}, "local_ip_address: expected an IPv4 address"),
        ({"local_ip_address": "300.1.1.1"}, "local_ip_address: expected an IPv4 address"),
        ({"local_ip_address": ""}, "local_ip_address: expected an IPv4 address"),
        ({"manufacturer": " "}, "manufacturer: must not be empty"),
        ({"manufacturer": "m" * 301}, "manufacturer: at most 300 characters"),
        ({"model": ""}, "model: must not be empty"),
        ({"model": "m" * 301}, "model: at most 300 characters"),
        ({"model": 5}, "model"),
    ],
)
async def test_the_new_fields_are_validated_before_any_http_call(server, mock_gorelo, tool, base, extra, fragment):
    text = await call_tool_error(server, tool, {**base, **extra})
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_the_new_fields_accept_their_boundary_values(server, mock_gorelo):
    mock_gorelo.on("POST", CUSTOM, envelope({"Id": ASSET}))
    mock_gorelo.on("GET", GET_ROW, envelope(row_for(ASSET)))
    await call_tool(
        server, "create_custom_asset",
        {**CREATE_MIN, "manufacturer": "m" * 300, "model": "x" * 300, "warranty_start_date": "2028-02-29"},
    )
    body = mock_gorelo.requests[0].json
    assert (len(body["Manufacturer"]), len(body["Model"]), body["WarrantyStartDate"]) == (300, 300, "2028-02-29")


def test_the_ip_address_text_says_deprecated_and_the_new_fields_are_in_the_spec(spec_index):
    tools = {s.name: s for s in REGISTRY.specs}
    for name in ("create_custom_asset", "update_custom_asset"):
        assert "Deprecated, use local_ip_address" in next(
            p for p in tools[name].fn.__annotations__["ip_address"].__metadata__ if hasattr(p, "description")
        ).description
    for op in ("POST /v1/assets/custom", "PATCH /v1/assets/custom/{customAssetId}"):
        fields = spec_index.op(op).body["fields"]
        assert fields["IpAddress"]["deprecated"] is True
        assert {"LocalIPAddress", "Manufacturer", "Model", "WarrantyStartDate"} <= set(fields)


def test_the_nothing_to_change_text_names_the_new_fields():
    for fragment in ("warranty_start_date", "local_ip_address", "manufacturer", "model"):
        assert fragment in assets_module.NOTHING_TO_CHANGE


def test_the_device_status_labels_are_online_and_offline_with_unchanged_ids():
    assert assets_module.AGENT_STATUS_LABELS == {
        1: "Unreachable", 2: "Online", 3: "Offline", 4: "Pending Delete", 6: "Pending client inactive"
    }


def test_the_new_asset_modules_text_has_no_dashes():
    source = Path(assets_module.__file__).read_text(encoding="utf-8")
    assert chr(0x2014) not in source and chr(0x2013) not in source


# ==========================================================================
# Contract 6c30770dffc0: policies, asset tags, asset types (create, rename, delete) and update_agent
# ==========================================================================

POLICIES = "/v1/assets/policies"
ASSET_TAGS = "/v1/assets/tags"
TYPES = "/v1/assets/types"


def policy_record(n=1, **extra):
    record = {
        "Id": 100 + n,
        "Name": f"Policy {n}",
        "Os": "Windows",
        "Priority": 3,
        "Status": {"Id": 1, "Name": "Active"},
        "DistributedOn": None,
        "Checks": [{"Id": 1, "CheckId": 9, "IsEnabled": True}],
        "Scripts": [{"Id": 2, "ScriptId": uid(7), "IsEnabled": False}],
        "Plugins": [{"Id": 3, "PluginId": 4, "IsEnabled": True}],
        "Tags": {"AllOfTagIds": [6118], "AnyOfTagIds": [], "ExcludedTagIds": []},
        "CreatedOn": "2026-01-01T00:00:00Z",
        "UpdatedOn": "2026-01-02T00:00:00Z",
    }
    record.update(extra)
    return record


def tag_record(n=1, **extra):
    record = {
        "Id": 6100 + n,
        "Name": f"Tag {n}",
        "Type": {"Id": 2, "Name": "Custom"},
        "Conditions": {},
        "Description": None,
        "CreatedOn": "2026-01-01T00:00:00Z",
        "UpdatedOn": None,
    }
    record.update(extra)
    return record


# -- fixtures follow the spec ------------------------------------------------------------------------------------------


def test_the_policy_and_tag_fixtures_have_the_fields_of_the_spec(spec_index):
    assert spec_index.op("GET /v1/assets/policies").response["data"] == "[PolicyModel]"
    assert spec_index.op("GET /v1/assets/policies/{policyId}").response["data"] == "PolicyModel"
    assert spec_index.op("GET /v1/assets/tags").response["data"] == "[AssetTagModel]"
    assert set(policy_record()) == set(spec_index.schema("PolicyModel")["fields"])
    assert set(tag_record()) == set(spec_index.schema("AssetTagModel")["fields"])
    assert set(policy_record()["Tags"]) == set(spec_index.schema("PolicyTagsModel")["fields"])


# -- list_policies ------------------------------------------------------------------------------------------------------


def test_list_policies_query_names_are_the_ones_of_the_op(spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == "list_policies")
    assert spec.field_map == {"os": "Os", "page_size": "PageSize", "cursor": "Cursor"}
    assert {name.lower() for name in spec.field_map.values()} == {q.lower() for q in spec_index.op("GET /v1/assets/policies").query_params}
    assert spec_index.op("GET /v1/assets/policies").paged


async def test_list_policies_with_no_filter_sends_only_the_default_page_size(server, mock_gorelo):
    rows = [policy_record(1), policy_record(2)]
    mock_gorelo.on("GET", POLICIES, paged_envelope(rows, total_count=2))
    result = await call_tool(server, "list_policies")
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", POLICIES, {"PageSize": "50"}, b"")
    assert result == {"items": rows, "count": 2, "total_count": 2, "has_more": False, "next_cursor": None, "page_size": 50, "filters": {}}


@pytest.mark.parametrize("os_name", ["Windows", "MacOS", "Linux"])
async def test_list_policies_sends_the_os_filter_and_echoes_it(server, mock_gorelo, os_name):
    mock_gorelo.on("GET", POLICIES, paged_envelope([policy_record(1, Os=os_name)], next_cursor="c2", total_count=9))
    result = await call_tool(server, "list_policies", {"os": os_name, "page_size": 1, "cursor": "c1"})
    assert mock_gorelo.last.query == {"Os": os_name, "PageSize": "1", "Cursor": "c1"}
    assert result["filters"] == {"os": os_name} and result["has_more"] is True and result["next_cursor"] == "c2"


async def test_list_policies_follows_the_cursor_with_the_same_os(server, mock_gorelo):
    mock_gorelo.on("GET", POLICIES, paged_responder([[policy_record(1)], [policy_record(2)]], total_count=2))
    first = await call_tool(server, "list_policies", {"os": "Linux", "page_size": 1})
    second = await call_tool(server, "list_policies", {"os": "Linux", "page_size": 1, "cursor": first["next_cursor"]})
    assert second["has_more"] is False and second["items"] == [policy_record(2)]
    assert [r.query for r in mock_gorelo.requests] == [
        {"Os": "Linux", "PageSize": "1"}, {"Os": "Linux", "PageSize": "1", "Cursor": "c1"}
    ]


@pytest.mark.parametrize("asked, sent", [(500, 200), (0, 1), (200, 200)])
async def test_list_policies_clamps_page_size(server, mock_gorelo, asked, sent):
    mock_gorelo.on("GET", POLICIES, paged_envelope([policy_record()]))
    result = await call_tool(server, "list_policies", {"page_size": asked})
    assert mock_gorelo.last.query["PageSize"] == str(sent) and result["page_size"] == sent


async def test_list_policies_os_is_an_enum_in_the_schema(server):
    tool = next(t for t in await list_tools(server) if t.name == "list_policies")
    schema = tool.inputSchema["properties"]["os"]
    assert schema.get("enum") == ["Windows", "MacOS", "Linux"] or [v["enum"] for v in schema["anyOf"] if "enum" in v] == [["Windows", "MacOS", "Linux"]]
    assert "continues the os it was issued for" in schema["description"]


@pytest.mark.parametrize("arguments, param", [({"os": "BSD"}, "os"), ({"os": "windows"}, "os"), ({"os": 5}, "os"), ({"cursor": " "}, "cursor"), ({"page_size": "many"}, "page_size")])
async def test_list_policies_refuses_bad_input_before_any_request(server, mock_gorelo, arguments, param):
    text = await call_tool_error(server, "list_policies", arguments)
    assert param in text
    assert mock_gorelo.requests == []


async def test_list_policies_maps_a_gorelo_400_on_os_to_the_parameter(server, mock_gorelo):
    mock_gorelo.on("GET", POLICIES, error_envelope(400, [("070101", "Unknown operating system.", "Os")]))
    text = await call_tool_error(server, "list_policies", {})
    assert "list_policies" in text and "HTTP 400" in text and "os: Unknown operating system." in text


def test_list_policies_says_how_to_read_on_and_where_the_tag_ids_come_from():
    doc = flat(assets_module.list_policies.__doc__)
    assert "highest Priority first" in doc and "list_asset_tags" in doc and "override are not listed" in doc
    assert "Paging: pass next_cursor as cursor, same filters (os), until has_more is false." in doc


# -- get_policy ---------------------------------------------------------------------------------------------------------


async def test_get_policy_sends_one_get_and_returns_the_record_unchanged(server, mock_gorelo):
    row = policy_record(3)
    mock_gorelo.on("GET", f"{POLICIES}/103", envelope(row))
    assert await call_tool(server, "get_policy", {"policy_id": 103}) == row
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", f"{POLICIES}/103", {}, b"")
    assert len(mock_gorelo.requests) == 1


def test_get_policy_field_map_names_the_path_placeholder(spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == "get_policy")
    assert spec.field_map == {"policy_id": "policyId"}
    assert set(spec.field_map.values()) == set(spec_index.op("GET /v1/assets/policies/{policyId}").path_placeholders)


@pytest.mark.parametrize("bad", [0, -1, True, "5", 1.5, None])
async def test_get_policy_refuses_a_bad_id_before_any_request(server, mock_gorelo, bad):
    text = await call_tool_error(server, "get_policy", {"policy_id": bad})
    assert "policy_id" in text
    assert mock_gorelo.requests == []


async def test_get_policy_maps_a_404_to_the_tool(server, mock_gorelo):
    mock_gorelo.on("GET", f"{POLICIES}/999", error_envelope(404, [("070202", "The requested resource was not found.")]))
    text = await call_tool_error(server, "get_policy", {"policy_id": 999})
    assert "get_policy" in text and "HTTP 404" in text


async def test_get_policy_refuses_a_null_record(server, mock_gorelo):
    mock_gorelo.on("GET", f"{POLICIES}/5", envelope(None))
    assert "get_policy" in await call_tool_error(server, "get_policy", {"policy_id": 5})


# -- list_asset_tags ----------------------------------------------------------------------------------------------------


async def test_list_asset_tags_is_one_unpaged_get_with_no_query(server, mock_gorelo):
    rows = [tag_record(1), tag_record(2)]
    mock_gorelo.on("GET", ASSET_TAGS, envelope(rows))
    result = await call_tool(server, "list_asset_tags")
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", ASSET_TAGS, {}, b"")
    assert result == {"items": rows, "count": 2}


async def test_list_asset_tags_refuses_an_argument_it_does_not_have(server, mock_gorelo):
    assert "page_size" in await call_tool_error(server, "list_asset_tags", {"page_size": 5})
    assert mock_gorelo.requests == []


async def test_list_asset_tags_shows_a_gorelo_error(server, mock_gorelo):
    mock_gorelo.on("GET", ASSET_TAGS, error_envelope(500, [("070500", "Boom.")]))
    text = await call_tool_error(server, "list_asset_tags")
    assert "list_asset_tags" in text and "HTTP 500" in text


def test_list_asset_tags_says_where_the_ids_are_used_and_that_one_agents_tags_are_elsewhere():
    doc = flat(assets_module.list_asset_tags.__doc__)
    assert "the Tags of a policy carry" in doc and "list_policies" in doc and "list_agent_tags gives the tags of one agent" in doc
    assert "Not paged" in doc and "New first, then by name" in doc


def test_the_excluded_single_tag_op_is_not_declared_by_any_tool():
    assert not any("GET /v1/assets/tags/{tagId}" in spec.ops for spec in REGISTRY.specs)


# -- the writes: declarations -------------------------------------------------------------------------------------------


@pytest.fixture
def full(server_factory):
    """Core with the gated tools registered (delete_asset_type)."""
    return server_factory(toolsets={"core"}, destructive=True)


def test_the_new_writes_are_declared_with_their_ops_kinds_and_hints(spec_index):
    for name, (kind, ops, hint) in NEW_WRITE_TOOLS.items():
        spec = next(s for s in REGISTRY.specs if s.name == name)
        assert (spec.toolset, spec.kind, spec.ops, spec.destructive_hint) == ("core", kind, ops, hint), name
        assert all(op in spec_index.ops for op in spec.ops) and not set(spec.ops) & FORBIDDEN_OPS, name


def test_the_body_field_maps_match_the_spec_commands_of_the_new_writes(spec_index):
    agent = next(s for s in REGISTRY.specs if s.name == "update_agent")
    assert set(agent.field_map.values()) - {"agentAssetId"} == set(spec_index.op("PATCH /v1/assets/agents/{agentAssetId}").body["fields"])
    assert "agentAssetId" in spec_index.op("PATCH /v1/assets/agents/{agentAssetId}").path_placeholders
    create = next(s for s in REGISTRY.specs if s.name == "create_asset_type")
    assert set(create.field_map.values()) == set(spec_index.op("POST /v1/assets/types").body["fields"]) == {"Name"}
    update = next(s for s in REGISTRY.specs if s.name == "update_asset_type")
    assert set(update.field_map.values()) - {"assetTypeId"} == set(spec_index.op("PATCH /v1/assets/types/{assetTypeId}").body["fields"])
    delete = next(s for s in REGISTRY.specs if s.name == "delete_asset_type")
    assert set(delete.field_map.values()) == set(spec_index.op("DELETE /v1/assets/types/{assetTypeId}").path_placeholders)


async def test_the_new_writes_expose_exactly_the_documented_parameters(full):
    tools = {t.name: t for t in await list_tools(full)}
    expected = {
        "update_agent": (
            ["agent_id", "description", "hardware_type_id", "contact_id", "warranty_start_date", "warranty_end_date", "clear_fields"],
            ["agent_id"],
        ),
        "create_asset_type": (["name"], ["name"]),
        "update_asset_type": (["asset_type_id", "name"], ["asset_type_id", "name"]),
        "delete_asset_type": (["asset_type_id", "confirm"], ["asset_type_id"]),
    }
    for name, (params, required) in expected.items():
        tool = tools[name]
        assert list(tool.inputSchema["properties"]) == params, name
        assert tool.inputSchema.get("required", []) == required, name
        assert all(p.get("description") for p in tool.inputSchema["properties"].values()), name
        assert all(len(p["description"]) <= 220 for p in tool.inputSchema["properties"].values()), name
        assert len(tool.description) <= 900, name
        assert tool.annotations.readOnlyHint is False, name
        assert tool.annotations.destructiveHint is NEW_WRITE_TOOLS[name][2], name
    assert tools["update_agent"].inputSchema["properties"]["clear_fields"]["items"]["enum"] == [
        "description", "contact_id", "warranty_start_date", "warranty_end_date"
    ]
    assert "list_asset_types" in tools["update_agent"].inputSchema["properties"]["hardware_type_id"]["description"]
    confirm = tools["delete_asset_type"].inputSchema["properties"]["confirm"]
    assert confirm["default"] is False and confirm["type"] == "boolean"


async def test_the_gated_tool_exists_only_when_deletes_are_enabled(server_factory):
    plain = {t.name for t in await list_tools(server_factory(toolsets={"core"}))}
    gated = {t.name for t in await list_tools(server_factory(toolsets={"core"}, destructive=True))}
    assert "delete_asset_type" not in plain and "delete_asset_type" in gated
    assert {"update_agent", "create_asset_type", "update_asset_type"} <= plain


def test_the_new_write_texts_say_what_the_user_needs_to_know():
    agent = flat(assets_module.update_agent.__doc__)
    for fragment in (
        "read back by id", "clear_fields empties description, contact_id and the two warranty dates",
        "hardware_type_id (an asset type) cannot be cleared", "filled in again by Gorelo's warranty lookup", "a date set here is kept",
        "active contact of the agent's client", "agent awaiting adoption (no client) takes none", "Custom fields cannot be written",
        "hardware_type_id -> list_asset_types", "Side effects:", "cannot delete or uninstall agents",
    ):
        assert fragment in agent, fragment
    create = flat(assets_module.create_asset_type.__doc__)
    assert "409" in create and "names the Id of the existing type" in create and "Side effects:" in create
    update = flat(assets_module.update_asset_type.__doc__)
    assert "built-in (default) type cannot be renamed (409)" in update and "400 on name" in update and "no-op" in update
    delete = flat(assets_module.delete_asset_type.__doc__)
    for fragment in ("409", "default types can never be deleted", "deleting it again is a 404", "Ask the user first; needs confirm=true."):
        assert fragment in delete, fragment
    for text in (agent, create, update, delete):
        assert chr(0x2014) not in text and chr(0x2013) not in text


# -- update_agent -------------------------------------------------------------------------------------------------------

AGENT = uid(41)
AGENT_PATH = f"{AGENTS}/{AGENT}"


def update_agent_args(**extra):
    return {"agent_id": AGENT, **extra}


async def test_update_agent_sends_only_the_changed_fields_then_reads_the_agent_back(server, mock_gorelo):
    row = agent_record(41, Id=AGENT, Description="Front desk PC")
    mock_gorelo.on("PATCH", AGENT_PATH, envelope({"Id": AGENT}))
    mock_gorelo.on("GET", AGENT_PATH, envelope(row))
    result = await call_tool(server, "update_agent", update_agent_args(description="Front desk PC"))
    patch, get = mock_gorelo.requests
    assert (patch.method, patch.path, patch.query, patch.json) == ("PATCH", AGENT_PATH, {}, {"Description": "Front desk PC"})
    assert (get.method, get.path, get.query, get.content) == ("GET", AGENT_PATH, {}, b"")
    assert result == row and len(mock_gorelo.requests) == 2


async def test_update_agent_sends_every_field_under_its_pascal_case_name(server, mock_gorelo):
    mock_gorelo.on("PATCH", AGENT_PATH, envelope({"Id": AGENT}))
    mock_gorelo.on("GET", AGENT_PATH, envelope(agent_record(41, Id=AGENT)))
    await call_tool(
        server,
        "update_agent",
        update_agent_args(
            description="D", hardware_type_id=118, contact_id=7962, warranty_start_date="2024-03-31", warranty_end_date="2027-03-31"
        ),
    )
    assert mock_gorelo.requests[0].json == {
        "Description": "D", "HardwareTypeId": 118, "ContactId": 7962, "WarrantyStartDate": "2024-03-31", "WarrantyEndDate": "2027-03-31"
    }


async def test_update_agent_clear_fields_are_sent_as_json_null(server, mock_gorelo):
    mock_gorelo.on("PATCH", AGENT_PATH, envelope({"Id": AGENT}))
    mock_gorelo.on("GET", AGENT_PATH, envelope(agent_record(41, Id=AGENT)))
    await call_tool(
        server, "update_agent", update_agent_args(clear_fields=["description", "contact_id", "warranty_start_date", "warranty_end_date", "contact_id"])
    )
    patch = mock_gorelo.requests[0]
    assert patch.json == {"Description": None, "ContactId": None, "WarrantyStartDate": None, "WarrantyEndDate": None}
    assert b'"Description":null' in patch.content.replace(b" ", b"")


async def test_update_agent_can_set_one_field_and_clear_another(server, mock_gorelo):
    mock_gorelo.on("PATCH", AGENT_PATH, envelope({"Id": AGENT}))
    mock_gorelo.on("GET", AGENT_PATH, envelope(agent_record(41, Id=AGENT)))
    await call_tool(server, "update_agent", update_agent_args(hardware_type_id=5, clear_fields=["warranty_end_date"]))
    assert mock_gorelo.requests[0].json == {"HardwareTypeId": 5, "WarrantyEndDate": None}


async def test_update_agent_normalizes_an_upper_case_id(server, mock_gorelo):
    row = agent_record(41, Id=AGENT)
    mock_gorelo.on("PATCH", AGENT_PATH, envelope({"Id": AGENT.upper()}))
    mock_gorelo.on("GET", AGENT_PATH, envelope(row))
    assert await call_tool(server, "update_agent", update_agent_args(agent_id=AGENT.upper(), contact_id=3)) == row
    assert [r.path for r in mock_gorelo.requests] == [AGENT_PATH, AGENT_PATH]


def unusable_agent_read_back(found):
    return {
        "Id": AGENT,
        "warning": f"the write succeeded; re-reading it returned no usable record of this agent ({found}). Do not repeat the write; check with get_agent.",
    }


async def test_update_agent_returns_a_warning_when_the_read_back_fails(server, mock_gorelo):
    mock_gorelo.on("PATCH", AGENT_PATH, envelope({"Id": AGENT}))
    mock_gorelo.on("GET", AGENT_PATH, error_envelope(500, [("070500", "Boom.")]))
    result = await call_tool(server, "update_agent", update_agent_args(contact_id=3))
    assert set(result) == {"Id", "warning"} and result["Id"] == AGENT
    assert "the write succeeded" in result["warning"] and "HTTP 500" in result["warning"] and "Do not repeat the write" in result["warning"]


async def test_update_agent_returns_a_warning_when_the_read_back_answers_success_with_no_record(server, mock_gorelo):
    # for an agent id it does not know, Gorelo answers HTTP 200 with Data null on this GET (get_agent handles that too)
    mock_gorelo.on("PATCH", AGENT_PATH, envelope({"Id": AGENT}))
    mock_gorelo.on("GET", AGENT_PATH, envelope(None))
    result = await call_tool(server, "update_agent", update_agent_args(contact_id=3))
    assert set(result) == {"Id", "warning"} and "Data is null" in result["warning"] and "Do not repeat the write" in result["warning"]


@pytest.mark.parametrize(
    "answer, found",
    [
        (envelope({}), "an empty object"),
        (envelope(agent_record(42)), f"the record of agent {uid(42)}"),
        (envelope({"Name": "x"}), "a record without an Id"),
    ],
)
async def test_update_agent_returns_a_warning_when_the_read_back_is_not_this_agent(server, mock_gorelo, answer, found):
    mock_gorelo.on("PATCH", AGENT_PATH, envelope({"Id": AGENT}))
    mock_gorelo.on("GET", AGENT_PATH, answer)
    assert await call_tool(server, "update_agent", update_agent_args(contact_id=3)) == unusable_agent_read_back(found)
    assert [r.method for r in mock_gorelo.requests] == ["PATCH", "GET"]


@pytest.mark.parametrize("answer", [envelope(None), envelope({}), envelope({"Id": 0}), envelope([AGENT])])
async def test_update_agent_without_a_usable_id_in_the_answer_is_an_unconfirmed_write_and_reads_nothing_back(server, mock_gorelo, answer):
    mock_gorelo.on("PATCH", AGENT_PATH, answer)
    text = await call_tool_error(server, "update_agent", update_agent_args(contact_id=3))
    assert "update_agent" in text and "verify it with a read before repeating it" in text
    assert [r.method for r in mock_gorelo.requests] == ["PATCH"]


async def test_update_agent_answering_another_id_is_an_unconfirmed_write_naming_both(server, mock_gorelo):
    mock_gorelo.on("PATCH", AGENT_PATH, envelope({"Id": uid(42)}))
    text = await call_tool_error(server, "update_agent", update_agent_args(contact_id=3))
    assert text == (
        "Gorelo returned an unexpected response for update_agent: PATCH /v1/assets/agents/{agentAssetId}: "
        f"Gorelo reported success for agent {AGENT} but answered with Id {uid(42)}; check both with get_agent. "
        "The change may or may not have been applied. Verify with a read before retrying."
    )
    assert [r.method for r in mock_gorelo.requests] == ["PATCH"]


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({}, "nothing to change: give at least one of description, hardware_type_id, contact_id, warranty_start_date, warranty_end_date or clear_fields"),
        ({"agent_id": "abc", "contact_id": 3}, "agent_id"),
        ({"description": " "}, "description: must not be empty or whitespace only"),
        ({"description": "x" * 2001}, "description: at most 2000 characters, got 2001"),
        ({"warranty_end_date": "2027-03-31T00:00:00Z"}, "warranty_end_date: expected a calendar date written YYYY-MM-DD"),
        ({"warranty_start_date": "2027-02-30"}, "warranty_start_date: 2027-02-30 is not a real calendar date"),
        ({"contact_id": 0}, "contact_id"),
        ({"hardware_type_id": -4}, "hardware_type_id"),
        ({"clear_fields": []}, "clear_fields"),
        ({"clear_fields": ["hardware_type_id"]}, "clear_fields"),
        ({"clear_fields": ["name"]}, "clear_fields"),
        ({"description": "D", "clear_fields": ["description"]}, "description: cannot be given a value and listed in clear_fields in the same call"),
        ({"warranty_end_date": "2027-03-31", "clear_fields": ["warranty_end_date"]}, "warranty_end_date: cannot be given a value and listed in clear_fields"),
    ],
)
async def test_update_agent_refuses_bad_input_locally_naming_the_parameter(server, mock_gorelo, arguments, fragment):
    text = await call_tool_error(server, "update_agent", {"agent_id": AGENT, **arguments} if "agent_id" not in arguments else arguments)
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", [True, "5", 5.5])
async def test_update_agent_ids_are_strict(server, mock_gorelo, bad):
    for param in ("contact_id", "hardware_type_id"):
        assert param in await call_tool_error(server, "update_agent", update_agent_args(**{param: bad}))
    assert mock_gorelo.requests == []


async def test_update_agent_maps_gorelo_errors_to_the_parameters(server, mock_gorelo):
    mock_gorelo.on(
        "PATCH",
        AGENT_PATH,
        error_envelope(
            400,
            [
                ("070101", "The contact must be an active contact of the agent's client.", "ContactId"),
                ("070101", "Unknown asset type.", "HardwareTypeId"),
                ("070101", "Date must not carry a time.", "WarrantyEndDate"),
            ],
        ),
    )
    text = await call_tool_error(server, "update_agent", update_agent_args(contact_id=3, hardware_type_id=9, warranty_end_date="2027-03-31"))
    assert "Gorelo rejected update_agent (HTTP 400" in text
    assert "contact_id: The contact must be an active contact of the agent's client." in text
    assert "hardware_type_id: Unknown asset type." in text and "warranty_end_date: Date must not carry a time." in text
    assert len(mock_gorelo.requests) == 1


async def test_update_agent_maps_an_unknown_agent_404_to_the_tool(server, mock_gorelo):
    mock_gorelo.on("PATCH", AGENT_PATH, error_envelope(404, [("070202", "The requested resource was not found.")]))
    text = await call_tool_error(server, "update_agent", update_agent_args(contact_id=3))
    assert "update_agent" in text and "HTTP 404" in text
    assert len(mock_gorelo.requests) == 1


async def test_update_agent_timeout_is_unconfirmed_and_is_not_sent_again(server, mock_gorelo):
    mock_gorelo.on("PATCH", AGENT_PATH, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "update_agent", update_agent_args(contact_id=3))
    assert "did not confirm update_agent" in text and "may or may not have been applied" in text
    assert len(mock_gorelo.requests) == 1


# -- asset type writes: read-back helpers -------------------------------------------------------------------------------


def type_row(type_id, name):
    return {"Id": type_id, "Name": name}


# -- create_asset_type --------------------------------------------------------------------------------------------------


async def test_create_asset_type_posts_the_name_then_returns_its_row_from_the_list(server, mock_gorelo):
    rows = [type_row(1, "Desktop"), type_row(118, "Firewall")]
    mock_gorelo.on("POST", TYPES, envelope({"Id": 118}))
    mock_gorelo.on("GET", TYPES, envelope(rows))
    result = await call_tool(server, "create_asset_type", {"name": "Firewall"})
    post, get = mock_gorelo.requests
    assert (post.method, post.path, post.query, post.json) == ("POST", TYPES, {}, {"Name": "Firewall"})
    assert (get.method, get.path, get.query, get.content) == ("GET", TYPES, {}, b"")
    assert result == {"Id": 118, "Name": "Firewall"} and len(mock_gorelo.requests) == 2


async def test_create_asset_type_warns_when_the_list_cannot_be_read(server, mock_gorelo):
    mock_gorelo.on("POST", TYPES, envelope({"Id": 118}))
    mock_gorelo.on("GET", TYPES, error_envelope(500, [("070500", "Boom.")]))
    result = await call_tool(server, "create_asset_type", {"name": "Firewall"})
    assert set(result) == {"Id", "warning"} and result["Id"] == 118
    assert result["warning"] == (
        "the write succeeded; re-reading it failed: GET /v1/assets/types answered HTTP 500. "
        "Do not repeat the write; check with list_asset_types."
    )


async def test_create_asset_type_warns_when_the_list_does_not_show_the_new_type(server, mock_gorelo):
    mock_gorelo.on("POST", TYPES, envelope({"Id": 118}))
    mock_gorelo.on("GET", TYPES, envelope([type_row(1, "Desktop")]))
    result = await call_tool(server, "create_asset_type", {"name": "Firewall"})
    assert result == {
        "Id": 118,
        "warning": "the write succeeded; the list of asset types does not show type 118. Do not repeat the write; check with list_asset_types.",
    }


async def test_create_asset_type_warns_when_the_read_back_times_out(server, mock_gorelo):
    mock_gorelo.on("POST", TYPES, envelope({"Id": 118}))
    mock_gorelo.on("GET", TYPES, httpx.ReadTimeout("slow"))
    result = await call_tool(server, "create_asset_type", {"name": "Firewall"})
    assert set(result) == {"Id", "warning"} and "GET /v1/assets/types timed out" in result["warning"]


@pytest.mark.parametrize("answer", [envelope(None), envelope({}), envelope({"Id": 0}), envelope({"Id": True})])
async def test_create_asset_type_without_a_usable_id_is_an_unconfirmed_write(server, mock_gorelo, answer):
    mock_gorelo.on("POST", TYPES, answer)
    text = await call_tool_error(server, "create_asset_type", {"name": "Firewall"})
    assert "create_asset_type" in text and "verify it with a read before repeating it" in text
    assert [r.method for r in mock_gorelo.requests] == ["POST"]


async def test_a_name_that_exists_is_a_409_and_the_error_names_the_existing_type(server, mock_gorelo):
    mock_gorelo.on(
        "POST",
        TYPES,
        {
            **error_envelope(409, [("ResourceAlreadyExists", "An asset type named 'Firewall' already exists.", "Name")]),
            "DataContext": {"TraceId": TEST_TRACE_ID, "ExistingId": 118},
        },
    )
    mock_gorelo.on("GET", TYPES, envelope([type_row(1, "Desktop"), type_row(118, "firewall")]))
    text = await call_tool_error(server, "create_asset_type", {"name": "Firewall"})
    assert text == (
        "Gorelo rejected create_asset_type (HTTP 409, code ResourceAlreadyExists): name: An asset type named 'Firewall' already "
        "exists. The asset type with this name already exists: Id 118 (firewall); use that id as hardware_type_id, or choose "
        f"another name. [trace {TEST_TRACE_ID}]"
    )
    assert [r.method for r in mock_gorelo.requests] == ["POST", "GET"]


async def test_a_409_whose_name_is_not_listed_still_points_at_the_list(server, mock_gorelo):
    mock_gorelo.on("POST", TYPES, error_envelope(409, [("ResourceAlreadyExists", "Already exists.", "Name")]))
    mock_gorelo.on("GET", TYPES, envelope([type_row(1, "Desktop")]))
    text = await call_tool_error(server, "create_asset_type", {"name": "Firewall"})
    assert "Already exists. No listed asset type has exactly this name; look for it with list_asset_types." in text
    assert text.endswith(f"[trace {TEST_TRACE_ID}]")


async def test_a_409_whose_list_read_fails_keeps_gorelos_text_and_points_at_the_list(server, mock_gorelo):
    mock_gorelo.on("POST", TYPES, error_envelope(409, [("ResourceAlreadyExists", "Already exists.", "Name")]))
    mock_gorelo.on("GET", TYPES, error_envelope(500, [("070500", "Boom.")]))
    text = await call_tool_error(server, "create_asset_type", {"name": "Firewall"})
    assert "HTTP 409" in text and "Already exists." in text and "A type with this name exists already; find its Id with list_asset_types." in text


async def test_a_400_is_not_given_the_existing_type_lookup(server, mock_gorelo):
    mock_gorelo.on("POST", TYPES, error_envelope(400, [("070101", "Name is too long.", "Name")]))
    text = await call_tool_error(server, "create_asset_type", {"name": "Firewall"})
    assert "name: Name is too long." in text and "HTTP 400" in text
    assert [r.method for r in mock_gorelo.requests] == ["POST"]


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"name": ""}, "name: must not be empty or whitespace only"),
        ({"name": "   "}, "name: must not be empty or whitespace only"),
        ({"name": "x" * 101}, "name: at most 100 characters, got 101"),
        ({"name": 5}, "name"),
        ({}, "name"),
    ],
)
async def test_create_asset_type_refuses_bad_input_before_any_request(server, mock_gorelo, arguments, fragment):
    assert fragment in await call_tool_error(server, "create_asset_type", arguments)
    assert mock_gorelo.requests == []


async def test_create_asset_type_accepts_a_name_of_exactly_one_hundred_characters(server, mock_gorelo):
    name = "n" * 100
    mock_gorelo.on("POST", TYPES, envelope({"Id": 7}))
    mock_gorelo.on("GET", TYPES, envelope([type_row(7, name)]))
    assert await call_tool(server, "create_asset_type", {"name": name}) == type_row(7, name)


async def test_create_asset_type_timeout_is_unconfirmed_and_is_not_sent_again(server, mock_gorelo):
    mock_gorelo.on("POST", TYPES, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "create_asset_type", {"name": "Firewall"})
    assert "did not confirm create_asset_type" in text and "may or may not have been applied" in text
    assert [r.method for r in mock_gorelo.requests] == ["POST"]


# -- update_asset_type --------------------------------------------------------------------------------------------------


async def test_update_asset_type_patches_the_name_then_returns_its_row_from_the_list(server, mock_gorelo):
    mock_gorelo.on("PATCH", f"{TYPES}/118", envelope({"Id": 118}))
    mock_gorelo.on("GET", TYPES, envelope([type_row(1, "Desktop"), type_row(118, "Firewall appliance")]))
    result = await call_tool(server, "update_asset_type", {"asset_type_id": 118, "name": "Firewall appliance"})
    patch, get = mock_gorelo.requests
    assert (patch.method, patch.path, patch.query, patch.json) == ("PATCH", f"{TYPES}/118", {}, {"Name": "Firewall appliance"})
    assert (get.method, get.path) == ("GET", TYPES)
    assert result == type_row(118, "Firewall appliance")


async def test_update_asset_type_warns_when_the_list_cannot_show_the_type(server, mock_gorelo):
    mock_gorelo.on("PATCH", f"{TYPES}/118", envelope({"Id": 118}))
    mock_gorelo.on("GET", TYPES, envelope([]))
    result = await call_tool(server, "update_asset_type", {"asset_type_id": 118, "name": "X"})
    assert set(result) == {"Id", "warning"} and "does not show type 118" in result["warning"]


async def test_update_asset_type_answering_another_id_is_an_unconfirmed_write(server, mock_gorelo):
    mock_gorelo.on("PATCH", f"{TYPES}/118", envelope({"Id": 119}))
    text = await call_tool_error(server, "update_asset_type", {"asset_type_id": 118, "name": "X"})
    assert "answered with Id 119" in text and "asset type 118" in text and "update_asset_type" in text
    assert [r.method for r in mock_gorelo.requests] == ["PATCH"]


async def test_update_asset_type_without_an_id_in_the_answer_is_an_unconfirmed_write(server, mock_gorelo):
    mock_gorelo.on("PATCH", f"{TYPES}/118", envelope(None))
    text = await call_tool_error(server, "update_asset_type", {"asset_type_id": 118, "name": "X"})
    assert "verify it with a read before repeating it" in text
    assert [r.method for r in mock_gorelo.requests] == ["PATCH"]


async def test_a_built_in_type_is_a_409_shown_as_gorelo_wrote_it(server, mock_gorelo):
    mock_gorelo.on("PATCH", f"{TYPES}/1", error_envelope(409, [("ResourceUpdateBlocked", "A built-in asset type cannot be renamed.")]))
    text = await call_tool_error(server, "update_asset_type", {"asset_type_id": 1, "name": "X"})
    assert "Gorelo rejected update_asset_type (HTTP 409, code ResourceUpdateBlocked)" in text and "built-in" in text
    assert len(mock_gorelo.requests) == 1


async def test_a_name_used_by_another_type_is_a_400_on_name(server, mock_gorelo):
    mock_gorelo.on("PATCH", f"{TYPES}/118", error_envelope(400, [("070101", "Name is already used.", "Name")]))
    text = await call_tool_error(server, "update_asset_type", {"asset_type_id": 118, "name": "Desktop"})
    assert "name: Name is already used." in text


async def test_an_unknown_type_is_a_404_naming_the_tool(server, mock_gorelo):
    mock_gorelo.on("PATCH", f"{TYPES}/999", error_envelope(404, [("070202", "The requested resource was not found.")]))
    text = await call_tool_error(server, "update_asset_type", {"asset_type_id": 999, "name": "X"})
    assert "update_asset_type" in text and "HTTP 404" in text


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"asset_type_id": 118, "name": ""}, "name: must not be empty or whitespace only"),
        ({"asset_type_id": 118, "name": "x" * 101}, "name: at most 100 characters, got 101"),
        ({"asset_type_id": 0, "name": "X"}, "asset_type_id"),
        ({"asset_type_id": -1, "name": "X"}, "asset_type_id"),
        ({"asset_type_id": True, "name": "X"}, "asset_type_id"),
        ({"asset_type_id": "5", "name": "X"}, "asset_type_id"),
        ({"asset_type_id": 118}, "name"),
        ({"name": "X"}, "asset_type_id"),
    ],
)
async def test_update_asset_type_refuses_bad_input_before_any_request(server, mock_gorelo, arguments, fragment):
    assert fragment in await call_tool_error(server, "update_asset_type", arguments)
    assert mock_gorelo.requests == []


# -- delete_asset_type (gated) ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("confirm", [None, False])
async def test_delete_asset_type_without_confirm_is_refused_with_zero_http(full, mock_gorelo, confirm):
    arguments = {"asset_type_id": 118} if confirm is None else {"asset_type_id": 118, "confirm": confirm}
    text = await call_tool_error(full, "delete_asset_type", arguments)
    assert "confirm: refusing to delete asset type 118 without confirm=true." in text
    assert "removed from the account's asset types" in text and "never deletes a default type" in text
    assert "Nothing has been sent to Gorelo." in text and "Call again with confirm=true" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", ["true", 1, "yes", [True]])
async def test_delete_asset_type_confirm_is_strict(full, mock_gorelo, bad):
    assert "confirm" in await call_tool_error(full, "delete_asset_type", {"asset_type_id": 118, "confirm": bad})
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", [0, -3, True, "5", 1.5])
async def test_delete_asset_type_refuses_a_bad_id_before_any_request(full, mock_gorelo, bad):
    assert "asset_type_id" in await call_tool_error(full, "delete_asset_type", {"asset_type_id": bad, "confirm": True})
    assert mock_gorelo.requests == []


async def test_delete_asset_type_with_confirm_sends_one_delete_and_returns_the_id(full, mock_gorelo):
    mock_gorelo.on("DELETE", f"{TYPES}/118", envelope({"Id": 118}))
    result = await call_tool(full, "delete_asset_type", {"asset_type_id": 118, "confirm": True})
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("DELETE", f"{TYPES}/118", {}, b"")
    assert result == {"Id": 118} and len(mock_gorelo.requests) == 1


@pytest.mark.parametrize("answer", [envelope(None), envelope({}), envelope({"Id": 5}), envelope({"Id": True})])
async def test_delete_asset_type_answer_that_does_not_carry_the_id_is_an_unconfirmed_write(full, mock_gorelo, answer):
    mock_gorelo.on("DELETE", f"{TYPES}/118", answer)
    text = await call_tool_error(full, "delete_asset_type", {"asset_type_id": 118, "confirm": True})
    assert "delete_asset_type" in text and "DELETE /v1/assets/types/{assetTypeId}" in text
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "status, code, message",
    [
        (409, "ResourceDeleteBlocked", "3 agents and 2 custom assets still have this type."),
        (409, "ResourceNotDeletable", "A default asset type cannot be deleted."),
        (404, "070202", "The requested resource was not found."),
    ],
)
async def test_delete_asset_type_shows_gorelos_refusals(full, mock_gorelo, status, code, message):
    mock_gorelo.on("DELETE", f"{TYPES}/118", error_envelope(status, [(code, message)]))
    text = await call_tool_error(full, "delete_asset_type", {"asset_type_id": 118, "confirm": True})
    assert f"Gorelo rejected delete_asset_type (HTTP {status}, code {code}): {message}" in text
    assert len(mock_gorelo.requests) == 1


async def test_delete_asset_type_timeout_is_unconfirmed_and_is_not_sent_again(full, mock_gorelo):
    mock_gorelo.on("DELETE", f"{TYPES}/118", httpx.ReadTimeout("slow"))
    text = await call_tool_error(full, "delete_asset_type", {"asset_type_id": 118, "confirm": True})
    assert "did not confirm delete_asset_type" in text and "Verify with a read before retrying." in text
    assert len(mock_gorelo.requests) == 1


def test_no_other_tool_text_in_this_module_names_the_gated_delete():
    for name, spec in ((s.name, s) for s in REGISTRY.specs if s.fn.__module__ == assets_module.__name__):
        if name != "delete_asset_type":
            assert "delete_asset_type" not in flat(spec.fn.__doc__ or ""), name
