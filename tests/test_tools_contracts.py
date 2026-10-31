"""tools/contracts.py: list_contracts and get_contract (read), create_contract and update_contract (gated).

Offline: MockGorelo plus an in-process client."""

import json
import re
from pathlib import Path

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
    paged_envelope,
    paged_responder,
    uid,
)
from fastmcp.exceptions import ToolError

import tools  # noqa: F401  (importing the package registers every tool)
from gorelo_client import FORBIDDEN_OPS, is_forbidden_op
from tools import _common, contracts
from tools._common import MAX_ID, REGISTRY

pytestmark = pytest.mark.anyio

EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)
DESCRIPTION_MAX, PARAM_DESCRIPTION_MAX = 900, 220  # CONTRIBUTING.md: never more than these
LATEST_SPEC = Path(__file__).resolve().parent.parent / "backups" / "swagger-latest.json"
needs_spec_text = pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")

EXPECTED_READS = {
    "list_contracts": ["GET /v1/contracts"],
    "get_contract": ["GET /v1/contracts/{contractId}"],
}
EXPECTED_GATED = {
    "create_contract": ["POST /v1/contracts", "GET /v1/contracts/{contractId}"],
    "update_contract": ["GET /v1/contracts/{contractId}", "PATCH /v1/contracts/{contractId}"],
}
EXPECTED_TOOLS = {**EXPECTED_READS, **EXPECTED_GATED}


def contract(contract_id=9, client_id=9101, **overrides):
    """A ContractModel as GET /v1/contracts returns it (PascalCase, service lines inside). StartDate and EndDate are
    calendar dates (YYYY-MM-DD) since contract e15cb5a18ec2, no longer date-times."""
    record = {
        "Id": contract_id,
        "ClientId": client_id,
        "Name": "Managed Services",
        "Reference": "MSA-2026",
        "Status": {"Id": 1, "Name": "Active"},
        "StartDate": "2026-01-01",
        "EndDate": None,
        "RepeatPeriod": {"Id": 1, "Name": "Monthly"},
        "IsForAllLocations": True,
        "LocationIds": [],
        "RecurringAmount": 1500.0,
        "RecurringCost": 900.0,
        "ServiceLines": [
            {
                "Id": 77,
                "Name": "Support",
                "LaborTerms": {"Id": 3, "Name": "Block Hours"},
                "LaborRates": None,
                "RecurringAmount": 1500.0,
                "RecurringCost": 900.0,
                "CreatedOn": "2026-01-01T08:00:00Z",
                "UpdatedOn": None,
            }
        ],
        "CreatedOn": "2026-01-01T08:00:00Z",
        "UpdatedOn": None,
    }
    record.update(overrides)
    return record


def contract_detail(contract_id=9, **overrides):
    """A ContractDetailModel as GET /v1/contracts/{contractId} returns it (contract 6c30770dffc0: DaysUntilDue replaces
    InvoiceDue, the line item UnitCost replaces Cost, AutoApproveTime and AutoInvoiceTime sit on the service line)."""
    record = {
        "Id": contract_id,
        "Client": {"Id": 9101, "Name": "Example Co"},
        "Name": "Managed Services",
        "Reference": "MSA-2026",
        "Status": {"Id": 1, "Name": "Active"},
        "StartDate": "2026-01-01",
        "EndDate": None,
        "RepeatPeriod": {"Id": 1, "Name": "Monthly"},
        "DaysBeforeInvoiceCreation": 3,
        "DaysUntilDue": 30,
        "ProrateType": {"Id": 0, "Name": "Do not prorate"},
        "AutoInvoiceTicketProducts": False,
        "InvoiceTemplateId": None,
        "InvoiceEmailTemplateId": None,
        "BrandingThemeId": None,
        "AutoApproveAndSend": False,
        "Contacts": [{"Id": 9103, "Name": "Alex Example"}],
        "IsForAllLocations": True,
        "LocationIds": [],
        "RecurringAmount": 1500.0,
        "RecurringCost": 900.0,
        "ServiceLines": [
            {
                "Id": 77,
                "Name": "Support",
                "LaborTerms": {"Id": 3, "Name": "Block Hours"},
                "AutoApproveTime": False,
                "AutoInvoiceTime": False,
                "IncludedTimeGoesTo": {"Id": 1, "Name": "Complete"},
                "BlockHoursDetails": {
                    "ShowRemainingHoursInClientPortal": False,
                    "TemplateId": 0,
                    "Balance": 20.0,
                    "WarningThreshold": 20.0,
                    "OverrunThreshold": 10.0,
                },
                "LimitedHoursDetails": None,
                "PerHourDetails": None,
                "UnlimitedHoursDetails": None,
                "WorkRoles": [],
                "WorkTypes": [],
                "LineItems": [
                    {
                        "Id": 5, "Name": "Block of 20 hours", "Amount": 1500.0, "Quantity": 1.0, "UnitPrice": 1500.0,
                        "UnitCost": 900.0, "BillableStatus": {"Id": 1, "Name": "Billable"}, "ItemType": {"Id": 1, "Name": "Product"},
                    }
                ],
            }
        ],
        "CreatedOn": "2026-01-01T08:00:00Z",
        "UpdatedOn": None,
    }
    record.update(overrides)
    return record


def rejected(tool, status, code, detail):
    return f"Gorelo rejected {tool} (HTTP {status}, code {code}): {detail} [trace {TEST_TRACE_ID}]"


@pytest.fixture
def server(server_factory):
    """Every billing tool, the two gated ones included (the gate itself is tested through server_factory)."""
    return server_factory(destructive=True)


def module_specs():
    return {spec.name: spec for spec in REGISTRY.specs if spec.fn.__module__ == "tools.contracts"}


def operation(method, path):
    return json.loads(LATEST_SPEC.read_text(encoding="utf-8"))["paths"][path][method]


def param_text(method, path, name):
    return " ".join(next(p["description"] for p in operation(method, path)["parameters"] if p["name"] == name).split())


def scale_of(text):  # {id: label} from "1 Active, 2 Expired, 3 Archived"
    return {int(n): label for n, label in re.findall(r"(\d+) ([A-Z][a-z]+)", text)}


async def call_directly(client_factory, tool, **kwargs):
    """Call the decorated function itself (no pydantic in front of it): for the checks the schema normally pre-empts."""
    async with client_factory() as client:
        return await tool(make_ctx(client), **kwargs)


# --------------------------------------------------------------------------
# Declarations
# --------------------------------------------------------------------------


def test_the_module_declares_two_read_tools_and_two_gated_tools_in_the_billing_toolset():
    specs = module_specs()
    assert {name: spec.ops for name, spec in specs.items()} == EXPECTED_TOOLS
    assert {name: spec.kind for name, spec in specs.items()} == {
        "list_contracts": "read",
        "get_contract": "read",
        "create_contract": "destructive",
        "update_contract": "destructive",
    }
    assert {spec.toolset for spec in specs.values()} == {"billing"}
    assert {name: spec.destructive_hint for name, spec in specs.items()} == {
        "list_contracts": False,
        "get_contract": False,
        "create_contract": True,
        "update_contract": True,
    }


def test_the_private_copies_of_the_shared_helpers_are_gone():
    for name in ("_positive_id", "_int_ids", "_kind_of", "_shape_error", "_record"):
        assert not hasattr(contracts, name), name
    assert contracts.positive_ids is _common.positive_ids and contracts.expect_object is _common.expect_object


def test_no_contract_delete_exists_anywhere(spec_index):
    assert "DELETE /v1/contracts/{contractId}" in FORBIDDEN_OPS
    for spec in REGISTRY.specs:
        assert "DELETE /v1/contracts/{contractId}" not in spec.ops, spec.name
    # no tool can reach a contract delete under any placeholder spelling
    assert not [op for spec in module_specs().values() for op in spec.ops if is_forbidden_op(op)]
    assert {op.split(" ")[0] for spec in module_specs().values() for op in spec.ops} == {"GET", "POST", "PATCH"}


def test_the_declared_ops_exist_and_the_query_names_are_the_spec_ones(spec_index):
    for spec in module_specs().values():
        for op in spec.ops:
            assert op in spec_index.ops and op not in FORBIDDEN_OPS
    listing = spec_index.op("GET /v1/contracts")
    assert listing.paged
    assert set(contracts.LIST_FIELDS.values()) == set(listing.query_params)
    one = spec_index.op("GET /v1/contracts/{contractId}")
    assert not one.paged and one.path_placeholders == ("contractId",)
    assert spec_index.op("PATCH /v1/contracts/{contractId}").path_placeholders == ("contractId",)


async def test_the_read_tools_need_no_flag_and_the_two_writes_are_gated(server_factory):
    for destructive in (False, True):
        names = {t.name for t in await list_tools(server_factory(toolsets={"billing"}, destructive=destructive))}
        assert set(EXPECTED_READS) <= names
        assert set(EXPECTED_GATED) <= names if destructive else not names & set(EXPECTED_GATED)
        assert not [name for name in names if name.startswith("delete_contract")]
    names = {t.name for t in await list_tools(server_factory(toolsets={"core", "tickets", "time"}, destructive=True))}
    assert not names & set(EXPECTED_TOOLS)


async def test_a_gated_list_can_enable_one_of_the_two_without_the_other(server_factory):
    names = {
        t.name
        for t in await list_tools(server_factory(toolsets={"billing"}, destructive=True, destructive_tools=frozenset({"create_contract"})))
    }
    assert "create_contract" in names and "update_contract" not in names


