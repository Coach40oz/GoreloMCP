"""tools/clients.py: list_clients, get_client, create_client, update_client, list_client_locations."""

import copy
import json
import pickle
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
    list_tools,
    in_order,
    make_ctx,
    paged_envelope,
    paged_responder,
    pagination,
)
from fastmcp.exceptions import ToolError

import tools._common as common_module
import tools.clients as clients_module
from gorelo_client import FORBIDDEN_OPS, GoreloAPIError
from tools._common import REGISTRY, build_body

pytestmark = pytest.mark.anyio

CLIENTS = "/v1/clients"
CLIENT_9101 = "/v1/clients/9101"  # update_client: the id is in the path only (the command has no Id)
LOCATIONS = "/v1/clients/9101/locations"

TOOLS = {
    # name: (kind, ops, destructive_hint, parameters in order, required parameters)
    "list_clients": (
        "read", ["GET /v1/clients"], False,
        ["query", "status_ids", "created_since", "created_before", "updated_since", "updated_before", "page_size", "cursor"],
        [],
    ),
    "get_client": ("read", ["GET /v1/clients/{clientId}"], False, ["client_id"], ["client_id"]),
    "create_client": (
        "write", ["POST /v1/clients"], False,
        [
            "name", "location_name", "billing_name", "alternate_name", "domain", "location_phone_country_code",
            "location_phone", "location_phone_ext", "location_address1", "location_address2", "location_city",
            "location_state", "location_country", "location_postal_code", "location_time_zone",
        ],
        ["name", "location_name"],
    ),
    "update_client": (
        "write", ["GET /v1/clients/{clientId}", "PATCH /v1/clients/{clientId}"], True,
        ["client_id", "name", "status_id", "billing_name", "alternate_name", "custom_properties", "clear_custom_property_ids"],
        ["client_id"],
    ),
    "list_client_locations": (
        "read", ["GET /v1/clients/{clientId}/locations"], False, ["client_id"], ["client_id"]
    ),
}


@pytest.fixture
def server(server_factory):
    return server_factory(toolsets={"core"})


def client_record(client_id=9101, name="Example Co", **extra):
    record = {
        "Id": client_id,
        "Name": name,
        "BillingName": "",
        "AlternateName": "",
        "Status": {"Id": 1, "Name": "Active"},
        "Domains": [],
        "CreatedOn": "2026-01-01T00:00:00Z",
        "UpdatedOn": None,
        "IsDefault": None,
    }
    record.update(extra)
    return record


def location_record(location_id=9001, client_id=9101, **extra):
    record = {
        "Id": location_id,
        "ClientId": client_id,
        "Name": "Head Office",
        "Address1": "1 Main St",
        "City": "Springfield",
        "PhoneCountryCode": "US",
        "Phone": "5555550142",
        "IsDefault": True,
        "IsDefaultBilling": True,
    }
    record.update(extra)
    return record


def custom_property(field_id, *, is_set=True, value="x", **extra):
    """One CustomPropertyValue of the single client record (the list rows carry none): Id, Variable, Name, Type, Value, IsSet."""
    row = {
        "Id": field_id,
        "Variable": f"field_{field_id}",
        "Name": f"Field {field_id}",
        "Type": {"Id": 1, "Name": "Text"},
        "Value": value,
        "IsSet": is_set,
    }
    row.update(extra)
    return row


def client_with_custom_properties(saved=(), cleared=(), client_id=9101, **extra):
    """The client record Gorelo answers an update with: a CustomProperties row per field, IsSet true for the ids in
    `saved` and false for the ids in `cleared` (a cleared field shows its default, or null)."""
    rows = [custom_property(field_id) for field_id in saved]
    rows += [custom_property(field_id, is_set=False, value=None) for field_id in cleared]
    return client_record(client_id, CustomProperties=rows, **extra)


def trace(text):
    return f"{text} [trace {TEST_TRACE_ID}]"


# --------------------------------------------------------------------------
# Declarations
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_each_tool_is_declared_as_documented(name, spec_index):
    kind, ops, destructive, _params, _required = TOOLS[name]
    spec = next(s for s in REGISTRY.specs if s.name == name)
    assert (spec.toolset, spec.kind, spec.ops, spec.destructive_hint) == ("core", kind, ops, destructive)
    assert all(op in spec_index.ops for op in spec.ops)
    assert not set(spec.ops) & FORBIDDEN_OPS


@pytest.mark.parametrize("name", sorted(TOOLS))
async def test_each_tool_exposes_exactly_the_documented_parameters(name, server):
    _kind, _ops, _destructive, params, required = TOOLS[name]
    tool = next(t for t in await list_tools(server) if t.name == name)
    assert list(tool.inputSchema["properties"]) == params
    assert tool.inputSchema.get("required", []) == required
    assert all(p.get("description") for p in tool.inputSchema["properties"].values())


@pytest.mark.parametrize("name", sorted(TOOLS))
async def test_annotations_follow_the_kind_and_the_overwrite_rule(name, server):
    kind, _ops, destructive, _params, _required = TOOLS[name]
    tool = next(t for t in await list_tools(server) if t.name == name)
    assert tool.annotations.readOnlyHint is (kind == "read")
    assert tool.annotations.destructiveHint is destructive
    assert tool.annotations.idempotentHint is (kind == "read")


def has_body_field(spec_index, op, path):
    fields = op.body["fields"]
    parts = path.split(".")
    for position, part in enumerate(parts):
        entry = fields.get(part)
        if entry is None:
            return False
        if position < len(parts) - 1:
            fields = spec_index.schema(entry["ref"])["fields"]
    return True


@pytest.mark.parametrize("name", ["create_client", "update_client"])
def test_every_field_map_path_is_a_body_field_of_the_op(name, spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == name)
    op = spec_index.op(next(declared for declared in spec.ops if not declared.startswith("GET ")))  # the write, not the read
    assert spec.field_map
    for param, path in spec.field_map.items():
        if param == "client_id":  # update_client: the path placeholder, kept for error naming; never a body field
            assert name == "update_client" and path in op.path_placeholders
            assert not has_body_field(spec_index, op, path) and "Id" not in op.body["fields"]
            continue
        assert has_body_field(spec_index, op, path), f"{name}: {param} -> {path} is not in {op.key}"


@pytest.mark.parametrize("name", ["get_client", "list_client_locations"])
def test_the_field_map_of_an_id_tool_names_the_path_placeholder(name, spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == name)
    op = spec_index.op(spec.ops[0])
    assert list(spec.field_map) == ["client_id"]
    assert set(spec.field_map.values()) == set(op.path_placeholders)


@pytest.mark.parametrize("name", ["list_clients"])
def test_every_query_name_of_a_list_tool_is_a_query_param_of_the_op(name, spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == name)
    op = spec_index.op(spec.ops[0])
    allowed = {q.lower() for q in op.query_params}
    assert {path.lower() for path in spec.field_map.values()} == allowed


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_docstrings_follow_the_template(name):
    spec = next(s for s in REGISTRY.specs if s.name == name)
    doc = spec.fn.__doc__
    assert (TOOLS[name][0] == "write") == ("Side effects:" in doc)  # only writes have side effects to state
    assert ("Paging:" in doc) == (name == "list_clients")


@pytest.mark.parametrize("name", ["get_client", "update_client", "list_client_locations"])
async def test_the_client_id_description_names_the_tool_that_resolves_it(name, server):
    tool = next(t for t in await list_tools(server) if t.name == name)
    assert "list_clients" in tool.inputSchema["properties"]["client_id"]["description"]


def flat(text):
    return " ".join(text.split())


def test_the_single_client_is_the_list_row_plus_custom_properties_in_the_spec(spec_index):
    """The module text says the single record (ClientResponse) is the list row (ClientListItemResponse) PLUS
    CustomProperties, which the list leaves out on purpose (contract 1b1c0cec0e9a; before it the two were the same
    fields). If Gorelo changes either shape, this fails and the get_client and list_clients texts have to follow."""
    assert spec_index.op("GET /v1/clients").response["data"] == "[ClientListItemResponse]"
    assert spec_index.op("GET /v1/clients/{clientId}").response["data"] == "ClientResponse"
    assert spec_index.op("PATCH /v1/clients/{clientId}").response["data"] == "ClientResponse"
    assert spec_index.op("POST /v1/clients").response["data"] == "ClientResponse"
    row, record = spec_index.schema("ClientListItemResponse")["fields"], spec_index.schema("ClientResponse")["fields"]
    assert {"Id", "Name", "AlternateName", "BillingName", "Status", "Domains"} <= set(row)
    assert "CustomProperties" not in row
    assert set(record) == set(row) | {"CustomProperties"}
    assert {name: record[name] for name in row} == row  # every list field is the same field in the single record
    custom = record["CustomProperties"]
    assert (custom["type"], custom["items"]["ref"], custom["nullable"]) == ("array", "CustomPropertyValue", False)
    value = spec_index.schema("CustomPropertyValue")
    assert set(value["fields"]) == {"Id", "Variable", "Name", "Type", "Value", "IsSet"} and not value["additional_properties"]
    assert value["fields"]["Value"]["nullable"] is True and value["fields"]["IsSet"]["type"] == "boolean"
    doc = flat(clients_module.__doc__)
    assert "ClientListItemResponse" in doc and "the same fields as the single record" not in doc
    assert "is that list shape PLUS CustomProperties, which the list deliberately leaves out" in doc
    assert "CustomPropertyValue {Id, Variable, Name, Type, Value, IsSet}" in doc


def test_the_docstrings_carry_the_documented_facts():
    listing = flat(clients_module.list_clients.__doc__)
    # contract e15cb5a18ec2: "when omitted, inactive (2) clients are excluded, and when supplied only the listed statuses are returned"
    assert "Inactive clients are left out unless status_ids asks for the inactive status" in listing
    assert "never listed" not in listing
    assert "fit in one page" not in listing
    create = flat(clients_module.create_client.__doc__)
    assert "creates a real client" in create and "cannot delete clients" in create
    update = flat(clients_module.update_client.__doc__)
    assert "only custom fields can be cleared through the API" in update and "overwrites current values" in update
    assert "Custom fields: ids from get_client's CustomProperties, values are text whatever the Type" in update
    # a field that already held a value read IsSet true before the update, so the answer cannot prove an ignored one:
    # the model compares the returned Values itself (there is no read before the write)
    assert "Compare each returned CustomProperties Value with what you sent; tell the user if one differs (Gorelo may reformat)" in update
    assert "Update a client's name, status, billing name, alternate name or custom fields" in update
    # contract 1b1c0cec0e9a: the single record has the custom fields, the list rows leave them out
    assert "Rows leave out CustomProperties: get_client has them" in listing
    record = flat(clients_module.get_client.__doc__)
    assert "The record includes CustomProperties (each client custom field: Id, Name, Type, Value, IsSet" in record


LATEST_SPEC = Path(__file__).resolve().parent.parent / "backups" / "swagger-latest.json"


@pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")
def test_the_inactive_client_promise_is_the_one_in_the_spec_text():
    """spec/spec_index.json keeps no description text, so what list_clients promises about inactive clients is pinned
    to the StatusIds text of the full OpenAPI snapshot. On 2026-10-01 it said "Inactive clients are excluded either way";
    since contract e15cb5a18ec2 it says they are excluded only when StatusIds is omitted."""
    parameters = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))["paths"]["/v1/clients"]["get"]["parameters"]
    text = " ".join(next(p for p in parameters if p["name"] == "StatusIds")["description"].split())
    assert "when omitted, inactive (2) clients are excluded, and when supplied only the listed statuses are returned" in text
    assert "either way" not in text


async def test_the_status_ids_description_says_what_omitting_it_does(server):
    tool = next(t for t in await list_tools(server) if t.name == "list_clients")
    text = flat(tool.inputSchema["properties"]["status_ids"]["description"])
    assert "Omit it and inactive clients (status 2) are left out; give it and only those statuses are returned." in text
    assert "no lookup tool" in text


async def test_the_status_id_description_says_an_inactive_status_hides_the_client_like_a_delete(server):
    #
    tool = next(t for t in await list_tools(server) if t.name == "update_client")
    text = flat(tool.inputSchema["properties"]["status_id"]["description"])
    assert "An inactive status hides the client from default lists like a delete does" in text
    assert "its devices drop out of list_agents" in text
    assert "Status.Id" in text and "no lookup tool" in text


async def test_the_custom_property_parameters_are_advertised_with_the_shape_of_their_entries(server):
    tool = next(t for t in await list_tools(server) if t.name == "update_client")
    props = tool.inputSchema["properties"]
    entry = props["custom_properties"]["items"]
    assert props["custom_properties"]["type"] == "array" and entry["type"] == "object"
    assert entry["additionalProperties"] is False  # a key besides id and value is refused
    assert {name: definition["type"] for name, definition in entry["properties"].items()} == {
        "id": "integer", "value": "string",
    }
    # id is the only required key: a missing value must reach the tool, which says to use clear_custom_property_ids (a bare
    # schema error would not); the compacted schema shows no default and no null branch for it
    assert entry["required"] == ["id"] and entry["properties"]["value"] == {"type": "string"}
    assert props["clear_custom_property_ids"]["type"] == "array"
    assert props["clear_custom_property_ids"]["items"] == {"type": "integer"}
    assert tool.inputSchema["required"] == ["client_id"]  # both are optional
    assert not any("anyOf" in p or "default" in p for p in props.values())


async def test_the_custom_property_texts_say_where_ids_come_from_how_to_clear_and_what_a_required_field_does(server):
    tool = next(t for t in await list_tools(server) if t.name == "update_client")
    props = tool.inputSchema["properties"]
    saving = flat(props["custom_properties"]["description"])
    clearing = flat(props["clear_custom_property_ids"]["description"])
    assert "[{id, value}]" in saving and "non-blank text" in saving
    assert "To remove a value use clear_custom_property_ids" in saving
    assert "the saved value goes and the field shows its default again" in clearing
    assert "A required field cannot be cleared to nothing" in clearing
    # core tool texts stay short (tests/test_tools_meta.py caps a parameter at 160 and a tool at 700 characters)
    assert len(saving) <= 160 and len(clearing) <= 160 and len(flat(tool.description)) <= 700


def test_only_the_array_is_in_the_field_map_because_the_tool_relabels_its_own_custom_property_errors():
    # Gorelo counts the items of the one CustomProperties array (the saved entries first, then the cleared ones), so the
    # tool names the parameter and the custom field itself (see the relabelling tests below); the map only has the array
    spec = next(s for s in REGISTRY.specs if s.name == "update_client")
    assert spec.field_map["custom_properties"] == "CustomProperties"
    # build_body is given the whole array under custom_properties and never sees the ids of the other parameter
    assert clients_module.UPDATE_FIELD_MAP["custom_properties"] == "CustomProperties"
    assert "clear_custom_property_ids" not in clients_module.UPDATE_FIELD_MAP
    assert "clear_custom_property_ids" not in spec.field_map
    assert set(spec.field_map) - set(clients_module.UPDATE_FIELD_MAP) == {"client_id"}


def test_build_body_keeps_a_null_inside_the_custom_properties_list():
    # the cleared entries are {Id, Value: null}; build_body only judges the list itself, never what is inside it
    body = build_body(
        {"custom_properties": [{"Id": 5, "Value": "x"}, {"Id": 7, "Value": None}]}, clients_module.UPDATE_FIELD_MAP
    )
    assert body == {"CustomProperties": [{"Id": 5, "Value": "x"}, {"Id": 7, "Value": None}]}


def test_the_custom_property_entry_and_its_clear_are_the_ones_the_spec_text_describes(spec_index):
    update = spec_index.schema("CustomPropertyUpdate")
    assert set(update["fields"]) == {"Id", "Value"} and update["additional_properties"] is False
    assert update["fields"]["Id"]["nullable"] is False and update["fields"]["Value"]["nullable"] is True
    assert spec_index.schema("UpdateClientCommand")["additional_properties"] is False
    assert not spec_index.op("POST /v1/clients").body["fields"].get("CustomProperties")  # none on create


def raw_text(*path):
    node = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))
    for key in path:
        node = node[key]
    return " ".join(node.split())


@pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")
def test_the_custom_property_promises_are_the_ones_in_the_spec_text():
    """spec/spec_index.json keeps no description text, so what update_client and get_client rely on is pinned to the full
    OpenAPI snapshot (contract 1b1c0cec0e9a): a null Value clears, an empty string saves an empty value, IsSet says whether
    a value is saved, and the list leaves the custom properties out. If Gorelo rewords one, the tool texts are re-read."""
    schemas = ("components", "schemas")
    on_update = raw_text(*schemas, "UpdateClientCommand", "properties", "CustomProperties", "description")
    assert "an entry present updates that field's value, one absent from the array is left untouched" in on_update
    assert "omitting `CustomProperties` entirely leaves all custom properties untouched" in on_update
    assert "clears the saved value so the field falls back to its default; an empty string saves an empty value" in on_update
    assert "On a required field, either is rejected when it would leave the field with no value" in on_update
    on_value = raw_text(*schemas, "CustomPropertyUpdate", "properties", "Value", "description")
    assert "`null` clears the saved value, so the field falls back to its default" in on_value
    assert "an empty string saves an empty value" in on_value and "rejected with a 400 on a required field" in on_value
    is_set = raw_text(*schemas, "CustomPropertyValue", "properties", "IsSet", "description")
    assert "True when a value is saved on this record" in is_set
    assert "A saved value that happens to equal the default is still true" in is_set
    effective = raw_text(*schemas, "CustomPropertyValue", "properties", "Value", "description")
    assert "the value saved on this record, else the field's default, else null" in effective
    assert "Always a string regardless of Type" in effective
    assert "the list shape plus its custom properties, which the list deliberately leaves out" in raw_text(
        *schemas, "ClientResponse", "description"
    )
    assert "CustomProperties" not in json.loads(LATEST_SPEC.read_text(encoding="utf-8"))["components"]["schemas"][
        "ClientListItemResponse"
    ]["properties"]


# --------------------------------------------------------------------------
# list_clients
# --------------------------------------------------------------------------


async def test_list_clients_with_no_filters_sends_only_the_page_size(server, mock_gorelo):
    rows = [client_record(9102, "Sample Corp"), client_record(9101)]
    mock_gorelo.on("GET", CLIENTS, paged_envelope(rows, total_count=40))
    result = await call_tool(server, "list_clients")
    request = mock_gorelo.last
    assert (request.method, request.path, request.query) == ("GET", CLIENTS, {"PageSize": "200"})
    assert request.content == b""
    assert result == {
        "items": rows,
        "count": 2,
        "total_count": 40,
        "has_more": False,
        "next_cursor": None,
        "page_size": 200,
        "filters": {},
    }


async def test_list_clients_sends_every_filter_under_its_spec_name(server, mock_gorelo):
    mock_gorelo.on("GET", CLIENTS, paged_envelope([client_record()], next_cursor="c2", total_count=60))
    result = await call_tool(
        server,
        "list_clients",
        {
            "query": "acme",
            "status_ids": [1, 2],
            "created_since": "2026-01-01T00:00:00Z",
            "created_before": "2026-02-01T09:00:00-05:00",
            "updated_since": "2026-03-01T00:00:00+00:00",
            "updated_before": "2026-04-01T12:30:15.250000Z",
            "page_size": 50,
            "cursor": "c1",
        },
    )
    assert mock_gorelo.last.query == {
        "Query": "acme",
        "StatusIds": "1,2",
        "CreatedSince": "2026-01-01T00:00:00Z",
        "CreatedBefore": "2026-02-01T14:00:00Z",
        "UpdatedSince": "2026-03-01T00:00:00Z",
        "UpdatedBefore": "2026-04-01T12:30:15.250000Z",
        "PageSize": "50",
        "Cursor": "c1",
    }
    assert result["page_size"] == 50 and result["has_more"] is True and result["next_cursor"] == "c2"
    assert result["total_count"] == 60
    assert result["filters"] == {
        "query": "acme",
        "status_ids": [1, 2],
        "created_since": "2026-01-01T00:00:00Z",
        "created_before": "2026-02-01T14:00:00Z",
        "updated_since": "2026-03-01T00:00:00Z",
        "updated_before": "2026-04-01T12:30:15.250000Z",
    }


async def test_list_clients_never_sends_a_name_the_op_does_not_have(server, mock_gorelo, spec_index):
    mock_gorelo.on("GET", CLIENTS, paged_envelope([client_record()]))
    await call_tool(
        server,
        "list_clients",
        {"query": "a", "status_ids": [1], "created_since": "2026-01-01T00:00:00Z", "cursor": "c1"},
    )
    assert set(mock_gorelo.last.query) <= set(spec_index.op("GET /v1/clients").query_params)


async def test_list_clients_follows_the_cursor_with_the_same_filters(server, mock_gorelo):
    mock_gorelo.on("GET", CLIENTS, paged_responder([[client_record(1)], [client_record(2)]], total_count=2))
    first = await call_tool(server, "list_clients", {"query": "a", "page_size": 1})
    assert first["has_more"] is True and first["next_cursor"] == "c1"
    second = await call_tool(server, "list_clients", {"query": "a", "page_size": 1, "cursor": first["next_cursor"]})
    assert second["has_more"] is False and second["next_cursor"] is None
    assert second["items"] == [client_record(2)]
    assert [r.query for r in mock_gorelo.requests] == [
        {"Query": "a", "PageSize": "1"},
        {"Query": "a", "PageSize": "1", "Cursor": "c1"},
    ]


@pytest.mark.parametrize("asked, sent", [(500, 200), (200, 200), (1, 1), (0, 1), (-5, 1), (7, 7)])
async def test_list_clients_clamps_page_size_and_reports_the_size_used(server, mock_gorelo, asked, sent):
    mock_gorelo.on("GET", CLIENTS, paged_envelope([client_record()]))
    result = await call_tool(server, "list_clients", {"page_size": asked})
    assert mock_gorelo.last.query["PageSize"] == str(sent) and result["page_size"] == sent


