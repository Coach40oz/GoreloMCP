"""tools/contracts.py: list_contracts and get_contract (read only). Offline: MockGorelo plus an in-process client."""

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
)

import tools  # noqa: F401  (importing the package registers every tool)
from gorelo_client import FORBIDDEN_OPS
from tools import _common, contracts
from tools._common import MAX_ID, REGISTRY

pytestmark = pytest.mark.anyio

EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)
DESCRIPTION_MAX, PARAM_DESCRIPTION_MAX = 900, 220  # CONTRIBUTING.md: never more than these

EXPECTED_TOOLS = {
    "list_contracts": ["GET /v1/contracts"],
    "get_contract": ["GET /v1/contracts/{contractId}"],
}


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
    """A ContractDetailModel as GET /v1/contracts/{contractId} returns it."""
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
        "InvoiceDue": 30,
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
                "BlockHoursDetails": {"Hours": 20.0},
                "LimitedHoursDetails": None,
                "PerHourDetails": None,
                "UnlimitedHoursDetails": None,
                "WorkRoles": [{"Id": 3, "Name": "Technician"}],
                "WorkTypes": [{"Id": 4, "Name": "Remote"}],
                "LineItems": [
                    {
                        "Id": 5, "Name": "Block of 20 hours", "Amount": 1500.0, "Quantity": 1.0, "UnitPrice": 1500.0,
                        "BillableStatus": {"Id": 1, "Name": "Billable"}, "ItemType": {"Id": 1, "Name": "Product"},
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
    return server_factory()


def module_specs():
    return {spec.name: spec for spec in REGISTRY.specs if spec.fn.__module__ == "tools.contracts"}


# --------------------------------------------------------------------------
# Declarations
# --------------------------------------------------------------------------


def test_the_module_declares_exactly_the_two_read_tools_in_the_billing_toolset():
    specs = module_specs()
    assert {name: spec.ops for name, spec in specs.items()} == EXPECTED_TOOLS
    assert {spec.kind for spec in specs.values()} == {"read"}
    assert {spec.toolset for spec in specs.values()} == {"billing"}
    assert {spec.destructive_hint for spec in specs.values()} == {False}


def test_the_private_copies_of_the_shared_helpers_are_gone():
    for name in ("_positive_id", "_int_ids", "_kind_of", "_shape_error", "_record"):
        assert not hasattr(contracts, name), name
    assert contracts.positive_ids is _common.positive_ids and contracts.expect_object is _common.expect_object


def test_no_contract_delete_exists_anywhere(spec_index):
    assert "DELETE /v1/contracts/{contractId}" in FORBIDDEN_OPS
    for spec in REGISTRY.specs:
        assert "DELETE /v1/contracts/{contractId}" not in spec.ops, spec.name
    assert all(op.startswith("GET ") for spec in module_specs().values() for op in spec.ops)


def test_the_declared_ops_exist_and_the_query_names_are_the_spec_ones(spec_index):
    for spec in module_specs().values():
        for op in spec.ops:
            assert op in spec_index.ops and op not in FORBIDDEN_OPS
    listing = spec_index.op("GET /v1/contracts")
    assert listing.paged
    assert set(contracts.LIST_FIELDS.values()) == set(listing.query_params)
    one = spec_index.op("GET /v1/contracts/{contractId}")
    assert not one.paged and one.path_placeholders == ("contractId",)


async def test_the_tools_are_in_the_billing_toolset_and_never_need_the_destructive_flag(server_factory):
    for destructive in (False, True):
        names = {t.name for t in await list_tools(server_factory(toolsets={"billing"}, destructive=destructive))}
        assert set(EXPECTED_TOOLS) <= names
        assert not [name for name in names if name.startswith("delete_contract")]
    names = {t.name for t in await list_tools(server_factory(toolsets={"core", "tickets", "time"}, destructive=True))}
    assert not names & set(EXPECTED_TOOLS)


async def test_schemas_annotations_and_descriptions(server_factory):
    listed = {t.name: t for t in await list_tools(server_factory(destructive=True))}
    for name in EXPECTED_TOOLS:
        tool = listed[name]
        for param, entry_schema in tool.inputSchema["properties"].items():
            description = entry_schema.get("description")
            assert description, f"{name}.{param} has no description"
            assert len(description) <= PARAM_DESCRIPTION_MAX, f"{name}.{param}: {len(description)} characters"
            assert EM_DASH not in description and EN_DASH not in description
        text = " ".join(tool.description.split())
        assert len(tool.description) <= DESCRIPTION_MAX, f"{name}: {len(tool.description)} characters"
        assert EM_DASH not in text and EN_DASH not in text
        assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False
    listing = " ".join(listed["list_contracts"].description.split())
    assert "A service line's Id is the service_line_id of create_time_entry and update_time_entry" in listing
    assert "Paging: pass next_cursor back as cursor with the SAME filters until has_more is false." in listing
    assert "Expired and archived contracts are listed too: keep Status Active for live ones" in listing
    assert "get_contract has the line items and labor detail" in listing
    properties = listed["list_contracts"].inputSchema["properties"]
    assert "list_clients" in properties["client_ids"]["description"]  # the tool that resolves the ids
    assert properties["page_size"]["default"] == 50
    one = " ".join(listed["get_contract"].description.split())
    for fragment in (
        "invoice schedule",
        "every service line with labor detail and line items",
        "service_line_id time entries use",
        "404",
        "No tool changes or deletes contracts",
    ):
        assert fragment in one, fragment
    assert "list_contracts" in listed["get_contract"].inputSchema["properties"]["contract_id"]["description"]
    assert listed["get_contract"].inputSchema["required"] == ["contract_id"]


# --------------------------------------------------------------------------
# list_contracts
# --------------------------------------------------------------------------


async def test_list_contracts_sends_the_client_filter_and_the_paging_names(server, mock_gorelo, spec_index):
    mock_gorelo.on("GET", "/v1/contracts", paged_envelope([contract(9), contract(10, 9102)], next_cursor="c2", total_count=11))
    result = await call_tool(server, "list_contracts", {"client_ids": [9101, 9102], "page_size": 2, "cursor": "abc"})
    request = mock_gorelo.last
    assert (request.method, request.path, request.json) == ("GET", "/v1/contracts", None)
    assert request.query == {"ClientIds": "9101,9102", "PageSize": "2", "Cursor": "abc"}
    assert set(request.query) == set(spec_index.op("GET /v1/contracts").query_params)
    assert result == {
        "items": [contract(9), contract(10, 9102)],
        "count": 2,
        "total_count": 11,
        "has_more": True,
        "next_cursor": "c2",
        "page_size": 2,
        "filters": {"client_ids": [9101, 9102]},
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
    "property_name, param", [("ClientIds", "client_ids"), ("PageSize", "page_size"), ("Cursor", "cursor")]
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