def schema_descriptions(node, where):
    """Every description in a JSON schema (the nested models of service_lines included), with where it sits."""
    if isinstance(node, dict):
        if isinstance(node.get("description"), str):
            yield ".".join(where), node["description"]
        for key, value in node.items():
            yield from schema_descriptions(value, (*where, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from schema_descriptions(value, (*where, str(index)))


async def test_schemas_annotations_and_descriptions(server_factory):
    listed = {t.name: t for t in await list_tools(server_factory(destructive=True))}
    for name in EXPECTED_TOOLS:
        tool = listed[name]
        for param, entry_schema in tool.inputSchema["properties"].items():
            description = entry_schema.get("description")
            assert description, f"{name}.{param} has no description"
            assert len(description) <= PARAM_DESCRIPTION_MAX, f"{name}.{param}: {len(description)} characters"
            assert EM_DASH not in description and EN_DASH not in description
        for where, text in schema_descriptions(tool.inputSchema, ()):  # the nested models too
            assert len(text) <= PARAM_DESCRIPTION_MAX, f"{name} {where}: {len(text)} characters"
            assert EM_DASH not in text and EN_DASH not in text, where
        text = " ".join(tool.description.split())
        assert len(tool.description) <= DESCRIPTION_MAX, f"{name}: {len(tool.description)} characters"
        assert EM_DASH not in text and EN_DASH not in text
        if name in EXPECTED_READS:
            assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False
        else:
            assert tool.annotations.readOnlyHint is False and tool.annotations.destructiveHint is True
    listing = " ".join(listed["list_contracts"].description.split())
    assert "A service line's Id is the service_line_id of create_time_entry and update_time_entry" in listing
    assert "Paging: pass next_cursor back as cursor with the SAME filters until has_more is false." in listing
    assert "Without status_ids, Active and Expired contracts are listed and Archived ones are left out" in listing
    assert "status_ids=[1] for live contracts only" in listing
    assert "get_contract has the line items and labor detail" in listing
    properties = listed["list_contracts"].inputSchema["properties"]
    assert "list_clients" in properties["client_ids"]["description"]  # the tool that resolves the ids
    assert properties["status_ids"]["description"] == contracts.CONTRACT_STATUSES
    assert "1 Active, 2 Expired, 3 Archived" in properties["status_ids"]["description"]
    assert properties["status_ids"]["type"] == "array"
    assert properties["page_size"]["default"] == 50
    one = " ".join(listed["get_contract"].description.split())
    for fragment in (
        "invoice schedule",
        "DaysUntilDue",
        "every service line with labor detail, auto flags and line items",
        "service_line_id time entries use",
        "UnitCost",
        "404",
        "cannot be deleted through this server",
    ):
        assert fragment in one, fragment
    assert "list_contracts" in listed["get_contract"].inputSchema["properties"]["contract_id"]["description"]
    assert listed["get_contract"].inputSchema["required"] == ["contract_id"]
    # the read tools never name a gated tool (the consistency tests would ask for the gate phrase next to it)
    for name in EXPECTED_READS:
        texts = [listed[name].description, *(p.get("description", "") for p in listed[name].inputSchema["properties"].values())]
        assert not re.search(r"create_contract|update_contract", " ".join(texts)), name


# --------------------------------------------------------------------------
# list_contracts
# --------------------------------------------------------------------------


async def test_list_contracts_sends_the_client_filter_and_the_paging_names(server, mock_gorelo, spec_index):
    mock_gorelo.on("GET", "/v1/contracts", paged_envelope([contract(9), contract(10, 9102)], next_cursor="c2", total_count=11))
    result = await call_tool(
        server, "list_contracts", {"client_ids": [9101, 9102], "status_ids": [1, 2], "page_size": 2, "cursor": "abc"}
    )
    request = mock_gorelo.last
    assert (request.method, request.path, request.json) == ("GET", "/v1/contracts", None)
    assert request.query == {"ClientIds": "9101,9102", "StatusIds": "1,2", "PageSize": "2", "Cursor": "abc"}
    assert set(request.query) <= set(spec_index.op("GET /v1/contracts").query_params)
    assert result == {
        "items": [contract(9), contract(10, 9102)],
        "count": 2,
        "total_count": 11,
        "has_more": True,
        "next_cursor": "c2",
        "page_size": 2,
        "filters": {"client_ids": [9101, 9102], "status_ids": [1, 2]},
    }
    assert len(mock_gorelo.requests) == 1


async def test_list_contracts_defaults_to_a_page_of_50_and_no_filter(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts", paged_envelope([contract(9)]))
    result = await call_tool(server, "list_contracts")
    assert mock_gorelo.last.query == {"PageSize": "50"}
    assert result["page_size"] == 50 and result["filters"] == {} and result["count"] == 1
    assert result["has_more"] is False and result["next_cursor"] is None and result["total_count"] == 1


async def test_list_contracts_passes_service_lines_through_unchanged(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts", paged_envelope([contract(9)]))
    result = await call_tool(server, "list_contracts")
    line = result["items"][0]["ServiceLines"][0]
    assert line["Id"] == 77 and line["LaborTerms"] == {"Id": 3, "Name": "Block Hours"}


@pytest.mark.parametrize("asked, used", [(0, 1), (-3, 1), (1, 1), (50, 50), (200, 200), (201, 200), (999, 200)])
async def test_list_contracts_clamps_the_page_size_and_reports_it(server, mock_gorelo, asked, used):
    mock_gorelo.on("GET", "/v1/contracts", paged_envelope([]))
    result = await call_tool(server, "list_contracts", {"page_size": asked})
    assert mock_gorelo.last.query["PageSize"] == str(used) and result["page_size"] == used


async def test_list_contracts_pages_with_the_same_filter(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts", paged_responder([[contract(1)], [contract(2)]], total_count=2))
    first = await call_tool(server, "list_contracts", {"client_ids": [9101], "page_size": 1})
    assert first["has_more"] is True and first["next_cursor"] == "c1"
    second = await call_tool(server, "list_contracts", {"client_ids": [9101], "page_size": 1, "cursor": first["next_cursor"]})
    assert second["has_more"] is False and second["items"] == [contract(2)]
    assert mock_gorelo.requests[1].query == {"ClientIds": "9101", "PageSize": "1", "Cursor": "c1"}


async def test_a_client_without_contracts_is_an_explicit_empty_page(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts", paged_envelope([]))
    result = await call_tool(server, "list_contracts", {"client_ids": [424242]})
    assert result["items"] == [] and result["count"] == 0 and result["total_count"] == 0
    assert result["filters"] == {"client_ids": [424242]}


@pytest.mark.parametrize(
    "property_name, param",
    [
        ("ClientIds", "client_ids"),
        ("StatusIds", "status_ids"),
        ("PageSize", "page_size"),
        ("Cursor", "cursor"),
        ("Query", "query"),
        ("CreatedSince", "created_since"),
        ("CreatedBefore", "created_before"),
        ("UpdatedSince", "updated_since"),
        ("UpdatedBefore", "updated_before"),
        ("EndDateSince", "end_date_since"),
        ("EndDateBefore", "end_date_before"),
        ("SortBy", "sort_by"),
        ("SortOrder", "sort_order"),
    ],
)
async def test_list_contracts_maps_a_gorelo_400_to_the_snake_case_parameter(server, mock_gorelo, property_name, param):
    mock_gorelo.on("GET", "/v1/contracts", error_envelope(400, [("070101", "Not valid.", property_name)]))
    text = await call_tool_error(server, "list_contracts", {})
    assert text == rejected("list_contracts", 400, "070101", f"{param}: Not valid.")


@pytest.mark.parametrize(
    "args, fragment",
    [
        ({"client_ids": []}, "client_ids: expected at least one id, got an empty list"),
        ({"client_ids": [0]}, "client_ids[0]: expected a positive whole number such as 123, got zero or a negative number"),
        ({"client_ids": [5, -1]}, "client_ids[1]: expected a positive whole number"),
        ({"client_ids": [MAX_ID + 1]}, "client_ids[0]: expected a positive whole number such as 123, got a number above"),
        ({"client_ids": ["abc"]}, "client_ids"),
        ({"status_ids": []}, "status_ids: expected at least one id, got an empty list"),
        ({"status_ids": [0]}, "status_ids[0]: expected 1 Active, 2 Expired, 3 Archived, got 0"),
        ({"status_ids": [1, 4]}, "status_ids[1]: expected 1 Active, 2 Expired, 3 Archived, got 4"),
        ({"status_ids": [-2]}, "status_ids[0]: expected 1 Active, 2 Expired, 3 Archived, got -2"),
        ({"status_ids": [MAX_ID + 1]}, "status_ids[0]: expected a positive whole number such as 123, got a number above"),
        ({"cursor": ""}, "cursor: must not be empty or whitespace only"),
        ({"cursor": "  "}, "cursor: must not be empty or whitespace only"),
        ({"page_size": "lots"}, "page_size"),
        ({"status": "Active"}, "status"),
        ({"client_id": 9101}, "client_id"),
    ],
)
async def test_list_contracts_rejects_bad_input_before_any_http_call(server, mock_gorelo, args, fragment):
    text = await call_tool_error(server, "list_contracts", args)
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_list_contracts_refuses_an_answer_that_is_not_a_page(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts", envelope({"Id": 1}))
    text = await call_tool_error(server, "list_contracts")
    assert text.startswith("Gorelo returned an unexpected response for list_contracts")
    assert "expected Data to be a list" in text


async def test_list_contracts_reports_a_server_error(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts", error_envelope(500, [("070001", "Internal error.")]))
    text = await call_tool_error(server, "list_contracts")
    assert text == rejected("list_contracts", 500, "070001", "Internal error.")


@pytest.mark.parametrize("ids", [[1], [3], [1, 3], [1, 2, 3], [2, 2]])
async def test_list_contracts_sends_status_ids_as_a_comma_list_and_echoes_them(server, mock_gorelo, ids):
    mock_gorelo.on("GET", "/v1/contracts", paged_envelope([contract(9)]))
    result = await call_tool(server, "list_contracts", {"status_ids": ids})
    request = mock_gorelo.last
    assert (request.method, request.path, request.json) == ("GET", "/v1/contracts", None)
    assert request.query == {"StatusIds": ",".join(map(str, ids)), "PageSize": "50"}
    assert result["filters"] == {"status_ids": ids}
    assert len(mock_gorelo.requests) == 1


async def test_list_contracts_pages_with_the_same_status_filter(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts", paged_responder([[contract(1)], [contract(2)]], total_count=2))
    first = await call_tool(server, "list_contracts", {"status_ids": [1], "page_size": 1})
    assert first["has_more"] is True and first["next_cursor"] == "c1" and first["filters"] == {"status_ids": [1]}
    second = await call_tool(server, "list_contracts", {"status_ids": [1], "page_size": 1, "cursor": "c1"})
    assert second["has_more"] is False and second["items"] == [contract(2)] and second["filters"] == {"status_ids": [1]}
    assert mock_gorelo.requests[1].query == {"StatusIds": "1", "PageSize": "1", "Cursor": "c1"}


@pytest.mark.parametrize("bad", [True, False, "5", 5.0, "abc"])
async def test_status_ids_refuse_json_true_text_and_floats(server, mock_gorelo, bad):
    text = await call_tool_error(server, "list_contracts", {"status_ids": [bad]})
    assert "status_ids" in text and "valid integer" in text
    assert mock_gorelo.requests == []


async def test_status_ids_that_are_not_a_list_are_refused(server, mock_gorelo):
    text = await call_tool_error(server, "list_contracts", {"status_ids": 1})
    assert "status_ids" in text and "valid list" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("ids", [[1], [2], [3], [1, 2, 3]])
async def test_every_status_of_the_contract_scale_is_accepted_directly_and_through_the_schema(
    server, client_factory, mock_gorelo, ids
):
    mock_gorelo.on("GET", "/v1/contracts", paged_envelope([]))
    await call_tool(server, "list_contracts", {"status_ids": ids})
    await call_directly(client_factory, contracts.list_contracts, status_ids=ids)
    assert [request.query["StatusIds"] for request in mock_gorelo.requests] == [",".join(map(str, ids))] * 2


@pytest.mark.parametrize(
    "bad, kind",
    [(True, "a boolean"), (5.0, "a decimal number"), ("5", "a string"), (None, "null"), ([5], "a list of 1 item")],
)
async def test_calling_list_contracts_directly_keeps_the_id_messages_for_what_is_not_a_whole_number(
    client_factory, mock_gorelo, bad, kind
):
    with pytest.raises(ToolError) as info:
        await call_directly(client_factory, contracts.list_contracts, status_ids=[bad])
    assert str(info.value) == f"status_ids[0]: expected a positive whole number such as 123, got {kind}"
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", [0, -1, 4, MAX_ID, -MAX_ID])
async def test_calling_list_contracts_directly_answers_a_number_outside_the_scale_with_the_scale(
    client_factory, mock_gorelo, bad
):
    with pytest.raises(ToolError) as info:
        await call_directly(client_factory, contracts.list_contracts, status_ids=[1, bad])
    assert str(info.value) == f"status_ids[1]: expected 1 Active, 2 Expired, 3 Archived, got {bad}"
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "bad, what",
    [
        (MAX_ID + 1, "got a number above 9223372036854775807, the largest Gorelo id"),
        (-MAX_ID - 1, "got zero or a negative number"),
    ],
)
async def test_calling_list_contracts_directly_keeps_the_id_message_beyond_int64_and_below_one(
    client_factory, mock_gorelo, bad, what
):
    with pytest.raises(ToolError) as info:
        await call_directly(client_factory, contracts.list_contracts, status_ids=[1, bad])
    assert str(info.value) == f"status_ids[1]: expected a positive whole number such as 123, {what}"
    assert mock_gorelo.requests == []


async def test_list_contracts_sends_every_new_filter_by_its_spec_name(server, mock_gorelo, spec_index):
    mock_gorelo.on("GET", "/v1/contracts", paged_envelope([contract(9)]))
    result = await call_tool(
        server,
        "list_contracts",
        {
            "query": "MSA",
            "created_since": "2026-01-01T00:00:00Z",
            "created_before": "2026-06-01T02:00:00+02:00",
            "updated_since": "2026-02-01T00:00:00Z",
            "updated_before": "2026-03-01T00:00:00Z",
            "end_date_since": "2026-12-31",
            "end_date_before": "2027-12-31",
            "sort_by": "endDate",
            "sort_order": "asc",
        },
    )
    request = mock_gorelo.last
    assert (request.method, request.path, request.json) == ("GET", "/v1/contracts", None)
    assert request.query == {
        "Query": "MSA",
        "CreatedSince": "2026-01-01T00:00:00Z",
        "CreatedBefore": "2026-06-01T00:00:00Z",  # an offset is converted to UTC
        "UpdatedSince": "2026-02-01T00:00:00Z",
        "UpdatedBefore": "2026-03-01T00:00:00Z",
        "EndDateSince": "2026-12-31T00:00:00Z",
        "EndDateBefore": "2027-12-31T00:00:00Z",
        "SortBy": "endDate",
        "SortOrder": "asc",
        "PageSize": "50",
    }
    assert set(request.query) <= set(spec_index.op("GET /v1/contracts").query_params)
    assert result["filters"] == {
        "query": "MSA",
        "created_since": "2026-01-01T00:00:00Z",
        "created_before": "2026-06-01T00:00:00Z",
        "updated_since": "2026-02-01T00:00:00Z",
        "updated_before": "2026-03-01T00:00:00Z",
        "end_date_since": "2026-12-31",  # the echo is the calendar date the caller gave, so the next page repeats it
        "end_date_before": "2027-12-31",
        "sort_by": "endDate",
        "sort_order": "asc",
    }


@pytest.mark.parametrize("sort_by", ["createdOn", "updatedOn", "startDate", "endDate"])
@pytest.mark.parametrize("sort_order", ["asc", "desc"])
async def test_list_contracts_accepts_every_sort_the_spec_names(server, mock_gorelo, sort_by, sort_order):
    mock_gorelo.on("GET", "/v1/contracts", paged_envelope([]))
    await call_tool(server, "list_contracts", {"sort_by": sort_by, "sort_order": sort_order})
    assert mock_gorelo.last.query == {"SortBy": sort_by, "SortOrder": sort_order, "PageSize": "50"}


async def test_the_sort_enums_have_the_member_order_the_other_list_tools_pin(server_factory):
    listed = {t.name: t for t in await list_tools(server_factory(destructive=True))}
    properties = listed["list_contracts"].inputSchema["properties"]

    def enum_of(schema):
        return next(option["enum"] for option in schema.get("anyOf", [schema]) if "enum" in option)

    assert enum_of(properties["sort_order"]) == enum_of(listed["list_invoices"].inputSchema["properties"]["sort_order"]) == ["asc", "desc"]
    assert enum_of(properties["sort_by"]) == ["createdOn", "updatedOn", "startDate", "endDate"]
    assert enum_of(listed["update_contract"].inputSchema["properties"]["status"]) == ["active", "archived"]


@pytest.mark.parametrize(
    "args, fragment",
    [
        ({"created_since": "2026-01-01T00:00:00"}, "created_since: '2026-01-01T00:00:00' has no UTC offset"),
        ({"created_before": "yesterday"}, "created_before: 'yesterday' is not an ISO 8601 datetime"),
        ({"updated_since": "2026-01-01"}, "updated_since"),
        ({"updated_before": "2026-01-01T00:00:00"}, "updated_before"),
        ({"end_date_since": "2026-12-31T00:00:00"}, "end_date_since: expected a calendar date as YYYY-MM-DD"),
        ({"end_date_before": "soon"}, "end_date_before: expected a calendar date as YYYY-MM-DD"),
        ({"query": ""}, "query: must not be empty or whitespace only"),
        ({"query": "   "}, "query: must not be empty or whitespace only"),
        ({"query": "x" * 201}, "query: at most 200 characters, got 201"),
        ({"sort_by": "name"}, "sort_by"),
        ({"sort_order": "newest"}, "sort_order"),
    ],
)
async def test_list_contracts_rejects_a_bad_new_filter_before_any_http_call(server, mock_gorelo, args, fragment):
    text = await call_tool_error(server, "list_contracts", args)
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_list_contracts_takes_a_query_of_exactly_200_characters(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts", paged_envelope([]))
    await call_tool(server, "list_contracts", {"query": "x" * 200})
    assert mock_gorelo.last.query["Query"] == "x" * 200


async def test_list_contracts_pages_with_the_same_new_filters(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts", paged_responder([[contract(1)], [contract(2)]], total_count=2))
    args = {"query": "MSA", "sort_by": "startDate", "sort_order": "desc", "page_size": 1}
    first = await call_tool(server, "list_contracts", args)
    await call_tool(server, "list_contracts", {**args, "cursor": first["next_cursor"]})
    assert mock_gorelo.requests[1].query == {
        "Query": "MSA", "SortBy": "startDate", "SortOrder": "desc", "PageSize": "1", "Cursor": "c1"
    }


@needs_spec_text
def test_the_contract_status_scale_is_the_one_the_spec_text_documents():
    text = param_text("get", "/v1/contracts", "StatusIds")
    assert scale_of(text) == contracts.CONTRACT_STATUS_LABELS == {1: "Active", 2: "Expired", 3: "Archived"}
    assert "Omitted returns Active and Expired (archived excluded)" in text
    assert "Any other id is a 400" in text
    op = " ".join(operation("get", "/v1/contracts")["description"].split())
    assert "Archived contracts are left out unless `statusIds` names Archived" in op
    assert "a caller that wants only live contracts passes `statusIds=1`" in op
    assert "Omitted: Active and Expired (archived left out)" in contracts.CONTRACT_STATUSES


@needs_spec_text
def test_the_new_list_filters_are_the_ones_the_spec_text_documents():
    assert "createdOn (default), updatedOn, startDate or endDate" in param_text("get", "/v1/contracts", "SortBy")
    assert "asc or desc (default)" in param_text("get", "/v1/contracts", "SortOrder")
    assert "Max 200 characters" in param_text("get", "/v1/contracts", "Query")
    assert contracts.QUERY_MAX == 200
    assert "A contract never changed is excluded" in param_text("get", "/v1/contracts", "UpdatedSince")
    assert "A contract with no end date is excluded" in param_text("get", "/v1/contracts", "EndDateSince")
    assert "strictly before" in param_text("get", "/v1/contracts", "EndDateBefore")


def test_the_module_text_is_plain_ascii_without_dashes():
    for path in (Path(__file__).resolve().parent.parent / "tools" / "contracts.py", Path(__file__)):
        source = path.read_text(encoding="utf-8")
        assert source.isascii(), path.name
        assert EM_DASH not in source and EN_DASH not in source, path.name


# --------------------------------------------------------------------------
# get_contract
# --------------------------------------------------------------------------


async def test_get_contract_returns_the_detail_unchanged(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(contract_detail(9)))
    result = await call_tool(server, "get_contract", {"contract_id": 9})
    assert result == contract_detail(9)
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", "/v1/contracts/9", {}, None)
    assert len(mock_gorelo.requests) == 1
    assert result["ServiceLines"][0]["LineItems"][0]["Name"] == "Block of 20 hours"


@pytest.mark.parametrize(
    "notification, detail",
    [
        (("070404", "Contract not found."), "Contract not found."),
        (("070404", "Contract not found.", "contractId"), "contract_id: Contract not found."),
    ],
)
async def test_get_contract_reports_a_404(server, mock_gorelo, notification, detail):
    mock_gorelo.on("GET", "/v1/contracts/9", error_envelope(404, [notification]))
    text = await call_tool_error(server, "get_contract", {"contract_id": 9})
    assert text == rejected("get_contract", 404, "070404", detail)


@pytest.mark.parametrize(
    "bad, what", [(0, "zero or a negative number"), (-4, "zero or a negative number"), (MAX_ID + 1, "a number above")]
)
async def test_get_contract_rejects_a_bad_id_by_name(server, mock_gorelo, bad, what):
    text = await call_tool_error(server, "get_contract", {"contract_id": bad})
    assert text.startswith(f"contract_id: expected a positive whole number such as 123, got {what}")
    assert mock_gorelo.requests == []


async def test_get_contract_takes_the_largest_gorelo_id(server, mock_gorelo):
    mock_gorelo.on("GET", f"/v1/contracts/{MAX_ID}", envelope(contract_detail(3)))
    assert await call_tool(server, "get_contract", {"contract_id": MAX_ID}) == contract_detail(3)


# (tool, the id parameter under test, whether it is a list)
STRICT_ID_CASES = [
    pytest.param("list_contracts", "client_ids", True, id="list_contracts.client_ids"),
    pytest.param("get_contract", "contract_id", False, id="get_contract.contract_id"),
]


@pytest.mark.parametrize("bad", [True, False, "5", 5.0, "abc"])
@pytest.mark.parametrize("tool, param, is_list", STRICT_ID_CASES)
async def test_every_id_parameter_refuses_json_true_text_and_floats(server, mock_gorelo, tool, param, is_list, bad):
    text = await call_tool_error(server, tool, {param: [bad] if is_list else bad})
    assert param in text and "valid integer" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "bad, what",
    [(0, "zero or a negative number"), (-3, "zero or a negative number"), (MAX_ID + 1, "a number above")],
)
@pytest.mark.parametrize("tool, param, is_list", STRICT_ID_CASES)
async def test_every_id_parameter_is_range_checked_by_name(server, mock_gorelo, tool, param, is_list, bad, what):
    text = await call_tool_error(server, tool, {param: [bad] if is_list else bad})
    named = f"{param}[0]" if is_list else param
    assert text.startswith(f"{named}: expected a positive whole number such as 123, got {what}")
    assert mock_gorelo.requests == []


async def test_an_id_list_that_is_not_a_list_is_refused(server, mock_gorelo):
    text = await call_tool_error(server, "list_contracts", {"client_ids": 9101})
    assert "client_ids" in text and "valid list" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("args, fragment", [({}, "contract_id"), ({"contract_id": "abc"}, "contract_id"), ({"id": 9}, "id")])
async def test_get_contract_needs_a_numeric_contract_id(server, mock_gorelo, args, fragment):
    text = await call_tool_error(server, "get_contract", args)
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "body, fragment",
    [
        (envelope([contract_detail()]), "expected Data to be a non-empty object but got a list of 1 item"),
        (envelope(False), "expected Data to be a non-empty object but got a boolean"),
        (envelope(True), "expected Data to be a non-empty object but got a boolean"),
        (envelope({}), "expected Data to be a non-empty object but got an empty object"),
        (envelope(None), "Gorelo reported success but Data is null"),
    ],
)
async def test_get_contract_refuses_an_answer_that_is_not_a_record(server, mock_gorelo, body, fragment):
    mock_gorelo.on("GET", "/v1/contracts/9", body)
    text = await call_tool_error(server, "get_contract", {"contract_id": 9})
    assert text.startswith("Gorelo returned an unexpected response for get_contract") and fragment in text
    assert "refusing to guess" in text and "verify" not in text.lower()  # a read: nothing may have been applied


# --------------------------------------------------------------------------
# Builders for the two gated writes
# --------------------------------------------------------------------------

LABOR_NAMES = {1: "Unlimited Hours", 2: "Hourly", 3: "Block Hours", 4: "No Labor Terms", 5: "Limited Hours"}


def hourly_line(**overrides):
    return {"name": "Support", "labor_terms_id": 2, "per_hour_details": {}, **overrides}


def create_args(**overrides):
    return {"name": "Managed Services", "client_id": 9101, "service_lines": [hourly_line()], "confirm": True, **overrides}


def made(contract_id=55, name="Managed Services", client_id=9101, lines=(("Support", 2),), **overrides):
    """The contract as the read-back shows it after a create."""
    record = contract_detail(contract_id, Name=name, Client={"Id": client_id, "Name": "Example Co"}, **overrides)
    record["ServiceLines"] = [
        {"Id": 900 + index, "Name": line, "LaborTerms": {"Id": terms, "Name": LABOR_NAMES[terms]}, "LineItems": []}
        for index, (line, terms) in enumerate(lines)
    ]
    return record


def line_record(line_id, name, terms, items=(), **extra):
    return {
        "Id": line_id,
        "Name": name,
        "LaborTerms": {"Id": terms, "Name": LABOR_NAMES[terms]},
        "LineItems": [{"Id": number, "Name": f"Item {number}", "Quantity": 1.0, "UnitPrice": 10.0, "UnitCost": 4.0} for number in items],
        **extra,
    }


def three_lines(**overrides):
    """A contract as get_contract shows it: a Block Hours line with two items, an Hourly line (default rates) and an empty
    No Labor Terms line."""
    record = contract_detail(9, **overrides)
    record["ServiceLines"] = [
        line_record(
            77, "Support", 3, items=(5, 6), BlockHoursDetails={"TemplateId": 0, "Balance": 20.0, "WarningThreshold": 20.0}
        ),
        line_record(78, "Hourly", 2, items=(7,), PerHourDetails={"RateType": {"Id": 1, "Name": "Use default labor rates"}, "HourlyRate": None}),
        line_record(79, "Nothing", 4),
    ]
    return record


def edited(record, change):
    clone = json.loads(json.dumps(record))
    change(clone)
    return clone


def line_of(record, line_id):
    return next(line for line in record["ServiceLines"] if line["Id"] == line_id)


def serve_update(mock_gorelo, before, after, contract_id=9, *, patch=None):
    mock_gorelo.on("GET", f"/v1/contracts/{contract_id}", in_order(envelope(before), envelope(after)))
    mock_gorelo.on("PATCH", f"/v1/contracts/{contract_id}", envelope(after if patch is None else patch))


def update_args(**overrides):
    return {"contract_id": 9, "confirm": True, **overrides}


def sent_ops(mock_gorelo):
    return [f"{request.method} {request.path}" for request in mock_gorelo.requests]


# --------------------------------------------------------------------------
# create_contract
# --------------------------------------------------------------------------


async def test_create_contract_posts_the_command_and_reads_the_contract_back(server, mock_gorelo):
    mock_gorelo.on("POST", "/v1/contracts", envelope(made()))
    mock_gorelo.on("GET", "/v1/contracts/55", envelope(made()))
    result = await call_tool(server, "create_contract", create_args())
    assert sent_ops(mock_gorelo) == ["POST /v1/contracts", "GET /v1/contracts/55"]
    post, read = mock_gorelo.requests
    assert post.query == {} and read.query == {} and read.json is None
    assert post.json == {
        "Name": "Managed Services",
        "ClientId": 9101,
        "ServiceLines": [{"Name": "Support", "LaborTermsId": 2, "PerHourDetails": {}}],
    }
    assert result == made()


async def test_create_contract_sends_every_field_in_its_gorelo_shape(server, mock_gorelo):
    lines = [
        {
            "name": "Everything",
            "labor_terms_id": 1,
            "unlimited_hours_details": {},
            "work_type_ids": [1, 2],
            "work_role_ids": [3],
            "auto_approve_time": True,
            "auto_invoice_time": False,
            "included_time_goes_to_id": 2,
            "line_items": [
                {
                    "item_id": uid(1), "quantity": 2.5, "description": "Monitoring", "unit_price": 10, "unit_cost": 4,
                    "discount_percent": 5, "tax_id": 3, "billable_status_id": 2, "start_date": "2026-02-01", "end_date": "2026-12-31",
                },
                {"item_id": uid(2), "quantity": 1},
            ],
        },
        {
            "name": "Custom block",
            "labor_terms_id": 3,
            "block_hours_details": {
                "template_id": 0, "balance": 20, "price": 2000, "warning_threshold": 20, "overrun_threshold": 10,
                "show_remaining_hours_in_client_portal": True,
            },
            "included_time_goes_to_id": 1,
        },
        {"name": "Template block", "labor_terms_id": 3, "block_hours_details": {"template_id": 14}},
        {
            "name": "Limited",
            "labor_terms_id": 5,
            "limited_hours_details": {"allocated_hours": 10, "rate_type_id": 2, "hourly_rate": 150, "show_remaining_hours_in_client_portal": False},
        },
        {
            "name": "Rates",
            "labor_terms_id": 2,
            "per_hour_details": {
                "rate_type_id": 3,
                "custom_rates": {
                    "work_roles": [{"work_role_id": 12, "hourly_rate": 95, "coa_code": "270"}],
                    "work_types": [{"work_type_id": 176, "hourly_multiplier": 1.5}],
                },
            },
        },
        {"name": "Nothing", "labor_terms_id": 4},
    ]
    names = [(line["name"], line["labor_terms_id"]) for line in lines]
    mock_gorelo.on("POST", "/v1/contracts", envelope(made(lines=names)))
    mock_gorelo.on("GET", "/v1/contracts/55", envelope(made(lines=names)))
    await call_tool(
        server,
        "create_contract",
        create_args(
            service_lines=lines,
            is_for_all_locations=False,
            location_ids=[11, 12],
            reference="PO-1",
            start_date="2026-02-01",
            end_date="2027-01-31",
            prorate_type_id=1,
            auto_invoice_ticket_products=False,
            repeat_period_id=3,
            days_before_invoice_creation=5,
            days_until_due=14,
            auto_approve_and_send=True,
            contact_ids=[9103],
            invoice_template_id=7,
            invoice_email_template_id=8,
            branding_theme_id=uid(3),
        ),
    )
    assert mock_gorelo.requests[0].json == {
        "Name": "Managed Services",
        "ClientId": 9101,
        "IsForAllLocations": False,
        "LocationIds": [11, 12],
        "Reference": "PO-1",
        "StartDate": "2026-02-01T00:00:00Z",
        "EndDate": "2027-01-31T00:00:00Z",
        "ProrateTypeId": 1,
        "AutoInvoiceTicketProducts": False,
        "RepeatPeriodId": 3,
        "DaysBeforeInvoiceCreation": 5,
        "DaysUntilDue": 14,
        "AutoApproveAndSend": True,
        "ContactIds": [9103],
        "InvoiceTemplateId": 7,
        "InvoiceEmailTemplateId": 8,
        "BrandingThemeId": uid(3),
        "ServiceLines": [
            {
                "Name": "Everything",
                "LaborTermsId": 1,
                "WorkTypeIds": [1, 2],
                "WorkRoleIds": [3],
                "AutoApproveTime": True,
                "AutoInvoiceTime": False,
                "IncludedTimeGoesToId": 2,
                "UnlimitedHoursDetails": {},
                "LineItems": [
                    {
                        "ItemId": uid(1), "Quantity": 2.5, "Description": "Monitoring", "UnitPrice": 10, "UnitCost": 4,
                        "DiscountPercent": 5, "TaxId": 3, "BillableStatusId": 2, "StartDate": "2026-02-01T00:00:00Z",
                        "EndDate": "2026-12-31T00:00:00Z",
                    },
                    {"ItemId": uid(2), "Quantity": 1},
                ],
            },
            {
                "Name": "Custom block",
                "LaborTermsId": 3,
                "IncludedTimeGoesToId": 1,
                "BlockHoursDetails": {
                    "TemplateId": 0, "Balance": 20, "Price": 2000, "WarningThreshold": 20, "OverrunThreshold": 10,
                    "ShowRemainingHoursInClientPortal": True,
                },
            },
            {"Name": "Template block", "LaborTermsId": 3, "BlockHoursDetails": {"TemplateId": 14}},
            {
                "Name": "Limited",
                "LaborTermsId": 5,
                "LimitedHoursDetails": {
                    "AllocatedHours": 10, "ShowRemainingHoursInClientPortal": False, "RateTypeId": 2, "HourlyRate": 150,
                },
            },
            {
                "Name": "Rates",
                "LaborTermsId": 2,
                "PerHourDetails": {
                    "RateTypeId": 3,
                    "CustomRates": {
                        "WorkRoles": [{"WorkRoleId": 12, "HourlyRate": 95, "CoaCode": "270"}],
                        "WorkTypes": [{"WorkTypeId": 176, "HourlyMultiplier": 1.5}],
                    },
                },
            },
            {"Name": "Nothing", "LaborTermsId": 4},
        ],
    }


async def test_create_contract_refuses_without_confirm_and_sends_nothing(server, mock_gorelo):
    text = await call_tool_error(server, "create_contract", create_args(confirm=False))
    assert text.startswith("confirm: refusing to create a contract for client 9101 with 1 service line without confirm=true.")
    assert "contracts cannot be deleted through this server, only archived" in text
    assert "Nothing has been sent to Gorelo." in text
    assert "Block Hours" not in text and "accounting system" not in text
    assert mock_gorelo.requests == []
    text = await call_tool_error(server, "create_contract", {k: v for k, v in create_args().items() if k != "confirm"})
    assert text.startswith("confirm: refusing to create a contract")
    assert mock_gorelo.requests == []


async def test_create_contract_refusal_names_the_block_invoice_and_the_auto_approval(server, mock_gorelo):
    lines = [
        {"name": "B1", "labor_terms_id": 3, "block_hours_details": {"template_id": 4}},
        {"name": "B2", "labor_terms_id": 3, "block_hours_details": {"template_id": 0, "balance": 1, "price": 1}},
        hourly_line(),
    ]
    text = await call_tool_error(server, "create_contract", create_args(confirm=False, service_lines=lines, auto_approve_and_send=True))
    assert "with 3 service lines" in text
    assert "2 Block Hours lines raise a Draft invoice for the block at once" in text
    assert "auto_approve_and_send=true makes Gorelo approve and send every invoice the contract raises on its own" in text
    assert "approving pushes each one to the connected accounting system" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", ["true", "yes", 1, 0, None])
async def test_create_contract_needs_a_strict_boolean_confirm(server, mock_gorelo, bad):
    text = await call_tool_error(server, "create_contract", create_args(confirm=bad))
    assert "confirm" in text
    assert mock_gorelo.requests == []


async def test_a_confirm_refusal_comes_after_the_local_checks_and_before_any_request(server, mock_gorelo):
    text = await call_tool_error(server, "create_contract", create_args(confirm=False, name=" "))
    assert text.startswith("name: must not be empty or whitespace only")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"service_lines": []}, "service_lines: at least one service line is required"),
        ({"name": ""}, "name: must not be empty or whitespace only"),
        ({"client_id": 0}, "client_id: expected a positive whole number such as 123, got zero or a negative number"),
        ({"reference": " "}, "reference: must not be empty or whitespace only"),
        ({"start_date": "2026-02-01T00:00:00Z"}, "start_date: expected a calendar date as YYYY-MM-DD"),
        ({"end_date": "2026-13-01"}, "end_date: expected a calendar date as YYYY-MM-DD"),
        ({"start_date": "2026-05-01", "end_date": "2026-05-01"}, "end_date: 2026-05-01 is not after start_date 2026-05-01"),
        ({"start_date": "2026-05-01", "end_date": "2026-04-01"}, "end_date: 2026-04-01 is not after start_date 2026-05-01"),
        ({"repeat_period_id": 5}, "repeat_period_id: expected 1 Monthly, 2 Quarterly, 3 Yearly, 4 Half Yearly, got 5"),
        ({"repeat_period_id": 0}, "repeat_period_id: expected 1 Monthly"),
        ({"prorate_type_id": 3}, "prorate_type_id: expected 0 Do not prorate, 1 Prorate and add to next invoice, 2 Prorate and generate standalone invoice, got 3"),
        ({"prorate_type_id": -1}, "prorate_type_id: expected 0 Do not prorate"),
        ({"days_before_invoice_creation": 32}, "days_before_invoice_creation: expected a whole number from 0 to 31, got 32"),
        ({"days_before_invoice_creation": -1}, "days_before_invoice_creation: expected a whole number from 0 to 31, got -1"),
        ({"days_until_due": -1}, "days_until_due: expected a whole number from 0 to 2147483647, got -1"),
        ({"days_until_due": 2**31}, "days_until_due: expected a whole number from 0 to 2147483647"),
        ({"branding_theme_id": "nope"}, "branding_theme_id"),
        ({"invoice_template_id": 0}, "invoice_template_id: expected a positive whole number"),
        ({"invoice_email_template_id": -2}, "invoice_email_template_id: expected a positive whole number"),
        ({"contact_ids": []}, "contact_ids: expected at least one id, got an empty list (omit it for none)"),
        ({"contact_ids": [5, 0]}, "contact_ids[1]: expected a positive whole number"),
        ({"is_for_all_locations": False}, "location_ids: at least one location of the client is required when is_for_all_locations is false"),
        ({"location_ids": [1]}, "location_ids: only with is_for_all_locations=false"),
        ({"is_for_all_locations": True, "location_ids": [1]}, "location_ids: only with is_for_all_locations=false"),
        ({"is_for_all_locations": False, "location_ids": []}, "location_ids"),
        (
            {"is_for_all_locations": False, "location_ids": [1], "auto_invoice_ticket_products": True},
            "auto_invoice_ticket_products: only on a contract that covers all locations",
        ),
    ],
)
async def test_create_contract_rejects_bad_contract_fields_before_any_http_call(server, mock_gorelo, overrides, fragment):
    text = await call_tool_error(server, "create_contract", create_args(**overrides))
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_a_bad_value_is_refused_by_the_schema_with_its_parameter_name(server, mock_gorelo):
    for args, name in (
        ({"client_id": True}, "client_id"),
        ({"client_id": "9101"}, "client_id"),
        ({"days_until_due": 1.5}, "days_until_due"),
        ({"days_until_due": True}, "days_until_due"),
        ({"auto_approve_and_send": "true"}, "auto_approve_and_send"),
        ({"contact_ids": ["5"]}, "contact_ids"),
        ({"service_lines": "Support"}, "service_lines"),
        ({"service_lines": [hourly_line(labor_terms_id=True)]}, "labor_terms_id"),
        ({"service_lines": [hourly_line(labor_terms_id="2")]}, "labor_terms_id"),
        ({"service_lines": [hourly_line(auto_approve_time="yes")]}, "auto_approve_time"),
        ({"service_lines": [hourly_line(per_hour_details={"hourly_rate": True})]}, "hourly_rate"),
        ({"service_lines": [hourly_line(per_hour_details={"hourly_rate": "5"})]}, "hourly_rate"),
        ({"service_lines": [hourly_line(work_type_ids=[True])]}, "work_type_ids"),
        ({"service_lines": [hourly_line(line_items=[{"item_id": uid(1), "quantity": "2"}])]}, "quantity"),
    ):
        text = await call_tool_error(server, "create_contract", create_args(**args))
        assert name in text, (args, text)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "extra_line",
    [
        {"unknown_field": 1},
        {"id": 5},
        {"remove_line_item_ids": [1]},
        {"per_hour_details": {"hourly_rat": 5}},
        {"per_hour_details": {"custom_rates": {"work_role": []}}},
        {"line_items": [{"item_id": uid(1), "quantity": 1, "id": 4}]},
        {"line_items": [{"item_id": uid(1), "quantity": 1, "clear_fields": ["end_date"]}]},
        {"line_items": [{"item_id": uid(1), "quantity": 1, "cost": 5}]},
    ],
)
async def test_a_nested_object_with_an_unknown_field_is_refused(server, mock_gorelo, extra_line):
    text = await call_tool_error(server, "create_contract", create_args(service_lines=[hourly_line(**extra_line)]))
    assert "Extra inputs are not permitted" in text or "service_lines" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "line, fragment",
    [
        ({"labor_terms_id": 6}, "service_lines[0].labor_terms_id: expected 1 Unlimited Hours, 2 Hourly, 3 Block Hours, 4 No Labor Terms, 5 Limited Hours, got 6"),
        ({"labor_terms_id": 0}, "service_lines[0].labor_terms_id: expected 1 Unlimited Hours"),
        ({"name": " "}, "service_lines[0].name: must not be empty or whitespace only"),
        ({"per_hour_details": None}, "service_lines[0].per_hour_details: required for labor_terms_id 2 (Hourly)"),
        (
            {"labor_terms_id": 1, "per_hour_details": None, "unlimited_hours_details": None},
            "service_lines[0].unlimited_hours_details: required for labor_terms_id 1 (Unlimited Hours) (send {} for Unlimited Hours)",
        ),
        ({"labor_terms_id": 4}, "service_lines[0].per_hour_details: does not match labor_terms_id 4 (No Labor Terms); a No Labor Terms line takes none"),
        (
            {"labor_terms_id": 3, "block_hours_details": {"template_id": 0, "balance": 1, "price": 1}},
            "service_lines[0].per_hour_details: does not match labor_terms_id 3 (Block Hours); give block_hours_details only",
        ),
        (
            {"unlimited_hours_details": {}},
            "service_lines[0].unlimited_hours_details: does not match labor_terms_id 2 (Hourly); give per_hour_details only",
        ),
        ({"per_hour_details": {"hourly_rate": 150}}, "service_lines[0].per_hour_details.hourly_rate: only with rate_type_id 2"),
        (
            {"per_hour_details": {"rate_type_id": 1, "hourly_rate": 150}},
            "service_lines[0].per_hour_details.hourly_rate: only with rate_type_id 2",
        ),
        (
            {"per_hour_details": {"rate_type_id": 3, "hourly_rate": 150}},
            "service_lines[0].per_hour_details.hourly_rate: only with rate_type_id 2",
        ),
        ({"per_hour_details": {"rate_type_id": 2}}, "service_lines[0].per_hour_details.hourly_rate: required with rate_type_id 2"),
        ({"per_hour_details": {"rate_type_id": 4}}, "service_lines[0].per_hour_details.rate_type_id: expected 1 Use default labor rates, 2 Use fixed labor rate, 3 Custom labor rates, got 4"),
        ({"per_hour_details": {"rate_type_id": 2, "hourly_rate": -1}}, "service_lines[0].per_hour_details.hourly_rate: expected a number of 0 or more"),
        ({"per_hour_details": {"custom_rates": {}}}, "service_lines[0].per_hour_details.custom_rates: give work_roles, work_types or both"),
        ({"per_hour_details": {"custom_rates": {"work_roles": []}}}, "custom_rates.work_roles: expected at least one row"),
        (
            {"per_hour_details": {"custom_rates": {"work_roles": [{"work_role_id": 3}, {"work_role_id": 3}]}}},
            "custom_rates.work_roles[1].work_role_id: the id appears more than once",
        ),
        ({"per_hour_details": {"custom_rates": {"work_types": [{"work_type_id": 3, "coa_code": " "}]}}}, "work_types[0].coa_code: must not be empty"),
        ({"work_type_ids": []}, "service_lines[0].work_type_ids: expected at least one id"),
        ({"work_role_ids": [0]}, "service_lines[0].work_role_ids[0]: expected a positive whole number"),
        ({"included_time_goes_to_id": 1}, "service_lines[0].included_time_goes_to_id: only on Unlimited, Block and Limited Hours lines, got labor terms 2 (Hourly)"),
        (
            {"labor_terms_id": 1, "per_hour_details": None, "unlimited_hours_details": {}, "included_time_goes_to_id": 3},
            "service_lines[0].included_time_goes_to_id: expected 1 Complete, 2 Waiting to invoice, got 3",
        ),
        ({"labor_terms_id": 5, "per_hour_details": None, "limited_hours_details": {}}, "service_lines[0].limited_hours_details.allocated_hours: required on a new Limited Hours line"),
        (
            {"labor_terms_id": 5, "per_hour_details": None, "limited_hours_details": {"allocated_hours": 5, "hourly_rate": 9}},
            "service_lines[0].limited_hours_details.hourly_rate: only with rate_type_id 2",
        ),
        (
            {"labor_terms_id": 5, "per_hour_details": None, "limited_hours_details": {"allocated_hours": -5}},
            "service_lines[0].limited_hours_details.allocated_hours: expected a number of 0 or more",
        ),
        ({"line_items": []}, "service_lines[0].line_items: expected at least one line item, got an empty list"),
        ({"line_items": [{"item_id": uid(1)}]}, "service_lines.0.line_items.0.quantity"),  # the schema asks for it first
        ({"line_items": [{"item_id": "x", "quantity": 1}]}, "service_lines[0].line_items[0].item_id"),
        ({"line_items": [{"item_id": uid(1), "quantity": 0}]}, "service_lines[0].line_items[0].quantity: expected a number above 0"),
        ({"line_items": [{"item_id": uid(1), "quantity": 1, "unit_price": -1}]}, "line_items[0].unit_price: expected a number of 0 or more"),
        ({"line_items": [{"item_id": uid(1), "quantity": 1, "unit_cost": -1}]}, "line_items[0].unit_cost: expected a number of 0 or more"),
        ({"line_items": [{"item_id": uid(1), "quantity": 1, "discount_percent": 101}]}, "line_items[0].discount_percent: expected a number of at most 100"),
        ({"line_items": [{"item_id": uid(1), "quantity": 1, "discount_percent": -1}]}, "line_items[0].discount_percent: expected a number of 0 or more"),
        ({"line_items": [{"item_id": uid(1), "quantity": 1, "tax_id": 0}]}, "line_items[0].tax_id: expected a positive whole number"),
        (
            {"line_items": [{"item_id": uid(1), "quantity": 1, "billable_status_id": 4}]},
            "line_items[0].billable_status_id: expected 1 Billable, 2 No charge, 3 Non-billable, got 4",
        ),
        ({"line_items": [{"item_id": uid(1), "quantity": 1, "start_date": "2026-01-01T00:00:00Z"}]}, "line_items[0].start_date: expected a calendar date"),
        ({"line_items": [{"item_id": uid(1), "quantity": 1, "description": " "}]}, "line_items[0].description: must not be empty"),
    ],
)
async def test_create_contract_rejects_a_bad_service_line_before_any_http_call(server, mock_gorelo, line, fragment):
    text = await call_tool_error(server, "create_contract", create_args(service_lines=[hourly_line(**line)]))
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "line, fragment",
    [
        ({"name": "N", "labor_terms_id": 4, "work_type_ids": [1]}, "service_lines[0].work_type_ids: not accepted on a No Labor Terms line"),
        ({"name": "N", "labor_terms_id": 4, "work_role_ids": [1]}, "service_lines[0].work_role_ids: not accepted on a No Labor Terms line"),
        ({"name": "N", "labor_terms_id": 4, "auto_approve_time": True}, "service_lines[0].auto_approve_time: not accepted on a No Labor Terms line"),
        ({"name": "N", "labor_terms_id": 4, "auto_invoice_time": True}, "service_lines[0].auto_invoice_time: not accepted on a No Labor Terms line"),
        ({"name": "N", "labor_terms_id": 4, "included_time_goes_to_id": 1}, "service_lines[0].included_time_goes_to_id: only on Unlimited, Block and Limited Hours lines"),
        ({"name": "N", "labor_terms_id": 3}, "service_lines[0].block_hours_details: required for labor_terms_id 3 (Block Hours)"),
        (
            {"name": "N", "labor_terms_id": 3, "block_hours_details": {"balance": 1, "price": 1}},
            "service_lines[0].block_hours_details.template_id: required on a new Block Hours line: 0 for Custom, or a Block Hours Template id",
        ),
        (
            {"name": "N", "labor_terms_id": 3, "block_hours_details": {"template_id": 0, "balance": 1}},
            "service_lines[0].block_hours_details: template_id 0 (Custom) needs balance and price",
        ),
        (
            {"name": "N", "labor_terms_id": 3, "block_hours_details": {"template_id": 0, "price": 1}},
            "service_lines[0].block_hours_details: template_id 0 (Custom) needs balance and price",
        ),
        (
            {"name": "N", "labor_terms_id": 3, "block_hours_details": {"template_id": 5, "balance": 1}},
            "service_lines[0].block_hours_details.balance: refused with a Block Hours Template",
        ),
        (
            {"name": "N", "labor_terms_id": 3, "block_hours_details": {"template_id": 5, "price": 1}},
            "service_lines[0].block_hours_details.price: refused with a Block Hours Template",
        ),
        (
            {"name": "N", "labor_terms_id": 3, "block_hours_details": {"template_id": 0, "balance": -1, "price": 1}},
            "service_lines[0].block_hours_details.balance: expected a number of 0 or more",
        ),
        (
            {"name": "N", "labor_terms_id": 3, "block_hours_details": {"template_id": 5, "warning_threshold": 91}},
            "service_lines[0].block_hours_details.warning_threshold: expected a whole number from 0 to 90, got 91",
        ),
        (
            {"name": "N", "labor_terms_id": 3, "block_hours_details": {"template_id": 5, "warning_threshold": -1}},
            "block_hours_details.warning_threshold: expected a whole number from 0 to 90, got -1",
        ),
        (
            {"name": "N", "labor_terms_id": 3, "block_hours_details": {"template_id": 5, "overrun_threshold": 51}},
            "service_lines[0].block_hours_details.overrun_threshold: expected a whole number from 0 to 50, got 51",
        ),
        (
            {"name": "N", "labor_terms_id": 3, "block_hours_details": {"template_id": -1}},
            "service_lines[0].block_hours_details.template_id: expected a whole number from 0 to",
        ),
        (
            {"name": "N", "labor_terms_id": 1, "unlimited_hours_details": {}, "block_hours_details": {"template_id": 1}},
            "service_lines[0].block_hours_details: does not match labor_terms_id 1 (Unlimited Hours); give unlimited_hours_details only",
        ),
    ],
)
async def test_create_contract_rejects_what_a_labor_term_cannot_have_before_any_http_call(server, mock_gorelo, line, fragment):
    text = await call_tool_error(server, "create_contract", create_args(service_lines=[line]))
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_the_error_names_the_line_by_its_position(server, mock_gorelo):
    lines = [hourly_line(), hourly_line(name="Second", labor_terms_id=3, per_hour_details=None, block_hours_details={"template_id": 0})]
    text = await call_tool_error(server, "create_contract", create_args(service_lines=lines))
    assert text.startswith("service_lines[1].block_hours_details: template_id 0 (Custom) needs balance and price")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("false_flag", ["auto_approve_time", "auto_invoice_time"])
async def test_a_false_auto_flag_on_a_no_labor_terms_line_is_not_refused_locally(server, mock_gorelo, false_flag):
    mock_gorelo.on("POST", "/v1/contracts", envelope(made(lines=[("N", 4)])))
    mock_gorelo.on("GET", "/v1/contracts/55", envelope(made(lines=[("N", 4)])))
    await call_tool(server, "create_contract", create_args(service_lines=[{"name": "N", "labor_terms_id": 4, false_flag: False}]))
    assert mock_gorelo.requests[0].json["ServiceLines"][0][{"auto_approve_time": "AutoApproveTime", "auto_invoice_time": "AutoInvoiceTime"}[false_flag]] is False


async def test_a_direct_call_checks_nested_objects_that_the_schema_would_have_refused(client_factory, mock_gorelo):
    for lines, fragment in (
        ("Support", "service_lines: expected a list of service lines, each an object with name and labor_terms_id, got a string"),
        ([None], "service_lines[0]: must be an object with name and labor_terms_id, got null"),
        ([{"name": "N"}], "service_lines[0].labor_terms_id: required on a new service line"),
        ([{"name": "N", "labor_terms_id": 2, "per_hour_details": {}, "bogus": 1}], "service_lines[0]: unknown field(s) bogus"),
        ([{"name": "N", "labor_terms_id": 2, "per_hour_details": {"bogus": 1}}], "per_hour_details: unknown field(s) bogus"),
        ([{"name": "N", "labor_terms_id": 2, "per_hour_details": "x"}], "service_lines[0].per_hour_details: must be an object, got a string"),
        ([{"name": "N", "labor_terms_id": 2, "per_hour_details": {}, "id": 4}], "service_lines[0].id: not accepted when the contract is created"),
        ([{"name": "N", "labor_terms_id": True, "per_hour_details": {}}], "service_lines[0].labor_terms_id: expected 1 Unlimited Hours"),
        ([{"name": "N", "labor_terms_id": 2, "per_hour_details": {}, "auto_approve_time": "yes"}], "auto_approve_time: must be true or false, got a string"),
        ([{"name": 5, "labor_terms_id": 2, "per_hour_details": {}}], "service_lines[0].name: expected text, got a number"),
        (
            [{"name": "N", "labor_terms_id": 2, "per_hour_details": {"hourly_rate": float("nan"), "rate_type_id": 2}}],
            "hourly_rate: expected a finite number",
        ),
        (
            [{"name": "N", "labor_terms_id": 2, "per_hour_details": {}, "line_items": [{"item_id": uid(1), "quantity": 1, "id": 4}]}],
            "line_items[0].id: not accepted when the contract is created",
        ),
        (
            [{"name": "N", "labor_terms_id": 2, "per_hour_details": {}, "line_items": ["x"]}],
            "line_items[0]: must be an object with item_id and quantity, got a string",
        ),
        (
            [{"name": "N", "labor_terms_id": 2, "per_hour_details": {}, "line_items": [{"item_id": uid(1), "quantity": 1, "tax_id": True}]}],
            "line_items[0].tax_id: expected a positive whole number",
        ),
    ):
        with pytest.raises(ToolError) as info:
            await call_directly(client_factory, contracts.create_contract, name="N", client_id=1, service_lines=lines, confirm=True)
        assert fragment in str(info.value), (lines, str(info.value))
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "prop, shown",
    [
        ("ClientId", "client_id"),
        ("Name", "name"),
        ("StartDate", "start_date"),
        ("DaysUntilDue", "days_until_due"),
        ("BrandingThemeId", "branding_theme_id"),
        ("ContactIds", "contact_ids"),
        ("ServiceLines[0].BlockHoursDetails.Balance", "service_lines (item 1, BlockHoursDetails, Balance)"),
        ("ServiceLines[1].LineItems[0].Quantity", "service_lines (item 2, LineItems item 1, Quantity)"),
        ("ServiceLines", "service_lines"),
    ],
)
async def test_create_contract_maps_a_gorelo_400_to_the_parameter_to_fix(server, mock_gorelo, prop, shown):
    mock_gorelo.on("POST", "/v1/contracts", error_envelope(400, [("070101", "Not valid.", prop)]))
    text = await call_tool_error(server, "create_contract", create_args())
    assert text == rejected("create_contract", 400, "070101", f"{shown}: Not valid.")
    assert sent_ops(mock_gorelo) == ["POST /v1/contracts"]  # a refusal is not read back