async def test_list_clients_reports_an_empty_page_as_an_empty_page(server, mock_gorelo):
    mock_gorelo.on("GET", CLIENTS, envelope([], pagination(None, 0, has_more=False)))
    result = await call_tool(server, "list_clients", {"query": "nobody"})
    assert result["items"] == [] and result["count"] == 0 and result["total_count"] == 0
    assert result["filters"] == {"query": "nobody"}


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"query": "  "}, "query: must not be empty or whitespace only"),
        ({"status_ids": []}, "status_ids: expected at least one id, got an empty list"),
        ({"status_ids": [3, 0]}, "status_ids[1]: expected a positive whole number such as 123, got zero or a negative number"),
        ({"status_ids": [-2]}, "status_ids[0]: expected a positive whole number"),
        ({"created_since": "2026-01-01T00:00:00"}, "created_since: '2026-01-01T00:00:00' has no UTC offset"),
        ({"created_before": "2026-01-01"}, "created_before: '2026-01-01' has no UTC offset"),
        ({"updated_since": "yesterday"}, "updated_since: 'yesterday' is not an ISO 8601 datetime"),
        ({"updated_before": "  "}, "updated_before: expected an ISO 8601 datetime with a UTC offset"),
        ({"cursor": " "}, "cursor: must not be empty or whitespace only"),
    ],
)
async def test_list_clients_local_errors_name_the_parameter_and_send_nothing(server, mock_gorelo, arguments, fragment):
    text = await call_tool_error(server, "list_clients", arguments)
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "property_name, param",
    [("StatusIds", "status_ids"), ("PageSize", "page_size"), ("CreatedSince", "created_since"), ("Query", "query"), ("cursor", "cursor")],
)
async def test_list_clients_maps_a_gorelo_error_to_the_snake_case_parameter(server, mock_gorelo, property_name, param):
    mock_gorelo.on("GET", CLIENTS, error_envelope(400, [("070101", "Rejected value.", property_name)]))
    text = await call_tool_error(server, "list_clients", {})
    assert text == trace(f"Gorelo rejected list_clients (HTTP 400, code 070101): {param}: Rejected value.")


async def test_list_clients_refuses_the_legacy_lowercase_body_instead_of_returning_zero_rows(server, mock_gorelo):
    mock_gorelo.on("GET", CLIENTS, {"data": [{"id": 1, "name": "Acme"}], "nextCursor": None, "hasMore": False})
    text = await call_tool_error(server, "list_clients", {})
    assert text.startswith("Gorelo returned an unexpected response for list_clients") and "refusing to guess" in text


async def test_list_clients_refuses_a_page_with_rows_but_no_pagination(server, mock_gorelo):
    mock_gorelo.on("GET", CLIENTS, envelope([client_record()]))
    text = await call_tool_error(server, "list_clients", {})
    assert "paged response without Pagination" in text


# --------------------------------------------------------------------------
# get_client
# --------------------------------------------------------------------------


async def test_get_client_reads_one_client_and_returns_the_record_unchanged(server, mock_gorelo):
    record = client_record(
        Domains=[{"Id": 5, "Name": "example.invalid", "ExpirationDate": "2027-01-31", "Status": {"Id": 1, "Name": "Active"}}]
    )
    mock_gorelo.on("GET", "/v1/clients/9101", envelope(record))
    result = await call_tool(server, "get_client", {"client_id": 9101})
    assert result == record
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", "/v1/clients/9101", {}, b"")
    assert len(mock_gorelo.requests) == 1


async def test_get_client_maps_a_missing_client_to_a_tool_error(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/99999", error_envelope(404, [("070404", "Client not found.")]))
    text = await call_tool_error(server, "get_client", {"client_id": 99999})
    assert text == trace("Gorelo rejected get_client (HTTP 404, code 070404): Client not found.")


@pytest.mark.parametrize("property_name", ["clientId", "ClientId"])
async def test_get_client_maps_a_gorelo_error_about_the_id_to_client_id(server, mock_gorelo, property_name):
    mock_gorelo.on("GET", "/v1/clients/9101", error_envelope(400, [("070101", "The id is not valid.", property_name)]))
    text = await call_tool_error(server, "get_client", {"client_id": 9101})
    assert text == trace("Gorelo rejected get_client (HTTP 400, code 070101): client_id: The id is not valid.")


@pytest.mark.parametrize("bad", [0, -3])
async def test_get_client_rejects_an_id_that_cannot_exist_naming_client_id(server, mock_gorelo, bad):
    text = await call_tool_error(server, "get_client", {"client_id": bad})
    assert text.startswith("client_id: expected a positive whole number")
    assert mock_gorelo.requests == []


async def test_get_client_rejects_a_non_numeric_id_before_any_http_call(server, mock_gorelo):
    text = await call_tool_error(server, "get_client", {"client_id": "abc"})
    assert "client_id" in text
    assert mock_gorelo.requests == []


async def test_get_client_refuses_a_success_without_data(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/9101", envelope(None))
    text = await call_tool_error(server, "get_client", {"client_id": 9101})
    assert "Data is null" in text


@pytest.mark.parametrize("data, found", [({}, "an empty object"), ([client_record()], "a list of 1 item"), (True, "a boolean"), ("x", "a string")])
async def test_get_client_refuses_a_record_that_is_not_an_object(server, mock_gorelo, data, found):
    mock_gorelo.on("GET", "/v1/clients/9101", envelope(data))
    text = await call_tool_error(server, "get_client", {"client_id": 9101})
    assert text.startswith("Gorelo returned an unexpected response for get_client: GET /v1/clients/{clientId}: expected Data to be a non-empty object")
    assert f"but got {found}" in text and "refusing to guess" in text
    assert "write may have been applied" not in text  # a read: nothing to verify


# --------------------------------------------------------------------------
# create_client
# --------------------------------------------------------------------------


async def test_create_client_sends_only_the_two_required_fields_when_nothing_else_is_given(server, mock_gorelo):
    created = client_record(9112, "MCPTEST-1")
    mock_gorelo.on("POST", CLIENTS, envelope(created))
    result = await call_tool(server, "create_client", {"name": "MCPTEST-1", "location_name": "HQ"})
    assert result == created
    assert len(mock_gorelo.requests) == 1
    request = mock_gorelo.last
    assert (request.method, request.path, request.query) == ("POST", CLIENTS, {})
    assert request.json == {"Name": "MCPTEST-1", "Location": {"Name": "HQ"}}


async def test_create_client_nests_every_location_field_under_location(server, mock_gorelo):
    mock_gorelo.on("POST", CLIENTS, envelope(client_record(9113, "MCPTEST-2")))
    await call_tool(
        server,
        "create_client",
        {
            "name": "MCPTEST-2",
            "location_name": "Head Office",
            "billing_name": "MCPTEST-2 Billing LLC",
            "alternate_name": "MCPTEST Two",
            "domain": "example.invalid",
            "location_phone_country_code": "US",
            "location_phone": "5555550142",
            "location_phone_ext": "12",
            "location_address1": "1 Main St",
            "location_address2": "Suite 4",
            "location_city": "Springfield",
            "location_state": "CA",
            "location_country": "United States",
            "location_postal_code": "00000",
            "location_time_zone": "UTC",
        },
    )
    assert mock_gorelo.last.json == {
        "Name": "MCPTEST-2",
        "BillingName": "MCPTEST-2 Billing LLC",
        "AlternateName": "MCPTEST Two",
        "Domain": "example.invalid",
        "Location": {
            "Name": "Head Office",
            "PhoneCountryCode": "US",
            "Phone": "5555550142",
            "PhoneExt": "12",
            "Address1": "1 Main St",
            "Address2": "Suite 4",
            "City": "Springfield",
            "State": "CA",
            "Country": "United States",
            "PostalCode": "00000",
            "TimeZone": "UTC",
        },
    }


@pytest.mark.parametrize("region", ["US", "CA", "GB"])
async def test_create_client_accepts_an_iso_region_with_a_phone(server, mock_gorelo, region):
    mock_gorelo.on("POST", CLIENTS, envelope(client_record(9114, "MCPTEST-3")))
    await call_tool(
        server,
        "create_client",
        {"name": "MCPTEST-3", "location_name": "HQ", "location_phone": "5550142", "location_phone_country_code": region},
    )
    assert mock_gorelo.last.json["Location"] == {"Name": "HQ", "PhoneCountryCode": region, "Phone": "5550142"}


async def test_create_client_does_not_invent_a_country_code_for_a_phone(server, mock_gorelo):
    text = await call_tool_error(
        server, "create_client", {"name": "MCPTEST-3", "location_name": "HQ", "location_phone": "5555550142"}
    )
    assert text.startswith("location_phone_country_code: required when location_phone is given")
    assert "assumes none" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("dial_code", ["1", "+1", "us", "USA", " US", ""])
async def test_create_client_rejects_a_dial_code_or_a_badly_written_region(server, mock_gorelo, dial_code):
    text = await call_tool_error(
        server,
        "create_client",
        {"name": "A", "location_name": "HQ", "location_phone": "5550142", "location_phone_country_code": dial_code},
    )
    assert text.startswith("location_phone_country_code: expected a 2 letter ISO region code in capitals")
    assert "dial codes like 1 or +1 are not accepted" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"name": "", "location_name": "HQ"}, "name: must not be empty or whitespace only"),
        ({"name": "  ", "location_name": "HQ"}, "name: must not be empty or whitespace only"),
        ({"name": "A", "location_name": ""}, "location_name: must not be empty or whitespace only"),
        ({"name": "A", "location_name": "HQ", "billing_name": " "}, "billing_name: must not be empty or whitespace only"),
        ({"name": "A", "location_name": "HQ", "domain": ""}, "domain: must not be empty or whitespace only"),
        ({"name": "A", "location_name": "HQ", "location_city": ""}, "location_city: must not be empty or whitespace only"),
    ],
)
async def test_create_client_rejects_blank_text_naming_the_parameter(server, mock_gorelo, arguments, fragment):
    text = await call_tool_error(server, "create_client", arguments)
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_create_client_requires_a_name_and_a_location_name(server, mock_gorelo):
    assert "name" in await call_tool_error(server, "create_client", {"location_name": "HQ"})
    assert "location_name" in await call_tool_error(server, "create_client", {"name": "A"})
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "property_name, param",
    [
        ("Phone", "location_phone"),
        ("Location.Phone", "location_phone"),
        ("PhoneCountryCode", "location_phone_country_code"),
        ("Location.Name", "location_name"),
        ("Name", "name or location_name"),
        ("Domain", "domain"),
        ("BillingName", "billing_name"),
        ("PostalCode", "location_postal_code"),
        ("Location.TimeZone", "location_time_zone"),
    ],
)
async def test_create_client_maps_a_gorelo_error_to_the_snake_case_parameter(server, mock_gorelo, property_name, param):
    mock_gorelo.on("POST", CLIENTS, error_envelope(400, [("070101", "Value is not valid.", property_name)]))
    text = await call_tool_error(server, "create_client", {"name": "A", "location_name": "HQ"})
    assert text == trace(f"Gorelo rejected create_client (HTTP 400, code 070101): {param}: Value is not valid.")


async def test_create_client_shows_every_notification(server, mock_gorelo):
    notes = [("070101", "Phone is not valid.", "Phone"), ("070101", "Domain is not valid.", "Domain")]
    mock_gorelo.on("POST", CLIENTS, error_envelope(400, notes))
    text = await call_tool_error(server, "create_client", {"name": "A", "location_name": "HQ"})
    assert "location_phone: Phone is not valid.; domain: Domain is not valid." in text


async def test_create_client_reports_a_body_gorelo_could_not_read_without_inventing_a_parameter(server, mock_gorelo):
    mock_gorelo.on("POST", CLIENTS, error_envelope(400, [("070201", "Invalid or malformed request body.")]))
    text = await call_tool_error(server, "create_client", {"name": "A", "location_name": "HQ"})
    assert text == trace("Gorelo rejected create_client (HTTP 400, code 070201): Invalid or malformed request body.")


async def test_create_client_that_times_out_says_gorelo_did_not_confirm_and_is_not_retried(server, mock_gorelo):
    mock_gorelo.on("POST", CLIENTS, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "create_client", {"name": "A", "location_name": "HQ"})
    assert text.startswith("Gorelo did not confirm create_client") and "Verify with a read before retrying" in text
    assert len(mock_gorelo.requests) == 1


async def test_create_client_after_a_server_error_says_the_client_may_exist(server, mock_gorelo):
    mock_gorelo.on("POST", CLIENTS, httpx.Response(500, text="boom", headers={"content-type": "text/plain"}))
    text = await call_tool_error(server, "create_client", {"name": "A", "location_name": "HQ"})
    assert "The change may or may not have been applied" in text
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "data, problem",
    [
        (None, "Data is null, not an object with an Id"),
        ({}, "Data is an object without an Id"),
        ([], "Data is an empty list, not an object with an Id"),
        (False, "Data is a boolean, not an object with an Id"),
        (True, "Data is a boolean, not an object with an Id"),
        ("ok", "Data is a string, not an object with an Id"),
        ({"Name": "A"}, "Data is an object without an Id"),
        ({"Id": None}, "Data.Id is null"),
        ({"Id": 0}, "Data.Id is zero or negative"),
        ({"Id": -3}, "Data.Id is zero or negative"),
        ({"Id": True}, "Data.Id is a boolean"),
        ({"Id": ""}, "Data.Id is blank"),
        ({"Id": 1.5}, "Data.Id is a decimal number"),
    ],
)
async def test_create_client_refuses_an_answer_that_is_not_a_record_with_an_id(server, mock_gorelo, data, problem):
    # created_id(): a write whose answer cannot be used raises (shape, write_unconfirmed), never returns success
    mock_gorelo.on("POST", CLIENTS, envelope(data))
    text = await call_tool_error(server, "create_client", {"name": "A", "location_name": "HQ"})
    assert text.startswith("Gorelo returned an unexpected response for create_client: POST /v1/clients: ")
    assert f"Gorelo reported success but the answer carries no usable Id for the record ({problem})" in text
    assert "the write may have been applied, so verify it with a read before repeating it" in text
    assert len(mock_gorelo.requests) == 1  # never repeated by the tool


async def test_create_client_passes_through_a_record_that_only_carries_the_id(server, mock_gorelo):
    mock_gorelo.on("POST", CLIENTS, envelope({"Id": 9115}))
    assert await call_tool(server, "create_client", {"name": "A", "location_name": "HQ"}) == {"Id": 9115}
    assert len(mock_gorelo.requests) == 1


# --------------------------------------------------------------------------
# update_client
# --------------------------------------------------------------------------


async def test_update_client_puts_the_id_in_the_path_and_sends_only_the_given_field(server, mock_gorelo):
    updated = client_record(9101, "Example Co Renamed")
    mock_gorelo.on("PATCH", CLIENT_9101, envelope(updated))
    result = await call_tool(server, "update_client", {"client_id": 9101, "name": "Example Co Renamed"})
    assert result == updated
    request = mock_gorelo.last
    assert (request.method, request.path, request.query) == ("PATCH", CLIENT_9101, {})
    assert request.json == {"Name": "Example Co Renamed"}


async def test_update_client_sends_no_id_in_the_body_and_the_id_only_in_the_path(server, mock_gorelo):
    # contract e15cb5a18ec2: PATCH /v1/clients/{clientId}, and UpdateClientCommand has no Id field
    mock_gorelo.on("PATCH", CLIENT_9101, envelope(client_record(9101, "Renamed")))
    mock_gorelo.on("PATCH", "/v1/clients/9200", envelope(client_record(9200, "Other", AlternateName="x")))
    await call_tool(server, "update_client", {"client_id": 9101, "name": "Renamed"})
    await call_tool(server, "update_client", {"client_id": 9200, "alternate_name": "x"})
    first, second = mock_gorelo.requests
    assert (first.method, first.path, first.raw_path) == ("PATCH", CLIENT_9101, CLIENT_9101)
    assert first.json == {"Name": "Renamed"}
    assert (second.method, second.path) == ("PATCH", "/v1/clients/9200")
    assert second.json == {"AlternateName": "x"}
    for request in (first, second):
        assert "Id" not in request.json and "ClientId" not in request.json and "clientId" not in request.json
    assert not any(r.path == CLIENTS for r in mock_gorelo.requests)  # never the removed collection form


def test_update_client_declares_the_published_operation_and_no_override_is_involved(spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == "update_client")
    assert spec.ops == ["GET /v1/clients/{clientId}", "PATCH /v1/clients/{clientId}"]  # the read before a custom field write
    for declared in spec.ops:
        assert declared in spec_index.ops and declared not in spec_index.override_ops  # published, not swapped in
        assert not spec_index.ops[declared].is_live_override
    assert "PATCH /v1/clients" not in spec_index.ops  # the collection form is gone from the published spec
    op = spec_index.op("PATCH /v1/clients/{clientId}")
    assert op.path_params == {"clientId": {"type": "integer", "format": "int64"}}
    assert set(op.body["fields"]) == {"Name", "StatusId", "BillingName", "AlternateName", "CustomProperties"}
    assert op.body["required"] == []
    assert "Id" not in op.body["fields"]
    custom = op.body["fields"]["CustomProperties"]  # contract 1b1c0cec0e9a: an upsert by Id, null Value clears
    assert (custom["type"], custom["items"]["ref"], custom["nullable"]) == ("array", "CustomPropertyUpdate", True)
    doc = clients_module.__doc__ or ""
    assert "PATCH /v1/clients/{clientId}" in doc and "no Id field" in doc and "section retired" in doc


async def test_update_client_reports_a_405_as_an_error_never_as_a_success(server, mock_gorelo):
    # what the retired collection form answered; if Gorelo ever moves the operation again this must stay loud
    mock_gorelo.on("PATCH", CLIENT_9101, httpx.Response(405, headers={"Allow": "GET, POST"}))
    text = await call_tool_error(server, "update_client", {"client_id": 9101, "name": "N"})
    assert text.startswith("Gorelo returned an unexpected response for update_client: PATCH /v1/clients/{clientId}: ")
    assert "HTTP 405" in text and len(mock_gorelo.requests) == 1


async def test_update_client_makes_no_read_before_the_write(server, mock_gorelo):
    mock_gorelo.on("PATCH", CLIENT_9101, envelope(client_record()))
    await call_tool(server, "update_client", {"client_id": 9101, "billing_name": "Example Co Billing"})
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("PATCH", CLIENT_9101)]


async def test_update_client_sends_all_four_changes_under_their_pascal_names(server, mock_gorelo):
    mock_gorelo.on("PATCH", CLIENT_9101, envelope(client_record()))
    await call_tool(
        server,
        "update_client",
        {"client_id": 9101, "name": "N", "status_id": 2, "billing_name": "B", "alternate_name": "A"},
    )
    assert mock_gorelo.last.json == {"Name": "N", "StatusId": 2, "BillingName": "B", "AlternateName": "A"}


@pytest.mark.parametrize(
    "arguments, body",
    [
        ({"status_id": 3}, {"StatusId": 3}),
        ({"alternate_name": "MCPTEST-ALT"}, {"AlternateName": "MCPTEST-ALT"}),
        ({"billing_name": "Invoices Inc"}, {"BillingName": "Invoices Inc"}),
    ],
)
async def test_update_client_never_sends_a_field_that_was_not_given(server, mock_gorelo, arguments, body):
    mock_gorelo.on("PATCH", CLIENT_9101, envelope(client_record()))
    await call_tool(server, "update_client", {"client_id": 9101, **arguments})
    assert mock_gorelo.last.json == body


async def test_update_client_with_nothing_to_change_is_a_local_error(server, mock_gorelo):
    text = await call_tool_error(server, "update_client", {"client_id": 9101})
    assert text == (
        "nothing to update: pass at least one of name, status_id, billing_name, alternate_name, custom_properties "
        "or clear_custom_property_ids (only the fields you pass are changed)"
    )
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("param", ["name", "billing_name", "alternate_name"])
@pytest.mark.parametrize("blank", ["", "   "])
async def test_update_client_rejects_blank_text_and_says_gorelo_cannot_clear_client_fields(server, mock_gorelo, param, blank):
    text = await call_tool_error(server, "update_client", {"client_id": 9101, param: blank})
    assert text.startswith(f"{param}: must not be empty. Gorelo cannot clear client fields through the API")
    assert f"omit {param} to keep the current value" in text
    assert mock_gorelo.requests == []


async def test_update_client_has_no_clear_option_for_the_plain_fields_only_custom_fields_can_be_cleared(server):
    tool = next(t for t in await list_tools(server) if t.name == "update_client")
    props = tool.inputSchema["properties"]
    assert "clear_fields" not in props  # name, status, billing name and alternate name still cannot be cleared
    assert "clear_custom_property_ids" in props


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"client_id": 0, "name": "N"}, "client_id: expected a positive whole number"),
        ({"client_id": -1, "name": "N"}, "client_id: expected a positive whole number"),
    ],
)
async def test_update_client_rejects_a_client_id_that_cannot_exist(server, mock_gorelo, arguments, fragment):
    assert fragment in await call_tool_error(server, "update_client", arguments)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", [0, -1, -999])
async def test_update_client_validates_status_id_with_positive_id_and_sends_nothing(server, mock_gorelo, bad):
    #
    text = await call_tool_error(server, "update_client", {"client_id": 9101, "name": "N", "status_id": bad})
    assert text.startswith("status_id: expected a positive whole number such as 123, got zero or a negative number")
    assert mock_gorelo.requests == []


async def test_update_client_rejects_a_status_id_above_the_largest_gorelo_id(server, mock_gorelo):
    text = await call_tool_error(server, "update_client", {"client_id": 9101, "status_id": 2**63})
    assert text.startswith("status_id: expected a positive whole number such as 123, got a number above ")
    assert mock_gorelo.requests == []


async def test_update_client_leaves_status_ids_to_gorelo_since_they_are_tenant_data(server, mock_gorelo):
    mock_gorelo.on("PATCH", CLIENT_9101, error_envelope(400, [("070101", "Unknown status.", "StatusId")]))
    text = await call_tool_error(server, "update_client", {"client_id": 9101, "status_id": 999})
    assert mock_gorelo.last.json == {"StatusId": 999}
    assert text == trace("Gorelo rejected update_client (HTTP 400, code 070101): status_id: Unknown status.")


@pytest.mark.parametrize(
    "property_name, param",
    [
        ("clientId", "client_id"), ("ClientId", "client_id"), ("Name", "name"), ("StatusId", "status_id"),
        ("BillingName", "billing_name"), ("AlternateName", "alternate_name"),
    ],
)
async def test_update_client_maps_a_gorelo_error_to_the_snake_case_parameter(server, mock_gorelo, property_name, param):
    mock_gorelo.on("PATCH", CLIENT_9101, error_envelope(400, [("070101", "Value is not valid.", property_name)]))
    text = await call_tool_error(server, "update_client", {"client_id": 9101, "name": "N"})
    assert text == trace(f"Gorelo rejected update_client (HTTP 400, code 070101): {param}: Value is not valid.")


async def test_update_client_on_a_missing_client_is_a_tool_error(server, mock_gorelo):
    mock_gorelo.on("PATCH", "/v1/clients/99999", error_envelope(404, [("070404", "Client not found.")]))
    text = await call_tool_error(server, "update_client", {"client_id": 99999, "name": "N"})
    assert text == trace("Gorelo rejected update_client (HTTP 404, code 070404): Client not found.")


