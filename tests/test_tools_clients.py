"""tools/clients.py: list_clients, get_client, create_client, update_client, list_client_locations."""

import json
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
    paged_envelope,
    paged_responder,
    pagination,
)

import tools.clients as clients_module
from gorelo_client import FORBIDDEN_OPS
from tools._common import REGISTRY

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
        "write", ["PATCH /v1/clients/{clientId}"], True,
        ["client_id", "name", "status_id", "billing_name", "alternate_name"],
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
    op = spec_index.op(spec.ops[0])
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


def test_the_client_list_rows_and_the_single_client_are_the_same_fields_in_the_spec(spec_index):
    """The module text says a list row (ClientListItemResponse) has the fields of the single record (ClientResponse):
    if Gorelo ever makes the list row smaller, this fails and the list_clients text has to say what is missing."""
    assert spec_index.op("GET /v1/clients").response["data"] == "[ClientListItemResponse]"
    assert spec_index.op("GET /v1/clients/{clientId}").response["data"] == "ClientResponse"
    assert spec_index.op("PATCH /v1/clients/{clientId}").response["data"] == "ClientResponse"
    row, record = spec_index.schema("ClientListItemResponse")["fields"], spec_index.schema("ClientResponse")["fields"]
    assert row == record
    assert {"Id", "Name", "AlternateName", "BillingName", "Status", "Domains"} <= set(row)
    doc = flat(clients_module.__doc__)
    assert "ClientListItemResponse" in doc and "the same fields as the single record" in doc


def test_the_docstrings_carry_the_documented_facts():
    listing = flat(clients_module.list_clients.__doc__)
    # contract e15cb5a18ec2: "when omitted, inactive (2) clients are excluded, and when supplied only the listed statuses are returned"
    assert "Inactive clients are left out unless status_ids asks for the inactive status" in listing
    assert "never listed" not in listing
    assert "fit in one page" not in listing
    create = flat(clients_module.create_client.__doc__)
    assert "creates a real client" in create and "cannot delete clients" in create
    update = flat(clients_module.update_client.__doc__)
    assert "cannot clear a client field through the API" in update and "overwrites current values" in update


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
    assert spec.ops == ["PATCH /v1/clients/{clientId}"]
    assert spec.ops[0] in spec_index.ops and spec.ops[0] not in spec_index.override_ops  # published, not swapped in
    assert not spec_index.ops[spec.ops[0]].is_live_override
    assert "PATCH /v1/clients" not in spec_index.ops  # the collection form is gone from the published spec
    op = spec_index.op(spec.ops[0])
    assert op.path_params == {"clientId": {"type": "integer", "format": "int64"}}
    assert set(op.body["fields"]) == {"Name", "StatusId", "BillingName", "AlternateName"} and op.body["required"] == []
    assert "Id" not in op.body["fields"]
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
    assert text.startswith("nothing to update: pass at least one of name, status_id, billing_name or alternate_name")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("param", ["name", "billing_name", "alternate_name"])
@pytest.mark.parametrize("blank", ["", "   "])
async def test_update_client_rejects_blank_text_and_says_gorelo_cannot_clear_client_fields(server, mock_gorelo, param, blank):
    text = await call_tool_error(server, "update_client", {"client_id": 9101, param: blank})
    assert text.startswith(f"{param}: must not be empty. Gorelo cannot clear client fields through the API")
    assert f"omit {param} to keep the current value" in text
    assert mock_gorelo.requests == []


async def test_update_client_has_no_clear_option(server):
    tool = next(t for t in await list_tools(server) if t.name == "update_client")
    assert "clear_fields" not in tool.inputSchema["properties"]


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