async def test_create_contract_does_not_read_back_a_refused_write_and_never_repeats_it(server, mock_gorelo):
    mock_gorelo.on("POST", "/v1/contracts", error_envelope(500, [("070001", "Internal error.")]))
    text = await call_tool_error(server, "create_contract", create_args())
    assert "Gorelo rejected create_contract (HTTP 500" in text and "Verify with a read before retrying" in text
    assert sent_ops(mock_gorelo) == ["POST /v1/contracts"]


async def test_create_contract_says_the_write_is_unconfirmed_after_a_timeout(server, mock_gorelo):
    import httpx

    mock_gorelo.on("POST", "/v1/contracts", httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "create_contract", create_args())
    assert text.startswith("Gorelo did not confirm create_contract (the request timed out). The change may or may not have been applied.")
    assert len(mock_gorelo.requests) == 1  # a write is never sent again


@pytest.mark.parametrize("answer", [None, {}, {"Name": "x"}, {"Id": 0}, {"Id": True}, [], "55"])
async def test_create_contract_refuses_an_answer_without_a_usable_id(server, mock_gorelo, answer):
    mock_gorelo.on("POST", "/v1/contracts", envelope(answer))
    text = await call_tool_error(server, "create_contract", create_args())
    assert text.startswith("Gorelo returned an unexpected response for create_contract")
    assert "may have been applied" in text and "verify it with a read before repeating it" in text
    assert sent_ops(mock_gorelo) == ["POST /v1/contracts"]