async def test_update_client_that_times_out_is_not_retried_and_says_to_verify(server, mock_gorelo):
    mock_gorelo.on("PATCH", CLIENT_9101, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "update_client", {"client_id": 9101, "name": "N"})
    assert text.startswith("Gorelo did not confirm update_client") and "Verify with a read before retrying" in text
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "data, found",
    [(None, "null"), ({}, "an empty object"), ([], "an empty list"), (False, "a boolean"), (True, "a boolean"), ("ok", "a string")],
)
async def test_update_client_refuses_an_answer_that_is_not_the_client_record(server, mock_gorelo, data, found):
    # expect_object(): null, {}, false and the rest raise (shape, write_unconfirmed), never return success
    mock_gorelo.on("PATCH", CLIENT_9101, envelope(data))
    text = await call_tool_error(server, "update_client", {"client_id": 9101, "name": "N"})
    assert text.startswith("Gorelo returned an unexpected response for update_client: PATCH /v1/clients/{clientId}: ")
    assert f"expected Data to be a non-empty object but got {found}" in text
    assert "the write may have been applied, so verify it with a read before repeating it" in text
    assert len(mock_gorelo.requests) == 1


async def test_update_client_passes_through_a_record_that_only_carries_the_id(server, mock_gorelo):
    mock_gorelo.on("PATCH", CLIENT_9101, envelope({"Id": 9101}))
    assert await call_tool(server, "update_client", {"client_id": 9101, "name": "N"}) == {"Id": 9101}


# --------------------------------------------------------------------------
# update_client: custom properties (contract 1b1c0cec0e9a)
# --------------------------------------------------------------------------

SAVE_5 = {"custom_properties": [{"id": 5, "value": "SECRET-VALUE"}]}
CLEAR_7 = {"clear_custom_property_ids": [7]}
SAVE_5_CLEAR_7 = {**SAVE_5, **CLEAR_7}
EMPTY_VALUE = (
    "must be text that is not empty or blank. To remove a saved value pass its id in clear_custom_property_ids "
    "(custom_properties never sends an empty or null value)"
)
POSITIVE = "expected a positive whole number such as 123"
NO_VALUE_5 = "custom field(s) 5 show no text value, although they were sent with one"


async def call_update_directly(client_factory, **kwargs):
    """Call the decorated update_client itself (no pydantic in front of it): for what the schema normally pre-empts."""
    async with client_factory() as client:
        return await clients_module.update_client(make_ctx(client), **kwargs)


def prior_fields(arguments):
    """The CustomProperties that the read before the write answers for a call: exactly the fields the call names, a saved
    field unset and a cleared field set, so that the answers of client_with_custom_properties change nothing else."""
    rows = [custom_property(item["id"], is_set=False, value=None) for item in arguments.get("custom_properties", [])]
    rows += [custom_property(identifier, value="before") for identifier in arguments.get("clear_custom_property_ids", [])]
    return rows


def route_update(mock_gorelo, arguments, answer, *, prior=None, after=None, second=None, client_id=9101):
    """Serve what update_client asks Gorelo for when it writes custom fields: the read of the client, the PATCH, and the
    read of the client again (one more read than that is an unmatched request and fails the test).

    `answer` is a response spec for the PATCH (envelope(...), error_envelope(...), an exception). `prior` is the list of
    CustomProperties the first read shows (by default prior_fields(arguments)); `after` is the client record the second
    read shows (by default the record the PATCH answers with, when it answers with an envelope of an object); `second`
    is a response spec for the second read as it is (an error, an exception, an odd envelope) and wins over `after`. A
    PATCH that fails has no second read."""
    rows = prior_fields(arguments) if prior is None else prior
    if after is None and isinstance(answer, dict) and answer.get("IsSuccess") is True and isinstance(answer.get("Data"), dict):
        after = answer["Data"]
    reads = [envelope(client_record(client_id, CustomProperties=rows))]
    if second is not None:
        reads.append(second)
    elif after is not None:
        reads.append(envelope(after))
    mock_gorelo.on("GET", f"/v1/clients/{client_id}", in_order(*reads))
    mock_gorelo.on("PATCH", f"/v1/clients/{client_id}", answer)


async def test_update_client_saves_custom_properties_as_id_and_value_entries_and_returns_the_answer(server, mock_gorelo):
    answer = client_with_custom_properties(saved=[5, 9])
    arguments = {"custom_properties": [{"id": 5, "value": "Gold"}, {"id": 9, "value": "2026-10-14"}]}
    route_update(mock_gorelo, arguments, envelope(answer))
    result = await call_tool(server, "update_client", {"client_id": 9101, **arguments})
    assert result == answer  # the record Gorelo answered, unchanged
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [
        ("GET", CLIENT_9101), ("PATCH", CLIENT_9101), ("GET", CLIENT_9101)  # the client is read, written and read again
    ]
    request = mock_gorelo.calls("PATCH")[-1]
    assert (request.method, request.path, request.query, request.raw_path) == ("PATCH", CLIENT_9101, {}, CLIENT_9101)
    # only custom properties were given, so nothing else is sent (and the client id is in the path only)
    assert request.json == {"CustomProperties": [{"Id": 5, "Value": "Gold"}, {"Id": 9, "Value": "2026-10-14"}]}


async def test_update_client_clears_a_custom_field_with_a_real_json_null(server, mock_gorelo):
    answer = client_with_custom_properties(cleared=[7, 8])
    arguments = {"clear_custom_property_ids": [7, 8]}
    route_update(mock_gorelo, arguments, envelope(answer))
    result = await call_tool(server, "update_client", {"client_id": 9101, **arguments})
    assert result == answer
    request = mock_gorelo.calls("PATCH")[-1]
    assert (request.method, request.path, request.query) == ("PATCH", CLIENT_9101, {})
    assert request.json == {"CustomProperties": [{"Id": 7, "Value": None}, {"Id": 8, "Value": None}]}
    # null on the wire: not the text "null", not an empty string and not a missing Value
    assert request.content.count(b"null") == 2 and b'""' not in request.content and request.content.count(b'"Value"') == 2


async def test_update_client_sends_the_saved_entries_first_then_the_cleared_ones_whatever_the_argument_order(
    server, mock_gorelo
):
    arguments = {
        "clear_custom_property_ids": [8, 7],
        "custom_properties": [{"id": 9, "value": "b"}, {"id": 5, "value": "a"}],
    }
    route_update(mock_gorelo, arguments, envelope(client_with_custom_properties(saved=[9, 5], cleared=[8, 7])))
    await call_tool(server, "update_client", {"client_id": 9101, **arguments})
    assert mock_gorelo.calls("PATCH")[-1].json == {
        "CustomProperties": [
            {"Id": 9, "Value": "b"}, {"Id": 5, "Value": "a"}, {"Id": 8, "Value": None}, {"Id": 7, "Value": None},
        ]
    }


@pytest.mark.parametrize(
    "arguments, answer",
    [
        (SAVE_5, client_with_custom_properties(saved=[5])),
        (CLEAR_7, client_with_custom_properties(cleared=[7])),
        (SAVE_5_CLEAR_7, client_with_custom_properties(saved=[5], cleared=[7])),
    ],
)
async def test_update_client_with_only_custom_properties_works_and_is_not_nothing_to_update(
    server, mock_gorelo, arguments, answer
):
    route_update(mock_gorelo, arguments, envelope(answer))
    assert await call_tool(server, "update_client", {"client_id": 9101, **arguments}) == answer
    assert [set(r.json) for r in mock_gorelo.calls("PATCH")] == [{"CustomProperties"}]  # no other field is sent


async def test_update_client_sends_custom_properties_beside_the_four_plain_fields(server, mock_gorelo):
    arguments = {
        "name": "N", "status_id": 2, "billing_name": "B", "alternate_name": "A",
        "custom_properties": [{"id": 5, "value": "v"}], "clear_custom_property_ids": [7],
    }
    route_update(mock_gorelo, arguments, envelope(client_with_custom_properties(saved=[5], cleared=[7], Name="N")))
    result = await call_tool(server, "update_client", {"client_id": 9101, **arguments})
    assert mock_gorelo.calls("PATCH")[-1].json == {
        "Name": "N", "StatusId": 2, "BillingName": "B", "AlternateName": "A",
        "CustomProperties": [{"Id": 5, "Value": "v"}, {"Id": 7, "Value": None}],
    }
    assert result["Name"] == "N" and [r.method for r in mock_gorelo.requests] == ["GET", "PATCH", "GET"]  # one write


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(" padded ", id="padded"),
        pytest.param("line one\nline two", id="two-lines"),
        pytest.param("\u00dcn\u00efc\u00f6d\u00e9 \u2713", id="unicode"),
        pytest.param("0", id="zero-as-text"),
        pytest.param("false", id="false-as-text"),
        pytest.param("null", id="null-as-text"),
        pytest.param("[]", id="brackets"),
        pytest.param("<b>x</b>", id="markup"),
        pytest.param("a" * 5000, id="5000-characters"),
    ],
)
async def test_update_client_sends_a_custom_value_exactly_as_given_as_text(server, mock_gorelo, text):
    # "null", "0" and "false" are words here, never a JSON null, a number or a boolean; nothing is trimmed or reshaped
    arguments = {"custom_properties": [{"id": 5, "value": text}]}
    route_update(mock_gorelo, arguments, envelope(client_with_custom_properties(saved=[5])))
    await call_tool(server, "update_client", {"client_id": 9101, **arguments})
    assert mock_gorelo.calls("PATCH")[-1].json == {"CustomProperties": [{"Id": 5, "Value": text}]}


async def test_update_client_accepts_the_largest_gorelo_id_for_a_custom_field(server, mock_gorelo):
    biggest = 2**63 - 1
    arguments = {"custom_properties": [{"id": biggest, "value": "v"}], "clear_custom_property_ids": [1]}
    route_update(mock_gorelo, arguments, envelope(client_with_custom_properties(saved=[biggest], cleared=[1])))
    await call_tool(server, "update_client", {"client_id": 9101, **arguments})
    assert mock_gorelo.calls("PATCH")[-1].json["CustomProperties"] == [{"Id": biggest, "Value": "v"}, {"Id": 1, "Value": None}]


async def test_update_client_accepts_the_entry_model_and_a_plain_object_alike(client_factory, mock_gorelo):
    entries = [clients_module.CustomPropertySet(id=5, value="a"), {"id": 6, "value": "b"}]
    route_update(
        mock_gorelo, {"custom_properties": [{"id": 5}, {"id": 6}]}, envelope(client_with_custom_properties(saved=[5, 6]))
    )
    await call_update_directly(client_factory, client_id=9101, custom_properties=entries)
    assert mock_gorelo.calls("PATCH")[-1].json == {"CustomProperties": [{"Id": 5, "Value": "a"}, {"Id": 6, "Value": "b"}]}


async def test_an_explicit_null_for_both_custom_property_parameters_means_not_given(server, mock_gorelo):
    mock_gorelo.on("PATCH", CLIENT_9101, envelope(client_record()))
    await call_tool(
        server,
        "update_client",
        {"client_id": 9101, "name": "N", "custom_properties": None, "clear_custom_property_ids": None},
    )
    assert mock_gorelo.last.json == {"Name": "N"}  # and the answer is not checked for custom properties
    assert [r.method for r in mock_gorelo.requests] == ["PATCH"]  # a plain update reads nothing first
    text = await call_tool_error(
        server, "update_client", {"client_id": 9101, "custom_properties": None, "clear_custom_property_ids": None}
    )
    assert text.startswith("nothing to update: pass at least one of")


@pytest.mark.parametrize("entry", [{"id": 7}, {"id": 7, "value": None}])
async def test_an_entry_with_no_value_gets_the_guided_message_through_the_client_not_a_schema_error(
    server, mock_gorelo, entry
):
    # {"id": 7} used to stop at the schema ("Field required"); now it reaches the tool, which says how to clear a field
    text = await call_tool_error(server, "update_client", {"client_id": 9101, "custom_properties": [entry]})
    assert text == f"custom_properties (item 1, value): {EMPTY_VALUE}"
    assert "validation error" not in text and "Field required" not in text
    assert mock_gorelo.requests == []


def test_the_entry_model_defaults_value_to_none_so_a_missing_value_is_the_tools_to_judge():
    entry = clients_module.CustomPropertySet(id=7)
    assert entry.value is None and entry.model_dump() == {"id": 7, "value": None}
    assert clients_module.CustomPropertySet.model_fields["id"].is_required()
    assert not clients_module.CustomPropertySet.model_fields["value"].is_required()


# -- local validation: every refusal is before any HTTP call --------------------------------------------------------

LOCAL_CUSTOM_ERRORS = [
    pytest.param(
        {"custom_properties": []},
        "custom_properties: expected at least one object with id and value, got an empty list "
        "(omit custom_properties if you save no custom field)",
        id="custom-properties-empty-list",
    ),
    pytest.param(
        {"clear_custom_property_ids": []},
        "clear_custom_property_ids: expected at least one id, got an empty list "
        "(omit clear_custom_property_ids if you have no ids to give)",
        id="clear-empty-list",
    ),
    pytest.param(
        {"custom_properties": [{"id": 0, "value": "x"}]},
        f"custom_properties (item 1, id): {POSITIVE}, got zero or a negative number",
        id="id-zero",
    ),
    pytest.param(
        {"custom_properties": [{"id": 4, "value": "x"}, {"id": -3, "value": "x"}]},
        f"custom_properties (item 2, id): {POSITIVE}, got zero or a negative number",
        id="second-id-negative",
    ),
    pytest.param(
        {"custom_properties": [{"id": 2**63, "value": "x"}]},
        f"custom_properties (item 1, id): {POSITIVE}, got a number above 9223372036854775807",
        id="id-beyond-int64",
    ),
    pytest.param(
        {"clear_custom_property_ids": [4, 0]},
        f"clear_custom_property_ids[1]: {POSITIVE}, got zero or a negative number",
        id="clear-id-zero",
    ),
    pytest.param(
        {"clear_custom_property_ids": [-1]},
        f"clear_custom_property_ids[0]: {POSITIVE}, got zero or a negative number",
        id="clear-id-negative",
    ),
    pytest.param(
        {"clear_custom_property_ids": [2**63]},
        f"clear_custom_property_ids[0]: {POSITIVE}, got a number above 9223372036854775807",
        id="clear-id-beyond-int64",
    ),
    pytest.param({"custom_properties": [{"id": 5, "value": ""}]}, f"custom_properties (item 1, value): {EMPTY_VALUE}", id="empty-value"),
    pytest.param({"custom_properties": [{"id": 5, "value": " "}]}, f"custom_properties (item 1, value): {EMPTY_VALUE}", id="blank-value"),
    pytest.param({"custom_properties": [{"id": 5, "value": "\t \n"}]}, f"custom_properties (item 1, value): {EMPTY_VALUE}", id="whitespace-value"),
    pytest.param({"custom_properties": [{"id": 5, "value": None}]}, f"custom_properties (item 1, value): {EMPTY_VALUE}", id="null-value"),
    pytest.param({"custom_properties": [{"id": 7}]}, f"custom_properties (item 1, value): {EMPTY_VALUE}", id="value-key-missing"),
    pytest.param(
        {"custom_properties": [{"id": 4, "value": "ok"}, {"id": 7}]},
        f"custom_properties (item 2, value): {EMPTY_VALUE}",
        id="second-value-key-missing",
    ),
    pytest.param(
        {"custom_properties": [{"id": 4, "value": "ok"}, {"id": 5, "value": "  "}]},
        f"custom_properties (item 2, value): {EMPTY_VALUE}",
        id="second-value-blank",
    ),
    pytest.param(
        {"custom_properties": [{"id": 5, "value": "a"}, {"id": 6, "value": "b"}, {"id": 5, "value": "c"}]},
        "custom_properties (item 3, id): custom field 5 is already in item 1; give each id once",
        id="id-twice-in-custom-properties",
    ),
    pytest.param(
        {"clear_custom_property_ids": [5, 6, 5]},
        "clear_custom_property_ids[2]: custom field 5 is already at [0]; give each id once",
        id="id-twice-in-clear",
    ),
    pytest.param(
        {"custom_properties": [{"id": 4, "value": "a"}, {"id": 5, "value": "b"}], "clear_custom_property_ids": [6, 5]},
        "clear_custom_property_ids[1]: custom field 5 is also in custom_properties (item 2); "
        "an id is either saved or cleared in one call, not both",
        id="same-id-saved-and-cleared",
    ),
]


@pytest.mark.parametrize("arguments, fragment", LOCAL_CUSTOM_ERRORS)
async def test_update_client_custom_property_local_errors_name_the_parameter_and_send_nothing(
    server, mock_gorelo, arguments, fragment
):
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **arguments})
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_update_client_reports_a_bad_plain_field_before_a_bad_custom_property(server, mock_gorelo):
    text = await call_tool_error(
        server, "update_client", {"client_id": 9101, "name": " ", "custom_properties": [{"id": 0, "value": "x"}]}
    )
    assert text.startswith("name: must not be empty")
    assert mock_gorelo.requests == []


SCHEMA_CUSTOM_REFUSALS = [
    pytest.param({"custom_properties": [{"id": True, "value": "x"}]}, "custom_properties.0.id", "valid integer", id="id-true"),
    pytest.param({"custom_properties": [{"id": "5", "value": "x"}]}, "custom_properties.0.id", "valid integer", id="id-digit-string"),
    pytest.param({"custom_properties": [{"id": 5.0, "value": "x"}]}, "custom_properties.0.id", "valid integer", id="id-decimal"),
    pytest.param({"custom_properties": [{"id": "abc", "value": "x"}]}, "custom_properties.0.id", "valid integer", id="id-text"),
    pytest.param({"custom_properties": [{"value": "x"}]}, "custom_properties.0.id", "Field required", id="id-missing"),
    pytest.param({"custom_properties": [{"id": 5, "value": 42}]}, "custom_properties.0.value", "valid string", id="value-number"),
    pytest.param({"custom_properties": [{"id": 5, "value": True}]}, "custom_properties.0.value", "valid string", id="value-true"),
    pytest.param({"custom_properties": [{"id": 5, "value": "x", "name": "n"}]}, "custom_properties.0.name", "Extra inputs are not permitted", id="unknown-key"),
    pytest.param({"custom_properties": [{"id": 5, "value": "x", "Id": 5}]}, "custom_properties.0.Id", "Extra inputs are not permitted", id="pascal-key"),
    pytest.param({"custom_properties": ["x"]}, "custom_properties.0", "valid dictionary", id="item-text"),
    pytest.param({"custom_properties": [5]}, "custom_properties.0", "valid dictionary", id="item-number"),
    pytest.param({"custom_properties": [[5, "x"]]}, "custom_properties.0", "valid dictionary", id="item-list"),
    pytest.param({"custom_properties": [None]}, "custom_properties.0", "valid dictionary", id="item-null"),
    pytest.param({"custom_properties": {"id": 5, "value": "x"}}, "custom_properties", "valid list", id="one-object-not-a-list"),
    pytest.param({"custom_properties": "5=x"}, "custom_properties", "valid list", id="text-not-a-list"),
    pytest.param({"clear_custom_property_ids": [True]}, "clear_custom_property_ids.0", "valid integer", id="clear-true"),
    pytest.param({"clear_custom_property_ids": ["5"]}, "clear_custom_property_ids.0", "valid integer", id="clear-digit-string"),
    pytest.param({"clear_custom_property_ids": [5.0]}, "clear_custom_property_ids.0", "valid integer", id="clear-decimal"),
    pytest.param({"clear_custom_property_ids": [1, True]}, "clear_custom_property_ids.1", "valid integer", id="clear-second-true"),
    pytest.param({"clear_custom_property_ids": "5"}, "clear_custom_property_ids", "valid list", id="clear-text-not-a-list"),
    pytest.param({"clear_custom_property_ids": 5}, "clear_custom_property_ids", "valid list", id="clear-number-not-a-list"),
]


@pytest.mark.parametrize("arguments, where, phrase", SCHEMA_CUSTOM_REFUSALS)
async def test_the_schema_refuses_a_wrongly_typed_custom_property_naming_where_before_the_tool_runs(
    server, mock_gorelo, arguments, where, phrase
):
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **arguments})
    assert where in text and phrase in text
    assert mock_gorelo.requests == []


DIRECT_CUSTOM_REFUSALS = [
    pytest.param({"custom_properties": [{"id": True, "value": "x"}]}, f"custom_properties (item 1, id): {POSITIVE}, got a boolean", id="id-true"),
    pytest.param({"custom_properties": [{"id": "5", "value": "x"}]}, f"custom_properties (item 1, id): {POSITIVE}, got a string", id="id-digit-string"),
    pytest.param({"custom_properties": [{"id": 5.0, "value": "x"}]}, f"custom_properties (item 1, id): {POSITIVE}, got a decimal number", id="id-decimal"),
    pytest.param({"custom_properties": [{"value": "x"}]}, f"custom_properties (item 1, id): {POSITIVE}, got null", id="id-missing"),
    pytest.param({"custom_properties": [{"id": 5}]}, f"custom_properties (item 1, value): {EMPTY_VALUE}", id="value-missing"),
    pytest.param({"custom_properties": [{"id": 5, "value": 42}]}, "custom_properties (item 1, value): must be text, got a number (a number or a date is written as text)", id="value-number"),
    pytest.param({"custom_properties": [{"id": 5, "value": True}]}, "custom_properties (item 1, value): must be text, got a boolean", id="value-true"),
    pytest.param({"custom_properties": [{"id": 5, "value": ["a"]}]}, "custom_properties (item 1, value): must be text, got a list of 1 item", id="value-list"),
    pytest.param({"custom_properties": [{"id": 5, "value": "x", "extra": 1}]}, "custom_properties (item 1): unknown field(s) extra; allowed: id, value", id="unknown-key"),
    pytest.param({"custom_properties": [{"id": 5, "value": "x", "Value": "y", "Id": 5}]}, "custom_properties (item 1): unknown field(s) Id, Value; allowed: id, value", id="pascal-keys-are-unknown"),
    pytest.param({"custom_properties": ["x"]}, "custom_properties (item 1): must be an object with id and value, got a string", id="item-text"),
    pytest.param({"custom_properties": [{"id": 5, "value": "x"}, 7]}, "custom_properties (item 2): must be an object with id and value, got a number", id="second-item-number"),
    pytest.param({"custom_properties": [None]}, "custom_properties (item 1): must be an object with id and value, got null", id="item-null"),
    pytest.param({"custom_properties": [[5, "x"]]}, "custom_properties (item 1): must be an object with id and value, got a list of 2 items", id="item-list"),
    pytest.param({"custom_properties": "5=x"}, "custom_properties: expected a list of objects with id and value, got a string", id="not-a-list-text"),
    pytest.param({"custom_properties": {"id": 5, "value": "x"}}, "custom_properties: expected a list of objects with id and value, got an object", id="not-a-list-object"),
    pytest.param({"custom_properties": 5}, "custom_properties: expected a list of objects with id and value, got a number", id="not-a-list-number"),
    pytest.param({"clear_custom_property_ids": [True]}, f"clear_custom_property_ids[0]: {POSITIVE}, got a boolean", id="clear-true"),
    pytest.param({"clear_custom_property_ids": [3, "5"]}, f"clear_custom_property_ids[1]: {POSITIVE}, got a string", id="clear-digit-string"),
    pytest.param({"clear_custom_property_ids": "5"}, "clear_custom_property_ids: expected a list of ids such as [123, 456], got a string", id="clear-not-a-list"),
]


@pytest.mark.parametrize("overrides, fragment", DIRECT_CUSTOM_REFUSALS)
async def test_update_client_refuses_directly_what_the_schema_normally_pre_empts(
    client_factory, mock_gorelo, overrides, fragment
):
    with pytest.raises(ToolError) as caught:
        await call_update_directly(client_factory, client_id=9101, **overrides)
    assert fragment in str(caught.value)
    assert mock_gorelo.requests == []


async def test_a_refusal_never_echoes_the_value_the_caller_gave(client_factory, mock_gorelo):
    with pytest.raises(ToolError) as caught:
        await call_update_directly(
            client_factory, client_id=9101, custom_properties=[{"id": 5, "value": "SECRET-VALUE", "bad": "SECRET-KEY-VALUE"}]
        )
    assert "SECRET-VALUE" not in str(caught.value) and "SECRET-KEY-VALUE" not in str(caught.value)
    with pytest.raises(ToolError) as caught:
        await call_update_directly(client_factory, client_id=9101, custom_properties=[{"id": "SECRET-ID", "value": "x"}])
    assert "SECRET-ID" not in str(caught.value)
    assert mock_gorelo.requests == []


# -- the second read must show the custom fields as they were sent ------------------------------------------------------------
#
# After the PATCH the tool reads the client again (the same GET as before the write, so the same rendering) and runs its checks
# on that read; the PATCH answer is another rendering that was never compared with a read, and only has to be an object.
# route_update serves the record of each case below as that second read.

UNCONFIRMED_ANSWERS = [
    pytest.param(SAVE_5, client_record(CustomProperties=[]), "no entry for custom field(s) 5", id="empty-list"),
    pytest.param(SAVE_5, client_with_custom_properties(saved=[6]), "no entry for custom field(s) 5", id="another-field-only"),
    pytest.param(SAVE_5_CLEAR_7, client_with_custom_properties(), "no entry for custom field(s) 5, 7", id="both-missing"),
    pytest.param(SAVE_5_CLEAR_7, client_with_custom_properties(cleared=[7]), "no entry for custom field(s) 5", id="the-saved-one-missing"),
    pytest.param(SAVE_5_CLEAR_7, client_with_custom_properties(saved=[5]), "no entry for custom field(s) 7", id="the-cleared-one-missing"),
    pytest.param(SAVE_5, client_with_custom_properties(cleared=[5]), "IsSet is not true for custom field(s) 5, which were sent with a value", id="saved-but-not-set"),
    pytest.param(CLEAR_7, client_with_custom_properties(saved=[7]), "IsSet is not false for custom field(s) 7, which were sent to be cleared", id="cleared-but-still-set"),
    pytest.param(
        SAVE_5_CLEAR_7,
        client_with_custom_properties(saved=[7], cleared=[5]),
        "IsSet is not true for custom field(s) 5, which were sent with a value; "
        "IsSet is not false for custom field(s) 7, which were sent to be cleared",
        id="both-inverted",
    ),
    pytest.param(
        {"custom_properties": [{"id": 5, "value": "a"}, {"id": 6, "value": "b"}], "clear_custom_property_ids": [7, 8]},
        client_with_custom_properties(saved=[5, 7], cleared=[6, 9]),
        "no entry for custom field(s) 8; IsSet is not true for custom field(s) 6, which were sent with a value; "
        "IsSet is not false for custom field(s) 7, which were sent to be cleared",
        id="several-things-wrong-each-field-named",
    ),
    # A saved field is confirmed by IsSet true AND a Value that is non-blank text (this tool never sends an empty value, so an
    # empty one is not what was sent); a non-blank Value is not compared with what was sent.
    pytest.param(SAVE_5, client_record(CustomProperties=[custom_property(5, value=None)]), NO_VALUE_5, id="saved-value-null"),
    pytest.param(SAVE_5, client_record(CustomProperties=[custom_property(5, value="")]), NO_VALUE_5, id="saved-value-empty"),
    pytest.param(SAVE_5, client_record(CustomProperties=[custom_property(5, value=" \t\n")]), NO_VALUE_5, id="saved-value-blank"),
    pytest.param(SAVE_5, client_record(CustomProperties=[{"Id": 5, "IsSet": True}]), NO_VALUE_5, id="saved-value-missing"),
    pytest.param(
        {"custom_properties": [{"id": 41, "value": "Gold"}]},
        client_record(CustomProperties=[{"Id": 41, "Value": "", "IsSet": True}]),
        "custom field(s) 41 show no text value, although they were sent with one",
        id="is-set-true-but-the-value-is-empty",
    ),
    pytest.param(
        SAVE_5,
        client_record(CustomProperties=[custom_property(5, is_set=False, value="")]),
        "IsSet is not true for custom field(s) 5, which were sent with a value",
        id="not-set-and-no-value-is-one-problem",
    ),
    pytest.param(
        {"custom_properties": [{"id": 5, "value": "a"}, {"id": 6, "value": "b"}]},
        client_record(CustomProperties=[custom_property(5), custom_property(6, value=None)]),
        "custom field(s) 6 show no text value, although they were sent with one",
        id="the-second-saved-field-has-no-value",
    ),
    pytest.param(
        {"custom_properties": [{"id": 5, "value": "a"}, {"id": 6, "value": "b"}]},
        client_record(CustomProperties=[custom_property(6, value=None), custom_property(5, value="")]),
        "custom field(s) 5, 6 show no text value, although they were sent with one",
        id="both-saved-fields-have-no-value",
    ),
    pytest.param(
        {"custom_properties": [{"id": 5, "value": "a"}, {"id": 6, "value": "b"}], "clear_custom_property_ids": [7]},
        client_record(CustomProperties=[custom_property(5, value=""), custom_property(6, is_set=False), custom_property(7)]),
        "IsSet is not true for custom field(s) 6, which were sent with a value; "
        "custom field(s) 5 show no text value, although they were sent with one; "
        "IsSet is not false for custom field(s) 7, which were sent to be cleared",
        id="no-value-and-the-other-problems-together",
    ),
]

UNCONFIRMED_TEXT = (
    "Gorelo returned an unexpected response for update_client: PATCH /v1/clients/{{clientId}}: the update was sent, but a "
    "read of the client after it does not confirm the custom fields that were sent ({detail}). Gorelo answered with "
    "success; the update may have been applied, so verify it with a read before repeating it (get_client(client_id=9101))"
)


@pytest.mark.parametrize("arguments, record, detail", UNCONFIRMED_ANSWERS)
async def test_update_client_refuses_a_second_read_that_does_not_show_the_custom_fields_as_sent(
    server, mock_gorelo, arguments, record, detail
):
    route_update(mock_gorelo, arguments, envelope(record))
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **arguments})
    assert text == UNCONFIRMED_TEXT.format(detail=detail)
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH", "GET"]  # the tool never repeats the write
    assert "SECRET-VALUE" not in text  # a value is never echoed
    assert "The update was applied" not in text  # it answered with success; whether it took effect is for a read to say


async def test_the_unconfirmed_custom_property_error_is_the_house_write_unconfirmed_shape_error(client_factory, mock_gorelo):
    route_update(mock_gorelo, SAVE_5, envelope(client_record()), after=client_record(CustomProperties=[]))
    with pytest.raises(ToolError) as caught:
        await call_update_directly(client_factory, client_id=9101, **SAVE_5)
    cause = caught.value.__cause__
    assert isinstance(cause, GoreloAPIError)
    assert (cause.kind, cause.write_unconfirmed, cause.status, cause.op_key) == ("shape", True, 200, "PATCH /v1/clients/{clientId}")
    assert cause.notifications == [] and cause.trace_id is None
    assert "verify it with a read before repeating it (get_client(client_id=9101))" in str(cause)


async def test_update_client_accepts_a_second_read_that_shows_each_field_as_sent_and_lists_others(server, mock_gorelo):
    answer = client_record(
        CustomProperties=[
            custom_property(3, value="untouched"),
            custom_property(5),
            custom_property(7, is_set=False, value="the field's default"),
            custom_property(9, is_set=False, value=None),
        ]
    )
    # the fields that were not sent (3 and 9) come back exactly as the read before the write showed them
    prior = [custom_property(3, value="untouched"), custom_property(5, is_set=False, value=None), custom_property(7),
             custom_property(9, is_set=False, value=None)]
    route_update(mock_gorelo, SAVE_5_CLEAR_7, envelope(answer), prior=prior)
    result = await call_tool(server, "update_client", {"client_id": 9101, **SAVE_5_CLEAR_7})
    assert result == answer
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH", "GET"]


@pytest.mark.parametrize(
    "sent, shown",
    [("2026-10-14", "14/10/2026"), ("1", "1.0"), ("yes", "Yes"), ("a  b", "a b"), ("Gold", "Silver")],
)
async def test_update_client_does_not_compare_a_non_blank_value_because_gorelo_shows_display_text(
    server, mock_gorelo, sent, shown
):
    # A number or a date may come back normalised, so a non-blank Value is not compared with what was sent (the last pair
    # is not even close: an ignored update on a field that held no value reads the same as a new one, which is why the
    # update_client text tells the model to compare the returned Values itself). A blank Value is another matter, see above.
    answer = client_record(CustomProperties=[custom_property(5, value=shown)])
    arguments = {"custom_properties": [{"id": 5, "value": sent}]}
    route_update(mock_gorelo, arguments, envelope(answer))
    result = await call_tool(server, "update_client", {"client_id": 9101, **arguments})
    assert result == answer


@pytest.mark.parametrize(
    "data",
    [client_record(), {"Id": 9101}, client_record(CustomProperties=None), client_record(CustomProperties="odd"), client_record(CustomProperties=[])],
)
async def test_update_client_checks_no_custom_property_when_none_was_sent(server, mock_gorelo, data):
    # a plain update behaves as it did before custom properties: whatever the answer says about them is passed through
    mock_gorelo.on("PATCH", CLIENT_9101, envelope(data))
    assert await call_tool(server, "update_client", {"client_id": 9101, "name": "N"}) == data
    assert [r.method for r in mock_gorelo.requests] == ["PATCH"]


@pytest.mark.parametrize(
    "patch_answer",
    [
        pytest.param({"Id": 9101}, id="only-an-id"),
        pytest.param(client_record(), id="no-custom-properties"),
        pytest.param(client_record(CustomProperties=None), id="null-custom-properties"),
        pytest.param(client_record(CustomProperties=[]), id="no-custom-fields-at-all"),
        pytest.param(client_record(CustomProperties=[custom_property(5, is_set=False, value=None)]), id="the-field-looks-unset-here"),
        pytest.param(client_record(CustomProperties="odd"), id="not-a-list"),
    ],
)
async def test_the_patch_answer_is_another_rendering_and_proves_nothing_either_way(server, mock_gorelo, patch_answer):
    # A correct write must not raise the alarm because the PATCH answer has no CustomProperties or renders a field another
    # way; only the second read, which has the rendering of the first, is compared with it.
    second = client_with_custom_properties(saved=[5])
    route_update(mock_gorelo, SAVE_5, envelope(patch_answer), after=second)
    assert await call_tool(server, "update_client", {"client_id": 9101, **SAVE_5}) == second
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH", "GET"]


async def test_the_second_read_is_what_the_tool_returns_not_the_patch_answer(server, mock_gorelo):
    first = [custom_property(5, is_set=False, value=None), custom_property(6, value="kept", Name="Notes")]
    second = client_record(CustomProperties=[custom_property(5, value="Gold"), custom_property(6, value="kept", Name="Notes")], Name="Renamed")
    stale = client_record(Name="a stale rendering", CustomProperties=[custom_property(5)])
    route_update(mock_gorelo, {"name": "Renamed", **SAVE_5}, envelope(stale), prior=first, after=second)
    result = await call_tool(server, "update_client", {"client_id": 9101, "name": "Renamed", **SAVE_5})
    assert result == second and result["Name"] == "Renamed"


async def test_a_wipe_that_the_patch_answer_does_not_show_is_found_by_the_second_read(server, mock_gorelo):
    # production applied the PATCH as a replace; its answer still lists everything as it was
    arguments = {"custom_properties": [{"id": 41, "value": "Gold"}]}
    still_there = answer_after(SAVED_41)
    wiped = answer_after({**SAVED_41, 42: {"IsSet": False, "Value": None}, 44: {"IsSet": False, "Value": None}})
    route_update(mock_gorelo, arguments, envelope(still_there), prior=BEFORE, after=wiped)
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **arguments})
    assert f'custom field 42 ("{OWNER}") was set to "Alpha", now reads unset' in text
    assert f'custom field 44 ("{NOTES}") was set to "Keep me", now reads unset' in text


# -- Gorelo's own errors: the tool names the parameter, the item and the custom field, then Gorelo's own name ---------------
#
# Gorelo counts the items of the one CustomProperties array that was sent: the saved entries first, then the cleared ones.
# That its number is the 0-based position in the array that was sent was never probed, so every label ends with Gorelo's own
# name, without brackets ("; Gorelo: CustomProperties 1 Value"), so that a reader can see Gorelo's own PropertyName. The field
# map cannot know which list an item belongs to, so update_client relabels the PropertyName itself. A value is never named
# (SAVE_5 carries one that must not show up), a custom field id is.

TWO_SAVED = {"custom_properties": [{"id": 5, "value": "a"}, {"id": 9, "value": "b"}]}
TWO_CLEARED = {"clear_custom_property_ids": [7, 8]}
TWO_AND_TWO = {**TWO_SAVED, **TWO_CLEARED}  # on the wire: item 0 is field 5, 1 is 9 (saved), 2 is 7, 3 is 8 (cleared)
BOTH_NAMES = "custom_properties or clear_custom_property_ids"


async def rejected(server, mock_gorelo, arguments, *notes, status=400):
    """The error text of an update_client call that Gorelo answers with `notes`, each (PropertyName, message)."""
    route_update(mock_gorelo, arguments, error_envelope(status, [("070101", message, prop) for prop, message in notes]))
    return await call_tool_error(server, "update_client", {"client_id": 9101, **arguments})


LABELS = [
    # an index that fits: the parameter, the item, the custom field, Gorelo's sub-field, then Gorelo's own name
    pytest.param(TWO_AND_TWO, "CustomProperties[0].Value", "custom_properties (item 1, custom field 5, Value; Gorelo: CustomProperties 0 Value)", id="first-saved"),
    pytest.param(TWO_AND_TWO, "CustomProperties[1].Value", "custom_properties (item 2, custom field 9, Value; Gorelo: CustomProperties 1 Value)", id="second-saved"),
    pytest.param(TWO_AND_TWO, "CustomProperties[1].Id", "custom_properties (item 2, custom field 9, Id; Gorelo: CustomProperties 1 Id)", id="second-saved-id"),
    pytest.param(TWO_AND_TWO, "CustomProperties[2].Value", "clear_custom_property_ids (item 1, custom field 7, Value; Gorelo: CustomProperties 2 Value)", id="first-cleared"),
    pytest.param(TWO_AND_TWO, "CustomProperties[3].Value", "clear_custom_property_ids (item 2, custom field 8, Value; Gorelo: CustomProperties 3 Value)", id="second-cleared"),
    pytest.param(TWO_AND_TWO, "CustomProperties[3]", "clear_custom_property_ids (item 2, custom field 8; Gorelo: CustomProperties 3)", id="second-cleared-without-a-field"),
    pytest.param(TWO_AND_TWO, "customproperties[3].value", "clear_custom_property_ids (item 2, custom field 8, value; Gorelo: customproperties 3 value)", id="lower-case"),
    pytest.param(TWO_AND_TWO, "CUSTOMPROPERTIES[0].VALUE", "custom_properties (item 1, custom field 5, VALUE; Gorelo: CUSTOMPROPERTIES 0 VALUE)", id="upper-case"),
    pytest.param(TWO_AND_TWO, " CustomProperties[1].Value ", "custom_properties (item 2, custom field 9, Value; Gorelo: CustomProperties 1 Value)", id="spaces-around-the-name"),
    pytest.param(TWO_AND_TWO, "CustomProperties[1].Value.Text", "custom_properties (item 2, custom field 9, Value.Text; Gorelo: CustomProperties 1 Value Text)", id="deeper-path"),
    pytest.param(TWO_SAVED, "CustomProperties[1].Value", "custom_properties (item 2, custom field 9, Value; Gorelo: CustomProperties 1 Value)", id="two-saved-only"),
    pytest.param(TWO_CLEARED, "CustomProperties[1].Value", "clear_custom_property_ids (item 2, custom field 8, Value; Gorelo: CustomProperties 1 Value)", id="two-cleared-only"),
    pytest.param(SAVE_5, "CustomProperties[0].Value", "custom_properties (item 1, custom field 5, Value; Gorelo: CustomProperties 0 Value)", id="one-saved"),
    pytest.param(CLEAR_7, "CustomProperties[0].Value", "clear_custom_property_ids (item 1, custom field 7, Value; Gorelo: CustomProperties 0 Value)", id="one-cleared"),
    # an index that fits with a tail that is not a plain dotted name: placed all the same, the tail only in Gorelo's name
    pytest.param(SAVE_5_CLEAR_7, "CustomProperties[1].", "clear_custom_property_ids (item 1, custom field 7; Gorelo: CustomProperties 1)", id="a-cleared-entry-with-a-trailing-dot"),
    pytest.param(TWO_AND_TWO, "CustomProperties[2].Value[1]", "clear_custom_property_ids (item 1, custom field 7; Gorelo: CustomProperties 2 Value 1)", id="an-index-in-the-tail"),
    pytest.param(TWO_AND_TWO, "CustomProperties[2][0]", "clear_custom_property_ids (item 1, custom field 7; Gorelo: CustomProperties 2 0)", id="a-second-index"),
    pytest.param(TWO_AND_TWO, "CustomProperties[0].Value[1].Text", "custom_properties (item 1, custom field 5; Gorelo: CustomProperties 0 Value 1 Text)", id="an-index-inside-a-longer-tail"),
    # no usable item number: several entries sent cannot be told apart, so the sent ids are listed
    pytest.param(TWO_AND_TWO, "CustomProperties.Value", f"{BOTH_NAMES} (Value of one of custom fields 5, 9, 7, 8; Gorelo: CustomProperties Value)", id="no-index-and-a-tail"),
    pytest.param(TWO_AND_TWO, "CustomProperties", f"{BOTH_NAMES} (one of custom fields 5, 9, 7, 8; Gorelo: CustomProperties)", id="bare-array"),
    pytest.param(TWO_AND_TWO, "CustomProperties[]", f"{BOTH_NAMES} (one of custom fields 5, 9, 7, 8; Gorelo: CustomProperties)", id="empty-index"),
    pytest.param(TWO_AND_TWO, "CustomProperties[].Value", f"{BOTH_NAMES} (Value of one of custom fields 5, 9, 7, 8; Gorelo: CustomProperties Value)", id="empty-index-with-value"),
    pytest.param(TWO_AND_TWO, "CustomProperties[x]", f"{BOTH_NAMES} (one of custom fields 5, 9, 7, 8; Gorelo: CustomProperties x)", id="index-is-text"),
    pytest.param(TWO_AND_TWO, "CustomProperties[-1].Value", f"{BOTH_NAMES} (one of custom fields 5, 9, 7, 8; Gorelo: CustomProperties -1 Value)", id="negative-index"),
    pytest.param(TWO_AND_TWO, "CustomProperties[1.5]", f"{BOTH_NAMES} (one of custom fields 5, 9, 7, 8; Gorelo: CustomProperties 1 5)", id="decimal-index"),
    pytest.param(TWO_AND_TWO, "CustomProperties[0", f"{BOTH_NAMES} (one of custom fields 5, 9, 7, 8; Gorelo: CustomProperties 0)", id="unclosed-index"),
    pytest.param(TWO_AND_TWO, "Value", f"{BOTH_NAMES} (Value of one of custom fields 5, 9, 7, 8; Gorelo: Value)", id="bare-value"),
    pytest.param(TWO_SAVED, "Value", "custom_properties (Value of one of custom fields 5, 9; Gorelo: Value)", id="two-saved-bare-value"),
    pytest.param(TWO_CLEARED, "Value", "clear_custom_property_ids (Value of one of custom fields 7, 8; Gorelo: Value)", id="two-cleared-bare-value"),
    pytest.param(TWO_CLEARED, "CustomProperties", "clear_custom_property_ids (one of custom fields 7, 8; Gorelo: CustomProperties)", id="two-cleared-bare-array"),
    # a bare Id may be the client's own: the command has an Id too, bound from the route, so it is never pinned on a field
    pytest.param(TWO_AND_TWO, "Id", "Id (client_id, or one of custom fields 5, 9, 7, 8; Gorelo: Id)", id="bare-id"),
    pytest.param(SAVE_5, "Id", "Id (client_id, or one of custom fields 5; Gorelo: Id)", id="bare-id-one-entry"),
    pytest.param(TWO_AND_TWO, "id", "id (client_id, or one of custom fields 5, 9, 7, 8; Gorelo: id)", id="bare-id-lower-case"),
    # one entry sent: a name with no usable item number can only be that entry
    pytest.param(SAVE_5, "Value", "custom_properties (item 1, custom field 5, Value; Gorelo: Value)", id="one-saved-bare-value"),
    pytest.param(SAVE_5, "CustomProperties", "custom_properties (item 1, custom field 5; Gorelo: CustomProperties)", id="one-saved-bare-array"),
    pytest.param(SAVE_5, "CustomProperties.Value", "custom_properties (item 1, custom field 5, Value; Gorelo: CustomProperties Value)", id="one-saved-no-index-and-a-tail"),
    pytest.param(CLEAR_7, "Value", "clear_custom_property_ids (item 1, custom field 7, Value; Gorelo: Value)", id="one-cleared-bare-value"),
    pytest.param(CLEAR_7, "CustomProperties", "clear_custom_property_ids (item 1, custom field 7; Gorelo: CustomProperties)", id="one-cleared-bare-array"),
    # an index past the entries keeps Gorelo's own number under the parameter(s) that were sent; it never crashes
    pytest.param(TWO_AND_TWO, "CustomProperties[4].Value", f"{BOTH_NAMES} (item 5, Value; Gorelo: CustomProperties 4 Value)", id="past-the-end-both-lists"),
    pytest.param(TWO_AND_TWO, "CustomProperties[4]", f"{BOTH_NAMES} (item 5; Gorelo: CustomProperties 4)", id="past-the-end-without-a-field"),
    pytest.param(TWO_AND_TWO, "CustomProperties[4].Value[1]", f"{BOTH_NAMES} (item 5; Gorelo: CustomProperties 4 Value 1)", id="past-the-end-with-an-index-in-the-tail"),
    pytest.param(SAVE_5, "CustomProperties[1].Value", "custom_properties (item 2, Value; Gorelo: CustomProperties 1 Value)", id="past-the-end-saved-only"),
    pytest.param(CLEAR_7, "CustomProperties[3].Value", "clear_custom_property_ids (item 4, Value; Gorelo: CustomProperties 3 Value)", id="past-the-end-cleared-only"),
    pytest.param(TWO_AND_TWO, "CustomProperties[999999999999999999].Id", f"{BOTH_NAMES} (item 1000000000000000000, Id; Gorelo: CustomProperties 999999999999999999 Id)", id="past-the-end-18-digits"),
    # more than 18 digits is no item number: past the end, never converted
    pytest.param(TWO_AND_TWO, "CustomProperties[1234567890123456789].Value", f"{BOTH_NAMES} (item past the end, Value; Gorelo: CustomProperties 1234567890123456789 Value)", id="past-the-end-19-digits"),
    pytest.param(SAVE_5, "CustomProperties[1234567890123456789]", "custom_properties (item past the end; Gorelo: CustomProperties 1234567890123456789)", id="past-the-end-19-digits-saved-only"),
]