async def test_create_contract_returns_id_and_warning_when_the_read_back_fails(server, mock_gorelo):
    mock_gorelo.on("POST", "/v1/contracts", envelope(made()))
    mock_gorelo.on("GET", "/v1/contracts/55", error_envelope(500, [("070001", "Internal error.")]))
    result = await call_tool(server, "create_contract", create_args())
    assert set(result) == {"Id", "warning"} and result["Id"] == 55
    assert result["warning"].startswith("the write succeeded; re-reading it failed")
    assert "Do not repeat the write" in result["warning"]
    assert len(mock_gorelo.calls("POST")) == 1


@pytest.mark.parametrize(
    "shown, fragment",
    [
        (made(name="Other"), "Name is not the name that was sent"),
        (made(client_id=1), "Client is not the client that was sent"),
        (made(lines=[("Support", 2), ("Extra", 2)]), "ServiceLines does not hold the 1 line(s) that were sent"),
        (made(lines=[("Support", 4)]), "the labor terms of the service lines are not the ones that were sent"),
        (made(lines=[("Renamed", 2)]), "the names of the service lines are not the ones that were sent"),
        (contract_detail(55, ServiceLines=None), "ServiceLines does not hold the 1 line(s) that were sent"),
    ],
)
async def test_create_contract_says_so_when_the_read_back_does_not_match_what_was_sent(server, mock_gorelo, shown, fragment):
    mock_gorelo.on("POST", "/v1/contracts", envelope(made()))
    mock_gorelo.on("GET", "/v1/contracts/55", envelope(shown))
    text = await call_tool_error(server, "create_contract", create_args())
    assert text.startswith("Gorelo returned an unexpected response for create_contract")
    assert fragment in text and "the write may have been applied" in text and "get_contract, contract 55" in text
    assert len(mock_gorelo.calls("POST")) == 1


async def test_create_contract_compares_names_after_trimming(server, mock_gorelo):
    mock_gorelo.on("POST", "/v1/contracts", envelope(made(name="Managed Services", lines=[("Support", 2)])))
    mock_gorelo.on("GET", "/v1/contracts/55", envelope(made(name="Managed Services", lines=[("Support", 2)])))
    await call_tool(server, "create_contract", create_args(name="  Managed Services ", service_lines=[hourly_line(name=" Support ")]))


async def test_create_contract_has_no_status_and_no_delete_option(server_factory):
    listed = {t.name: t for t in await list_tools(server_factory(destructive=True))}
    properties = set(listed["create_contract"].inputSchema["properties"])
    assert not properties & {"status", "status_id", "contract_id", "clear_fields", "id"}
    assert listed["create_contract"].inputSchema["required"] == ["name", "client_id", "service_lines"]


# --------------------------------------------------------------------------
# update_contract
# --------------------------------------------------------------------------


async def test_a_plain_update_sends_the_patch_and_reads_the_contract_back_without_a_read_before(server, mock_gorelo):
    before = three_lines()
    after = edited(before, lambda r: r.update(Name="Renamed", DaysUntilDue=14, AutoApproveAndSend=False, DaysBeforeInvoiceCreation=0))
    mock_gorelo.on("PATCH", "/v1/contracts/9", envelope(after))
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(after))
    result = await call_tool(
        server, "update_contract", update_args(name="Renamed", days_until_due=14, auto_approve_and_send=False, days_before_invoice_creation=0)
    )
    assert sent_ops(mock_gorelo) == ["PATCH /v1/contracts/9", "GET /v1/contracts/9"]
    patch = mock_gorelo.requests[0]
    assert patch.query == {} and patch.json == {
        "Name": "Renamed", "DaysUntilDue": 14, "AutoApproveAndSend": False, "DaysBeforeInvoiceCreation": 0
    }
    assert result == after


async def test_an_update_sends_every_scalar_field_in_its_gorelo_shape(server, mock_gorelo):
    after = three_lines(
        Name="N", StartDate="2026-03-01", EndDate="2027-02-28", RepeatPeriod={"Id": 2, "Name": "Quarterly"}, DaysBeforeInvoiceCreation=7,
        DaysUntilDue=21, ProrateType={"Id": 2, "Name": "x"}, AutoInvoiceTicketProducts=False, AutoApproveAndSend=True,
        Contacts=[{"Id": 1, "Name": "a"}, {"Id": 2, "Name": "b"}], IsForAllLocations=False, LocationIds=[4, 3], Reference="R",
        InvoiceTemplateId=7, InvoiceEmailTemplateId=8, BrandingThemeId=uid(3).upper(),
    )
    mock_gorelo.on("PATCH", "/v1/contracts/9", envelope(after))
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(after))
    result = await call_tool(
        server,
        "update_contract",
        update_args(
            name="N", reference="R", start_date="2026-03-01", end_date="2027-02-28", repeat_period_id=2, days_before_invoice_creation=7,
            days_until_due=21, auto_approve_and_send=True, contact_ids=[1, 2], is_for_all_locations=False, location_ids=[3, 4],
            prorate_type_id=2, auto_invoice_ticket_products=False, invoice_template_id=7, invoice_email_template_id=8,
            branding_theme_id=uid(3),
        ),
    )
    assert mock_gorelo.requests[0].json == {
        "Name": "N", "Reference": "R", "StartDate": "2026-03-01T00:00:00Z", "EndDate": "2027-02-28T00:00:00Z", "RepeatPeriodId": 2,
        "DaysBeforeInvoiceCreation": 7, "DaysUntilDue": 21, "AutoApproveAndSend": True, "ContactIds": [1, 2],
        "IsForAllLocations": False, "LocationIds": [3, 4], "ProrateTypeId": 2, "AutoInvoiceTicketProducts": False,
        "InvoiceTemplateId": 7, "InvoiceEmailTemplateId": 8, "BrandingThemeId": uid(3),
    }
    assert result == after
    assert "ClientId" not in mock_gorelo.requests[0].json


async def test_clear_fields_send_null_and_an_empty_contact_list(server, mock_gorelo):
    after = three_lines(
        Reference=None, EndDate=None, InvoiceTemplateId=None, InvoiceEmailTemplateId=None, BrandingThemeId=None, Contacts=[]
    )
    mock_gorelo.on("PATCH", "/v1/contracts/9", envelope(after))
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(after))
    await call_tool(
        server,
        "update_contract",
        update_args(clear_fields=["reference", "end_date", "invoice_template_id", "invoice_email_template_id", "branding_theme_id", "contact_ids"]),
    )
    body = mock_gorelo.requests[0].json
    assert body == {
        "Reference": None, "EndDate": None, "InvoiceTemplateId": None, "InvoiceEmailTemplateId": None, "BrandingThemeId": None,
        "ContactIds": [],
    }
    assert body["Reference"] is None and body["ContactIds"] == []


@pytest.mark.parametrize(
    "field, wire, still",
    [
        ("reference", "Reference", {"Reference": "MSA"}),
        ("end_date", "EndDate", {"EndDate": "2027-01-01"}),
        ("invoice_template_id", "InvoiceTemplateId", {"InvoiceTemplateId": 3}),
        ("invoice_email_template_id", "InvoiceEmailTemplateId", {"InvoiceEmailTemplateId": 3}),
        ("branding_theme_id", "BrandingThemeId", {"BrandingThemeId": uid(4)}),
        ("contact_ids", "ContactIds", {"Contacts": [{"Id": 1, "Name": "a"}]}),
    ],
)
async def test_a_clear_that_the_read_back_does_not_show_is_reported(server, mock_gorelo, field, wire, still):
    cleared = {"Reference": None, "EndDate": None, "InvoiceTemplateId": None, "InvoiceEmailTemplateId": None, "BrandingThemeId": None, "Contacts": []}
    after = three_lines(**{**cleared, **still})
    mock_gorelo.on("PATCH", "/v1/contracts/9", envelope(after))
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(after))
    text = await call_tool_error(server, "update_contract", update_args(clear_fields=[field]))
    assert "the read-back does not show the change" in text and "may have been applied" in text


@pytest.mark.parametrize("name, status_id", [("archived", 3), ("active", 1)])
async def test_status_archives_with_3_and_restores_with_1(server, mock_gorelo, name, status_id):
    after = three_lines(Status={"Id": status_id, "Name": name})
    mock_gorelo.on("PATCH", "/v1/contracts/9", envelope(after))
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(after))
    result = await call_tool(server, "update_contract", update_args(status=name))
    assert mock_gorelo.requests[0].json == {"StatusId": status_id}
    assert result["Status"]["Id"] == status_id


async def test_a_restored_contract_whose_end_date_has_passed_reads_expired_and_is_not_a_failure(server, mock_gorelo):
    after = three_lines(Status={"Id": 2, "Name": "Expired"}, EndDate="2026-01-31")
    mock_gorelo.on("PATCH", "/v1/contracts/9", envelope(after))
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(after))
    result = await call_tool(server, "update_contract", update_args(status="active"))
    assert result["Status"]["Id"] == 2


@pytest.mark.parametrize("shown", [1, 2])
async def test_an_archive_that_the_read_back_does_not_show_is_reported(server, mock_gorelo, shown):
    after = three_lines(Status={"Id": shown, "Name": "x"})
    mock_gorelo.on("PATCH", "/v1/contracts/9", envelope(after))
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(after))
    text = await call_tool_error(server, "update_contract", update_args(status="archived"))
    assert "Status is not the one that was asked for" in text


@pytest.mark.parametrize("bad", ["expired", "Archived", "archive", 3, True, ""])
async def test_status_takes_exactly_active_or_archived(server, mock_gorelo, bad):
    text = await call_tool_error(server, "update_contract", update_args(status=bad))
    assert "status" in text
    assert mock_gorelo.requests == []