@pytest.mark.parametrize("arguments, property_name, label", LABELS)
async def test_update_client_names_the_parameter_the_item_and_the_custom_field_of_a_gorelo_error(
    server, mock_gorelo, arguments, property_name, label
):
    text = await rejected(server, mock_gorelo, arguments, (property_name, "Value is not valid."))
    assert text == trace(f"Gorelo rejected update_client (HTTP 400, code 070101): {label}: Value is not valid.")
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH"]  # the read, then the one write
    assert "SECRET-VALUE" not in text  # a custom field id is named, a value never is
    # the model-facing label has no bracket (the shared error text would try to read it as a path), and it ends with
    # Gorelo's own name, so the number the tool read as a position can always be checked
    shown = text.split("): ", 1)[1].rsplit(": Value is not valid.", 1)[0]
    assert "[" not in shown and "]" not in shown and shown.count("; Gorelo: ") == 1 and shown.endswith(")")


def test_the_raw_name_is_the_name_gorelo_sent_with_brackets_and_dots_turned_into_spaces():
    assert clients_module._gorelo_name("CustomProperties[1].Value") == "CustomProperties 1 Value"
    assert clients_module._gorelo_name("CustomProperties[2][0]") == "CustomProperties 2 0"
    assert clients_module._gorelo_name("CustomProperties[1].") == "CustomProperties 1"
    assert clients_module._gorelo_name("CustomProperties[]") == "CustomProperties"
    assert clients_module._gorelo_name("Value") == "Value"
    assert clients_module._gorelo_name(" Custom  Properties\n[x] ") == "Custom Properties x"
    long = clients_module._gorelo_name("CustomProperties[" + "9" * 5000 + "].Value")
    assert len(long) == clients_module._RAW_NAME_MAX and long.endswith("...") and "[" not in long


@pytest.mark.parametrize(
    "property_name, nothing_but",
    [
        ("CustomProperties[0].Id", "CustomProperties 0 Id"),
        ("CustomProperties[0].Value", "CustomProperties 0 Value"),
        ("CustomProperties[0]", "CustomProperties 0"),
        ("CustomProperties", "CustomProperties"),
        ("Value", "Value"),
        ("Id", "Id"),
    ],
)
async def test_a_call_with_one_entry_shows_gorelos_raw_name_in_the_label_and_keeps_its_message_and_trace(
    server, mock_gorelo, property_name, nothing_but
):
    arguments = {"custom_properties": [{"id": 9_000_000_000_000, "value": "MCPTEST-20991231000000"}]}
    text = await rejected(server, mock_gorelo, arguments, (property_name, "The custom field does not exist."))
    assert text.startswith("Gorelo rejected update_client (HTTP 400, code 070101): ")
    detail = text.partition("): ")[2]  # what Gorelo said, after the lead
    assert detail.startswith(("custom_properties (item 1, custom field 9000000000000", "Id (client_id"))
    assert f"Gorelo: {nothing_but})" in detail
    assert detail.endswith(f"The custom field does not exist. [trace {TEST_TRACE_ID}]")
    assert "MCPTEST-20991231000000" not in text  # the value that was sent is never named


@pytest.mark.parametrize(
    "property_name, shown",
    [
        ("CustomPropertiesX", "CustomPropertiesX"),
        ("CustomProperties_Value", "CustomProperties_Value"),
        ("Variable", "Variable"),
        ("Name", "name"),
        ("StatusId", "status_id"),
        ("clientId", "client_id"),
    ],
)
async def test_update_client_leaves_a_name_that_is_not_about_the_array_to_the_generic_text(
    server, mock_gorelo, property_name, shown
):
    # nothing is guessed about a name that does not start with CustomProperties as a name of its own
    text = await rejected(server, mock_gorelo, TWO_AND_TWO, (property_name, "Rejected."))
    assert text == trace(f"Gorelo rejected update_client (HTTP 400, code 070101): {shown}: Rejected.")


async def test_update_client_relabels_each_notification_by_itself_and_keeps_the_others(server, mock_gorelo):
    text = await rejected(
        server,
        mock_gorelo,
        {"name": "N", **TWO_AND_TWO},
        ("CustomProperties[1].Value", "Bad value."),
        ("Name", "Name is too long."),
        ("CustomProperties[3].Value", "Required."),
        ("StatusId", "Unknown status."),
        ("Value", "Also bad."),
        ("CustomProperties[2][0]", "Odd."),
    )
    assert text == trace(
        "Gorelo rejected update_client (HTTP 400, code 070101): "
        "custom_properties (item 2, custom field 9, Value; Gorelo: CustomProperties 1 Value): Bad value.; "
        "name: Name is too long.; "
        "clear_custom_property_ids (item 2, custom field 8, Value; Gorelo: CustomProperties 3 Value): Required.; "
        "status_id: Unknown status.; "
        f"{BOTH_NAMES} (Value of one of custom fields 5, 9, 7, 8; Gorelo: Value): Also bad.; "
        "clear_custom_property_ids (item 1, custom field 7; Gorelo: CustomProperties 2 0): Odd."
    )


async def test_update_client_shows_gorelos_refusal_to_clear_a_required_field_against_the_cleared_id(server, mock_gorelo):
    note = "Custom field 'Tier' is required and cannot be left without a value."
    text = await rejected(server, mock_gorelo, {**SAVE_5, **CLEAR_7}, ("CustomProperties[1].Value", note))
    assert text == trace(
        "Gorelo rejected update_client (HTTP 400, code 070101): "
        f"clear_custom_property_ids (item 1, custom field 7, Value; Gorelo: CustomProperties 1 Value): {note}"
    )
    assert mock_gorelo.last.json == {"CustomProperties": [{"Id": 5, "Value": "SECRET-VALUE"}, {"Id": 7, "Value": None}]}


async def test_update_client_does_not_relabel_when_no_custom_property_was_sent(server, mock_gorelo):
    # the generic text keeps its map: custom_properties is the array's parameter
    text = await rejected(server, mock_gorelo, {"name": "N"}, ("CustomProperties[0].Value", "Rejected."))
    assert text == trace("Gorelo rejected update_client (HTTP 400, code 070101): custom_properties (item 1, Value): Rejected.")


@pytest.mark.parametrize("status, kind", [(400, "http"), (404, "http"), (200, "envelope")])
async def test_a_relabelled_error_keeps_its_kind_status_trace_flag_codes_and_messages(
    client_factory, mock_gorelo, status, kind
):
    notes = [("070101", "Bad value.", "CustomProperties[1].Value"), ("070102", "Other.", "Name")]
    route_update(mock_gorelo, TWO_AND_TWO, error_envelope(status, notes))
    with pytest.raises(ToolError) as caught:
        await call_update_directly(client_factory, client_id=9101, name="N", **TWO_AND_TWO)
    relabelled = caught.value.__cause__
    original = relabelled.__cause__
    assert isinstance(relabelled, GoreloAPIError) and isinstance(original, GoreloAPIError) and relabelled is not original
    for attribute in ("message", "status", "op_key", "kind", "trace_id", "write_unconfirmed"):
        assert getattr(relabelled, attribute) == getattr(original, attribute), attribute
    assert (relabelled.status, relabelled.kind, relabelled.trace_id) == (status, kind, TEST_TRACE_ID)
    assert relabelled.write_unconfirmed is False  # Gorelo says nothing was applied
    assert [(n["code"], n["message"]) for n in relabelled.notifications] == [("070101", "Bad value."), ("070102", "Other.")]
    assert [n["property"] for n in relabelled.notifications] == [
        "custom_properties (item 2, custom field 9, Value; Gorelo: CustomProperties 1 Value)",
        "Name",
    ]
    assert [n["property"] for n in original.notifications] == ["CustomProperties[1].Value", "Name"]  # the original is untouched
    text = str(caught.value)
    assert text.endswith(f"[trace {TEST_TRACE_ID}]")
    assert "may have applied the change" not in text


@pytest.mark.parametrize("status", [400, 500])
async def test_an_absurd_index_keeps_the_status_the_trace_and_the_advice_of_the_error(server, mock_gorelo, status):
    # int() refuses more than 4300 digits: neither the tool's relabelling nor the shared error text may turn Gorelo's error
    # into a crash, or the model loses the status, the trace and the advice to verify a write with a read
    huge = "9" * 5000
    text = await rejected(
        server, mock_gorelo, TWO_AND_TWO,
        (f"CustomProperties[{huge}].Value", "Bad."),
        (f"Name[{huge}].First", "Also bad."),  # a name the tool does not touch: the shared text keeps it as Gorelo wrote it
        status=status,
    )
    assert text.endswith(f"[trace {TEST_TRACE_ID}]")
    # the raw name is cut at 80 characters: "CustomProperties " and 60 nines, then three dots
    label = f"{BOTH_NAMES} (item past the end, Value; Gorelo: CustomProperties " + "9" * 60 + "...)"
    assert f"{label}: Bad." in text
    assert f"Name[{huge}].First: Also bad." in text
    if status == 400:
        assert "(HTTP 400, code 070101)" in text and "verify it with a read" not in text.lower()
    else:  # a 5xx may have been applied: the error that lists what the fields held, with the same advice
        assert "Gorelo answered HTTP 500 (code 070101): " in text and "Before the update this tool read" in text
        assert "may or may not have been applied, so verify it with a read before repeating it" in text


def test_relabelling_returns_the_error_itself_when_there_is_nothing_to_rename():
    about_name = GoreloAPIError(
        "m", status=400, op_key=clients_module.UPDATE_OP, kind="http", notifications=[{"Code": "1", "Message": "x", "PropertyName": "Name"}]
    )
    assert clients_module._relabel_custom_property_errors(about_name, [5], [7]) is about_name
    no_notifications = GoreloAPIError("t", kind="timeout", write_unconfirmed=True)
    assert clients_module._relabel_custom_property_errors(no_notifications, [5], [7]) is no_notifications
    about_array = GoreloAPIError("m", status=400, kind="http", notifications=[{"Code": "1", "Message": "x", "PropertyName": "Value"}])
    assert clients_module._relabel_custom_property_errors(about_array, [], []) is about_array  # nothing was sent for it
    assert clients_module._relabel_custom_property_errors(about_array, [5], []) is not about_array


@pytest.mark.parametrize(
    "prop",
    [
        pytest.param(None, id="none"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param(5, id="a-number"),
        pytest.param(["Value"], id="a-list"),
        pytest.param({"Value": 1}, id="an-object"),
        pytest.param("Variable", id="another-nested-name"),
        pytest.param("Name", id="another-field"),
        pytest.param("CustomPropertiesX", id="a-longer-name"),
        pytest.param("CustomProperties_Value", id="a-longer-name-with-an-underscore"),
        pytest.param("Custom Properties[0].Value", id="a-space-in-the-name"),
        pytest.param("XCustomProperties[0].Value", id="a-longer-name-in-front"),
        pytest.param("Properties[0].Value", id="a-shorter-name"),
    ],
)
def test_a_property_name_that_is_not_about_the_array_has_no_label(prop):
    assert clients_module._custom_property_label(prop, [5], [7]) is None


@pytest.mark.parametrize("digits", [18, 19, 4300, 4301, 5000, 50000])
def test_an_index_of_any_length_gets_a_short_label_and_is_never_converted_past_18_digits(digits):
    label = clients_module._custom_property_label("CustomProperties[" + "9" * digits + "].Value", [5], [7])
    assert label is not None and len(label) < 200 and "[" not in label
    assert ("item past the end" in label) is (digits > 18)
    assert ("item 1000000000000000000" in label) is (digits == 18)


def test_a_long_tail_is_left_out_of_the_label_and_only_the_cut_raw_name_carries_it():
    prop = "CustomProperties[0].Value" + ".x" * 3000
    label = clients_module._custom_property_label(prop, [5], [7])
    assert label is not None and len(label) < 200 and "[" not in label
    assert label.startswith("custom_properties (item 1, custom field 5; Gorelo: CustomProperties 0 Value x x x") and label.endswith("...)")


def test_the_cap_on_an_item_number_is_the_one_of_the_shared_error_text():
    assert clients_module._INDEX_DIGITS_MAX == common_module._PATH_INDEX_DIGITS_MAX == 18


async def test_update_client_on_a_missing_client_with_custom_properties_fails_at_the_read_and_writes_nothing(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/99999", error_envelope(404, [("070404", "Client not found.")]))
    text = await call_tool_error(server, "update_client", {"client_id": 99999, **SAVE_5})
    assert text == trace("Gorelo rejected update_client (HTTP 404, code 070404): Client not found.")
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", "/v1/clients/99999")]


# -- the read before the write: ids must be listed there, other custom fields must come back unchanged ------------------------
#
# The published text says CustomProperties is an upsert (a field left out keeps its value), but no live call has shown it: if
# production applied it as a replace, an update of one field would silently wipe the others. So update_client reads the client
# first (only when it is about to write a custom field), refuses an id the client does not have before anything is sent, and
# compares the answer with what it read. The previous values are shown on purpose when something else changed (they are the
# user's own data and are needed to restore them); the values the caller sent are never echoed (SAVE_41 carries one).

SAVE_41 = {"custom_properties": [{"id": 41, "value": "SECRET-VALUE"}]}
OWNER, SEATS, NOTES = "Contract owner", "Seats", "Notes"
BEFORE = [  # what the client holds before the call: 41 unset, 42 set, 43 unset (its default shows), 44 set
    custom_property(41, is_set=False, value=None, Name="Region"),
    custom_property(42, value="Alpha", Name=OWNER),
    custom_property(43, is_set=False, value="dflt", Name=SEATS),
    custom_property(44, value="Keep me", Name=NOTES),
]


def answer_after(changes=None, *, drop=(), add=(), **extra):
    """BEFORE as an answer: each field in `changes` ({id: keys to override}) changed, each id in `drop` left out, `add` appended."""
    rows = [{**row, **(changes or {}).get(row["Id"], {})} for row in BEFORE if row["Id"] not in drop]
    return client_record(CustomProperties=rows + list(add), **extra)


SAVED_41 = {41: {"IsSet": True, "Value": "Gold"}}  # the field the call sets, as a read of the client shows it


def route_read(mock_gorelo, spec, client_id=9101):
    """Serve only the read before the write (a test that must see no PATCH registers no PATCH route)."""
    mock_gorelo.on("GET", f"/v1/clients/{client_id}", spec)


async def test_custom_properties_are_written_between_two_reads_of_the_client_and_the_second_read_is_returned(server, mock_gorelo):
    arguments = {"custom_properties": [{"id": 41, "value": "Gold"}], "clear_custom_property_ids": [44]}
    after = answer_after({41: SAVED_41[41], 44: {"IsSet": False, "Value": None}})
    # the PATCH answer is another rendering that is never compared with a read: here it is not even a client record
    route_update(mock_gorelo, arguments, envelope({"Id": 9101}), prior=BEFORE, after=after)
    assert await call_tool(server, "update_client", {"client_id": 9101, **arguments}) == after  # the second read
    first, write, second = mock_gorelo.requests
    for read in (first, second):  # the same operation twice: the two renderings can be compared
        assert (read.method, read.path, read.raw_path, read.query, read.content) == ("GET", CLIENT_9101, CLIENT_9101, {}, b"")
    assert (write.method, write.path, write.raw_path, write.query) == ("PATCH", CLIENT_9101, CLIENT_9101, {})
    assert write.json == {"CustomProperties": [{"Id": 41, "Value": "Gold"}, {"Id": 44, "Value": None}]}


@pytest.mark.parametrize(
    "arguments",
    [{"name": "N"}, {"status_id": 2}, {"billing_name": "B", "alternate_name": "A"}, {"name": "N", "custom_properties": None}],
)
async def test_a_plain_update_sends_no_read(server, mock_gorelo, arguments):
    mock_gorelo.on("PATCH", CLIENT_9101, envelope(client_record()))
    await call_tool(server, "update_client", {"client_id": 9101, **arguments})
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("PATCH", CLIENT_9101)]


async def test_the_plain_fields_travel_with_the_custom_ones_after_one_read(server, mock_gorelo):
    arguments = {"name": "N", **SAVE_41}
    route_update(mock_gorelo, arguments, envelope(answer_after(SAVED_41, Name="N")), prior=BEFORE)
    await call_tool(server, "update_client", {"client_id": 9101, **arguments})
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH", "GET"]
    assert mock_gorelo.calls("PATCH")[-1].json == {"Name": "N", "CustomProperties": [{"Id": 41, "Value": "SECRET-VALUE"}]}


@pytest.mark.parametrize(
    "spec, text",
    [
        pytest.param(error_envelope(404, [("070404", "Client not found.")]), trace("Gorelo rejected update_client (HTTP 404, code 070404): Client not found."), id="not-found"),
        pytest.param(error_envelope(500, [("070101", "Something broke.")]), trace("Gorelo rejected update_client (HTTP 500, code 070101): Something broke."), id="server-error"),
        pytest.param(
            error_envelope(403, [("080203", "API key does not have 'Clients' scope")]),
            trace("Gorelo rejected update_client (HTTP 403, code 080203): the API key does not have the 'Clients' scope. Grant it on the API key in Gorelo, then retry"),
            id="scope",
        ),
        pytest.param(
            httpx.ReadTimeout("slow"),
            "Gorelo did not answer update_client (the request timed out). This was a read, so retrying is safe.",
            id="timeout",
        ),
        pytest.param(
            envelope(None),
            "Gorelo returned an unexpected response for update_client: GET /v1/clients/{clientId}: "
            "Gorelo reported success but Data is null; refusing to guess",
            id="success-without-data",
        ),
    ],
)
@pytest.mark.parametrize("arguments", [SAVE_41, CLEAR_7, {"name": "N", **SAVE_41}])
async def test_a_read_that_fails_raises_its_own_error_and_nothing_is_written(server, mock_gorelo, spec, text, arguments):
    route_read(mock_gorelo, spec)  # no PATCH route: a PATCH would fail the test
    assert await call_tool_error(server, "update_client", {"client_id": 9101, **arguments}) == text
    assert [r.method for r in mock_gorelo.requests] == ["GET"]
    assert "applied" not in text  # a read: there is nothing that may have been applied


UNREADABLE = (
    "Gorelo returned an unexpected response for update_client: GET /v1/clients/{{clientId}}: {detail}; nothing was written, "
    "because the state of the custom fields before the update is unknown"
)


def without(row, key):
    return {name: value for name, value in row.items() if name != key}


UNREADABLE_CLIENTS = [
    pytest.param(envelope({}), "expected Data to be a non-empty object but got an empty object", id="data-empty"),
    pytest.param(envelope([]), "expected Data to be a non-empty object but got an empty list", id="data-a-list"),
    pytest.param(envelope("x"), "expected Data to be a non-empty object but got a string", id="data-text"),
    pytest.param(envelope(client_record()), "the client record has no CustomProperties", id="no-custom-properties"),
    pytest.param(envelope(client_record(CustomProperties=None)), "the client's CustomProperties is null, not a list", id="null"),
    pytest.param(envelope(client_record(CustomProperties={"Id": 41})), "the client's CustomProperties is an object, not a list", id="an-object"),
    pytest.param(envelope(client_record(CustomProperties="41")), "the client's CustomProperties is a string, not a list", id="text"),
    pytest.param(envelope(client_record(CustomProperties=[None])), "CustomProperties item 1 is null, not an object", id="a-null-item"),
    pytest.param(envelope(client_record(CustomProperties=[custom_property(41), "x"])), "CustomProperties item 2 is a string, not an object", id="a-text-item"),
    pytest.param(envelope(client_record(CustomProperties=[[41]])), "CustomProperties item 1 is a list of 1 item, not an object", id="a-list-item"),
    pytest.param(envelope(client_record(CustomProperties=[without(custom_property(41), "Id")])), "CustomProperties item 1 has no integer Id", id="no-id"),
    pytest.param(envelope(client_record(CustomProperties=[custom_property(41), custom_property("42")])), "CustomProperties item 2 has no integer Id", id="id-as-text"),
    pytest.param(envelope(client_record(CustomProperties=[custom_property(41.0)])), "CustomProperties item 1 has no integer Id", id="id-as-a-decimal"),
    pytest.param(envelope(client_record(CustomProperties=[custom_property(True)])), "CustomProperties item 1 has no integer Id", id="id-true"),
    pytest.param(envelope(client_record(CustomProperties=[custom_property(None)])), "CustomProperties item 1 has no integer Id", id="id-null"),
    pytest.param(envelope(client_record(CustomProperties=[custom_property(41), custom_property(41)])), "custom field 41 is listed more than once", id="an-id-twice"),
    pytest.param(envelope(client_record(CustomProperties=[custom_property(41, is_set=None)])), "custom field 41 has no boolean IsSet", id="is-set-null"),
    pytest.param(envelope(client_record(CustomProperties=[custom_property(41, is_set="true")])), "custom field 41 has no boolean IsSet", id="is-set-text"),
    pytest.param(envelope(client_record(CustomProperties=[custom_property(41, is_set=1)])), "custom field 41 has no boolean IsSet", id="is-set-one"),
    pytest.param(envelope(client_record(CustomProperties=[without(custom_property(41), "IsSet")])), "custom field 41 has no boolean IsSet", id="no-is-set"),
    pytest.param(envelope(client_record(CustomProperties=[custom_property(41, value=42)])), "custom field 41 has a Value that is neither text nor null", id="value-a-number"),
    pytest.param(envelope(client_record(CustomProperties=[custom_property(41, value=True)])), "custom field 41 has a Value that is neither text nor null", id="value-true"),
    pytest.param(envelope(client_record(CustomProperties=[custom_property(41, value=["x"])])), "custom field 41 has a Value that is neither text nor null", id="value-a-list"),
]


@pytest.mark.parametrize("arguments", [SAVE_41, CLEAR_7])
@pytest.mark.parametrize("spec, detail", UNREADABLE_CLIENTS)
async def test_a_read_that_cannot_be_used_raises_a_shape_error_and_nothing_is_written(server, mock_gorelo, spec, detail, arguments):
    route_read(mock_gorelo, spec)
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **arguments})
    assert text == UNREADABLE.format(detail=detail)
    assert [r.method for r in mock_gorelo.requests] == ["GET"]


async def test_an_unusable_read_is_a_shape_error_that_is_not_write_unconfirmed(client_factory, mock_gorelo):
    # nothing was sent, so there is nothing to verify: the flag stays off (an unconfirmed write would say "may have been applied")
    route_read(mock_gorelo, envelope(client_record()))
    with pytest.raises(ToolError) as caught:
        await call_update_directly(client_factory, client_id=9101, **SAVE_41)
    cause = caught.value.__cause__
    assert isinstance(cause, GoreloAPIError)
    assert (cause.kind, cause.write_unconfirmed, cause.status, cause.op_key) == ("shape", False, 200, "GET /v1/clients/{clientId}")
    assert "applied" not in str(caught.value) and "nothing was written" in str(caught.value)


async def test_a_client_with_no_custom_fields_has_an_empty_list_and_every_id_is_unknown(server, mock_gorelo):
    route_read(mock_gorelo, envelope(client_record(CustomProperties=[])))
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    assert text == "custom_properties (item 1, id): custom field 41 is not a custom field of client 9101 (get_client lists them)"
    assert [r.method for r in mock_gorelo.requests] == ["GET"]


UNKNOWN_ID = "custom field {id} is not a custom field of client 9101 (get_client lists them)"


@pytest.mark.parametrize(
    "arguments, text",
    [
        pytest.param(
            {"custom_properties": [{"id": 9000000000000, "value": "x"}]},
            "custom_properties (item 1, id): " + UNKNOWN_ID.format(id=9000000000000),
            id="saved",
        ),
        pytest.param(
            {"custom_properties": [{"id": 41, "value": "ok"}, {"id": 99, "value": "x"}]},
            "custom_properties (item 2, id): " + UNKNOWN_ID.format(id=99),
            id="second-saved",
        ),
        pytest.param({"clear_custom_property_ids": [777]}, "clear_custom_property_ids[0]: " + UNKNOWN_ID.format(id=777), id="cleared"),
        pytest.param(
            {"clear_custom_property_ids": [42, 777]}, "clear_custom_property_ids[1]: " + UNKNOWN_ID.format(id=777), id="second-cleared"
        ),
        pytest.param(
            {"custom_properties": [{"id": 98, "value": "x"}, {"id": 41, "value": "y"}, {"id": 97, "value": "z"}], "clear_custom_property_ids": [96, 42, 95]},
            "custom_properties (item 1, id): " + UNKNOWN_ID.format(id=98) + "; custom_properties (item 3, id): " + UNKNOWN_ID.format(id=97)
            + "; clear_custom_property_ids[0]: " + UNKNOWN_ID.format(id=96) + "; clear_custom_property_ids[2]: " + UNKNOWN_ID.format(id=95),
            id="every-unknown-id-in-one-message",
        ),
        pytest.param(
            {"name": "N", "status_id": 2, "custom_properties": [{"id": 98, "value": "x"}]},
            "custom_properties (item 1, id): " + UNKNOWN_ID.format(id=98),
            id="the-plain-fields-are-not-written-either",
        ),
    ],
)
async def test_an_id_the_client_does_not_have_is_refused_locally_before_anything_is_written(server, mock_gorelo, arguments, text):
    route_read(mock_gorelo, envelope(client_record(CustomProperties=BEFORE)))  # no PATCH route: a PATCH would fail the test
    assert await call_tool_error(server, "update_client", {"client_id": 9101, **arguments}) == text
    assert [r.method for r in mock_gorelo.requests] == ["GET"]


async def test_the_local_checks_come_before_the_read(server, mock_gorelo):
    # a bad id or a blank value is refused with no request at all, as before: the read is only for what passed them
    for arguments, fragment in (
        ({"custom_properties": [{"id": 0, "value": "x"}]}, "custom_properties (item 1, id): " + POSITIVE),
        ({"custom_properties": [{"id": 41, "value": " "}]}, "custom_properties (item 1, value): must be text"),
        ({"clear_custom_property_ids": [41], "custom_properties": [{"id": 41, "value": "x"}]}, "also in custom_properties"),
    ):
        assert fragment in await call_tool_error(server, "update_client", {"client_id": 9101, **arguments})
    assert mock_gorelo.requests == []


# -- the errors for an update that was sent and answered with success: what each one says ---------------------------------
#
# Three things can be wrong after a 2xx PATCH: the read of the client after it shows other custom fields changed or the
# sent ones not as sent; the answer to the PATCH cannot be used as the client record; the read after it fails or cannot be
# used. The read after runs whatever the PATCH answer looks like. When it fails or cannot be used, the error lists what
# every custom field held before (the first read), because nothing else holds those values any more if the update was
# applied as a replace.

CLOSING = (
    "Gorelo answered with success; the update may have been applied, so verify it with a read before repeating it "
    "(get_client(client_id=9101))"
)
ASK_FIRST = "ask before putting anything back: another user or an automation may have made the change. "
ONE_CALL_HEAD = (
    "To put the fields back, make ONE update_client call that lists every field to put back together with the values "
    "you meant to set in this call (a call for one field alone could wipe the others again): "
    "update_client(client_id=9101, {arguments})"
)
ONE_CALL_TAIL = ", to which you add the custom_properties and clear_custom_property_ids you meant to send in this call. "
KEEP_ONLY_WHAT_IS_SET = " (in clear_custom_property_ids keep only the fields that hold a value now)"
EMPTY_NOTE = "Custom field(s) {ids} were set to an empty value, which this tool cannot send: {fix} in the Gorelo app. "
REQUIRED_NOTE = "If Gorelo refuses to clear one of the unset fields (a required field), fix that one in the Gorelo app. "
ALSO = "The read after the update also does not confirm the fields that were sent ({problems}). "
ANSWER_ALSO = "The answer to the update also cannot be used as the client record: {detail}. "


def advice(set_before, unset_before, empty_before, *, from_the_first_read):
    """What follows the order to ask first: the ONE call (previous values filled in) and the notes for the Gorelo app.
    `set_before` is [(id, previous text)], `unset_before` and `empty_before` are lists of ids; `from_the_first_read` is
    true when it is not known which of those fields changed (the read after the update failed)."""
    arguments = []
    if set_before:
        entries = ", ".join('{"id": %d, "value": %s}' % (identifier, json.dumps(text)) for identifier, text in set_before)
        arguments.append(f"custom_properties=[{entries}]")
    if unset_before:
        arguments.append("clear_custom_property_ids=[" + ", ".join(str(identifier) for identifier in unset_before) + "]")
    text = ""
    if arguments:
        keep = KEEP_ONLY_WHAT_IS_SET if from_the_first_read and unset_before else ""
        text += ONE_CALL_HEAD.format(arguments=", ".join(arguments)) + keep + ONE_CALL_TAIL
    if empty_before:
        text += EMPTY_NOTE.format(
            ids=", ".join(str(identifier) for identifier in empty_before),
            fix="if they differ, fix them" if from_the_first_read else "fix them",
        )
    if unset_before:
        text += REQUIRED_NOTE
    return text


COLLATERAL = (
    "Gorelo returned an unexpected response for update_client: PATCH /v1/clients/{{clientId}}: the update was sent, but a "
    "read of the client after it does not show custom fields that were not in this call as they were before it "
    "({shown}). Tell the user at once, and " + ASK_FIRST + "{restore}{also}" + CLOSING
)
UNCHECKED = (
    "Gorelo returned an unexpected response for update_client: PATCH /v1/clients/{{clientId}}: the update was sent, but the "
    "custom fields cannot be checked: {why}. {answer}Before the update this tool read the client's custom fields: "
    "{states}. Compare that list with get_client(client_id=9101). {restore}" + CLOSING
)
IF_CHANGED = "If a custom field that was not in this call has changed, tell the user at once, and " + ASK_FIRST
BEFORE_STATES = (  # BEFORE, as the error lists it
    f'custom field 41 ("Region") was unset; custom field 42 ("{OWNER}") was set to "Alpha"; '
    f'custom field 43 ("{SEATS}") was unset; custom field 44 ("{NOTES}") was set to "Keep me"'
)


def unchecked(why, *, set_before=(), unset_before=(), empty_before=(), answer=None, unsent=True, states=BEFORE_STATES):
    """The whole text for a read after the update that failed or cannot be used, by default for the client of BEFORE.
    `unsent` is false when every custom field of the client was in the call (nothing else to put back)."""
    restore = ""
    if unsent:
        restore = IF_CHANGED + advice(set_before, unset_before, empty_before, from_the_first_read=True)
    return UNCHECKED.format(
        why=why,
        answer=ANSWER_ALSO.format(detail=answer) if answer else "",
        states=states,
        restore=restore,
    )


def collateral(shown, *, set_before=(), unset_before=(), empty_before=(), also=None, answer=None):
    """The whole text for fields that changed without being sent. `also` is what the read after the update says is wrong
    with the fields that were sent, `answer` what is wrong with the answer to the update."""
    after_the_advice = (ALSO.format(problems=also) if also else "") + (ANSWER_ALSO.format(detail=answer) if answer else "")
    restore = advice(set_before, unset_before, empty_before, from_the_first_read=False)
    return COLLATERAL.format(shown=shown, restore=restore, also=after_the_advice)


SECOND_READ_FAILS = [
    pytest.param(
        error_envelope(404, [("070404", "Client not found.")]), "the read of the client after it answered HTTP 404", id="not-found"
    ),
    pytest.param(
        error_envelope(500, [("070101", "Something broke.")]), "the read of the client after it answered HTTP 500", id="server-error"
    ),
    pytest.param(
        error_envelope(403, [("080203", "API key does not have 'Clients' scope")]),
        "the read of the client after it answered HTTP 403",
        id="scope",
    ),
    pytest.param(
        error_envelope(200, [("070101", "Odd.")]), "the read of the client after it reported a failure", id="a-failure-in-a-success-status"
    ),
    pytest.param(httpx.ReadTimeout("slow"), "the read of the client after it timed out", id="timeout"),
    pytest.param(httpx.ConnectError("down"), "the connection failed during the read of the client after it", id="connection"),
    pytest.param(envelope(None), "the read of the client after it returned an answer that cannot be used", id="success-without-data"),
]
# the calls, each with the fields of BEFORE that it leaves out, as the error puts them in its one call
SEND_42_AND_CLEAR_43 = {
    "custom_properties": [{"id": 42, "value": "SECRET-VALUE-2"}],
    "clear_custom_property_ids": [43],
}
SEND_EVERY_FIELD = {
    "custom_properties": [{"id": 41, "value": "SECRET-VALUE-3"}, {"id": 42, "value": "SECRET-VALUE-4"}],
    "clear_custom_property_ids": [43, 44],
}
UNCHECKED_CALLS = [
    pytest.param(SAVE_41, {"set_before": [(42, "Alpha"), (44, "Keep me")], "unset_before": [43]}, id="one-field-sent"),
    pytest.param(
        SEND_42_AND_CLEAR_43, {"set_before": [(44, "Keep me")], "unset_before": [41]}, id="a-saved-and-a-cleared-field-sent"
    ),
    pytest.param(SEND_EVERY_FIELD, {"unsent": False}, id="every-field-sent-so-nothing-else-to-put-back"),
]


@pytest.mark.parametrize("arguments, expected", UNCHECKED_CALLS)
@pytest.mark.parametrize("spec, why", SECOND_READ_FAILS)
async def test_a_read_after_the_write_that_fails_lists_what_every_custom_field_held_before(
    server, mock_gorelo, spec, why, arguments, expected
):
    route_update(mock_gorelo, arguments, envelope({"Id": 9101}), prior=BEFORE, second=spec)
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **arguments})
    assert text == unchecked(why, **expected)
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH", "GET"]  # the write is sent once, never repeated
    assert "SECRET-VALUE" not in text and "The update was applied" not in text  # what the caller sent is never echoed


@pytest.mark.parametrize("arguments, expected", UNCHECKED_CALLS[:2])
@pytest.mark.parametrize("spec, detail", UNREADABLE_CLIENTS)
async def test_a_read_after_the_write_that_cannot_be_used_lists_what_every_custom_field_held_before(
    server, mock_gorelo, spec, detail, arguments, expected
):
    # the same shapes that make the read before the write unusable (see above); here the update was already sent
    route_update(mock_gorelo, arguments, envelope({"Id": 9101}), prior=BEFORE, second=spec)
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **arguments})
    assert text == unchecked(f"the read of the client after it cannot be used: {detail}", **expected)
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH", "GET"]


async def test_the_list_of_what_the_fields_held_covers_every_field_in_the_order_of_the_first_read(server, mock_gorelo):
    prior = [
        custom_property(7, value="seven", Name=None),
        custom_property(5, is_set=False, value="a default", Name="Five"),
        custom_property(9, value="", Name="Nine"),
        custom_property(6, value='say "hi"  twice\t', Name='Six "6"'),
    ]
    arguments = {"custom_properties": [{"id": 7, "value": "SECRET-VALUE"}]}
    route_update(mock_gorelo, arguments, envelope({"Id": 9101}), prior=prior, second=httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **arguments})
    states = (
        'custom field 7 was set to "seven"; custom field 5 ("Five") was unset; custom field 9 ("Nine") was set to an '
        'empty value; custom field 6 ("Six \\"6\\"") was set to "say \\"hi\\" \\u0020twice\\t"'
    )
    assert f"read the client's custom fields: {states}. Compare that list with get_client(client_id=9101). " in text
    # the fields that were not in the call, with the one call built from the first read (field 9 holds nothing to send)
    assert (
        'update_client(client_id=9101, custom_properties=[{"id": 6, "value": "say \\"hi\\" \\u0020twice\\t"}], '
        "clear_custom_property_ids=[5])" + KEEP_ONLY_WHAT_IS_SET + ONE_CALL_TAIL
    ) in text
    assert "Custom field(s) 9 were set to an empty value, which this tool cannot send: if they differ, fix them" in text
    assert "SECRET-VALUE" not in text


@pytest.mark.parametrize(
    "arguments",
    [pytest.param(SAVE_41, id="a-saved-field"), pytest.param({"clear_custom_property_ids": [41]}, id="a-cleared-field")],
)
async def test_a_field_of_the_call_is_listed_with_what_it_held_but_never_put_in_the_call_to_restore(server, mock_gorelo, arguments):
    # the sent field is in the list (the model compares it too) and is not among the fields to put back: the call that
    # the model makes adds what it meant to set for it
    prior = [custom_property(41, value="before", Name="Region"), custom_property(42, value="Alpha", Name=OWNER)]
    route_update(mock_gorelo, arguments, envelope({"Id": 9101}), prior=prior, second=httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **arguments})
    assert 'custom field 41 ("Region") was set to "before"' in text
    assert 'custom_properties=[{"id": 42, "value": "Alpha"}])' in text and '"id": 41' not in text


async def test_unset_fields_alone_make_a_call_with_only_clear_custom_property_ids(server, mock_gorelo):
    prior = [custom_property(41, value="before", Name="Region"), custom_property(43, is_set=False, value="dflt", Name=SEATS)]
    route_update(mock_gorelo, SAVE_41, envelope({"Id": 9101}), prior=prior, second=httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    assert text == unchecked(
        "the read of the client after it timed out",
        states=f'custom field 41 ("Region") was set to "before"; custom field 43 ("{SEATS}") was unset',
        unset_before=[43],
    )
    assert "update_client(client_id=9101, clear_custom_property_ids=[43])" + KEEP_ONLY_WHAT_IS_SET in text


async def test_a_field_with_an_empty_value_alone_leaves_no_call_to_offer_only_the_note_for_the_gorelo_app(server, mock_gorelo):
    prior = [custom_property(41, value="before", Name="Region"), custom_property(45, value="", Name="Blank one")]
    route_update(mock_gorelo, SAVE_41, envelope({"Id": 9101}), prior=prior, second=httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    assert text == unchecked(
        "the read of the client after it timed out",
        states='custom field 41 ("Region") was set to "before"; custom field 45 ("Blank one") was set to an empty value',
        empty_before=[45],
    )
    assert "update_client(client_id=9101," not in text


async def test_the_unchecked_error_is_a_collateral_change_error_whose_log_text_holds_no_value(client_factory, mock_gorelo):
    route_update(mock_gorelo, SAVE_41, envelope({"Id": 9101}), prior=BEFORE, second=error_envelope(500, [("070101", "Something broke.")]))
    with pytest.raises(ToolError) as caught:
        await call_update_directly(client_factory, client_id=9101, **SAVE_41)
    error = caught.value.__cause__
    assert isinstance(error, clients_module.CollateralChangeError) and isinstance(error, GoreloAPIError)
    assert (error.kind, error.write_unconfirmed, error.status, error.op_key) == ("shape", True, 200, "PATCH /v1/clients/{clientId}")
    assert error.notifications == [] and error.trace_id is None
    assert "Alpha" in str(error) and "Keep me" in str(error)  # the model needs what the fields held ...
    assert error.log_summary == (  # ... the log gets the operation, the client and the field ids
        "PATCH /v1/clients/{clientId} on client 9101: the custom fields could not be checked after the update; "
        "previous states of custom field(s) 41, 42, 43, 44 withheld from the log"
    )
    for held in ("Alpha", "Keep me", OWNER, NOTES, SEATS, "Region", "SECRET-VALUE"):
        assert held not in error.log_summary
    read = error.__cause__  # the failure of the read after the update, for whoever debugs it
    assert isinstance(read, GoreloAPIError) and (read.status, read.op_key) == (500, "GET /v1/clients/{clientId}")


async def test_the_unchecked_error_of_a_read_that_cannot_be_used_has_no_cause_but_still_a_log_text(client_factory, mock_gorelo):
    route_update(mock_gorelo, SAVE_41, envelope({"Id": 9101}), prior=BEFORE, second=envelope(client_record()))
    with pytest.raises(ToolError) as caught:
        await call_update_directly(client_factory, client_id=9101, **SAVE_41)
    error = caught.value.__cause__
    assert isinstance(error, clients_module.CollateralChangeError) and error.__cause__ is None
    assert "the read of the client after it cannot be used: the client record has no CustomProperties" in str(error)
    assert error.log_summary.endswith("previous states of custom field(s) 41, 42, 43, 44 withheld from the log")


# -- a copy of an error that holds what custom fields held is the same error ---------------------------------------------------
#
# GoreloAPIError.__reduce__ rebuilds a plain GoreloAPIError, so a copy or a pickle of the alarm would keep the message (with
# the previous values) and lose log_summary: the log filter would then print the copy with its traceback. The alarm has a
# __reduce__ of its own that keeps the class and everything it carries.

COPIES = [
    pytest.param(copy.copy, id="copy"),
    pytest.param(copy.deepcopy, id="deepcopy"),
    *[
        pytest.param(lambda error, protocol=protocol: pickle.loads(pickle.dumps(error, protocol=protocol)), id=f"pickle-{protocol}")
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1)
    ],
]
LICENCE_KEY = "LICENCE-KEY-7f3a"


def errors_that_hold_previous_values():
    """Both errors that list what custom fields held, built the way update_client builds them."""
    held = clients_module._CustomField("Licence", True, LICENCE_KEY)
    changed = clients_module._collateral_error([(42, held, "now reads unset")], ["a problem"], 9101, "an answer problem")
    unchecked_ = clients_module._unchecked_error({42: held}, "the read of the client after it timed out", [41], None, 9101)
    failure = GoreloAPIError(
        "PATCH /v1/clients/{clientId}: HTTP 500: boom", status=500, op_key="PATCH /v1/clients/{clientId}", kind="http",
        notifications=[{"code": "070101", "message": "boom", "property": "Name"}], trace_id="00-trace", write_unconfirmed=True,
    )
    not_confirmed_ = clients_module._unconfirmed_patch_error({42: held}, failure, [41], 9101)
    return {"changed": changed, "unchecked": unchecked_, "not-confirmed": not_confirmed_}


@pytest.mark.parametrize("which", ["changed", "unchecked", "not-confirmed"])
@pytest.mark.parametrize("make_copy", COPIES)
def test_a_copy_of_an_error_that_holds_previous_values_keeps_its_class_its_log_text_and_everything_else(make_copy, which):
    error = errors_that_hold_previous_values()[which]
    assert LICENCE_KEY in str(error) and LICENCE_KEY not in error.log_summary
    error.trace_id = "00-trace"  # whatever is set after the construction travels too
    error.notifications.append({"code": "070101", "message": "m", "property": "p"})
    duplicate = make_copy(error)
    assert duplicate is not error and type(duplicate) is clients_module.CollateralChangeError
    assert duplicate.log_summary == error.log_summary and LICENCE_KEY not in duplicate.log_summary
    assert (str(duplicate), duplicate.message, duplicate.args) == (str(error), error.message, error.args)
    assert (duplicate.kind, duplicate.status, duplicate.op_key, duplicate.write_unconfirmed) == (
        "shape", {"changed": 200, "unchecked": 200, "not-confirmed": 500}[which], "PATCH /v1/clients/{clientId}", True
    )
    assert duplicate.trace_id == "00-trace" and duplicate.notifications == error.notifications
    assert duplicate.__dict__ == error.__dict__


@pytest.mark.parametrize("make_copy", COPIES)
def test_a_copy_of_a_plain_gorelo_error_is_still_a_plain_gorelo_error(make_copy):
    # the base class keeps its own __reduce__; the alarm's must not leak into it
    plain = GoreloAPIError("PATCH /v1/clients/{clientId}: boom", status=500, op_key="PATCH /v1/clients/{clientId}", kind="http")
    duplicate = make_copy(plain)
    assert type(duplicate) is GoreloAPIError and not hasattr(duplicate, "log_summary")
    assert (duplicate.message, duplicate.status, duplicate.kind) == (plain.message, 500, "http")


@pytest.mark.parametrize(
    "kind, status, why",
    [
        ("rate_limit", 429, "the read of the client after it was rate limited (HTTP 429)"),
        ("envelope", 200, "the read of the client after it reported a failure"),
        ("http", 502, "the read of the client after it answered HTTP 502"),
        ("http", None, "the read of the client after it failed"),
        ("spec", None, "the read of the client after it failed"),
        ("timeout", None, "the read of the client after it timed out"),
        ("transport", None, "the connection failed during the read of the client after it"),
        ("shape", 200, "the read of the client after it returned an answer that cannot be used"),
    ],
)
def test_the_failure_of_the_read_after_is_worded_without_anything_from_its_answer(kind, status, why):
    err = GoreloAPIError("SECRET-VALUE came back in the body", status=status, op_key="GET /v1/clients/{clientId}", kind=kind)
    assert clients_module._read_failure(err) == why


# -- the PATCH ends in a timeout, a connection error or a 5xx: Gorelo may have applied the update ---------------------------
#
# The failure says nothing about the custom fields, but the tool holds what every one of them held before (the first read),
# and under an update applied as a replace those values would be lost for good. So the error keeps the standard meaning (it
# may or may not have been applied: verify it with a read before repeating it) and lists them, like the error for a second
# read that fails. A 4xx refusal and a failure envelope are Gorelo saying that nothing was applied: they stay as they are.

TRACE_OF_AN_ENVELOPE = f" [trace {TEST_TRACE_ID}]"
NOT_CONFIRMED = (
    "Gorelo returned an unexpected response for update_client: PATCH /v1/clients/{{clientId}}: the update was sent, but "
    "Gorelo did not confirm it: {why}. Before the update this tool read the client's custom fields: {states}. "
    "Compare that list with get_client(client_id=9101). {restore}"
    "Gorelo did not confirm the update; it may or may not have been applied, so verify it with a read before repeating "
    "it (get_client(client_id=9101)){trace}"
)


def not_confirmed(why, *, trace="", set_before=(), unset_before=(), empty_before=(), unsent=True, states=BEFORE_STATES):
    """The whole text for a PATCH that ended in a failure Gorelo did not settle, for the client of BEFORE by default."""
    restore = ""
    if unsent:
        restore = IF_CHANGED + advice(set_before, unset_before, empty_before, from_the_first_read=True)
    return NOT_CONFIRMED.format(why=why, states=states, restore=restore, trace=trace)


PATCH_FAILURES = [
    pytest.param(httpx.ReadTimeout("slow"), "the request timed out", "", id="timeout"),
    pytest.param(httpx.ConnectError("down"), "the connection failed", "", id="connection-error"),
    pytest.param(
        error_envelope(500, [("070101", "Something broke.")]),
        "Gorelo answered HTTP 500 (code 070101): Something broke",
        TRACE_OF_AN_ENVELOPE,
        id="500-with-a-note",
    ),
    pytest.param(error_envelope(503, []), "Gorelo answered HTTP 503", TRACE_OF_AN_ENVELOPE, id="503-without-notes"),
    pytest.param(
        error_envelope(500, [("070101", "One."), ("070102", "Two."), ("070101", "Three."), ("070103", "Four.")]),
        "Gorelo answered HTTP 500 (code 070101/070102/070103): One.; Two.; Three.; and 1 more",
        TRACE_OF_AN_ENVELOPE,
        id="500-with-four-notes",
    ),
    pytest.param(
        httpx.Response(502, content=b"<html>bad gateway</html>", headers={"content-type": "text/html"}),
        "unexpected response shape; refusing to guess (HTTP 502, a body that is not JSON, content-type text/html, 24 bytes)",
        "",
        id="502-page",
    ),
    pytest.param(
        httpx.Response(504, content=b""),
        "unexpected response shape; refusing to guess (HTTP 504, an empty body, content-type no content-type, 0 bytes)",
        "",
        id="504-empty-body",
    ),
]


@pytest.mark.parametrize("arguments, expected", UNCHECKED_CALLS)
@pytest.mark.parametrize("spec, why, trace_text", PATCH_FAILURES)
async def test_a_patch_that_ends_without_an_answer_lists_what_every_custom_field_held_before(
    server, mock_gorelo, spec, why, trace_text, arguments, expected
):
    route_update(mock_gorelo, arguments, spec, prior=BEFORE)
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **arguments})
    assert text == not_confirmed(why, trace=trace_text, **expected)
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH"]  # sent once, never repeated, no read after it
    assert "SECRET-VALUE" not in text  # what the caller sent is never echoed
    assert "may or may not have been applied" in text and text.count("verify it with a read before repeating it") == 1