async def test_the_client_cannot_be_changed_so_there_is_no_client_parameter(server, mock_gorelo, server_factory):
    listed = {t.name: t for t in await list_tools(server_factory(destructive=True))}
    assert "client_id" not in listed["update_contract"].inputSchema["properties"]
    text = await call_tool_error(server, "update_contract", update_args(client_id=9101, name="x"))
    assert "client_id" in text
    assert mock_gorelo.requests == []


async def test_update_contract_refuses_without_confirm_and_sends_nothing(server, mock_gorelo):
    for args in ({"name": "x"}, {"status": "archived"}, {"remove_service_line_ids": [79]}, {"auto_approve_and_send": True}):
        text = await call_tool_error(server, "update_contract", update_args(confirm=False, **args))
        assert text.startswith("confirm: refusing to update contract 9 without confirm=true.")
        assert text.endswith("Call again with confirm=true if you really want to update contract 9.")
        assert "Nothing has been sent to Gorelo." in text
    assert mock_gorelo.requests == []


async def test_the_update_refusal_says_what_would_happen(server, mock_gorelo):
    text = await call_tool_error(server, "update_contract", update_args(confirm=False, status="archived"))
    assert "The contract is archived (StatusId 3); contracts cannot be deleted through this server." in text
    text = await call_tool_error(server, "update_contract", update_args(confirm=False, status="active"))
    assert "The contract is restored (StatusId 1)." in text
    text = await call_tool_error(
        server,
        "update_contract",
        update_args(
            confirm=False,
            remove_service_line_ids=[78, 79],
            service_lines=[{"id": 77, "name": "x"}, {"name": "New", "labor_terms_id": 3, "block_hours_details": {"template_id": 2}}],
            auto_approve_and_send=True,
            clear_fields=["reference"],
        ),
    )
    assert "Gorelo replaces the contract's service lines: 1 service line updated, 1 added, 2 removed (78, 79; Gorelo refuses a line with un-invoiced time)" in text
    assert "the tool sends every other current line unchanged" in text
    assert (
        "1 Block Hours line (labor_terms_id 3, new or converted from No Labor Terms): each new or converted block raises a "
        "Draft invoice for the block at once (service_lines[1]: the price of Block Hours Template 2)." in text
    )
    assert "auto_approve_and_send=true makes Gorelo approve and send every invoice the contract raises on its own" in text
    assert "Cleared: reference." in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", ["true", "yes", 1, 0, None])
async def test_update_contract_needs_a_strict_boolean_confirm(server, mock_gorelo, bad):
    text = await call_tool_error(server, "update_contract", update_args(name="x", confirm=bad))
    assert "confirm" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "args, fragment",
    [
        ({}, "nothing to change: give at least one of name, status, reference"),
        ({"contract_id": 0, "name": "x"}, "contract_id: expected a positive whole number such as 123, got zero or a negative number"),
        ({"name": " "}, "name: must not be empty or whitespace only"),
        ({"reference": " "}, "reference: must not be empty or whitespace only"),
        ({"reference": "R", "clear_fields": ["reference"]}, "reference: cannot be given a value and cleared in the same call"),
        ({"end_date": "2027-01-01", "clear_fields": ["end_date"]}, "end_date: cannot be given a value and cleared in the same call"),
        ({"contact_ids": [1], "clear_fields": ["contact_ids"]}, "contact_ids: cannot be given a value and cleared in the same call"),
        ({"contact_ids": []}, "contact_ids: expected at least one id, got an empty list (to remove every auto-approve contact pass clear_fields=['contact_ids'])"),
        ({"clear_fields": []}, "clear_fields: must not be an empty list"),
        ({"clear_fields": ["name"]}, "clear_fields"),
        ({"clear_fields": ["start_date"]}, "clear_fields"),
        ({"start_date": "2026-03-01 00:00"}, "start_date: expected a calendar date as YYYY-MM-DD"),
        ({"start_date": "2026-05-01", "end_date": "2026-05-01"}, "end_date: 2026-05-01 is not after start_date 2026-05-01"),
        ({"repeat_period_id": 9}, "repeat_period_id: expected 1 Monthly, 2 Quarterly, 3 Yearly, 4 Half Yearly, got 9"),
        ({"prorate_type_id": 5}, "prorate_type_id: expected 0 Do not prorate"),
        ({"days_before_invoice_creation": 40}, "days_before_invoice_creation: expected a whole number from 0 to 31, got 40"),
        ({"is_for_all_locations": True, "location_ids": [1]}, "location_ids: only with is_for_all_locations=false"),
        (
            {"is_for_all_locations": False, "auto_invoice_ticket_products": True, "location_ids": [1]},
            "auto_invoice_ticket_products: only on a contract that covers all locations",
        ),
        ({"branding_theme_id": "x"}, "branding_theme_id"),
        ({"invoice_template_id": 0}, "invoice_template_id: expected a positive whole number"),
        ({"service_lines": []}, "service_lines: expected at least one entry, got an empty list"),
        ({"remove_service_line_ids": []}, "remove_service_line_ids: expected at least one id"),
        ({"remove_service_line_ids": [5, 5]}, "remove_service_line_ids[1]: the id appears more than once"),
        ({"remove_service_line_ids": [0]}, "remove_service_line_ids[0]: expected a positive whole number"),
        ({"service_lines": [{"id": 77}]}, "service_lines[0]: names only an id, which changes nothing"),
        ({"service_lines": [{"id": 77, "name": "a"}, {"id": 77, "name": "b"}]}, "service_lines[1].id: the service line appears more than once"),
        (
            {"service_lines": [{"id": 77, "name": "a"}], "remove_service_line_ids": [77]},
            "service_lines[0].id: the line is also in remove_service_line_ids; update it or remove it, not both",
        ),
        ({"service_lines": [{"id": 0, "name": "a"}]}, "service_lines[0].id: expected a positive whole number"),
        ({"service_lines": [{"name": "New"}]}, "service_lines[0].labor_terms_id: required on a new service line"),
        ({"service_lines": [{"labor_terms_id": 4}]}, "service_lines[0].name: required on a new service line"),
        ({"service_lines": [{"name": "N", "labor_terms_id": 2}]}, "service_lines[0].per_hour_details: required for labor_terms_id 2 (Hourly)"),
        (
            {"service_lines": [{"name": "N", "labor_terms_id": 3, "block_hours_details": {"template_id": 0}}]},
            "service_lines[0].block_hours_details: template_id 0 (Custom) needs balance and price",
        ),
        (
            {"service_lines": [{"name": "N", "labor_terms_id": 4, "auto_approve_time": True}]},
            "service_lines[0].auto_approve_time: not accepted on a No Labor Terms line",
        ),
        (
            {"service_lines": [{"name": "N", "labor_terms_id": 4, "remove_line_item_ids": [1]}]},
            "service_lines[0].remove_line_item_ids: a new service line has no line items to remove",
        ),
        (
            {"service_lines": [{"id": 77, "line_items": [{"id": 5, "quantity": 1}], "remove_line_item_ids": [5]}]},
            "service_lines[0].remove_line_item_ids: line item 5 is also updated in line_items; update it or remove it, not both",
        ),
        ({"service_lines": [{"id": 77, "remove_line_item_ids": [5, 5]}]}, "service_lines[0].remove_line_item_ids[1]: the id appears more than once"),
        ({"service_lines": [{"id": 77, "line_items": []}]}, "service_lines[0].line_items: expected at least one line item, got an empty list"),
        (
            {"service_lines": [{"id": 77, "line_items": [{"id": 5, "quantity": 1}, {"id": 5, "quantity": 2}]}]},
            "service_lines[0].line_items[1].id: the line item appears more than once",
        ),
        ({"service_lines": [{"id": 77, "line_items": [{"quantity": 1}]}]}, "service_lines[0].line_items[0]: missing item_id (a new line item needs both)"),
        ({"service_lines": [{"id": 77, "line_items": [{"item_id": uid(1)}]}]}, "service_lines[0].line_items[0]: missing quantity (a new line item needs both)"),
        (
            {"service_lines": [{"id": 77, "line_items": [{"id": 5, "quantity": 0}]}]},
            "service_lines[0].line_items[0].quantity: expected a number above 0",
        ),
        (
            {"service_lines": [{"id": 77, "line_items": [{"id": 5, "description": "d", "clear_fields": ["description"]}]}]},
            "service_lines[0].line_items[0].description: cannot be given a value and cleared in the same call",
        ),
        (
            {"service_lines": [{"id": 77, "line_items": [{"id": 5, "end_date": "2026-12-31", "clear_fields": ["end_date"]}]}]},
            "line_items[0].end_date: cannot be given a value and cleared in the same call",
        ),
        (
            {"service_lines": [{"id": 77, "line_items": [{"id": 5, "clear_fields": ["quantity"]}]}]},
            "clear_fields.0",
        ),
        (
            {"service_lines": [{"name": "N", "labor_terms_id": 4, "line_items": [{"item_id": uid(1), "quantity": 1, "clear_fields": ["end_date"]}]}]},
            "line_items[0].clear_fields: only for an existing line item (one with id)",
        ),
    ],
)
async def test_update_contract_rejects_bad_input_before_any_http_call(server, mock_gorelo, args, fragment):
    text = await call_tool_error(server, "update_contract", update_args(**args))
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_a_line_that_gets_no_other_change_is_just_left_out_of_the_list(server, mock_gorelo):
    """A line you do not name is kept: there is nothing to say for it, so naming it with an id alone is refused."""
    text = await call_tool_error(server, "update_contract", update_args(service_lines=[{"id": 78}]))
    assert "names only an id, which changes nothing (lines you do not name are kept)" in text
    assert mock_gorelo.requests == []


async def test_updating_one_line_keeps_every_other_line_as_an_id_alone(server, mock_gorelo):
    before = three_lines()
    after = edited(before, lambda r: line_of(r, 78).update(Name="Renamed", PerHourDetails={"RateType": {"Id": 2, "Name": "Use fixed labor rate"}, "HourlyRate": 150.0}))
    serve_update(mock_gorelo, before, after)
    result = await call_tool(
        server,
        "update_contract",
        update_args(service_lines=[{"id": 78, "name": "Renamed", "per_hour_details": {"rate_type_id": 2, "hourly_rate": 150}}]),
    )
    assert sent_ops(mock_gorelo) == ["GET /v1/contracts/9", "PATCH /v1/contracts/9", "GET /v1/contracts/9"]
    assert mock_gorelo.requests[1].json == {
        "ServiceLines": [
            {"Id": 77},
            {"Id": 78, "Name": "Renamed", "PerHourDetails": {"RateTypeId": 2, "HourlyRate": 150}},
            {"Id": 79},
        ]
    }
    assert result == after


async def test_a_line_the_call_does_not_name_is_never_removed(server, mock_gorelo):
    before = three_lines()
    after = edited(before, lambda r: r.update(Name="Renamed"))
    serve_update(mock_gorelo, before, after)
    await call_tool(server, "update_contract", update_args(name="Renamed", service_lines=[{"id": 79, "name": "Nothing"}]))
    assert [line["Id"] for line in mock_gorelo.requests[1].json["ServiceLines"]] == [77, 78, 79]


async def test_remove_service_line_ids_removes_exactly_those_lines(server, mock_gorelo):
    before = three_lines()
    after = edited(before, lambda r: r.update(ServiceLines=[line for line in r["ServiceLines"] if line["Id"] != 79]))
    serve_update(mock_gorelo, before, after)
    result = await call_tool(server, "update_contract", update_args(remove_service_line_ids=[79]))
    assert mock_gorelo.requests[1].json == {"ServiceLines": [{"Id": 77}, {"Id": 78}]}
    assert [line["Id"] for line in result["ServiceLines"]] == [77, 78]


async def test_a_new_line_is_added_after_the_current_ones(server, mock_gorelo):
    before = three_lines()
    new = {"name": "Extra", "labor_terms_id": 1, "unlimited_hours_details": {}, "line_items": [{"item_id": uid(1), "quantity": 2}]}
    after = edited(before, lambda r: r["ServiceLines"].append(line_record(80, "Extra", 1, items=(8,))))
    line_of(after, 80)["LineItems"][0]["Quantity"] = 2.0
    serve_update(mock_gorelo, before, after)
    await call_tool(server, "update_contract", update_args(service_lines=[new]))
    assert mock_gorelo.requests[1].json == {
        "ServiceLines": [
            {"Id": 77}, {"Id": 78}, {"Id": 79},
            {"Name": "Extra", "LaborTermsId": 1, "UnlimitedHoursDetails": {}, "LineItems": [{"ItemId": uid(1), "Quantity": 2}]},
        ]
    }


async def test_a_line_can_be_replaced_by_removing_one_and_adding_one_in_the_same_call(server, mock_gorelo):
    before = three_lines()

    def change(record):
        record["ServiceLines"] = [line for line in record["ServiceLines"] if line["Id"] != 79]
        record["ServiceLines"].append(line_record(81, "Replacement", 2))

    after = edited(before, change)
    serve_update(mock_gorelo, before, after)
    await call_tool(
        server,
        "update_contract",
        update_args(
            remove_service_line_ids=[79],
            service_lines=[{"name": "Replacement", "labor_terms_id": 2, "per_hour_details": {}}],
        ),
    )
    assert mock_gorelo.requests[1].json == {
        "ServiceLines": [{"Id": 77}, {"Id": 78}, {"Name": "Replacement", "LaborTermsId": 2, "PerHourDetails": {}}]
    }


async def test_a_line_without_line_item_params_is_sent_without_line_items(server, mock_gorelo):
    before = three_lines()
    after = edited(before, lambda r: line_of(r, 77).update(Name="Renamed"))
    serve_update(mock_gorelo, before, after)
    await call_tool(server, "update_contract", update_args(service_lines=[{"id": 77, "name": "Renamed"}]))
    assert "LineItems" not in mock_gorelo.requests[1].json["ServiceLines"][0]


async def test_line_items_replace_the_set_so_every_current_item_is_kept_unless_named(server, mock_gorelo):
    before = three_lines()

    def change(record):
        line = line_of(record, 77)
        line["LineItems"][0]["Quantity"] = 3.0
        line["LineItems"].append({"Id": 8, "Name": "New", "Quantity": 1.0})

    after = edited(before, change)
    serve_update(mock_gorelo, before, after)
    await call_tool(
        server,
        "update_contract",
        update_args(service_lines=[{"id": 77, "line_items": [{"id": 5, "quantity": 3}, {"item_id": uid(1), "quantity": 1}]}]),
    )
    assert mock_gorelo.requests[1].json == {
        "ServiceLines": [
            {"Id": 77, "LineItems": [{"Id": 5, "Quantity": 3}, {"Id": 6}, {"ItemId": uid(1), "Quantity": 1}]},
            {"Id": 78},
            {"Id": 79},
        ]
    }


async def test_remove_line_item_ids_removes_exactly_those_items(server, mock_gorelo):
    before = three_lines()
    after = edited(before, lambda r: line_of(r, 77).update(LineItems=[item for item in line_of(r, 77)["LineItems"] if item["Id"] != 6]))
    serve_update(mock_gorelo, before, after)
    await call_tool(server, "update_contract", update_args(service_lines=[{"id": 77, "remove_line_item_ids": [6]}]))
    assert mock_gorelo.requests[1].json == {"ServiceLines": [{"Id": 77, "LineItems": [{"Id": 5}]}, {"Id": 78}, {"Id": 79}]}


async def test_every_line_item_can_be_removed_when_the_call_names_each_one(server, mock_gorelo):
    before = three_lines()
    after = edited(before, lambda r: line_of(r, 77).update(LineItems=[]))
    serve_update(mock_gorelo, before, after)
    await call_tool(server, "update_contract", update_args(service_lines=[{"id": 77, "remove_line_item_ids": [5, 6]}]))
    assert mock_gorelo.requests[1].json["ServiceLines"][0] == {"Id": 77, "LineItems": []}


async def test_line_item_fields_clear_with_null(server, mock_gorelo):
    before = three_lines()
    line_of(before, 77)["LineItems"][0].update(Description="Old", EndDate="2027-01-01")
    after = edited(before, lambda r: line_of(r, 77)["LineItems"][0].update(Description=None, EndDate=None, StartDate="2026-05-01"))
    serve_update(mock_gorelo, before, after)
    await call_tool(
        server,
        "update_contract",
        update_args(service_lines=[{"id": 77, "line_items": [{"id": 5, "clear_fields": ["description", "end_date"], "start_date": "2026-05-01"}]}]),
    )
    assert mock_gorelo.requests[1].json["ServiceLines"][0] == {
        "Id": 77,
        "LineItems": [{"Id": 5, "StartDate": "2026-05-01T00:00:00Z", "Description": None, "EndDate": None}, {"Id": 6}],
    }


async def test_a_no_labor_terms_line_takes_labor_terms_once_with_its_details(server, mock_gorelo):
    before = three_lines()
    after = edited(
        before,
        lambda r: line_of(r, 79).update(
            LaborTerms={"Id": 3, "Name": "Block Hours"}, BlockHoursDetails={"TemplateId": 0, "Balance": 10.0}
        ),
    )
    serve_update(mock_gorelo, before, after)
    await call_tool(
        server,
        "update_contract",
        update_args(service_lines=[{"id": 79, "labor_terms_id": 3, "block_hours_details": {"template_id": 0, "balance": 10, "price": 500}}]),
    )
    assert mock_gorelo.requests[1].json["ServiceLines"][2] == {
        "Id": 79, "LaborTermsId": 3, "BlockHoursDetails": {"TemplateId": 0, "Balance": 10, "Price": 500}
    }


async def test_the_labor_terms_a_line_already_has_are_not_sent_again(server, mock_gorelo):
    before = three_lines()
    after = edited(before, lambda r: line_of(r, 78).update(Name="Hourly 2"))
    serve_update(mock_gorelo, before, after)
    await call_tool(server, "update_contract", update_args(service_lines=[{"id": 78, "labor_terms_id": 2, "name": "Hourly 2"}]))
    assert mock_gorelo.requests[1].json["ServiceLines"][1] == {"Id": 78, "Name": "Hourly 2"}


async def test_an_existing_block_line_can_change_its_balance_and_thresholds(server, mock_gorelo):
    before = three_lines()
    after = edited(
        before,
        lambda r: line_of(r, 77)["BlockHoursDetails"].update(
            Balance=40.0, WarningThreshold=30.0, OverrunThreshold=5.0, ShowRemainingHoursInClientPortal=True
        ),
    )
    serve_update(mock_gorelo, before, after)
    await call_tool(
        server,
        "update_contract",
        update_args(
            service_lines=[
                {"id": 77, "block_hours_details": {"balance": 40, "warning_threshold": 30, "overrun_threshold": 5, "show_remaining_hours_in_client_portal": True}}
            ]
        ),
    )
    assert mock_gorelo.requests[1].json["ServiceLines"][0] == {
        "Id": 77,
        "BlockHoursDetails": {"Balance": 40, "WarningThreshold": 30, "OverrunThreshold": 5, "ShowRemainingHoursInClientPortal": True},
    }