async def test_a_patch_that_ends_in_a_5xx_keeps_the_labels_of_gorelos_notifications(server, mock_gorelo):
    arguments = {"custom_properties": [{"id": 41, "value": "SECRET-VALUE"}], "clear_custom_property_ids": [44]}
    note = "Custom field 'Notes' is required."
    spec = error_envelope(500, [("070101", note, "CustomProperties[1].Value"), ("070102", "Other.", "Name")])
    route_update(mock_gorelo, arguments, spec, prior=BEFORE)
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **arguments})
    label = "clear_custom_property_ids (item 1, custom field 44, Value; Gorelo: CustomProperties 1 Value)"
    assert f"Gorelo did not confirm it: Gorelo answered HTTP 500 (code 070101/070102): {label}: {note}; Name: Other. " in text
    assert text.endswith(TRACE_OF_AN_ENVELOPE)


async def test_the_unconfirmed_patch_error_is_a_collateral_change_error_that_keeps_what_the_failure_carried(
    client_factory, mock_gorelo
):
    notes = [("070101", "Bad value.", "CustomProperties[1].Value"), ("070102", "Other.", "Name")]
    route_update(mock_gorelo, TWO_AND_TWO, error_envelope(500, notes))
    with pytest.raises(ToolError) as caught:
        await call_update_directly(client_factory, client_id=9101, name="N", **TWO_AND_TWO)
    error = caught.value.__cause__
    assert isinstance(error, clients_module.CollateralChangeError) and isinstance(error, GoreloAPIError)
    assert (error.kind, error.write_unconfirmed, error.status, error.op_key, error.trace_id) == (
        "shape", True, 500, "PATCH /v1/clients/{clientId}", TEST_TRACE_ID
    )
    assert [(n["code"], n["message"]) for n in error.notifications] == [("070101", "Bad value."), ("070102", "Other.")]
    assert [n["property"] for n in error.notifications] == [
        "custom_properties (item 2, custom field 9, Value; Gorelo: CustomProperties 1 Value)",
        "Name",
    ]
    original = error.__cause__  # the failure itself, as the client raised it
    assert isinstance(original, GoreloAPIError) and not isinstance(original, clients_module.CollateralChangeError)
    assert (original.kind, original.status, original.write_unconfirmed) == ("http", 500, True)
    assert [n["property"] for n in original.notifications] == ["CustomProperties[1].Value", "Name"]
    assert error.log_summary == (  # the kind of failure and the field ids, nothing Gorelo said
        "PATCH /v1/clients/{clientId} on client 9101: the update was not confirmed (HTTP 500); "
        "previous states of custom field(s) 5, 9, 7, 8 withheld from the log"
    )
    assert "Bad value" not in error.log_summary and "SECRET-VALUE" not in error.log_summary


@pytest.mark.parametrize(
    "spec, how, cause_kind",
    [
        pytest.param(httpx.ReadTimeout("slow"), "timeout", "timeout", id="timeout"),
        pytest.param(httpx.ConnectError("down"), "connection error", "transport", id="connection-error"),
        pytest.param(httpx.Response(502, content=b"<html>x</html>"), "HTTP 502", "shape", id="502-page"),
    ],
)
async def test_the_unconfirmed_patch_error_without_notifications_has_a_log_text_of_the_kind_only(
    client_factory, mock_gorelo, spec, how, cause_kind
):
    route_update(mock_gorelo, SAVE_41, spec, prior=BEFORE)
    with pytest.raises(ToolError) as caught:
        await call_update_directly(client_factory, client_id=9101, **SAVE_41)
    error = caught.value.__cause__
    assert isinstance(error, clients_module.CollateralChangeError) and error.write_unconfirmed is True
    assert error.notifications == [] and error.trace_id is None and error.__cause__.kind == cause_kind
    assert error.status == (None if cause_kind != "shape" else 502)
    assert error.log_summary == (
        f"PATCH /v1/clients/{{clientId}} on client 9101: the update was not confirmed ({how}); "
        "previous states of custom field(s) 41, 42, 43, 44 withheld from the log"
    )


@pytest.mark.parametrize("status", [200, 400, 404, 409, 422])
async def test_a_refusal_stays_the_standard_text_and_lists_nothing(server, mock_gorelo, status):
    # a 4xx and a failure envelope in a success status are Gorelo saying that nothing was applied
    text = await rejected(server, mock_gorelo, SAVE_41, ("CustomProperties[0].Value", "Bad."), status=status)
    assert text == trace(
        f"Gorelo rejected update_client (HTTP {status}, code 070101): "
        "custom_properties (item 1, custom field 41, Value; Gorelo: CustomProperties 0 Value): Bad."
    )
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH"]


async def test_a_refusal_is_not_a_collateral_change_error(client_factory, mock_gorelo):
    route_update(mock_gorelo, SAVE_41, error_envelope(400, [("070101", "Bad.")]))
    with pytest.raises(ToolError) as caught:
        await call_update_directly(client_factory, client_id=9101, **SAVE_41)
    assert type(caught.value.__cause__) is GoreloAPIError and not hasattr(caught.value.__cause__, "log_summary")


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param(httpx.ReadTimeout("slow"), id="timeout"),
        pytest.param(error_envelope(500, [("070101", "Something broke.")]), id="500"),
        pytest.param(httpx.Response(502, content=b"<html>x</html>"), id="502-page"),
    ],
)
async def test_a_plain_update_that_ends_without_an_answer_keeps_the_standard_text_and_reads_nothing(server, mock_gorelo, spec):
    mock_gorelo.on("PATCH", CLIENT_9101, spec)
    text = await call_tool_error(server, "update_client", {"client_id": 9101, "name": "N"})
    assert "Before the update this tool read" not in text and "Verify with a read before retrying" in text
    assert [r.method for r in mock_gorelo.requests] == ["PATCH"]


# -- an answer to the PATCH that cannot be used is one more problem in the text, not a stop ------------------------------------

EMPTY_BODY = "unexpected response shape; refusing to guess (an empty body, content-type no content-type, 0 bytes)"
NOT_AN_OBJECT = "expected Data to be a non-empty object but got {found}"
PATCH_ANSWERS = [  # what the PATCH answers with a 2xx status, and what update_client says is wrong with it
    pytest.param(envelope(None), NOT_AN_OBJECT.format(found="null"), id="data-null"),
    pytest.param(envelope({}), NOT_AN_OBJECT.format(found="an empty object"), id="data-an-empty-object"),
    pytest.param(envelope([]), NOT_AN_OBJECT.format(found="an empty list"), id="data-an-empty-list"),
    pytest.param(envelope([{"Id": 9101}]), NOT_AN_OBJECT.format(found="a list of 1 item"), id="data-a-list"),
    pytest.param(envelope("ok"), NOT_AN_OBJECT.format(found="a string"), id="data-a-string"),
    pytest.param(envelope(True), NOT_AN_OBJECT.format(found="a boolean"), id="data-a-boolean"),
    pytest.param(envelope(7), NOT_AN_OBJECT.format(found="a number"), id="data-a-number"),
    pytest.param(httpx.Response(200, content=b""), EMPTY_BODY, id="an-empty-body"),
    pytest.param(httpx.Response(204), EMPTY_BODY, id="no-content"),
    pytest.param(
        httpx.Response(200, content=b"<html>bad</html>", headers={"content-type": "text/html"}),
        "unexpected response shape; refusing to guess (a body that is not JSON, content-type text/html, 16 bytes)",
        id="a-page",
    ),
    pytest.param(
        httpx.Response(200, json=[1, 2]),
        "unexpected response shape; refusing to guess (JSON a list instead of an envelope, content-type application/json)",
        id="a-bare-list",
    ),
    pytest.param(
        httpx.Response(200, json={"title": "Odd"}),
        "unexpected response shape; refusing to guess (a JSON object without a boolean IsSuccess, content-type "
        "application/json) [title: Odd]",
        id="an-object-without-is-success",
    ),
]
ANSWER_ONLY = (
    "Gorelo returned an unexpected response for update_client: PATCH /v1/clients/{{clientId}}: the update was sent, and a "
    "read of the client after it shows no problem with the custom fields, but the answer to the update cannot be used as "
    "the client record: {detail}. " + CLOSING
)
PROBLEMS_ONLY = (
    "Gorelo returned an unexpected response for update_client: PATCH /v1/clients/{{clientId}}: the update was sent, but a "
    "read of the client after it does not confirm the custom fields that were sent ({problems}). {also}" + CLOSING
)
NOT_SET_41 = "IsSet is not true for custom field(s) 41, which were sent with a value"
FEW_ANSWERS = [PATCH_ANSWERS[0], PATCH_ANSWERS[7]]  # one Data that is not an object, one body with no envelope


@pytest.mark.parametrize("spec, detail", PATCH_ANSWERS)
async def test_an_answer_that_cannot_be_used_is_the_only_problem_when_the_second_read_is_fine(server, mock_gorelo, spec, detail):
    route_update(mock_gorelo, SAVE_41, spec, prior=BEFORE, after=answer_after(SAVED_41))
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    assert text == ANSWER_ONLY.format(detail=detail)
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH", "GET"]  # the second read ran: the answer is no stop
    assert "SECRET-VALUE" not in text and "The update was applied" not in text


@pytest.mark.parametrize("spec, detail", FEW_ANSWERS)
async def test_an_answer_that_cannot_be_used_comes_after_the_problems_with_the_sent_fields(server, mock_gorelo, spec, detail):
    route_update(mock_gorelo, SAVE_41, spec, prior=BEFORE, after=answer_after({}))  # 41 reads unset
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    assert text == PROBLEMS_ONLY.format(problems=NOT_SET_41, also=ANSWER_ALSO.format(detail=detail))
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH", "GET"]


@pytest.mark.parametrize("spec, detail", PATCH_ANSWERS)
async def test_an_answer_that_cannot_be_used_does_not_hide_a_wipe_the_second_read_shows(server, mock_gorelo, spec, detail):
    # the case this exists for: the update was applied as a replace and its answer says nothing; the first read holds
    # the values that are gone, so they are in the alarm
    wiped = answer_after({**SAVED_41, 42: {"IsSet": False, "Value": None}, 44: {"IsSet": False, "Value": None}})
    route_update(mock_gorelo, SAVE_41, spec, prior=BEFORE, after=wiped)
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    shown = f'{WIPED_42}; custom field 44 ("{NOTES}") was set to "Keep me", now reads unset'
    assert text == collateral(shown, set_before=[(42, "Alpha"), (44, "Keep me")], answer=detail)
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH", "GET"]


@pytest.mark.parametrize("spec, detail", PATCH_ANSWERS)
async def test_an_answer_that_cannot_be_used_is_listed_in_the_error_when_the_second_read_fails_too(server, mock_gorelo, spec, detail):
    route_update(mock_gorelo, SAVE_41, spec, prior=BEFORE, second=error_envelope(500, [("070101", "Something broke.")]))
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    assert text == unchecked(
        "the read of the client after it answered HTTP 500",
        set_before=[(42, "Alpha"), (44, "Keep me")],
        unset_before=[43],
        answer=detail,
    )
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH", "GET"]


@pytest.mark.parametrize("spec, detail", FEW_ANSWERS)
async def test_an_answer_that_cannot_be_used_is_listed_in_the_error_when_the_second_read_cannot_be_used(server, mock_gorelo, spec, detail):
    route_update(mock_gorelo, SAVE_41, spec, prior=BEFORE, second=envelope(client_record()))
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    assert text == unchecked(
        "the read of the client after it cannot be used: the client record has no CustomProperties",
        set_before=[(42, "Alpha"), (44, "Keep me")],
        unset_before=[43],
        answer=detail,
    )


async def test_an_answer_that_cannot_be_used_is_not_marked_when_there_is_nothing_to_withhold(client_factory, mock_gorelo):
    route_update(mock_gorelo, SAVE_41, envelope(None), prior=BEFORE, after=answer_after(SAVED_41))
    with pytest.raises(ToolError) as caught:
        await call_update_directly(client_factory, client_id=9101, **SAVE_41)
    error = caught.value.__cause__
    assert isinstance(error, GoreloAPIError) and not isinstance(error, clients_module.CollateralChangeError)
    assert (error.kind, error.write_unconfirmed, error.status, error.op_key) == ("shape", True, 200, "PATCH /v1/clients/{clientId}")
    assert not hasattr(error, "log_summary")


@pytest.mark.parametrize(
    "data, expected",
    [
        ({"Id": 9101}, None),
        ({"Id": 9101, "CustomProperties": None}, None),
        (None, "expected Data to be a non-empty object but got null"),
        ({}, "expected Data to be a non-empty object but got an empty object"),
        ([], "expected Data to be a non-empty object but got an empty list"),
        (["x", "y"], "expected Data to be a non-empty object but got a list of 2 items"),
        ("SECRET-VALUE", "expected Data to be a non-empty object but got a string"),
        (b"bytes", "expected Data to be a non-empty object but got binary data"),
    ],
)
def test_a_patch_answer_can_be_used_when_it_is_a_non_empty_object_and_is_never_quoted_when_it_is_not(data, expected):
    assert clients_module._unusable_patch_answer(data) == expected


def shape_error(status, *, kind="shape", op=clients_module.UPDATE_OP, message="unexpected response shape; refusing to guess (an empty body)"):
    return GoreloAPIError(f"{op}: {message}", status=status, op_key=op, kind=kind, write_unconfirmed=True)


@pytest.mark.parametrize(
    "err, expected",
    [
        pytest.param(shape_error(200), "unexpected response shape; refusing to guess (an empty body)", id="200"),
        pytest.param(shape_error(204), "unexpected response shape; refusing to guess (an empty body)", id="204"),
        pytest.param(shape_error(299), "unexpected response shape; refusing to guess (an empty body)", id="299"),
        pytest.param(shape_error(199), None, id="below-2xx"),
        pytest.param(shape_error(300), None, id="3xx"),
        pytest.param(shape_error(502), None, id="a-gateway-page-is-no-2xx"),
        pytest.param(shape_error(None), None, id="no-status"),
        pytest.param(shape_error(200, kind="envelope"), None, id="a-failure-envelope-is-a-refusal"),
        pytest.param(shape_error(200, kind="http"), None, id="http"),
        pytest.param(shape_error(200, kind="timeout"), None, id="timeout"),
        pytest.param(shape_error(200, op="GET /v1/clients/{clientId}"), None, id="another-operation"),
        pytest.param(shape_error(200, message="x   y\n z"), "x y z", id="whitespace-is-squeezed-as-in-the-error"),
    ],
)
def test_only_a_2xx_answer_with_no_gorelo_envelope_in_it_counts_as_an_answer_that_cannot_be_read(err, expected):
    assert clients_module._unreadable_patch_answer(err) == expected


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param(httpx.Response(200, content=b""), id="an-empty-body"),
        pytest.param(httpx.Response(200, json=[1]), id="a-bare-list"),
    ],
)
async def test_a_plain_update_with_an_answer_that_cannot_be_read_stays_an_error_with_no_read(server, mock_gorelo, spec):
    # no custom field is written, so there is nothing to check: the update behaves as it did before custom fields
    mock_gorelo.on("PATCH", CLIENT_9101, spec)
    text = await call_tool_error(server, "update_client", {"client_id": 9101, "name": "N"})
    assert text.startswith("Gorelo returned an unexpected response for update_client: PATCH /v1/clients/{clientId}: unexpected response shape")
    assert [r.method for r in mock_gorelo.requests] == ["PATCH"]


# (a) a saved field that was set before and still shows its previous value although a different text was sent

STILL = (
    "custom field(s) {ids} still show their previous value (if you wrote the same value another way, nothing is wrong; "
    "otherwise Gorelo ignored it)"
)
STILL_42 = STILL.format(ids="42")
SAVE_42 = {"custom_properties": [{"id": 42, "value": "Beta"}]}


def unconfirmed(detail):
    return UNCONFIRMED_TEXT.format(detail=detail)


@pytest.mark.parametrize(
    "arguments, changes, detail",
    [
        pytest.param(SAVE_42, {}, STILL_42, id="the-second-read-still-shows-the-previous-value"),
        pytest.param(SAVE_42, {42: {"Value": "Beta"}}, None, id="the-second-read-shows-the-new-text"),
        pytest.param(SAVE_42, {42: {"Value": "BETA"}}, None, id="the-second-read-shows-a-reformatted-text"),
        pytest.param(SAVE_42, {42: {"Value": "Alpha "}}, None, id="the-second-read-differs-from-the-previous-value-however-little"),
        pytest.param({"custom_properties": [{"id": 42, "value": "Alpha"}]}, {}, None, id="the-same-text-sent-again-is-no-change"),
        pytest.param({"custom_properties": [{"id": 43, "value": "Beta"}]}, {43: {"IsSet": True}}, None, id="a-field-that-was-unset-has-no-previous-value-to-keep"),
        pytest.param({"custom_properties": [{"id": 43, "value": "dflt"}]}, {43: {"IsSet": True}}, None, id="a-new-text-equal-to-the-default-is-fine"),
        pytest.param(
            {"custom_properties": [{"id": 42, "value": "Beta"}, {"id": 44, "value": "New"}]},
            {44: {"Value": "New"}},
            STILL_42,
            id="only-the-unchanged-one-is-named",
        ),
        pytest.param(
            {"custom_properties": [{"id": 42, "value": "Beta"}, {"id": 44, "value": "New"}]},
            {},
            STILL.format(ids="42, 44"),
            id="both-unchanged",
        ),
        pytest.param(
            SAVE_42,
            {42: {"IsSet": False}},
            "IsSet is not true for custom field(s) 42, which were sent with a value",
            id="not-set-is-reported-by-is-set-only",
        ),
        pytest.param(
            {"custom_properties": [{"id": 42, "value": "Beta"}], "clear_custom_property_ids": [44]},
            {},
            f"{STILL_42}; IsSet is not false for custom field(s) 44, which were sent to be cleared",
            id="with-the-other-problems-in-their-order",
        ),
    ],
)
async def test_a_saved_field_that_still_shows_its_previous_value_is_not_confirmed(server, mock_gorelo, arguments, changes, detail):
    answer = answer_after(changes)
    route_update(mock_gorelo, arguments, envelope({"Id": 9101}), prior=BEFORE, after=answer)  # the PATCH answer proves nothing
    if detail is None:
        assert await call_tool(server, "update_client", {"client_id": 9101, **arguments}) == answer
    else:
        text = await call_tool_error(server, "update_client", {"client_id": 9101, **arguments})
        assert text == unconfirmed(detail)
        assert "The update was applied" not in text
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH", "GET"]


async def test_a_previous_value_that_was_empty_is_not_a_value_to_keep(server, mock_gorelo):
    # the field held an empty text; a read that still shows nothing is "no text value", not "the previous value"
    prior = [custom_property(42, value="", Name=OWNER)]
    after = client_record(CustomProperties=[custom_property(42, value="")])
    route_update(mock_gorelo, SAVE_42, envelope({"Id": 9101}), prior=prior, after=after)
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_42})
    assert text == unconfirmed("custom field(s) 42 show no text value, although they were sent with one")