async def test_the_hourly_rate_follows_the_rate_type_the_line_has_now(server, mock_gorelo):
    before = three_lines()
    line_of(before, 78)["PerHourDetails"] = {"RateType": {"Id": 2, "Name": "Use fixed labor rate"}, "HourlyRate": 100.0}
    after = edited(before, lambda r: line_of(r, 78)["PerHourDetails"].update(HourlyRate=120.0))
    serve_update(mock_gorelo, before, after)
    await call_tool(server, "update_contract", update_args(service_lines=[{"id": 78, "per_hour_details": {"hourly_rate": 120}}]))
    assert mock_gorelo.requests[1].json["ServiceLines"][1] == {"Id": 78, "PerHourDetails": {"HourlyRate": 120}}


@pytest.mark.parametrize(
    "args, fragment",
    [
        ({"remove_service_line_ids": [999]}, "remove_service_line_ids[0]: the contract has no service line with that id (get_contract lists them)"),
        ({"service_lines": [{"id": 999, "name": "x"}]}, "service_lines[0].id: the contract has no service line with that id (get_contract lists them)"),
        ({"service_lines": [{"id": 77, "labor_terms_id": 1, "unlimited_hours_details": {}}]}, "service_lines[0].labor_terms_id: the line is Block Hours; only a No Labor Terms line can change its labor terms (once)"),
        ({"service_lines": [{"id": 78, "labor_terms_id": 4}]}, "service_lines[0].labor_terms_id: the line is Hourly; only a No Labor Terms line can change"),
        ({"service_lines": [{"id": 77, "labor_terms_id": 9}]}, "service_lines[0].labor_terms_id: expected 1 Unlimited Hours"),
        ({"service_lines": [{"id": 77, "per_hour_details": {}}]}, "service_lines[0].per_hour_details: does not match labor_terms_id 3 (Block Hours); give block_hours_details only"),
        ({"service_lines": [{"id": 79, "block_hours_details": {"template_id": 0}}]}, "service_lines[0].block_hours_details: does not match labor_terms_id 4 (No Labor Terms); a No Labor Terms line takes none"),
        ({"service_lines": [{"id": 77, "block_hours_details": {"template_id": 3}}]}, "service_lines[0].block_hours_details.template_id: cannot change on an existing Block Hours line"),
        ({"service_lines": [{"id": 77, "block_hours_details": {"price": 3}}]}, "service_lines[0].block_hours_details.price: cannot change on an existing Block Hours line"),
        ({"service_lines": [{"id": 77, "block_hours_details": {"warning_threshold": 95}}]}, "block_hours_details.warning_threshold: expected a whole number from 0 to 90, got 95"),
        ({"service_lines": [{"id": 78, "per_hour_details": {"hourly_rate": 100}}]}, "service_lines[0].per_hour_details.hourly_rate: only with rate_type_id 2"),
        ({"service_lines": [{"id": 78, "per_hour_details": {"rate_type_id": 2}}]}, "per_hour_details.hourly_rate: required with rate_type_id 2"),
        ({"service_lines": [{"id": 79, "work_type_ids": [1]}]}, "service_lines[0].work_type_ids: not accepted on a No Labor Terms line"),
        ({"service_lines": [{"id": 79, "auto_approve_time": True}]}, "service_lines[0].auto_approve_time: not accepted on a No Labor Terms line"),
        ({"service_lines": [{"id": 78, "included_time_goes_to_id": 1}]}, "service_lines[0].included_time_goes_to_id: only on Unlimited, Block and Limited Hours lines, got labor terms 2 (Hourly)"),
        (
            {"service_lines": [{"id": 79, "labor_terms_id": 3}]},
            "service_lines[0].block_hours_details: required for labor_terms_id 3 (Block Hours)",
        ),
        (
            {"service_lines": [{"id": 79, "labor_terms_id": 5, "limited_hours_details": {}}]},
            "service_lines[0].limited_hours_details.allocated_hours: required on a new Limited Hours line",
        ),
        (
            {"service_lines": [{"id": 77, "remove_line_item_ids": [999]}]},
            "service_lines[0].remove_line_item_ids[0]: the service line has no line item with that id (get_contract lists them)",
        ),
        (
            {"service_lines": [{"id": 77, "line_items": [{"id": 999, "quantity": 1}]}]},
            "service_lines[0].line_items: the service line has no line item with id 999 (get_contract lists them)",
        ),
        (
            {"service_lines": [{"id": 77, "line_items": [{"id": 7, "quantity": 1}]}]},
            "service_lines[0].line_items: the service line has no line item with id 7",
        ),
        (
            {"remove_service_line_ids": [77, 78, 79]},
            "remove_service_line_ids: this would remove every service line; a contract keeps at least one",
        ),
    ],
)
async def test_update_contract_refuses_what_the_read_shows_is_wrong_and_never_patches(server, mock_gorelo, args, fragment):
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(three_lines()))
    text = await call_tool_error(server, "update_contract", update_args(**args))
    assert fragment in text
    assert sent_ops(mock_gorelo) == ["GET /v1/contracts/9"]


async def test_removing_every_line_is_allowed_when_a_new_line_takes_its_place(server, mock_gorelo):
    before = three_lines()
    after = edited(before, lambda r: r.update(ServiceLines=[line_record(90, "Only", 4)]))
    serve_update(mock_gorelo, before, after)
    await call_tool(
        server,
        "update_contract",
        update_args(remove_service_line_ids=[77, 78, 79], service_lines=[{"name": "Only", "labor_terms_id": 4}]),
    )
    assert mock_gorelo.requests[1].json == {"ServiceLines": [{"Name": "Only", "LaborTermsId": 4}]}


@pytest.mark.parametrize(
    "broken, fragment",
    [
        (lambda r: r.update(ServiceLines=None), "expected ServiceLines to be a list but got null"),
        (lambda r: r.update(ServiceLines="x"), "expected ServiceLines to be a list but got a string"),
        (lambda r: r["ServiceLines"][0].pop("Id"), "a service line of the contract has no usable Id"),
        (lambda r: r["ServiceLines"][1].update(Id=True), "a service line of the contract has no usable Id"),
        (lambda r: r["ServiceLines"][0].update(Id="77"), "a service line of the contract has no usable Id"),
        (lambda r: r["ServiceLines"][0].update(LaborTerms=None), "service line 77 shows no usable LaborTerms"),
        (lambda r: r["ServiceLines"][0].update(LaborTerms={"Id": 9}), "service line 77 shows no usable LaborTerms"),
    ],
)
async def test_a_contract_read_that_cannot_be_used_stops_the_update_before_any_write(server, mock_gorelo, broken, fragment):
    record = three_lines()
    broken(record)
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(record))
    text = await call_tool_error(server, "update_contract", update_args(service_lines=[{"id": 77, "name": "x"}]))
    assert text.startswith("Gorelo returned an unexpected response for update_contract")
    assert fragment in text and "refusing to guess, so nothing was changed" in text
    assert "may have been applied" not in text  # nothing was sent
    assert sent_ops(mock_gorelo) == ["GET /v1/contracts/9"]


@pytest.mark.parametrize(
    "broken, fragment",
    [
        (lambda r: r["ServiceLines"][0].update(LineItems=None), "expected LineItems of service line 77 to be a list but got null"),
        (lambda r: r["ServiceLines"][0]["LineItems"][0].pop("Id"), "a line item of service line 77 has no usable Id"),
        (lambda r: r["ServiceLines"][0]["LineItems"].append("x"), "a line item of service line 77 has no usable Id"),
    ],
)
async def test_line_items_that_cannot_be_read_stop_a_line_item_update_before_any_write(server, mock_gorelo, broken, fragment):
    record = three_lines()
    broken(record)
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(record))
    text = await call_tool_error(server, "update_contract", update_args(service_lines=[{"id": 77, "remove_line_item_ids": [5]}]))
    assert fragment in text and sent_ops(mock_gorelo) == ["GET /v1/contracts/9"]


async def test_the_line_items_of_a_line_the_call_does_not_touch_are_never_read_or_checked(server, mock_gorelo):
    record = three_lines()
    line_of(record, 78)["LineItems"] = None  # unreadable, but this call does not touch the items of line 78
    mock_gorelo.on("GET", "/v1/contracts/9", in_order(envelope(record), envelope(record)))
    mock_gorelo.on("PATCH", "/v1/contracts/9", envelope(record))
    await call_tool(server, "update_contract", update_args(service_lines=[{"id": 77, "name": "Support"}]))
    assert sent_ops(mock_gorelo) == ["GET /v1/contracts/9", "PATCH /v1/contracts/9", "GET /v1/contracts/9"]


async def test_the_read_before_an_update_that_fails_is_reported_as_a_read(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts/9", error_envelope(404, [("070404", "Contract not found.", "contractId")]))
    text = await call_tool_error(server, "update_contract", update_args(remove_service_line_ids=[79]))
    assert text == rejected("update_contract", 404, "070404", "contract_id: Contract not found.")
    assert sent_ops(mock_gorelo) == ["GET /v1/contracts/9"]


@pytest.mark.parametrize("answer", [None, {}, [], False])
async def test_a_read_before_an_update_that_is_not_a_record_stops_it(server, mock_gorelo, answer):
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(answer))
    text = await call_tool_error(server, "update_contract", update_args(remove_service_line_ids=[79]))
    assert text.startswith("Gorelo returned an unexpected response for update_contract")
    assert sent_ops(mock_gorelo) == ["GET /v1/contracts/9"]


async def test_location_ids_alone_are_refused_when_the_contract_covers_all_locations(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(three_lines(IsForAllLocations=True)))
    text = await call_tool_error(server, "update_contract", update_args(location_ids=[4]))
    assert "location_ids: the contract covers all locations, so Gorelo would ignore the list; pass is_for_all_locations=false with it" in text
    assert "Nothing was changed." in text
    assert sent_ops(mock_gorelo) == ["GET /v1/contracts/9"]


async def test_location_ids_alone_are_sent_when_the_contract_is_scoped_to_locations(server, mock_gorelo):
    before = three_lines(IsForAllLocations=False, LocationIds=[3])
    after = three_lines(IsForAllLocations=False, LocationIds=[4, 5])
    mock_gorelo.on("GET", "/v1/contracts/9", in_order(envelope(before), envelope(after)))
    mock_gorelo.on("PATCH", "/v1/contracts/9", envelope(after))
    await call_tool(server, "update_contract", update_args(location_ids=[4, 5]))
    assert sent_ops(mock_gorelo) == ["GET /v1/contracts/9", "PATCH /v1/contracts/9", "GET /v1/contracts/9"]
    assert mock_gorelo.requests[1].json == {"LocationIds": [4, 5]}


async def test_location_ids_with_the_flag_need_no_read_before(server, mock_gorelo):
    after = three_lines(IsForAllLocations=False, LocationIds=[4])
    mock_gorelo.on("PATCH", "/v1/contracts/9", envelope(after))
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(after))
    await call_tool(server, "update_contract", update_args(location_ids=[4], is_for_all_locations=False))
    assert sent_ops(mock_gorelo) == ["PATCH /v1/contracts/9", "GET /v1/contracts/9"]
    assert mock_gorelo.requests[0].json == {"IsForAllLocations": False, "LocationIds": [4]}


@pytest.mark.parametrize(
    "prop, shown",
    [
        ("StartDate", "start_date"),
        ("RepeatPeriodId", "repeat_period_id"),
        ("StatusId", "status"),
        ("EndDate", "end_date"),
        ("ContactIds", "contact_ids"),
        ("ServiceLines[1].LineItems[0].Quantity", "service_lines (item 2, LineItems item 1, Quantity)"),
        ("ServiceLines[0].BlockHoursDetails.TemplateId", "service_lines (item 1, BlockHoursDetails, TemplateId)"),
        ("contractId", "contract_id"),
    ],
)
async def test_update_contract_maps_a_gorelo_refusal_to_the_parameter_to_fix(server, mock_gorelo, prop, shown):
    mock_gorelo.on("PATCH", "/v1/contracts/9", error_envelope(409, [("070409", "Refused.", prop)]))
    text = await call_tool_error(server, "update_contract", update_args(name="x"))
    assert text == rejected("update_contract", 409, "070409", f"{shown}: Refused.")
    assert sent_ops(mock_gorelo) == ["PATCH /v1/contracts/9"]  # a refused write is not read back


async def test_update_contract_says_the_write_is_unconfirmed_after_a_timeout_and_never_repeats_it(server, mock_gorelo):
    import httpx

    mock_gorelo.on("PATCH", "/v1/contracts/9", httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "update_contract", update_args(name="x"))
    assert text.startswith("Gorelo did not confirm update_contract (the request timed out). The change may or may not have been applied.")
    assert sent_ops(mock_gorelo) == ["PATCH /v1/contracts/9"]


@pytest.mark.parametrize("answer", [None, {}, [], "x"])
async def test_update_contract_refuses_a_patch_answer_that_is_not_a_record(server, mock_gorelo, answer):
    mock_gorelo.on("PATCH", "/v1/contracts/9", envelope(answer))
    text = await call_tool_error(server, "update_contract", update_args(name="x"))
    assert text.startswith("Gorelo returned an unexpected response for update_contract")
    assert "may have been applied" in text and "verify it with a read before repeating it" in text
    assert sent_ops(mock_gorelo) == ["PATCH /v1/contracts/9"]


async def test_update_contract_returns_id_and_warning_when_the_read_back_fails(server, mock_gorelo):
    mock_gorelo.on("PATCH", "/v1/contracts/9", envelope(three_lines(Name="x")))
    mock_gorelo.on("GET", "/v1/contracts/9", error_envelope(500, [("070001", "Internal error.")]))
    result = await call_tool(server, "update_contract", update_args(name="x"))
    assert set(result) == {"Id", "warning"} and result["Id"] == 9
    assert "the write succeeded; re-reading it failed" in result["warning"] and "Do not repeat the write" in result["warning"]
    assert len(mock_gorelo.calls("PATCH")) == 1


@pytest.mark.parametrize(
    "args, shown, fragment",
    [
        ({"name": "New"}, {"Name": "Old"}, "Name is not the name that was sent"),
        ({"days_until_due": 14}, {"DaysUntilDue": 30}, "DaysUntilDue is not the value that was sent"),
        ({"auto_approve_and_send": True}, {"AutoApproveAndSend": False}, "AutoApproveAndSend is not the value that was sent"),
        ({"repeat_period_id": 2}, {}, "RepeatPeriodId is not the value that was sent"),
        ({"prorate_type_id": 1}, {}, "ProrateTypeId is not the value that was sent"),
        ({"start_date": "2026-03-01"}, {}, "StartDate is not the date that was sent"),
        ({"end_date": "2026-12-31"}, {"EndDate": None}, "EndDate is not the date that was sent"),
        ({"reference": "R"}, {}, "Reference is not the value that was sent"),
        ({"invoice_template_id": 7}, {}, "InvoiceTemplateId is not the value that was sent"),
        ({"branding_theme_id": uid(5)}, {"BrandingThemeId": uid(6)}, "BrandingThemeId is not the theme that was sent"),
        ({"contact_ids": [1, 2]}, {}, "Contacts are not the ones that were sent"),
        ({"is_for_all_locations": False, "location_ids": [4]}, {}, "LocationIds are not the ones that were sent"),
    ],
)
async def test_update_contract_says_so_when_the_read_back_does_not_show_a_field(server, mock_gorelo, args, shown, fragment):
    after = three_lines(**shown)
    mock_gorelo.on("PATCH", "/v1/contracts/9", envelope(after))
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(after))
    text = await call_tool_error(server, "update_contract", update_args(**args))
    assert text.startswith("Gorelo returned an unexpected response for update_contract")
    assert fragment in text and "the write may have been applied" in text and "get_contract, contract 9" in text
    assert len(mock_gorelo.calls("PATCH")) == 1


@pytest.mark.parametrize(
    "change, args, fragment",
    [
        (lambda r: None, {"remove_service_line_ids": [79]}, "a service line that was removed is still there"),
        (lambda r: r["ServiceLines"].pop(0), {"service_lines": [{"id": 78, "name": "Hourly"}]}, "a service line that should stay is missing"),
        (
            lambda r: None,
            {"service_lines": [{"name": "Extra", "labor_terms_id": 4}]},
            "the contract shows 0 new service line(s), 1 were added",
        ),
        (
            lambda r: r["ServiceLines"].append(line_record(80, "Other", 4)),
            {"service_lines": [{"name": "Extra", "labor_terms_id": 4}]},
            "the names of the added service lines are not the ones that were sent",
        ),
        (lambda r: None, {"service_lines": [{"id": 77, "name": "Renamed"}]}, "service line 77: Name is not the name that was sent"),
        (
            lambda r: None,
            {"service_lines": [{"id": 79, "labor_terms_id": 3, "block_hours_details": {"template_id": 4}}]},
            "service line 79: LaborTerms is not the one that was sent",
        ),
        (lambda r: None, {"service_lines": [{"id": 77, "auto_approve_time": True}]}, "service line 77: AutoApproveTime is not the value that was sent"),
        (lambda r: None, {"service_lines": [{"id": 77, "auto_invoice_time": True}]}, "service line 77: AutoInvoiceTime is not the value that was sent"),
        (lambda r: None, {"service_lines": [{"id": 77, "included_time_goes_to_id": 2}]}, "service line 77: IncludedTimeGoesTo is not the one that was sent"),
        (lambda r: None, {"service_lines": [{"id": 77, "remove_line_item_ids": [6]}]}, "service line 77: a line item that was removed is still there"),
        (
            lambda r: line_of(r, 77)["LineItems"].pop(0),
            {"service_lines": [{"id": 77, "line_items": [{"id": 6, "quantity": 2}]}]},
            "service line 77: a line item that should stay is missing",
        ),
        (
            lambda r: None,
            {"service_lines": [{"id": 77, "line_items": [{"item_id": uid(1), "quantity": 1}]}]},
            "service line 77: the line items added are not the number that was sent",
        ),
        (
            lambda r: line_of(r, 77).update(LineItems=None),
            {"service_lines": [{"id": 77, "remove_line_item_ids": [6]}]},
            "service line 77: LineItems is missing from the answer",
        ),
        (lambda r: r.update(ServiceLines=None), {"service_lines": [{"id": 77, "name": "Support"}]}, "ServiceLines is missing from the answer"),
    ],
)
async def test_update_contract_checks_the_lines_and_items_the_read_back_shows(server, mock_gorelo, change, args, fragment):
    before = three_lines()
    after = edited(before, change)
    serve_update(mock_gorelo, before, after)
    text = await call_tool_error(server, "update_contract", update_args(**args))
    assert text.startswith("Gorelo returned an unexpected response for update_contract")
    assert fragment in text and "the write may have been applied" in text
    assert len(mock_gorelo.calls("PATCH")) == 1


async def test_the_read_before_and_the_read_back_use_the_same_path(server, mock_gorelo):
    before = three_lines()
    serve_update(mock_gorelo, before, before)
    await call_tool(server, "update_contract", update_args(service_lines=[{"id": 77, "name": "Support"}]))
    assert [request.path for request in mock_gorelo.requests] == ["/v1/contracts/9"] * 3
    assert [request.method for request in mock_gorelo.requests] == ["GET", "PATCH", "GET"]
    assert all(request.query == {} for request in mock_gorelo.requests)


async def test_update_contract_takes_the_largest_gorelo_id(server, mock_gorelo):
    after = three_lines(Name="x")
    mock_gorelo.on("PATCH", f"/v1/contracts/{MAX_ID}", envelope(after))
    mock_gorelo.on("GET", f"/v1/contracts/{MAX_ID}", envelope(after))
    await call_tool(server, "update_contract", update_args(contract_id=MAX_ID, name="x"))
    assert mock_gorelo.requests[0].path == f"/v1/contracts/{MAX_ID}"


@pytest.mark.parametrize("bad", [True, "9", 9.0, None])
async def test_update_contract_refuses_a_contract_id_that_is_not_a_whole_number(server, mock_gorelo, bad):
    text = await call_tool_error(server, "update_contract", update_args(contract_id=bad, name="x"))
    assert "contract_id" in text
    assert mock_gorelo.requests == []


async def test_a_direct_call_checks_what_the_schema_would_have_refused(client_factory, mock_gorelo):
    cases = [
        ({"service_lines": "x"}, "service_lines: expected a list of service lines, got a string"),
        ({"service_lines": [None]}, "service_lines[0]: must be an object, got null"),
        ({"service_lines": [{"id": 77, "bogus": 1}]}, "service_lines[0]: unknown field(s) bogus"),
        ({"service_lines": [{"id": 77, "name": "x", "line_items": "x"}]}, "service_lines[0].line_items: expected a list of line items, got a string"),
        (
            {"service_lines": [{"id": 77, "line_items": [{"id": 5, "item_id": uid(1)}]}]},
            "service_lines[0].line_items[0].item_id: not accepted on an existing line item",
        ),
        (
            {"service_lines": [{"id": 77, "line_items": [{"id": 5, "clear_fields": "end_date"}]}]},
            "line_items[0].clear_fields: expected a list such as ['end_date'], got a string",
        ),
        (
            {"service_lines": [{"id": 77, "line_items": [{"id": 5, "clear_fields": []}]}]},
            "line_items[0].clear_fields: expected at least one name",
        ),
        ({"service_lines": [{"id": 77, "line_items": [{"id": 5, "bogus": 1}]}]}, "line_items[0]: unknown field(s) bogus"),
        ({"clear_fields": "end_date"}, "clear_fields: expected a list such as ['end_date'], got a string"),
        ({"clear_fields": [3]}, "clear_fields[0]: expected a field name such as 'end_date', got a number"),
        ({"status": "gone"}, "status: expected 'active' or 'archived', got a string"),
        ({"remove_service_line_ids": "7"}, "remove_service_line_ids: expected a list of ids such as [123, 456], got a string"),
        ({"is_for_all_locations": "no"}, "is_for_all_locations: must be true or false, got a string"),
    ]
    for kwargs, fragment in cases:
        with pytest.raises(ToolError) as info:
            await call_directly(client_factory, contracts.update_contract, contract_id=9, confirm=True, **kwargs)
        assert fragment in str(info.value), (kwargs, str(info.value))
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "bad, said",
    [
        (float("nan"), "expected a finite number"),
        (float("inf"), "expected a finite number"),
        (10**400, "expected a finite number"),
        (True, "expected a number, got a boolean"),
        ("5", "expected a number, got a string"),
    ],
)
async def test_every_numeric_value_must_be_a_finite_number(client_factory, mock_gorelo, bad, said):
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(three_lines()))
    kwargs = {"service_lines": [{"id": 77, "block_hours_details": {"balance": bad}}]}
    with pytest.raises(ToolError) as info:
        await call_directly(client_factory, contracts.update_contract, contract_id=9, confirm=True, **kwargs)
    assert f"service_lines[0].block_hours_details.balance: {said}" in str(info.value)
    assert sent_ops(mock_gorelo) == ["GET /v1/contracts/9"]  # never a PATCH


# --------------------------------------------------------------------------
# The inputs follow the spec, field for field
# --------------------------------------------------------------------------


def pascal(name):
    return "".join(part.capitalize() for part in name.split("_"))


def schema_props(name):
    spec = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))
    return set(spec["components"]["schemas"][name].get("properties", {}))


@needs_spec_text
@pytest.mark.parametrize(
    "model, schema, extra, absent",
    [
        (contracts.WorkRoleRate, "ContractCustomWorkRoleRateInput", set(), set()),
        (contracts.WorkTypeRate, "ContractCustomWorkTypeRateInput", set(), set()),
        (contracts.CustomRates, "ContractCustomRatesInput", set(), set()),
        (contracts.UnlimitedHoursDetails, "UnlimitedHoursDetailsInput", set(), set()),
        (contracts.PerHourDetails, "PerHourDetailsInput", set(), set()),
        (contracts.LimitedHoursDetails, "LimitedHoursDetailsInput", set(), set()),
        (contracts.BlockHoursDetails, "BlockHoursDetailsInput", set(), set()),
        (contracts.NewLineItem, "ContractLineItemInput", set(), {"Id"}),
        (contracts.LineItemChange, "ContractLineItemInput", {"ClearFields"}, {"ItemId"} - {"ItemId"}),
        (contracts.NewServiceLine, "ContractServiceLineInput", set(), {"Id"}),
        (contracts.ServiceLineChange, "ContractServiceLineInput", {"RemoveLineItemIds"}, set()),
    ],
)
def test_the_input_models_have_exactly_the_fields_of_the_spec_schemas(model, schema, extra, absent):
    have = {pascal(name) for name in model.model_fields}
    assert have - extra == schema_props(schema) - absent
    assert model.model_config.get("extra") == "forbid"  # additionalProperties false


@needs_spec_text
def test_the_contract_level_parameters_are_the_fields_of_the_two_commands():
    spec_create = schema_props("CreateContractCommand")
    assert {contracts.CONTRACT_FIELDS[param] for param in contracts.CONTRACT_FIELDS} | {"ClientId"} == spec_create
    assert set(contracts.CREATE_FIELDS.values()) == spec_create
    update = schema_props("UpdateContractCommand")
    # status is StatusId; ClientId "Not accepted: a contract's client can't change" is not offered
    assert {value for key, value in contracts.UPDATE_FIELDS.items() if key != "contract_id"} == update - {"ClientId"}


@needs_spec_text
def test_the_scales_are_the_ones_the_spec_text_documents():
    spec = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))["components"]["schemas"]
    text = " ".join(spec["ContractServiceLineInput"]["properties"]["LaborTermsId"]["description"].split())
    assert "1 Unlimited Hours, 2 Hourly, 3 Block Hours, 4 No Labor Terms, 5 Limited Hours" in text
    assert contracts.LABOR_TERMS_SCALE == "1 Unlimited Hours, 2 Hourly, 3 Block Hours, 4 No Labor Terms, 5 Limited Hours"
    included = " ".join(spec["ContractServiceLineInput"]["properties"]["IncludedTimeGoesToId"]["description"].split())
    assert "1 Complete (default) or 2 Waiting to invoice" in included and "Unlimited, Block and Limited Hours only" in included
    assert contracts.INCLUDED_TIME == {1: "Complete", 2: "Waiting to invoice"} and contracts.INCLUDED_TIME_TERMS == (1, 3, 5)
    rates = " ".join(spec["PerHourDetailsInput"]["properties"]["RateTypeId"]["description"].split())
    assert "1 Use default labor rates, 2 Use fixed labor rate, 3 Custom labor rates. Default 1" in rates
    assert "Required with RateTypeId 2, refused with any other" in " ".join(spec["PerHourDetailsInput"]["properties"]["HourlyRate"]["description"].split())
    block = {name: " ".join(spec["BlockHoursDetailsInput"]["properties"][name]["description"].split()) for name in spec["BlockHoursDetailsInput"]["properties"]}
    assert "0 for Custom" in block["TemplateId"] and "Balance and Price come from it and are refused" in block["TemplateId"]
    assert "a whole number 0-90" in block["WarningThreshold"] and (contracts.WARNING_THRESHOLD_MAX,) == (90,)
    assert "a whole number 0-50" in block["OverrunThreshold"] and (contracts.OVERRUN_THRESHOLD_MAX,) == (50,)
    command = {name: " ".join(spec["CreateContractCommand"]["properties"][name]["description"].split()) for name in spec["CreateContractCommand"]["properties"]}
    assert "0 Do not prorate (default), 1 Prorate and add to next invoice, 2 Prorate and generate standalone invoice" in command["ProrateTypeId"]
    assert "1 Monthly (default), 2 Quarterly, 3 Yearly, 4 Half Yearly" in command["RepeatPeriodId"]
    assert "0-31" in command["DaysBeforeInvoiceCreation"]
    assert "after StartDate" in command["EndDate"]
    assert "Only with IsForAllLocations true" in command["AutoInvoiceTicketProducts"]
    assert "Ignored otherwise" in command["LocationIds"]
    assert contracts.PRORATE_TYPES == {0: "Do not prorate", 1: "Prorate and add to next invoice", 2: "Prorate and generate standalone invoice"}
    assert contracts.REPEAT_PERIODS == {1: "Monthly", 2: "Quarterly", 3: "Yearly", 4: "Half Yearly"}
    item = {name: " ".join(spec["ContractLineItemInput"]["properties"][name]["description"].split()) for name in spec["ContractLineItemInput"]["properties"]}
    assert "1 Billable (default), 2 No charge, 3 Non-billable" in item["BillableStatusId"]
    assert "Greater than 0" in item["Quantity"] and "Not negative" in item["UnitPrice"] and "Not negative" in item["UnitCost"]
    assert "0-100" in item["DiscountPercent"]
    update = {name: " ".join(spec["UpdateContractCommand"]["properties"][name]["description"].split()) for name in spec["UpdateContractCommand"]["properties"]}
    assert "1 Active or 3 Archived" in update["StatusId"] and contracts.STATUS_WRITES == {"active": 1, "archived": 3}
    assert "Not accepted" in update["ClientId"]
    assert "[]` removes them all" in update["ContactIds"]
    for field in ("Reference", "EndDate", "InvoiceTemplateId", "InvoiceEmailTemplateId", "BrandingThemeId"):
        assert "null" in update[field], field
    assert set(contracts.CONTRACT_CLEAR_VALUES) == {"reference", "end_date", "invoice_template_id", "invoice_email_template_id", "branding_theme_id", "contact_ids"}
    for field in ("StartDate", "RepeatPeriodId"):
        assert "409" in update[field]


@needs_spec_text
def test_the_operation_texts_say_what_the_tool_texts_promise():
    create = " ".join(operation("post", "/v1/contracts")["description"].split())
    assert "A Block Hours line also raises a Draft invoice for the block" in create
    assert "A setting a labor term can't have is a 400 naming it" in create
    update = " ".join(operation("patch", "/v1/contracts/{contractId}")["description"].split())
    assert "`ServiceLines` replaces the set, matched by `Id`" in update
    assert "a line left out is removed, which is refused with 409 while it has time logged that hasn't been invoiced" in update
    assert "Each line's `LineItems` replaces its set the same way" in update
    assert "Only a No Labor Terms line can change its labor terms" in update
    assert "`StatusId` 3 archives the contract and 1 restores it" in update
    assert "`ClientId` can't change" in update
    assert "DELETE /v1/contracts/{contractId}" in FORBIDDEN_OPS


async def test_the_tool_texts_carry_the_side_effects_in_words(server_factory):
    listed = {t.name: t for t in await list_tools(server_factory(destructive=True))}
    said = {name: " ".join(listed[name].description.split()) for name in EXPECTED_GATED}
    for name, text in said.items():
        assert "Ask the user first; needs confirm=true." in text, name
        assert "Side effects:" in text, name
        assert "auto_approve_and_send=true makes Gorelo approve and send the recurring invoices on its own" in text, name
        assert "approval pushes them to the accounting system" in text, name
        assert "cannot be deleted through this server" in text, name
        # the invoices of contracts were seen EMAILED to the client; the consistency test (EMAIL_WHO) pins who
        assert "it emails each to the client and approval pushes them" in text, name
    assert "A Block Hours line raises a Draft invoice for the block at once" in said["create_contract"]
    # a line that is converted to Block Hours raises the block invoice too
    assert "A new or converted Block Hours line raises a Draft invoice for the block at once" in said["update_contract"]
    assert "REPLACES service lines and line items" in said["update_contract"]
    assert "keeps every line and line item you do not name" in said["update_contract"]
    assert "remove_service_line_ids" in said["update_contract"] and "remove_line_item_ids" in said["update_contract"]


# --------------------------------------------------------------------------
# Whole-module guard
# --------------------------------------------------------------------------


async def test_each_tool_sends_exactly_the_operations_it_declares(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts", paged_envelope([contract(9)]))
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(contract_detail(9)))
    specs = module_specs()
    for name, args in (("list_contracts", {}), ("get_contract", {"contract_id": 9})):
        mock_gorelo.reset()
        await call_tool(server, name, args)
        sent = {f"{r.method} {r.path}" for r in mock_gorelo.requests}
        assert sent == {op.replace("{contractId}", "9") for op in specs[name].ops}, name


async def test_the_gated_tools_send_exactly_the_operations_they_declare(server, mock_gorelo):
    specs = module_specs()
    mock_gorelo.on("POST", "/v1/contracts", envelope(made()))
    mock_gorelo.on("GET", "/v1/contracts/55", envelope(made()))
    await call_tool(server, "create_contract", create_args())
    assert {f"{r.method} {r.path}" for r in mock_gorelo.requests} == {op.replace("{contractId}", "55") for op in specs["create_contract"].ops}
    mock_gorelo.reset()
    before = three_lines()
    serve_update(mock_gorelo, before, before)
    await call_tool(server, "update_contract", update_args(service_lines=[{"id": 77, "name": "Support"}]))
    assert {f"{r.method} {r.path}" for r in mock_gorelo.requests} == {op.replace("{contractId}", "9") for op in specs["update_contract"].ops}


async def test_no_request_to_a_contract_delete_can_be_made_even_by_the_client(client_factory, mock_gorelo):
    from gorelo_client import GoreloAPIError

    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.delete("DELETE /v1/contracts/{contractId}", path_params={"contractId": 9}, tool="update_contract")
    assert info.value.kind in ("forbidden", "spec")
    assert mock_gorelo.requests == []


# --------------------------------------------------------------------------
# 1.5.0 regressions of the contract tools
# --------------------------------------------------------------------------


def convert_79(**overrides):
    """Regression: the No Labor Terms line 79 is converted to a custom Block Hours line."""
    return {"id": 79, "labor_terms_id": 3, "block_hours_details": {"template_id": 0, "balance": 10, "price": 5000, **overrides}}


async def test_converting_a_line_to_block_hours_says_it_raises_a_block_invoice_and_names_the_price(server, mock_gorelo):
    text = await call_tool_error(server, "update_contract", update_args(confirm=False, service_lines=[convert_79()]))
    assert (
        "1 Block Hours line (labor_terms_id 3, new or converted from No Labor Terms): each new or converted block raises a "
        "Draft invoice for the block at once (service_lines[0]: price 5000)." in text
    )
    assert "Nothing has been sent to Gorelo." in text and mock_gorelo.requests == []


async def test_new_and_converted_block_lines_are_counted_together_each_with_its_price(server, mock_gorelo):
    new = {"name": "Block", "labor_terms_id": 3, "block_hours_details": {"template_id": 0, "balance": 5, "price": 250.5}}
    text = await call_tool_error(server, "update_contract", update_args(confirm=False, service_lines=[new, convert_79()]))
    assert "2 Block Hours lines (labor_terms_id 3" in text
    assert "(service_lines[0]: price 250.5; service_lines[1]: price 5000)" in text


async def test_a_call_with_no_block_line_does_not_mention_a_block_invoice(server, mock_gorelo):
    text = await call_tool_error(server, "update_contract", update_args(confirm=False, service_lines=[{"id": 78, "name": "x"}]))
    assert "Block Hours" not in text and "Draft invoice" not in text


def test_the_effect_function_counts_a_converted_line_the_way_the_review_probe_does():
    plans, removes = contracts._plan_lines([convert_79()], None)
    blocks = len(contracts._block_lines(plans))
    assert blocks == 1  # before the fix only a NEW line (id None) was counted, so this was 0
    effect = contracts._update_effect(status=None, plans=plans, removes=removes, auto_approve_and_send=None, clear=[], blocks=blocks)
    assert "raises a Draft invoice for the block at once (service_lines[0]: price 5000)" in effect


# --------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["create_contract", "update_contract"])
async def test_the_refusal_of_auto_approve_and_send_says_the_invoices_are_emailed_to_the_client(server, mock_gorelo, tool):
    args = create_args(confirm=False, auto_approve_and_send=True) if tool == "create_contract" else update_args(confirm=False, auto_approve_and_send=True)
    text = await call_tool_error(server, tool, args)
    assert "each one is EMAILED to the client (seen live: sent about 2 minutes after it was created)" in text
    assert "approving pushes each one to the connected accounting system" in text
    assert mock_gorelo.requests == []


async def test_a_call_without_auto_approve_does_not_mention_email(server, mock_gorelo):
    text = await call_tool_error(server, "update_contract", update_args(confirm=False, name="x"))
    assert "EMAILED" not in text and "email" not in text.lower()


async def test_both_tool_texts_and_the_parameter_text_say_email(server_factory):
    listed = {t.name: t for t in await list_tools(server_factory(destructive=True))}
    for name in EXPECTED_GATED:
        assert "it emails each to the client and approval pushes them to the accounting system" in " ".join(listed[name].description.split())
        assert "emails it to the client" in listed[name].inputSchema["properties"]["auto_approve_and_send"]["description"]


# --------------------------------------------------------------------------