async def test_an_id_listed_twice_in_the_second_read_cannot_be_checked_whichever_row_is_right(server, mock_gorelo):
    rows = [custom_property(42), custom_property(42, is_set=False), *[r for r in BEFORE if r["Id"] != 42]]
    route_update(mock_gorelo, SAVE_42, envelope({"Id": 9101}), prior=BEFORE, after=client_record(CustomProperties=rows))
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_42})
    assert text == unchecked(
        "the read of the client after it cannot be used: custom field 42 is listed more than once",
        set_before=[(44, "Keep me")],
        unset_before=[41, 43],
    )


# (b) a field that was not in the call must come back as the first read showed it

WIPED_42 = f'custom field 42 ("{OWNER}") was set to "Alpha", now reads unset'
SET_43 = f'custom field 43 ("{SEATS}") was unset, now reads set'
CHANGED_44 = f'custom field 44 ("{NOTES}") was set to "Keep me", now shows a different value'
GONE_44 = f'custom field 44 ("{NOTES}") was set to "Keep me", now missing from that read'

@pytest.mark.parametrize(
    "answer, shown, set_before, unset_before",
    [
        pytest.param(answer_after({**SAVED_41, 42: {"IsSet": False, "Value": None}}), WIPED_42, [(42, "Alpha")], [], id="a-set-field-was-wiped"),
        pytest.param(answer_after({**SAVED_41, 43: {"IsSet": True}}), SET_43, [], [43], id="an-unset-field-was-set"),
        pytest.param(answer_after({**SAVED_41, 44: {"Value": "Changed"}}), CHANGED_44, [(44, "Keep me")], [], id="a-value-changed"),
        pytest.param(
            answer_after({**SAVED_41, 43: {"Value": "other default"}}),
            f'custom field 43 ("{SEATS}") was unset, now shows a different value',
            [],
            [43],
            id="the-value-of-an-unset-field-changed",
        ),
        pytest.param(answer_after(SAVED_41, drop=(44,)), GONE_44, [(44, "Keep me")], [], id="a-field-is-missing"),
        pytest.param(
            answer_after({**SAVED_41, 42: {"IsSet": False, "Value": None}, 43: {"IsSet": True}}),
            f"{WIPED_42}; {SET_43}",
            [(42, "Alpha")],
            [43],
            id="two-fields-in-the-order-of-the-read",
        ),
    ],
)
async def test_a_field_that_was_not_in_the_call_and_changed_is_one_loud_error_with_how_to_restore_it(
    server, mock_gorelo, answer, shown, set_before, unset_before
):
    # the PATCH answer is the unchanged client: only the read after the write shows what happened
    route_update(mock_gorelo, SAVE_41, envelope(answer_after(SAVED_41)), prior=BEFORE, after=answer)
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    assert text == collateral(shown, set_before=set_before, unset_before=unset_before)
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH", "GET"]
    assert "SECRET-VALUE" not in text  # the value the caller sent is never echoed
    assert "The update was applied" not in text and "may or may not have been applied" not in text


async def test_fields_the_second_read_adds_and_fields_that_come_back_unchanged_are_no_problem(server, mock_gorelo):
    answer = answer_after(SAVED_41, add=[custom_property(50, value="new field", Name="Added meanwhile")])
    route_update(mock_gorelo, SAVE_41, envelope({"Id": 9101}), prior=BEFORE, after=answer)
    assert await call_tool(server, "update_client", {"client_id": 9101, **SAVE_41}) == answer


async def test_the_fields_that_were_sent_are_not_collateral_whatever_they_show(server, mock_gorelo):
    # 41 and 44 were sent (one saved, one cleared): only the checks of sent fields apply to them, not the unchanged rule
    arguments = {"custom_properties": [{"id": 41, "value": "Gold"}], "clear_custom_property_ids": [44]}
    after = answer_after({**SAVED_41, 44: {"IsSet": False, "Value": "dflt"}})
    route_update(mock_gorelo, arguments, envelope({"Id": 9101}), prior=BEFORE, after=after)
    assert await call_tool(server, "update_client", {"client_id": 9101, **arguments}) == after


async def test_collateral_comes_first_when_the_sent_fields_have_problems_too(server, mock_gorelo):
    after = answer_after({41: {"IsSet": False}, 42: {"IsSet": False, "Value": None}})  # 41 not set, 42 wiped
    route_update(mock_gorelo, SAVE_41, envelope({"Id": 9101}), prior=BEFORE, after=after)
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    also = "IsSet is not true for custom field(s) 41, which were sent with a value"
    assert text == collateral(WIPED_42, set_before=[(42, "Alpha")], also=also)
    assert text.index("Tell the user at once") < text.index("also does not confirm")


async def test_a_second_read_that_lists_no_field_at_all_shows_every_field_not_sent_as_missing(server, mock_gorelo):
    route_update(mock_gorelo, SAVE_41, envelope(answer_after(SAVED_41)), prior=BEFORE, after=client_record(CustomProperties=[]))
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    shown = (
        f'custom field 42 ("{OWNER}") was set to "Alpha", now missing from that read; '
        f'custom field 43 ("{SEATS}") was unset, now missing from that read; '
        f'custom field 44 ("{NOTES}") was set to "Keep me", now missing from that read'
    )
    assert text == collateral(
        shown, set_before=[(42, "Alpha"), (44, "Keep me")], unset_before=[43], also="no entry for custom field(s) 41"
    )


async def test_a_set_field_with_an_empty_previous_value_cannot_be_restored_by_this_tool(server, mock_gorelo):
    prior = [*BEFORE, custom_property(45, value="", Name="Blank one")]
    after = client_record(CustomProperties=[*answer_after(SAVED_41)["CustomProperties"], custom_property(45, is_set=False, value=None, Name="Blank one")])
    route_update(mock_gorelo, SAVE_41, envelope({"Id": 9101}), prior=prior, after=after)
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    shown = 'custom field 45 ("Blank one") was set to an empty value, now reads unset'
    assert text == collateral(shown, empty_before=[45])  # nothing a call could put back: the Gorelo app it is
    assert "update_client(client_id=9101," not in text


async def test_an_empty_previous_value_is_left_out_of_the_one_restore_call_and_named_for_the_gorelo_app(server, mock_gorelo):
    prior = [*BEFORE, custom_property(45, value="", Name="Blank one")]
    rows = answer_after({**SAVED_41, 42: {"IsSet": False, "Value": None}})["CustomProperties"]
    after = client_record(CustomProperties=[*rows, custom_property(45, is_set=False, value=None, Name="Blank one")])
    route_update(mock_gorelo, SAVE_41, envelope({"Id": 9101}), prior=prior, after=after)
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    shown = f'{WIPED_42}; custom field 45 ("Blank one") was set to an empty value, now reads unset'
    assert text == collateral(shown, set_before=[(42, "Alpha")], empty_before=[45])
    assert '"id": 45' not in text


async def test_a_field_without_a_name_is_listed_by_its_id_alone(server, mock_gorelo):
    prior = [custom_property(41, is_set=False, value=None), custom_property(42, value="Alpha", Name=None)]
    after = client_record(CustomProperties=[custom_property(41), custom_property(42, is_set=False, value=None, Name=None)])
    route_update(mock_gorelo, SAVE_41, envelope({"Id": 9101}), prior=prior, after=after)
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    assert text == collateral('custom field 42 was set to "Alpha", now reads unset', set_before=[(42, "Alpha")])


@pytest.mark.parametrize(
    "previous, shown",
    [
        pytest.param("plain", '"plain"', id="plain"),
        pytest.param('say "hi"', '"say \\"hi\\""', id="quotes"),
        pytest.param("two\nlines\tand a tab", '"two\\nlines\\tand a tab"', id="newline-and-tab"),
        pytest.param("a  b", '"a \\u0020b"', id="two-spaces"),
        pytest.param("x     y", '"x \\u0020\\u0020\\u0020\\u0020y"', id="a-run-of-spaces"),
        pytest.param(" padded ", '" padded "', id="spaces-at-the-ends"),
        pytest.param("a\u00a0b", '"a\\u00a0b"', id="non-breaking-space"),
        pytest.param("\u00dcn\u00efc\u00f6d\u00e9 \u2713", '"\u00dcn\u00efc\u00f6d\u00e9 \u2713"', id="unicode"),
        pytest.param("back\\slash", '"back\\\\slash"', id="backslash"),
    ],
)
async def test_a_previous_value_is_shown_as_a_json_string_that_survives_the_error_text_unchanged(server, mock_gorelo, previous, shown):
    # GoreloAPIError squeezes whitespace; the value must still be restorable exactly, so it is written as a JSON string
    prior = [custom_property(41, is_set=False, value=None), custom_property(42, value=previous, Name=OWNER)]
    after = client_record(CustomProperties=[custom_property(41), custom_property(42, is_set=False, value=None, Name=OWNER)])
    route_update(mock_gorelo, SAVE_41, envelope({"Id": 9101}), prior=prior, after=after)
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    assert f'custom field 42 ("{OWNER}") was set to {shown}, now reads unset' in text
    quoted = text.split("was set to ", 1)[1].split(", now reads unset", 1)[0]
    assert json.loads(quoted) == previous  # a JSON parser reads the shown text back as the exact previous value
    assert f'custom_properties=[{{"id": 42, "value": {shown}}}]' in text  # and the same text is in the restore call


async def test_the_one_restore_call_names_the_client_and_lists_every_field_to_put_back(server, mock_gorelo):
    after = answer_after({**SAVED_41, 42: {"IsSet": False, "Value": None}, 43: {"IsSet": True}, 44: {"Value": "x"}})
    route_update(mock_gorelo, SAVE_41, envelope({"Id": 9101}), prior=BEFORE, after=after)
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    assert text.count("update_client(client_id=9101,") == 1  # ONE call, never one per field
    call = text.split("update_client(client_id=9101,", 1)[1].split(")), to which", 1)[0]
    assert 'custom_properties=[{"id": 42, "value": "Alpha"}, {"id": 44, "value": "Keep me"}]' in call
    assert "clear_custom_property_ids=[43]" in call
    assert "ask before putting anything back: another user or an automation may have made the change" in text
    assert "together with the values you meant to set in this call" in text
    assert "may or may not have been applied" not in text  # it is known: the update was sent and answered with success


async def test_the_restore_call_in_the_text_passes_the_tools_own_validation_with_the_exact_previous_values(server, mock_gorelo):
    previous = 'say "hi"  twice\t\n'
    prior = [*BEFORE[:3], custom_property(44, value=previous, Name=NOTES)]
    rows = answer_after({**SAVED_41, 42: {"IsSet": False, "Value": None}, 43: {"IsSet": True}, 44: {"IsSet": False, "Value": None}})
    route_update(mock_gorelo, SAVE_41, envelope({"Id": 9101}), prior=prior, after=rows)
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    found = re.search(r"update_client\(client_id=9101, custom_properties=(\[.*\]), clear_custom_property_ids=(\[[0-9, ]*\])\)", text)
    assert found, text
    entries, cleared = json.loads(found.group(1)), json.loads(found.group(2))
    assert entries == [{"id": 42, "value": "Alpha"}, {"id": 44, "value": previous}] and cleared == [43]
    assert clients_module._custom_property_changes(entries, cleared) == ([(42, "Alpha"), (44, previous)], [43])


async def test_the_collateral_error_is_a_write_unconfirmed_shape_error_about_the_patch(client_factory, mock_gorelo):
    after = answer_after({**SAVED_41, 42: {"IsSet": False, "Value": None}, 43: {"IsSet": True}})
    route_update(mock_gorelo, SAVE_41, envelope(answer_after(SAVED_41)), prior=BEFORE, after=after)
    with pytest.raises(ToolError) as caught:
        await call_update_directly(client_factory, client_id=9101, **SAVE_41)
    cause = caught.value.__cause__
    assert isinstance(cause, clients_module.CollateralChangeError) and isinstance(cause, GoreloAPIError)
    assert (cause.kind, cause.write_unconfirmed, cause.status, cause.op_key) == ("shape", True, 200, "PATCH /v1/clients/{clientId}")
    assert cause.notifications == [] and cause.trace_id is None
    assert str(caught.value).startswith("Gorelo returned an unexpected response for update_client: PATCH /v1/clients/{clientId}: ")
    # the text for the server log: the operation, the client and the field ids, and no value at all
    assert cause.log_summary == (
        "PATCH /v1/clients/{clientId} on client 9101: custom field(s) 42, 43 changed without being sent; "
        "previous values withheld from the log"
    )
    assert "Alpha" not in cause.log_summary and OWNER not in cause.log_summary


@pytest.mark.parametrize(
    "flow",
    ["collateral", "only-the-sent-fields", "cannot-be-checked", "only-the-answer", "answer-and-collateral", "answer-and-unchecked"],
)
async def test_every_error_after_a_sent_update_ends_with_the_same_sentence_and_never_says_it_was_applied(server, mock_gorelo, flow):
    wiped = answer_after({**SAVED_41, 42: {"IsSet": False, "Value": None}})
    if flow == "collateral":
        route_update(mock_gorelo, SAVE_41, envelope({"Id": 9101}), prior=BEFORE, after=wiped)
    elif flow == "only-the-sent-fields":
        route_update(mock_gorelo, SAVE_41, envelope({"Id": 9101}), prior=BEFORE, after=answer_after({}))
    elif flow == "cannot-be-checked":
        route_update(mock_gorelo, SAVE_41, envelope({"Id": 9101}), prior=BEFORE, second=httpx.ReadTimeout("slow"))
    elif flow == "only-the-answer":
        route_update(mock_gorelo, SAVE_41, envelope(None), prior=BEFORE, after=answer_after(SAVED_41))
    elif flow == "answer-and-collateral":
        route_update(mock_gorelo, SAVE_41, envelope(None), prior=BEFORE, after=wiped)
    else:
        route_update(mock_gorelo, SAVE_41, envelope(None), prior=BEFORE, second=httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    assert text.endswith(CLOSING)
    assert text.count("verify it with a read before repeating it") == 1  # the house sentence is not added a second time
    assert "The update was applied" not in text and "may or may not have been applied" not in text
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH", "GET"]


async def test_a_gorelo_error_of_the_write_is_still_relabelled_after_the_read(server, mock_gorelo):
    text = await rejected(server, mock_gorelo, {"custom_properties": [{"id": 41, "value": "x"}], "clear_custom_property_ids": [44]},
                          ("CustomProperties[1].Value", "Custom field 'Notes' is required."))
    assert text == trace(
        "Gorelo rejected update_client (HTTP 400, code 070101): clear_custom_property_ids (item 1, custom field 44, Value; "
        "Gorelo: CustomProperties 1 Value): Custom field 'Notes' is required."
    )


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param(error_envelope(400, [("070101", "Bad.")]), id="rejected"),
        pytest.param(error_envelope(500, [("070101", "Boom.")]), id="server-error"),
        pytest.param(httpx.ReadTimeout("slow"), id="timeout"),
        pytest.param(httpx.ConnectError("down"), id="connection"),
        pytest.param(httpx.Response(502, content=b"<html>bad gateway</html>"), id="a-gateway-page"),
        pytest.param(error_envelope(200, [("070101", "Refused in a success status.")]), id="a-failure-envelope-in-a-2xx"),
    ],
)
async def test_a_write_that_fails_is_not_followed_by_a_read(server, mock_gorelo, spec):
    route_update(mock_gorelo, SAVE_41, spec)  # no second read is served: asking for one would fail the test
    await call_tool_error(server, "update_client", {"client_id": 9101, **SAVE_41})
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH"]


async def test_the_description_says_the_client_is_read_first(server):
    tool = next(t for t in await list_tools(server) if t.name == "update_client")
    text = flat(tool.description)
    assert "It reads the client first: ids must be listed there, and other custom fields must come back unchanged." in text
    assert len(text) <= 700  # the cap for a core tool (tests/test_tools_meta.py)
    assert "GET" not in text  # the operation is declared, not explained to the model


# -- the records the other tools return carry (or leave out) the custom properties as Gorelo sends them ----------------


async def test_get_client_returns_the_custom_properties_of_the_record_unchanged(server, mock_gorelo):
    record = client_with_custom_properties(saved=[5], cleared=[7])
    mock_gorelo.on("GET", CLIENT_9101, envelope(record))
    result = await call_tool(server, "get_client", {"client_id": 9101})
    assert result == record
    assert [(row["Id"], row["IsSet"]) for row in result["CustomProperties"]] == [(5, True), (7, False)]
    assert mock_gorelo.last.query == {} and len(mock_gorelo.requests) == 1


async def test_list_clients_rows_are_passed_through_as_they_are_with_no_custom_properties_added(server, mock_gorelo):
    rows = [client_record(9102), client_record(9101)]
    mock_gorelo.on("GET", CLIENTS, paged_envelope(rows))
    result = await call_tool(server, "list_clients")
    assert result["items"] == rows and all("CustomProperties" not in row for row in result["items"])
    assert len(mock_gorelo.requests) == 1  # no GET of each client to fetch them


async def test_create_client_returns_the_record_with_its_custom_properties_unchanged(server, mock_gorelo):
    created = client_with_custom_properties(cleared=[5], client_id=9116, Name="MCPTEST-4")
    mock_gorelo.on("POST", CLIENTS, envelope(created))
    assert await call_tool(server, "create_client", {"name": "MCPTEST-4", "location_name": "HQ"}) == created
    assert mock_gorelo.last.json == {"Name": "MCPTEST-4", "Location": {"Name": "HQ"}}  # CreateClientCommand has none


# --------------------------------------------------------------------------
# list_client_locations
# --------------------------------------------------------------------------


async def test_list_client_locations_makes_a_plain_get_with_no_query_string(server, mock_gorelo):
    rows = [location_record(9001), location_record(9002, IsDefault=False, Name="Annex")]
    mock_gorelo.on("GET", LOCATIONS, envelope(rows))
    result = await call_tool(server, "list_client_locations", {"client_id": 9101})
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", LOCATIONS, {}, b"")
    assert "?" not in request.url  # the old pageSize=200 made Gorelo answer 400
    assert result == {"items": rows, "count": 2}


async def test_list_client_locations_returns_an_empty_list_as_an_empty_list(server, mock_gorelo):
    mock_gorelo.on("GET", LOCATIONS, envelope([]))
    assert await call_tool(server, "list_client_locations", {"client_id": 9101}) == {"items": [], "count": 0}


async def test_list_client_locations_has_no_paging_parameters(server):
    tool = next(t for t in await list_tools(server) if t.name == "list_client_locations")
    assert set(tool.inputSchema["properties"]) == {"client_id"}


async def test_list_client_locations_refuses_a_paged_looking_answer(server, mock_gorelo):
    mock_gorelo.on("GET", LOCATIONS, envelope({"Items": []}))
    text = await call_tool_error(server, "list_client_locations", {"client_id": 9101})
    assert "expected Data to be a list but got an object" in text


async def test_list_client_locations_maps_a_missing_client_to_a_tool_error(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/99999/locations", error_envelope(404, [("070404", "Client not found.")]))
    text = await call_tool_error(server, "list_client_locations", {"client_id": 99999})
    assert text == trace("Gorelo rejected list_client_locations (HTTP 404, code 070404): Client not found.")


@pytest.mark.parametrize("property_name", ["clientId", "ClientId"])
async def test_list_client_locations_maps_a_gorelo_error_about_the_id_to_client_id(server, mock_gorelo, property_name):
    mock_gorelo.on("GET", LOCATIONS, error_envelope(400, [("070101", "The id is not valid.", property_name)]))
    text = await call_tool_error(server, "list_client_locations", {"client_id": 9101})
    assert text == trace("Gorelo rejected list_client_locations (HTTP 400, code 070101): client_id: The id is not valid.")


@pytest.mark.parametrize("bad", [0, -1])
async def test_list_client_locations_rejects_an_id_that_cannot_exist_naming_client_id(server, mock_gorelo, bad):
    text = await call_tool_error(server, "list_client_locations", {"client_id": bad})
    assert text.startswith("client_id: expected a positive whole number")
    assert mock_gorelo.requests == []


# --------------------------------------------------------------------------
# Strict ids: JSON true, "5" and 5.0 are refused before the tool runs
# --------------------------------------------------------------------------

STRICT_CASES = [
    ("get_client", {"client_id": 9101}, "client_id"),
    ("list_client_locations", {"client_id": 9101}, "client_id"),
    ("update_client", {"client_id": 9101, "name": "N"}, "client_id"),
    ("update_client", {"client_id": 9101, "status_id": 2}, "status_id"),
]


@pytest.mark.parametrize("name, arguments, param", STRICT_CASES)
@pytest.mark.parametrize("bad", [True, False, "5", "abc", 5.0, 5.5, [5]])
async def test_a_single_id_is_strict_and_nothing_is_sent(server, mock_gorelo, name, arguments, param, bad):
    text = await call_tool_error(server, name, {**arguments, param: bad})
    assert param in text and "valid integer" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("name", ["get_client", "list_client_locations", "update_client"])
async def test_the_required_client_id_cannot_be_null(server, mock_gorelo, name):
    text = await call_tool_error(server, name, {"client_id": None, "name": "N"} if name == "update_client" else {"client_id": None})
    assert "client_id" in text and "valid integer" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", [[True], ["5"], [5.0], [1, True], "5", 5])
async def test_the_status_id_list_is_strict_item_by_item(server, mock_gorelo, bad):
    text = await call_tool_error(server, "list_clients", {"status_ids": bad})
    assert "status_ids" in text and ("valid integer" in text or "valid list" in text)
    assert mock_gorelo.requests == []


async def test_an_explicit_null_for_an_optional_id_is_still_accepted(server, mock_gorelo):
    # the schema no longer advertises null for optional parameters, but validation still accepts it
    mock_gorelo.on("PATCH", CLIENT_9101, envelope(client_record()))
    mock_gorelo.on("GET", CLIENTS, paged_envelope([client_record()]))
    await call_tool(server, "update_client", {"client_id": 9101, "name": "N", "status_id": None, "billing_name": None})
    assert mock_gorelo.last.json == {"Name": "N"}
    await call_tool(server, "list_clients", {"status_ids": None, "query": None, "cursor": None})
    assert mock_gorelo.last.query == {"PageSize": "200"}


async def test_the_optional_parameters_default_to_null_without_advertising_a_null_branch(server):
    # The server compacts every advertised schema (server.compact_input_schema, see test_schema_compaction.py): an
    # optional parameter shows only its real type and description, with no null branch and no "default": null.
    # It is optional because it is not in "required"; the declared type is a plain `X | None`, so null is accepted.
    tool = next(t for t in await list_tools(server) if t.name == "list_clients")
    props = tool.inputSchema["properties"]
    assert props["status_ids"]["type"] == "array" and "default" not in props["status_ids"]
    assert props["status_ids"]["items"] == {"type": "integer"}
    assert props["query"]["type"] == "string" and "default" not in props["query"]
    assert [p for p, definition in props.items() if "default" in definition] == ["page_size"]
    assert not tool.inputSchema.get("required")
    assert all("anyOf" not in p for p in props.values())