async def test_list_contracts_takes_calendar_dates_and_sends_them_as_midnight_utc(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts", paged_envelope([contract(9)]))
    result = await call_tool(server, "list_contracts", {"end_date_since": "2026-12-31", "end_date_before": "2027-01-01"})
    assert mock_gorelo.last.query == {"EndDateSince": "2026-12-31T00:00:00Z", "EndDateBefore": "2027-01-01T00:00:00Z", "PageSize": "50"}
    assert result["filters"]["end_date_since"] == "2026-12-31" and result["filters"]["end_date_before"] == "2027-01-01"


@pytest.mark.parametrize(
    "value", ["2026-12-31T00:00:00Z", "2026-12-31T00:00:00-05:00", "2026-12-31T00:00:00", "2026-02-30", "20261231", "soon"]
)
@pytest.mark.parametrize("param", ["end_date_since", "end_date_before"])
async def test_an_instant_or_a_bad_date_is_refused_before_any_request(server, mock_gorelo, param, value):
    # regression: an instant with an offset used to be converted to UTC, shifting the day Gorelo compares
    text = await call_tool_error(server, "list_contracts", {param: value})
    assert text.startswith(f"{param}: expected a calendar date as YYYY-MM-DD")
    assert mock_gorelo.requests == []


async def test_the_next_page_repeats_the_same_calendar_dates(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts", paged_responder([[contract(1)], [contract(2)]], total_count=2))
    args = {"end_date_since": "2026-12-31", "page_size": 1}
    first = await call_tool(server, "list_contracts", args)
    await call_tool(server, "list_contracts", {**args, "cursor": first["next_cursor"]})
    assert mock_gorelo.requests[1].query == {"EndDateSince": "2026-12-31T00:00:00Z", "PageSize": "1", "Cursor": "c1"}


async def test_the_parameter_texts_say_yyyy_mm_dd(server_factory):
    properties = {t.name: t for t in await list_tools(server_factory(destructive=True))}["list_contracts"].inputSchema["properties"]
    assert properties["end_date_since"]["description"].startswith("YYYY-MM-DD: ends on or after that day")
    assert properties["end_date_before"]["description"].startswith("YYYY-MM-DD: ends strictly before that day")


# --------------------------------------------------------------------------
# An index in a Gorelo error names the caller's entry, not a position of the list the tool built
# --------------------------------------------------------------------------


def refused_at(prop, message="Refused."):
    return error_envelope(400, [("070101", message, prop)])


async def test_an_error_on_a_converted_line_names_the_callers_entry_not_the_wire_position(server, mock_gorelo):
    # regression: the wire list is [77, 78, 79] and the converted line 79 is wire item 3; the caller gave ONE entry
    before = three_lines()
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(before))
    mock_gorelo.on("PATCH", "/v1/contracts/9", refused_at("ServiceLines[2].BlockHoursDetails.Balance", "Balance is required."))
    text = await call_tool_error(server, "update_contract", update_args(service_lines=[convert_79()]))
    assert text == rejected("update_contract", 400, "070101", "service_lines (item 1, BlockHoursDetails, Balance): Balance is required.")


async def test_a_new_line_after_the_current_ones_and_an_updated_line_map_to_their_own_entries(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(three_lines()))
    new = {"name": "Extra", "labor_terms_id": 2, "per_hour_details": {}}
    # wire: [77 kept, 78 updated, 79 kept, new]; the caller's list is [new, 78]
    mock_gorelo.on(
        "PATCH", "/v1/contracts/9",
        error_envelope(
            400,
            [
                ("070101", "HourlyRate is required.", "ServiceLines[3].PerHourDetails.HourlyRate"),
                ("070101", "Name is too long.", "ServiceLines[1].Name"),
            ],
        ),
    )
    text = await call_tool_error(server, "update_contract", update_args(service_lines=[new, {"id": 78, "name": "Hourly 2"}]))
    assert text == rejected(
        "update_contract", 400, "070101",
        "service_lines (item 1, PerHourDetails, HourlyRate): HourlyRate is required.; service_lines (item 2, Name): Name is too long.",
    )


async def test_a_kept_line_the_caller_did_not_name_is_called_a_kept_line_by_its_id(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(three_lines()))
    mock_gorelo.on("PATCH", "/v1/contracts/9", refused_at("ServiceLines[0].PerHourDetails.HourlyRate", "HourlyRate is required."))
    text = await call_tool_error(server, "update_contract", update_args(service_lines=[{"id": 79, "name": "Renamed"}]))
    assert text == rejected(
        "update_contract", 400, "070101", "kept service line Id 77 (PerHourDetails, HourlyRate): HourlyRate is required."
    )


async def test_line_item_indexes_are_translated_too(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(three_lines()))
    # wire items of line 77: [5 kept, 6 updated, new]; the caller's line_items are [new, 6]
    mock_gorelo.on(
        "PATCH", "/v1/contracts/9",
        error_envelope(
            400,
            [
                ("070101", "Quantity must be above 0.", "ServiceLines[0].LineItems[2].Quantity"),
                ("070101", "Price is not allowed.", "ServiceLines[0].LineItems[1].UnitPrice"),
                ("070101", "Tax not found.", "ServiceLines[0].LineItems[0].TaxId"),
            ],
        ),
    )
    items = [{"item_id": uid(1), "quantity": 1}, {"id": 6, "unit_price": 3}]
    text = await call_tool_error(server, "update_contract", update_args(service_lines=[{"id": 77, "line_items": items}]))
    assert text == rejected(
        "update_contract", 400, "070101",
        "service_lines (item 1, LineItems item 1, Quantity): Quantity must be above 0.; "
        "service_lines (item 1, LineItems item 2, UnitPrice): Price is not allowed.; "
        "service_lines (item 1, kept line item Id 5, TaxId): Tax not found.",
    )


async def test_the_items_of_a_new_line_keep_the_callers_order(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(three_lines()))
    mock_gorelo.on("PATCH", "/v1/contracts/9", refused_at("ServiceLines[3].LineItems[1].Quantity", "Quantity must be above 0."))
    new = {
        "name": "Extra", "labor_terms_id": 4,
        "line_items": [{"item_id": uid(1), "quantity": 1}, {"item_id": uid(2), "quantity": 1}],
    }
    text = await call_tool_error(server, "update_contract", update_args(service_lines=[new]))
    assert text == rejected("update_contract", 400, "070101", "service_lines (item 1, LineItems item 2, Quantity): Quantity must be above 0.")


async def test_an_error_on_something_else_and_a_call_that_sent_no_lines_keep_gorelos_own_name(server, mock_gorelo):
    mock_gorelo.on("PATCH", "/v1/contracts/9", refused_at("ServiceLines[4].Name", "Odd."))
    text = await call_tool_error(server, "update_contract", update_args(name="x"))  # no ServiceLines were sent
    assert text == rejected("update_contract", 400, "070101", "service_lines (item 5, Name): Odd.")


async def test_an_index_beyond_the_list_that_was_sent_keeps_gorelos_own_name(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contracts/9", envelope(three_lines()))
    mock_gorelo.on("PATCH", "/v1/contracts/9", refused_at("ServiceLines[9].Name", "Odd."))
    text = await call_tool_error(server, "update_contract", update_args(service_lines=[{"id": 78, "name": "y"}]))
    assert text == rejected("update_contract", 400, "070101", "service_lines (item 10, Name): Odd.")


async def test_create_contract_still_names_the_callers_own_index(server, mock_gorelo):
    mock_gorelo.on("POST", "/v1/contracts", refused_at("ServiceLines[1].PerHourDetails.HourlyRate", "HourlyRate is required."))
    lines = [hourly_line(), hourly_line(name="Second")]
    text = await call_tool_error(server, "create_contract", create_args(service_lines=lines))
    assert text == rejected("create_contract", 400, "070101", "service_lines (item 2, PerHourDetails, HourlyRate): HourlyRate is required.")


# --------------------------------------------------------------------------
# The confirm effect names the line item removals (and changes) per line
# --------------------------------------------------------------------------


async def test_the_refusal_names_the_line_items_that_are_removed_per_line_by_id(server, mock_gorelo):
    lines = [
        {"id": 77, "remove_line_item_ids": [5, 6]},
        {"id": 78, "remove_line_item_ids": [7], "line_items": [{"item_id": uid(1), "quantity": 1}]},
        {"id": 79, "name": "Renamed"},
    ]
    text = await call_tool_error(server, "update_contract", update_args(confirm=False, service_lines=lines))
    assert "Line items replace each line's set: service line 77: line items 5, 6 removed; service line 78: line item 7 removed; " in text
    assert "service line 78: line items 1 added; the tool keeps every other current line item." in text
    assert "service line 79" not in text.split("Line items replace")[1]
    assert mock_gorelo.requests == []


async def test_updated_and_added_line_items_are_counted_per_line(server, mock_gorelo):
    items = [{"id": 5, "quantity": 2}, {"id": 6, "quantity": 3}, {"item_id": uid(1), "quantity": 1}]
    text = await call_tool_error(server, "update_contract", update_args(confirm=False, service_lines=[{"id": 77, "line_items": items}]))
    assert "service line 77: line items 2 updated, 1 added" in text


# --------------------------------------------------------------------------
# The read-back is compared field by field with the first read and with every value that was sent
# --------------------------------------------------------------------------


def flagged(record, number=77, **flags):
    line = line_of(record, number)
    line.update({"AutoApproveTime": False, "AutoInvoiceTime": False, "IncludedTimeGoesTo": {"Id": 1, "Name": "Complete"}, **flags})
    return record


async def expect_unconfirmed(server, mock_gorelo, before, after, args, *fragments, absent=()):
    serve_update(mock_gorelo, before, after)
    text = await call_tool_error(server, "update_contract", update_args(**args))
    assert text.startswith("Gorelo returned an unexpected response for update_contract")
    assert "the write may have been applied" in text and "get_contract, contract 9" in text
    for fragment in fragments:
        assert fragment in text, (fragment, text)
    for fragment in absent:
        assert fragment not in text, (fragment, text)
    assert len(mock_gorelo.calls("PATCH")) == 1
    return text


RENAME_78 = {"service_lines": [{"id": 78, "name": "Hourly"}]}


async def test_the_review_probe_a_kept_line_whose_labor_terms_and_rate_type_changed(server, mock_gorelo):
    before = three_lines()
    after = edited(before, lambda r: (line_of(r, 77).update(LaborTerms={"Id": 2, "Name": "Hourly"}), line_of(r, 78).update(LaborTerms={"Id": 4, "Name": "No Labor Terms"})))
    await expect_unconfirmed(
        server, mock_gorelo, before, after, {"service_lines": [{"id": 79, "name": "Nothing"}]},
        "service line 77: LaborTerms expected 3, read 2", "service line 78: LaborTerms expected 2, read 4",
    )


async def test_a_kept_hourly_detail_that_changed_is_reported(server, mock_gorelo):
    before = three_lines()
    after = edited(before, lambda r: line_of(r, 78)["PerHourDetails"].update(RateType={"Id": 2, "Name": "Use fixed labor rate"}, HourlyRate=99.0))
    await expect_unconfirmed(
        server, mock_gorelo, before, after, {"service_lines": [{"id": 79, "name": "Nothing"}]},
        "service line 78: PerHourDetails: RateType expected 1, read 2", "service line 78: PerHourDetails: HourlyRate expected none, read 99.0",
    )


async def test_a_kept_block_detail_and_the_flags_of_a_kept_line_are_compared_with_the_first_read(server, mock_gorelo):
    before = flagged(three_lines())
    after = edited(
        before,
        lambda r: line_of(r, 77).update(
            AutoApproveTime=True, AutoInvoiceTime=True, IncludedTimeGoesTo={"Id": 2, "Name": "Waiting to invoice"},
            BlockHoursDetails={"TemplateId": 0, "Balance": 5.0, "WarningThreshold": 20.0},
        ),
    )
    await expect_unconfirmed(
        server, mock_gorelo, before, after, RENAME_78,
        "service line 77: AutoApproveTime expected False, read True", "service line 77: AutoInvoiceTime expected False, read True",
        "service line 77: IncludedTimeGoesTo expected 1, read 2", "service line 77: BlockHoursDetails: Balance expected 20.0, read 5.0",
    )


async def test_a_value_sent_in_a_details_object_must_be_the_value_read(server, mock_gorelo):
    before = three_lines()
    args = {"service_lines": [{"id": 77, "block_hours_details": {"balance": 40, "warning_threshold": 30, "overrun_threshold": 5, "show_remaining_hours_in_client_portal": True}}]}
    await expect_unconfirmed(
        server, mock_gorelo, before, before, args,
        "service line 77: BlockHoursDetails: Balance expected 40.0, read 20.0", "BlockHoursDetails: WarningThreshold expected 30, read 20.0",
        "BlockHoursDetails: OverrunThreshold expected 5, read none", "ShowRemainingHoursInClientPortal expected True, read none",
    )


async def test_moving_away_from_a_fixed_rate_expects_the_hourly_rate_to_clear(server, mock_gorelo):
    before = three_lines()
    line_of(before, 78)["PerHourDetails"] = {"RateType": {"Id": 2, "Name": "Use fixed labor rate"}, "HourlyRate": 100.0}
    after = edited(before, lambda r: line_of(r, 78)["PerHourDetails"].update(RateType={"Id": 1, "Name": "Use default labor rates"}, HourlyRate=None))
    serve_update(mock_gorelo, before, after)
    await call_tool(server, "update_contract", update_args(service_lines=[{"id": 78, "per_hour_details": {"rate_type_id": 1}}]))
    assert len(mock_gorelo.calls("PATCH")) == 1


async def test_a_kept_line_item_that_changed_is_reported_per_field(server, mock_gorelo):
    before = three_lines()
    before["ServiceLines"][0]["LineItems"][1].update(DiscountPercent=0.0, Tax={"Id": 2, "Name": "GST"}, StartDate="2026-01-01", EndDate=None)
    after = edited(
        before,
        lambda r: line_of(r, 77)["LineItems"][1].update(
            Quantity=9.0, UnitPrice=11.0, UnitCost=1.0, DiscountPercent=50.0, Tax={"Id": 3, "Name": "VAT"}, StartDate="2026-02-01", EndDate="2026-12-31"
        ),
    )
    await expect_unconfirmed(
        server, mock_gorelo, before, after, {"service_lines": [{"id": 77, "line_items": [{"id": 5, "quantity": 1}]}]},
        "service line 77: line item 6: Quantity expected 1.0, read 9.0", "line item 6: UnitPrice expected 10.0, read 11.0",
        "line item 6: UnitCost expected 4.0, read 1.0", "line item 6: DiscountPercent expected 0.0, read 50.0",
        "line item 6: Tax expected 2, read 3", "line item 6: StartDate expected 2026-01-01, read 2026-02-01",
        "line item 6: EndDate expected none, read 2026-12-31", absent=["line item 5"],
    )


async def test_the_items_of_a_line_that_sent_no_line_items_are_compared_too(server, mock_gorelo):
    before = three_lines()
    after = edited(before, lambda r: line_of(r, 77)["LineItems"][0].update(UnitPrice=99.0))
    await expect_unconfirmed(
        server, mock_gorelo, before, after, {"service_lines": [{"id": 77, "name": "Support"}]},
        "service line 77: line item 5: UnitPrice expected 10.0, read 99.0",
    )


async def test_every_value_sent_on_a_line_item_must_show(server, mock_gorelo):
    before = three_lines()
    after = edited(before, lambda r: line_of(r, 77)["LineItems"][0].update(Quantity=1.0, UnitPrice=10.0, Description="Old"))
    sent = {
        "id": 5, "quantity": 4, "unit_price": 12.5, "unit_cost": 6, "discount_percent": 10, "tax_id": 7, "billable_status_id": 2,
        "description": "New", "start_date": "2026-03-01", "end_date": "2026-09-30",
    }
    text = await expect_unconfirmed(
        server, mock_gorelo, before, after, {"service_lines": [{"id": 77, "line_items": [sent]}]},
        "line item 5: Quantity expected 4.0, read 1.0", "line item 5: UnitPrice expected 12.5, read 10.0", "line item 5: UnitCost expected 6.0, read 4.0",
        "line item 5: DiscountPercent expected 10.0, read none", "line item 5: Tax expected 7, read none",
        "line item 5: BillableStatus expected 2, read none", "line item 5: Description is not the text it should be (text not shown)",
        "line item 5: StartDate expected 2026-03-01, read none", "line item 5: EndDate expected 2026-09-30, read none",
    )
    assert "Old" not in text and "New" not in text


async def test_a_new_line_item_and_a_new_service_line_must_show_what_was_sent(server, mock_gorelo):
    before = three_lines()
    new_line = {"name": "Extra", "labor_terms_id": 3, "block_hours_details": {"template_id": 0, "balance": 10, "price": 500}, "line_items": [{"item_id": uid(3), "quantity": 2}]}
    after = edited(before, lambda r: r["ServiceLines"].append(line_record(80, "Extra", 3, items=(8,), BlockHoursDetails={"TemplateId": 0, "Balance": 5.0})))
    await expect_unconfirmed(
        server, mock_gorelo, before, after, {"service_lines": [new_line]},
        "new service line 1: BlockHoursDetails: Balance expected 10.0, read 5.0", "new service line 1: new line item 1: Quantity expected 2.0, read 1.0",
    )


async def test_work_types_and_roles_that_were_sent_must_be_read_back(server, mock_gorelo):
    before = three_lines()
    line_of(before, 78).update(WorkTypes=[{"Id": 1, "Name": "A"}], WorkRoles=[])
    after = edited(before, lambda r: None)
    await expect_unconfirmed(
        server, mock_gorelo, before, after, {"service_lines": [{"id": 78, "work_type_ids": [1, 2], "work_role_ids": [4]}]},
        "service line 78: WorkTypes expected [1, 2], read [1]", "service line 78: WorkRoles expected [4], read []",
    )


async def test_a_kept_custom_rate_set_that_changed_is_reported(server, mock_gorelo):
    before = three_lines()
    line_of(before, 78)["PerHourDetails"]["CustomRates"] = {"WorkRoles": [{"WorkRoleId": 4, "HourlyRate": 10.0}]}
    after = edited(before, lambda r: line_of(r, 78)["PerHourDetails"].update(CustomRates={"WorkRoles": []}))
    await expect_unconfirmed(
        server, mock_gorelo, before, after, {"service_lines": [{"id": 78, "name": "Hourly"}]},
        "service line 78: PerHourDetails CustomRates are not the ones the contract had",
    )


async def test_rounding_within_the_margin_is_not_a_difference(server, mock_gorelo):
    before = three_lines()
    after = edited(before, lambda r: line_of(r, 77)["LineItems"][0].update(UnitPrice=10.004, Quantity=1.00004))
    serve_update(mock_gorelo, before, after)
    await call_tool(server, "update_contract", update_args(service_lines=[{"id": 77, "name": "Support"}]))


async def test_a_read_back_that_does_not_show_a_field_the_first_read_never_showed_is_fine(server, mock_gorelo):
    # the fixtures of the older tests carry no AutoApproveTime, no WorkTypes and no dates: nothing to compare, so no false alarm
    before = three_lines()
    serve_update(mock_gorelo, before, before)
    result = await call_tool(server, "update_contract", update_args(service_lines=[{"id": 77, "name": "Support"}]))
    assert result == before


async def test_the_unconfirmed_text_never_quotes_a_name_or_a_description(server, mock_gorelo):
    before = three_lines()
    line_of(before, 77)["LineItems"][0]["Description"] = "Secret client text"
    after = edited(before, lambda r: line_of(r, 77)["LineItems"][0].update(Description="Another secret"))
    text = await expect_unconfirmed(
        server, mock_gorelo, before, after, {"service_lines": [{"id": 77, "name": "Support"}]},
        "line item 5: Description is not the text it should be (text not shown)",
    )
    assert "secret" not in text.lower()


async def test_a_converted_line_may_take_gorelos_defaults_for_what_the_call_did_not_send(server, mock_gorelo):
    # No Labor Terms -> Block Hours: where included time goes and the covered work types are filled in by Gorelo
    before = three_lines()
    line_of(before, 79).update(IncludedTimeGoesTo=None, WorkTypes=[], WorkRoles=[], BlockHoursDetails=None)
    after = edited(
        before,
        lambda r: line_of(r, 79).update(
            LaborTerms={"Id": 3, "Name": "Block Hours"}, IncludedTimeGoesTo={"Id": 1, "Name": "Complete"},
            WorkTypes=[{"Id": 1, "Name": "A"}, {"Id": 2, "Name": "B"}], BlockHoursDetails={"TemplateId": 0, "Balance": 10.0},
        ),
    )
    serve_update(mock_gorelo, before, after)
    await call_tool(server, "update_contract", update_args(service_lines=[convert_79()]))
    assert len(mock_gorelo.calls("PATCH")) == 1
