"""tools/catalog.py: items, categories, taxes (offline: MockGorelo and an in-process client)."""

import inspect
import json
import logging
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
    make_ctx,
    pagination,
    paged_envelope,
    uid,
)
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool

from tools import catalog
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


ITEMS = "/v1/items"
ITEM_ID = uid(11)
ITEM_PATH = f"/v1/items/{ITEM_ID}"
PRODUCT_A = uid(21)
PRODUCT_B = uid(22)


def item_row(item_id=ITEM_ID, bundle=False, **extra):
    """One ItemListModel row as Gorelo returns it."""
    row = {
        "Id": item_id,
        "Type": {"Id": 2, "Name": "Bundle"} if bundle else {"Id": 1, "Name": "Product"},
        "Status": {"Id": 1, "Name": "Active"},
        "Name": "Starter Kit" if bundle else "Widget",
        "Number": "W-100",
        "Description": "A very useful thing",
        "CategoryId": 4,
        "SubcategoryId": 9,
        "ClientId": None,
        "LocationId": None,
        "Sku": None if bundle else "WID-1",
        "PartNumber": None if bundle else "PN-77",
        "Manufacturer": None if bundle else "Acme",
        "Vendor": "Example Vendor A",
        "UnitCost": 40.0,
        "UnitPrice": 65.0,
        "TaxId": 2,
        "ExternalProductId": None,
        "CreatedOn": "2026-09-25T10:00:00Z",
        "UpdatedOn": None,
    }
    row.update(extra)
    return row


def item_detail(item_id=ITEM_ID, bundle=False, **extra):
    """One ItemDetailModel (the list row plus SubItems and the two invoice display flags)."""
    detail = item_row(item_id, bundle=bundle)
    detail["ShowSubItemsOnInvoice"] = True if bundle else None
    detail["ShowSubItemDescriptionsOnInvoice"] = False if bundle else None
    detail["SubItems"] = (
        [
            {"ItemId": PRODUCT_A, "Name": "Widget", "Quantity": 2.0, "UnitCost": 40.0, "UnitPrice": 65.0},
            {"ItemId": PRODUCT_B, "Name": "Gadget", "Quantity": 0.5, "UnitCost": 10.0, "UnitPrice": 15.0},
        ]
        if bundle
        else None
    )
    detail.update(extra)
    return detail


@pytest.fixture
def server(server_factory):
    """Every toolset on and delete tools enabled."""
    return server_factory(destructive=True)


def module_specs():
    return [spec for spec in REGISTRY.specs if spec.fn.__module__ == catalog.__name__]


def tool_def(name, tools):
    return next(tool for tool in tools if tool.name == name)


async def call_directly(client_factory, tool, **kwargs):
    """Call the decorated function itself (no pydantic in front of it): for the checks the schema normally pre-empts."""
    async with client_factory() as client:
        return await tool(make_ctx(client), **kwargs)


# --------------------------------------------------------------------------
# What the module declares
# --------------------------------------------------------------------------

EXPECTED_TOOLS = {
    "list_items": ("read", ["GET /v1/items"]),
    "get_item": ("read", ["GET /v1/items/{itemId}"]),
    "create_item": ("write", ["POST /v1/items", "GET /v1/items/{itemId}"]),
    "update_item": ("write", ["PATCH /v1/items/{itemId}", "GET /v1/items/{itemId}"]),
    "list_item_categories": ("read", ["GET /v1/items/categories"]),
    "list_taxes": ("read", ["GET /v1/taxes"]),
    "delete_item": ("destructive", ["DELETE /v1/items/{itemId}"]),
}


def test_the_module_declares_exactly_these_tools():
    assert {spec.name: (spec.kind, spec.ops) for spec in module_specs()} == EXPECTED_TOOLS
    assert {spec.toolset for spec in module_specs()} == {"billing"}


def test_the_fixtures_are_shaped_like_the_spec(spec_index):
    """The mocked answers carry exactly the fields the spec's response schemas define."""

    def fields(name):
        return set(spec_index.schema(name)["fields"])

    assert set(item_row()) == fields("ItemListModel")
    assert set(item_detail()) == fields("ItemDetailModel")
    assert set(item_detail(bundle=True)["SubItems"][0]) == fields("ItemSubItemModel")
    assert set(item_row()["Type"]) == set(item_row()["Status"]) == fields("CodeModel")
    assert set(CATEGORIES[0]) == fields("ProductCategoryModel")
    assert set(CATEGORIES[0]["Subcategories"][0]) == fields("ProductSubcategoryModel")
    assert set(TAXES[0]) == fields("TaxModel")
    assert set(TAXES[1]["SubTaxes"][0]) == fields("SubTaxModel")
    assert fields("CreateItemResult") == fields("UpdateItemResult") == fields("DeleteItemResult") == {"Id"}


def test_the_module_docstring_names_every_op_the_tools_use():
    for _kind, ops in EXPECTED_TOOLS.values():
        for op in ops:
            assert op in catalog.__doc__, op


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


def test_the_create_and_update_maps_cover_exactly_the_documented_fields(spec_index):
    create = spec_index.op("POST /v1/items").body["fields"]
    update = spec_index.op("PATCH /v1/items/{itemId}").body["fields"]
    assert set(catalog.CREATE_FIELDS.values()) == set(create)
    # every UpdateItemCommand field except TypeId, which Gorelo refuses whenever it is present
    assert set(catalog.UPDATE_FIELDS.values()) == set(update) - {"TypeId"}
    assert "TypeId" not in catalog.UPDATE_FIELDS.values()


def test_every_param_is_described_and_the_text_stays_within_the_concision_limits():
    for spec in module_specs():
        doc = inspect.getdoc(spec.fn)
        assert 40 < len(doc) <= 900, spec.name
        for name, definition in Tool.from_function(spec.fn).parameters["properties"].items():
            text = definition.get("description")
            assert text, f"{spec.name}.{name} needs a description"
            assert len(text) <= 160, f"{spec.name}.{name} description is {len(text)} characters"
    for name in ("create_item", "update_item"):
        assert "Side effects:" in inspect.getdoc(getattr(catalog, name)), name


def test_the_docstrings_state_the_facts_the_model_must_know():
    delete = " ".join(inspect.getdoc(catalog.delete_item).split())
    assert "Gorelo refuses (409) while it is a sub-item of a bundle or billed on a contract" in delete
    assert "Deleting a bundle keeps its sub-items" in delete
    # contract e15cb5a18ec2: an archived item can be deleted, and the 409 lists every blocker in the one answer
    assert "An archived item can be deleted" in delete and "the answer lists every blocker" in delete
    assert "Ask the user first; needs confirm=true." in delete  # the consistency rule of every destructive tool
    update = " ".join(inspect.getdoc(catalog.update_item).split())
    assert "(partial update)" in update and "The type never changes" in update
    assert "values are removed only through clear_fields" in update and "At least one change" in update
    assert "overwrites stored values" in update and "future contract and invoice lines" in update
    assert "{Id, warning}: the change WAS applied, do not repeat it" in update
    create = " ".join(inspect.getdoc(catalog.create_item).split())
    assert "creates a real catalog item that contracts and invoices can use" in create
    assert "{Id, warning}: the item exists, do not create it again" in create
    # delete_item exists only when deletes are enabled, so create_item may name it only on that condition,
    # and the always-available undo (archive through update_item) is said too
    assert create.count("delete_item") == 1 and "delete_item, when deletes are enabled" in create
    assert "archive it with update_item (status=archived, always possible)" in create
    listing = " ".join(inspect.getdoc(catalog.list_items).split())
    assert "Only get_item returns a bundle's SubItems" in listing and "labor items never appear" in listing
    categories = " ".join(inspect.getdoc(catalog.list_item_categories).split())
    assert "read a subcategory with its category" in categories


async def test_the_parameter_descriptions_carry_the_id_sources_and_the_confirm_rule(server):
    tools = await list_tools(server)

    def text(tool, param):
        return tool_def(tool, tools).inputSchema["properties"][param]["description"]

    assert "list_item_categories" in text("create_item", "category_id") and "list_taxes" in text("create_item", "tax_id")
    assert "list_client_locations" in text("create_item", "location_id") and "list_clients" in text("update_item", "client_id")
    # contract e15cb5a18ec2: a price may be negative (a discount or credit line), a cost may not
    for tool in ("create_item", "update_item"):
        assert text(tool, "unit_price") == "May be negative, for a discount or credit line.", tool
        assert "0 or more" in text(tool, "unit_cost"), tool
    assert "a product from list_items" in text("create_item", "sub_items")
    assert "COMPLETE new list" in text("update_item", "sub_items")
    # what a bundle's sub-items do to its cost
    assert "A bundle's cost comes from its sub-items" in text("create_item", "sub_items")
    assert "replaces the list and Gorelo recomputes the bundle's UnitCost" in text("update_item", "sub_items")
    for tool in ("get_item", "update_item", "delete_item"):
        assert "list_items" in text(tool, "item_id"), tool
    assert "Must be true" in text("delete_item", "confirm") and "user approves" in text("delete_item", "confirm")
    assert "same filters" in text("list_items", "cursor")
    # the paging stop condition is said where the cursor is
    assert text("list_items", "cursor") == "next_cursor from the previous call; same filters; repeat until has_more is false."


async def test_the_annotations_tell_a_client_what_each_tool_does(server):
    tools = {tool.name: tool for tool in await list_tools(server)}
    for name in ("list_items", "get_item", "list_item_categories", "list_taxes"):
        a = tools[name].annotations
        assert (a.readOnlyHint, a.destructiveHint, a.idempotentHint) == (True, False, True), name
    assert (tools["create_item"].annotations.readOnlyHint, tools["create_item"].annotations.destructiveHint) == (False, False)
    # update_item overwrites and can clear stored data
    assert (tools["update_item"].annotations.readOnlyHint, tools["update_item"].annotations.destructiveHint) == (False, True)
    assert (tools["delete_item"].annotations.readOnlyHint, tools["delete_item"].annotations.destructiveHint) == (False, True)
    confirm = tools["delete_item"].inputSchema["properties"]["confirm"]
    assert confirm["type"] == "boolean" and confirm["default"] is False


async def test_update_item_has_no_way_to_send_a_type(server):
    schema = tool_def("update_item", await list_tools(server)).inputSchema
    assert not {"type", "type_id"} & set(schema["properties"])
    assert schema["required"] == ["item_id"]
    assert "TypeId" not in catalog.UPDATE_FIELDS.values()


async def test_the_advertised_schema_does_not_forbid_a_negative_price(server):
    # a model is never told a price must be positive: the cost rule (0 or more) is enforced locally and said in text
    tools = await list_tools(server)
    for name in ("create_item", "update_item"):
        price = tool_def(name, tools).inputSchema["properties"]["unit_price"]
        assert price["type"] == "number" and not {"minimum", "exclusiveMinimum"} & set(price), name
        assert "May be negative" in price["description"] and "0 or more" not in price["description"], name


LATEST_SPEC = Path(__file__).resolve().parent.parent / "backups" / "swagger-latest.json"


@pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")
def test_the_money_and_delete_promises_are_the_ones_in_the_spec_text():
    """spec/spec_index.json keeps no description text, so what the item tools promise about money and deletes is
    pinned to the text of the full OpenAPI snapshot of contract e15cb5a18ec2."""

    def flat(text):
        return " ".join(text.split())

    document = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))
    schemas = document["components"]["schemas"]
    for command in ("CreateItemCommand", "UpdateItemCommand"):
        price = flat(schemas[command]["properties"]["UnitPrice"]["description"])
        assert "May be negative, for a discount or credit line" in price, command
    assert "a supplied negative value is a 400" in flat(schemas["UpdateItemCommand"]["properties"]["UnitCost"]["description"])
    delete = flat(document["paths"]["/v1/items/{itemId}"]["delete"]["description"])
    assert "An archived item can be deleted" in delete
    assert "every blocker that applies is listed in the one response" in delete
    assert "Deleting a bundle does not delete its own sub-items" in delete


# --------------------------------------------------------------------------
# list_item_categories and list_taxes
# --------------------------------------------------------------------------

CATEGORIES = [
    {
        "Id": 4,
        "Name": "Hardware",
        "Description": None,
        "TaxId": 2,
        "Subcategories": [{"Id": 9, "Name": "Laptops", "Description": None}, {"Id": 4, "Name": "Cables", "Description": None}],
    },
    {"Id": 5, "Name": "Services", "Description": "Labor and projects", "TaxId": None, "Subcategories": []},
]
TAXES = [
    {"Id": 2, "Name": "Sales tax", "Code": "TAX001", "IsDefault": True, "Percentage": 8.25, "SubTaxes": []},
    {
        "Id": 3,
        "Name": "HST",
        "Code": "HST",
        "IsDefault": False,
        "Percentage": 13.0,
        "SubTaxes": [
            {"Id": 31, "Code": "GST", "Name": "GST", "Percentage": 5.0},
            {"Id": 32, "Code": "PST", "Name": "PST", "Percentage": 8.0},
        ],
    },
]


async def test_list_item_categories_returns_the_unpaged_list(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/items/categories", envelope(CATEGORIES))
    result = await call_tool(server, "list_item_categories")
    assert result == {"items": CATEGORIES, "count": 2}
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", "/v1/items/categories", {}, None)


async def test_list_taxes_returns_the_unpaged_list(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/taxes", envelope(TAXES))
    result = await call_tool(server, "list_taxes")
    assert result == {"items": TAXES, "count": 2}
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", "/v1/taxes", {}, None)


@pytest.mark.parametrize("tool, path", [("list_item_categories", "/v1/items/categories"), ("list_taxes", "/v1/taxes")])
async def test_a_lookup_without_rows_is_an_empty_list_not_an_error(server, mock_gorelo, tool, path):
    mock_gorelo.on("GET", path, envelope([]))
    assert await call_tool(server, tool) == {"items": [], "count": 0}


@pytest.mark.parametrize("tool, path", [("list_item_categories", "/v1/items/categories"), ("list_taxes", "/v1/taxes")])
async def test_a_lookup_that_answers_with_something_other_than_a_list_is_refused(server, mock_gorelo, tool, path):
    mock_gorelo.on("GET", path, envelope({"Id": 1}))
    text = await call_tool_error(server, tool)
    assert text.startswith(f"Gorelo returned an unexpected response for {tool}") and "expected Data to be a list" in text


@pytest.mark.parametrize("tool, path", [("list_item_categories", "/v1/items/categories"), ("list_taxes", "/v1/taxes")])
async def test_a_lookup_error_names_the_tool_and_shows_gorelos_message(server, mock_gorelo, tool, path):
    mock_gorelo.on("GET", path, error_envelope(500, [("070001", "Something broke.")]))
    text = await call_tool_error(server, tool)
    assert text == f"Gorelo rejected {tool} (HTTP 500, code 070001): Something broke. [trace {TEST_TRACE_ID}]"
    assert len(mock_gorelo.requests) == 1


async def test_the_lookups_take_no_parameters(server):
    tools = {tool.name: tool for tool in await list_tools(server)}
    for name in ("list_item_categories", "list_taxes"):
        assert tools[name].inputSchema["properties"] == {}


# --------------------------------------------------------------------------
# list_items
# --------------------------------------------------------------------------


async def test_list_items_without_filters_sends_only_the_page_size(server, mock_gorelo):
    rows = [item_row(), item_row(uid(12), bundle=True)]
    mock_gorelo.on("GET", ITEMS, paged_envelope(rows, total_count=2))
    result = await call_tool(server, "list_items")
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", ITEMS, {"PageSize": "50"}, None)
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
    mock_gorelo.on("GET", ITEMS, paged_envelope([item_row()]))
    args = {
        "type": "bundle",
        "category_ids": [4, 5],
        "subcategory_ids": [9],
        "client_ids": [9102],
        "skus": ["WID-1", "WID 2"],
        "vendors": ["Example Vendor A", "Example Vendor B"],
        "part_numbers": ["PN-77"],
        "status": "archived",
        "query": "kit",
        "created_since": "2026-09-01T08:00:00Z",
        "created_before": "2026-09-02T08:00:00+02:00",
        "updated_since": "2026-09-03T08:00:00Z",
        "updated_before": "2026-09-04T08:00:00Z",
        "page_size": 100,
        "cursor": "opaque-1",
    }
    result = await call_tool(server, "list_items", args)
    assert mock_gorelo.last.query == {
        "TypeIds": "2",
        "CategoryIds": "4,5",
        "SubcategoryIds": "9",
        "ClientIds": "9102",
        "Skus": "WID-1,WID 2",
        "Vendors": "Example Vendor A,Example Vendor B",
        "PartNumbers": "PN-77",
        "StatusIds": "2",
        "Query": "kit",
        "CreatedSince": "2026-09-01T08:00:00Z",
        "CreatedBefore": "2026-09-02T06:00:00Z",
        "UpdatedSince": "2026-09-03T08:00:00Z",
        "UpdatedBefore": "2026-09-04T08:00:00Z",
        "PageSize": "100",
        "Cursor": "opaque-1",
    }
    # the result echoes the filters in the tool's own terms (names, not Gorelo ids)
    assert result["filters"] == {
        "type": "bundle",
        "category_ids": [4, 5],
        "subcategory_ids": [9],
        "client_ids": [9102],
        "skus": ["WID-1", "WID 2"],
        "vendors": ["Example Vendor A", "Example Vendor B"],
        "part_numbers": ["PN-77"],
        "status": "archived",
        "query": "kit",
        "created_since": "2026-09-01T08:00:00Z",
        "created_before": "2026-09-02T06:00:00Z",
        "updated_since": "2026-09-03T08:00:00Z",
        "updated_before": "2026-09-04T08:00:00Z",
    }


@pytest.mark.parametrize(
    "args, query",
    [
        ({"type": "product"}, {"TypeIds": "1"}),
        ({"type": "bundle"}, {"TypeIds": "2"}),
        ({"status": "active"}, {"StatusIds": "1"}),
        ({"status": "archived"}, {"StatusIds": "2"}),
    ],
)
async def test_type_and_status_names_become_gorelo_ids(server, mock_gorelo, args, query):
    mock_gorelo.on("GET", ITEMS, paged_envelope([]))
    await call_tool(server, "list_items", args)
    assert mock_gorelo.last.query == {**query, "PageSize": "50"}


@pytest.mark.parametrize("given, used", [(0, 1), (-3, 1), (1, 1), (200, 200), (999, 200)])
async def test_page_size_is_clamped_and_reported(server, mock_gorelo, given, used):
    mock_gorelo.on("GET", ITEMS, paged_envelope([]))
    result = await call_tool(server, "list_items", {"page_size": given})
    assert mock_gorelo.last.query["PageSize"] == str(used) and result["page_size"] == used


async def test_paging_follows_next_cursor_with_the_same_filters(server, mock_gorelo):
    mock_gorelo.on(
        "GET",
        ITEMS,
        in_order(
            envelope([item_row()], pagination("c-2", 2)),
            envelope([item_row(uid(12))], pagination(None, 2, has_more=False)),
        ),
    )
    args = {"status": "active", "page_size": 1}
    one = await call_tool(server, "list_items", args)
    assert (one["has_more"], one["next_cursor"], one["total_count"]) == (True, "c-2", 2)
    two = await call_tool(server, "list_items", {**args, "cursor": one["next_cursor"]})
    assert (two["has_more"], two["next_cursor"]) == (False, None)
    assert [r.query for r in mock_gorelo.requests] == [
        {"StatusIds": "1", "PageSize": "1"},
        {"StatusIds": "1", "PageSize": "1", "Cursor": "c-2"},
    ]
    assert one["filters"] == two["filters"] == {"status": "active"}


async def test_an_empty_page_is_reported_with_its_total(server, mock_gorelo):
    mock_gorelo.on("GET", ITEMS, envelope([], pagination(None, 0, has_more=False)))
    result = await call_tool(server, "list_items", {"skus": ["NOPE"]})
    assert (result["items"], result["count"], result["total_count"]) == ([], 0, 0)


async def test_list_items_refuses_an_answer_that_is_not_a_list(server, mock_gorelo):
    mock_gorelo.on("GET", ITEMS, envelope({"Id": ITEM_ID}, pagination(None, 1, has_more=False)))
    assert "expected Data to be a list" in await call_tool_error(server, "list_items")


@pytest.mark.parametrize(
    "property_name, param",
    [("TypeIds", "type"), ("StatusIds", "status"), ("Skus", "skus"), ("Vendors", "vendors"), ("PartNumbers", "part_numbers"),
     ("CategoryIds", "category_ids"), ("SubcategoryIds", "subcategory_ids"), ("Query", "query"), ("Cursor", "cursor"),
     ("CreatedSince", "created_since")],
)
async def test_a_gorelo_400_names_the_snake_case_param(server, mock_gorelo, property_name, param):
    mock_gorelo.on("GET", ITEMS, error_envelope(400, [("070101", "Not a valid value.", property_name)]))
    text = await call_tool_error(server, "list_items")
    assert text == f"Gorelo rejected list_items (HTTP 400, code 070101): {param}: Not a valid value. [trace {TEST_TRACE_ID}]"


LOCAL_LIST_ERRORS = [
    pytest.param(
        {"category_ids": []},
        "category_ids: expected at least one id, got an empty list (omit category_ids if you have no ids to give)",
        id="empty-category-ids",
    ),
    pytest.param({"subcategory_ids": []}, "subcategory_ids: expected at least one id, got an empty list", id="empty-subcategory-ids"),
    pytest.param({"client_ids": []}, "client_ids: expected at least one id, got an empty list", id="empty-client-ids"),
    pytest.param({"category_ids": [4, 0]}, "category_ids[1]: expected a positive whole number such as 123, got zero or a negative number", id="category-id-zero"),
    pytest.param({"client_ids": [2**63]}, "client_ids[0]: expected a positive whole number such as 123, got a number above 9223372036854775807", id="client-id-beyond-int64"),
    pytest.param({"skus": []}, "skus: the list must contain at least one id", id="empty-skus"),
    pytest.param({"vendors": []}, "vendors: the list must contain at least one id", id="empty-vendors"),
    pytest.param({"part_numbers": []}, "part_numbers: the list must contain at least one id", id="empty-part-numbers"),
    pytest.param({"skus": ["A-1", "  "]}, "skus: ids must not be blank", id="blank-sku"),
    pytest.param({"skus": ["A,B"]}, "skus: a value must not contain a comma (got 'A,B')", id="comma-in-sku"),
    pytest.param({"vendors": ["Acme, Inc."]}, "vendors: a value must not contain a comma", id="comma-in-vendor"),
    pytest.param({"query": "x" * 201}, "query: at most 200 characters", id="query-too-long"),
    pytest.param({"query": " "}, "query: must not be empty or whitespace only", id="blank-query"),
    pytest.param({"created_since": "2026-09-01T00:00:00"}, "created_since: '2026-09-01T00:00:00' has no UTC offset", id="naive-created-since"),
    pytest.param({"created_before": "2026-09-01"}, "created_before: '2026-09-01' has no UTC offset", id="date-only-created-before"),
    pytest.param({"updated_since": "soon"}, "updated_since: 'soon' is not an ISO 8601 datetime", id="garbage-updated-since"),
    pytest.param({"updated_before": "2026-09-01T00:00:00"}, "updated_before: ", id="naive-updated-before"),
    pytest.param({"cursor": " "}, "cursor: must not be empty or whitespace only", id="blank-cursor"),
]


@pytest.mark.parametrize("args, fragment", LOCAL_LIST_ERRORS)
async def test_list_items_local_errors_name_the_param_and_send_nothing(server, mock_gorelo, args, fragment):
    assert fragment in await call_tool_error(server, "list_items", args)
    assert mock_gorelo.requests == []


async def test_query_of_exactly_200_characters_is_accepted(server, mock_gorelo):
    mock_gorelo.on("GET", ITEMS, paged_envelope([]))
    await call_tool(server, "list_items", {"query": "x" * 200})
    assert mock_gorelo.last.query["Query"] == "x" * 200


@pytest.mark.parametrize("param", ["category_ids", "subcategory_ids", "client_ids"])
@pytest.mark.parametrize("bad", [True, "5", 5.0], ids=["json-true", "text-5", "decimal-5"])
async def test_an_id_list_is_strict_true_and_text_are_refused_instead_of_becoming_an_id(server, mock_gorelo, param, bad):
    text = await call_tool_error(server, "list_items", {param: [bad]})
    assert param in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("args, param", [({"type": "service"}, "type"), ({"status": "deleted"}, "status"), ({"type": "Product"}, "type")])
async def test_a_name_gorelo_does_not_know_is_refused_with_the_param_name(server, mock_gorelo, args, param):
    text = await call_tool_error(server, "list_items", args)
    assert param in text
    assert mock_gorelo.requests == []


async def test_calling_list_items_directly_with_an_unknown_type_is_refused_not_dropped(client_factory, mock_gorelo):
    with pytest.raises(ToolError, match="type: must be one of 'product', 'bundle', got 'service'"):
        await call_directly(client_factory, catalog.list_items, type="service")
    assert mock_gorelo.requests == []


# --------------------------------------------------------------------------
# get_item
# --------------------------------------------------------------------------


async def test_get_item_returns_the_record_unchanged(server, mock_gorelo):
    detail = item_detail(bundle=True)
    mock_gorelo.on("GET", ITEM_PATH, envelope(detail))
    result = await call_tool(server, "get_item", {"item_id": ITEM_ID})
    assert result == detail
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", ITEM_PATH, {}, None)


async def test_get_item_canonicalizes_the_id(server, mock_gorelo):
    mock_gorelo.on("GET", ITEM_PATH, envelope(item_detail()))
    await call_tool(server, "get_item", {"item_id": ITEM_ID.upper()})
    assert mock_gorelo.last.path == ITEM_PATH


async def test_get_item_404_names_item_id(server, mock_gorelo):
    mock_gorelo.on("GET", ITEM_PATH, error_envelope(404, [("070401", "Item not found.", "itemId")]))
    text = await call_tool_error(server, "get_item", {"item_id": ITEM_ID})
    assert text == f"Gorelo rejected get_item (HTTP 404, code 070401): item_id: Item not found. [trace {TEST_TRACE_ID}]"


async def test_get_item_refuses_a_success_without_data(server, mock_gorelo):
    mock_gorelo.on("GET", ITEM_PATH, envelope(None))
    text = await call_tool_error(server, "get_item", {"item_id": ITEM_ID})
    assert "Data is null; refusing to guess" in text


@pytest.mark.parametrize("data", [{}, [], [item_row()], True, "text"], ids=["empty-object", "empty-list", "list", "true", "text"])
async def test_get_item_refuses_an_answer_that_is_not_an_object_record(server, mock_gorelo, data):
    mock_gorelo.on("GET", ITEM_PATH, envelope(data))
    text = await call_tool_error(server, "get_item", {"item_id": ITEM_ID})
    assert text.startswith("Gorelo returned an unexpected response for get_item: GET /v1/items/{itemId}: expected Data to be a non-empty object but got ")
    assert text.endswith("refusing to guess")  # a read: nothing was written


@pytest.mark.parametrize("bad", ["categories", "12", "", "../taxes", f"{ITEM_ID}/x", f" {ITEM_ID}", "{" + ITEM_ID + "}"])
async def test_get_item_needs_a_guid_and_the_error_names_item_id(server, mock_gorelo, bad):
    text = await call_tool_error(server, "get_item", {"item_id": bad})
    assert text.startswith("item_id: expected a GUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b, got ")
    assert "itemId" not in text
    assert mock_gorelo.requests == []


# --------------------------------------------------------------------------
# create_item
# --------------------------------------------------------------------------


def route_create(mock, detail=None, item_id=ITEM_ID):
    mock.on("POST", ITEMS, envelope({"Id": item_id}))
    mock.on("GET", f"/v1/items/{item_id}", envelope(detail if detail is not None else item_detail(item_id)))


async def test_a_minimal_product_sends_only_type_and_name_then_rereads_it(server, mock_gorelo):
    route_create(mock_gorelo)
    result = await call_tool(server, "create_item", {"type": "product", "name": "Widget"})
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("POST", ITEMS), ("GET", ITEM_PATH)]
    post = mock_gorelo.requests[0]
    assert post.json == {"TypeId": 1, "Name": "Widget"} and post.query == {}
    assert result == item_detail()


async def test_a_full_product_sends_every_given_field_under_its_pascal_name(server, mock_gorelo):
    route_create(mock_gorelo)
    args = {
        "type": "product",
        "name": "Widget",
        "number": "W-100",
        "description": "A very useful thing",
        "category_id": 4,
        "subcategory_id": 9,
        "client_id": 9102,
        "location_id": 77,
        "vendor": "Example Vendor A",
        "unit_price": 65.5,
        "tax_id": 2,
        "external_product_id": "QB-100",
        "sku": "WID-1",
        "part_number": "PN-77",
        "manufacturer": "Acme",
        "unit_cost": 40,
    }
    await call_tool(server, "create_item", args)
    assert mock_gorelo.requests[0].json == {
        "TypeId": 1,
        "Name": "Widget",
        "Number": "W-100",
        "Description": "A very useful thing",
        "CategoryId": 4,
        "SubcategoryId": 9,
        "ClientId": 9102,
        "LocationId": 77,
        "Vendor": "Example Vendor A",
        "UnitPrice": 65.5,
        "TaxId": 2,
        "ExternalProductId": "QB-100",
        "Sku": "WID-1",
        "PartNumber": "PN-77",
        "Manufacturer": "Acme",
        "UnitCost": 40,
    }


async def test_a_bundle_sends_its_sub_items_and_display_flags(server, mock_gorelo):
    route_create(mock_gorelo, item_detail(bundle=True))
    args = {
        "type": "bundle",
        "name": "Starter Kit",
        "unit_price": 99.5,
        "sub_items": [{"item_id": PRODUCT_A, "quantity": 2}, {"item_id": PRODUCT_B.upper(), "quantity": 0.5}],
        "show_sub_items_on_invoice": True,
        "show_sub_item_descriptions_on_invoice": False,
    }
    result = await call_tool(server, "create_item", args)
    assert mock_gorelo.requests[0].json == {
        "TypeId": 2,
        "Name": "Starter Kit",
        "UnitPrice": 99.5,
        "SubItems": [{"ItemId": PRODUCT_A, "Quantity": 2.0}, {"ItemId": PRODUCT_B, "Quantity": 0.5}],  # ids in canonical form
        "ShowSubItemsOnInvoice": True,
        "ShowSubItemDescriptionsOnInvoice": False,  # false is a value, not "not given"
    }
    assert result["SubItems"][0]["ItemId"] == PRODUCT_A and result["Type"]["Name"] == "Bundle"


async def test_a_field_the_caller_did_not_give_is_never_sent(server, mock_gorelo):
    route_create(mock_gorelo)
    await call_tool(server, "create_item", {"type": "product", "name": "Widget", "vendor": "Example Vendor A"})
    assert set(mock_gorelo.requests[0].json) == {"TypeId", "Name", "Vendor"}


async def test_the_reread_uses_the_id_gorelo_returned(server, mock_gorelo):
    new_id = uid(31)
    mock_gorelo.on("POST", ITEMS, envelope({"Id": new_id.upper()}))
    mock_gorelo.on("GET", f"/v1/items/{new_id}", envelope(item_detail(new_id)))
    result = await call_tool(server, "create_item", {"type": "product", "name": "Widget"})
    assert result["Id"] == new_id and mock_gorelo.requests[1].path == f"/v1/items/{new_id}"


@pytest.mark.parametrize("field", ["sub_items", "show_sub_items_on_invoice", "show_sub_item_descriptions_on_invoice"])
async def test_a_bundle_only_field_on_a_product_is_a_local_error(server, mock_gorelo, field):
    values = {
        "sub_items": [{"item_id": PRODUCT_A, "quantity": 1}],
        "show_sub_items_on_invoice": False,  # even false: Gorelo rejects the field being present
        "show_sub_item_descriptions_on_invoice": False,
    }
    text = await call_tool_error(server, "create_item", {"type": "product", "name": "Widget", field: values[field]})
    assert text == (
        f"{field}: only valid for bundles (type='bundle'), but type is 'product'; "
        "Gorelo rejects it with a 400, so nothing was sent. Omit it."
    )
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("field, value", [("sku", "S-1"), ("part_number", "PN-1"), ("manufacturer", "Acme"), ("unit_cost", 0)])
async def test_a_product_only_field_on_a_bundle_is_a_local_error(server, mock_gorelo, field, value):
    args = {"type": "bundle", "name": "Kit", "sub_items": [{"item_id": PRODUCT_A, "quantity": 1}], field: value}
    text = await call_tool_error(server, "create_item", args)
    assert text == (
        f"{field}: only valid for products (type='product'), but type is 'bundle'; "
        "Gorelo rejects it with a 400, so nothing was sent. Omit it."
    )
    assert mock_gorelo.requests == []


async def test_every_wrong_type_field_is_named_in_one_error(server, mock_gorelo):
    args = {"type": "product", "name": "Widget", "sub_items": [{"item_id": PRODUCT_A, "quantity": 1}], "show_sub_items_on_invoice": True}
    text = await call_tool_error(server, "create_item", args)
    assert text.startswith("sub_items, show_sub_items_on_invoice: only valid for bundles")
    args = {"type": "bundle", "name": "Kit", "sub_items": [{"item_id": PRODUCT_A, "quantity": 1}], "sku": "S", "unit_cost": 3}
    text = await call_tool_error(server, "create_item", args)
    assert text.startswith("sku, unit_cost: only valid for products")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("sub_items", [None, []])
async def test_a_bundle_needs_at_least_one_sub_item(server, mock_gorelo, sub_items):
    args = {"type": "bundle", "name": "Kit"}
    if sub_items is not None:
        args["sub_items"] = sub_items
    text = await call_tool_error(server, "create_item", args)
    assert text.startswith("sub_items: a bundle needs at least one sub-item")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "sub_items, fragment",
    [
        pytest.param([{"item_id": "not-a-guid", "quantity": 1}], "sub_items[0].item_id: expected a GUID", id="bad-guid"),
        pytest.param([{"item_id": PRODUCT_A, "quantity": 1}, {"item_id": "12", "quantity": 1}], "sub_items[1].item_id: expected a GUID", id="second-entry-bad-guid"),
        pytest.param([{"item_id": PRODUCT_A, "quantity": 0}], "sub_items[0].quantity: must be a number greater than 0", id="zero-quantity"),
        pytest.param([{"item_id": PRODUCT_A, "quantity": -2}], "sub_items[0].quantity: must be a number greater than 0", id="negative-quantity"),
        pytest.param([{"item_id": PRODUCT_A, "quantity": float("nan")}], "sub_items[0].quantity: must be a number greater than 0", id="nan-quantity"),
        pytest.param([{"item_id": PRODUCT_A, "quantity": float("inf")}], "sub_items[0].quantity: must be a number greater than 0", id="infinite-quantity"),
        pytest.param([{"item_id": PRODUCT_A, "quantity": True}], "sub_items[0].quantity: must be a number greater than 0", id="bool-quantity"),
        pytest.param([{"item_id": PRODUCT_A}], "sub_items[0]: missing quantity", id="missing-quantity"),
        pytest.param([{"quantity": 1}], "sub_items[0]: missing item_id", id="missing-item-id"),
        pytest.param([{"item_id": PRODUCT_A, "quantity": 1, "price": 3}], "sub_items[0]: unknown field(s) price; allowed: item_id, quantity", id="unknown-field"),
        pytest.param(["just-a-string"], "sub_items[0]: must be an object with item_id and quantity", id="entry-not-an-object"),
        pytest.param("a string", "sub_items: expected a list of objects with item_id and quantity", id="not-a-list"),
    ],
)
async def test_sub_item_errors_name_the_entry_and_the_field(client_factory, mock_gorelo, sub_items, fragment):
    with pytest.raises(ToolError) as info:
        await call_directly(client_factory, catalog.create_item, type="bundle", name="Kit", sub_items=sub_items)
    assert fragment in str(info.value)
    assert mock_gorelo.requests == []


async def test_the_schema_already_refuses_bad_sub_items_before_the_tool_runs(server, mock_gorelo):
    for bad in ([{"item_id": PRODUCT_A, "quantity": 0}], [{"item_id": PRODUCT_A, "quantity": 1, "price": 3}], [{"item_id": PRODUCT_A}]):
        text = await call_tool_error(server, "create_item", {"type": "bundle", "name": "Kit", "sub_items": bad})
        assert "sub_items" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("field", ["name", "number", "description", "vendor", "external_product_id", "sku", "part_number", "manufacturer"])
@pytest.mark.parametrize("blank", ["", "   "])
async def test_blank_text_is_refused_on_create(server, mock_gorelo, field, blank):
    args = {"type": "product", "name": "Widget", field: blank}
    text = await call_tool_error(server, "create_item", args)
    assert text.startswith(f"{field}: must not be empty or whitespace only")
    assert mock_gorelo.requests == []


# What is not a finite number (a bool is not a number here, and text or a huge int never becomes one).
NOT_FINITE_NUMBERS = [
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param(float("-inf"), id="minus-inf"),
    pytest.param(True, id="bool"),
    pytest.param("5", id="text"),
    pytest.param(10**400, id="int-too-large-for-a-float"),
]


@pytest.mark.parametrize("value", [-0.01, -5, -1234.5])
async def test_a_negative_price_is_sent_as_given_it_is_a_discount_or_credit_line(server, mock_gorelo, value):
    # contract e15cb5a18ec2: CreateItemCommand.UnitPrice "May be negative, for a discount or credit line"
    route_create(mock_gorelo)
    await call_tool(server, "create_item", {"type": "product", "name": "Loyalty credit", "unit_price": value})
    assert mock_gorelo.requests[0].json == {"TypeId": 1, "Name": "Loyalty credit", "UnitPrice": value}  # not rounded, not made positive


async def test_a_bundle_may_carry_a_negative_price_too(server, mock_gorelo):
    route_create(mock_gorelo, item_detail(bundle=True))
    args = {"type": "bundle", "name": "Starter Kit", "unit_price": -20, "sub_items": [{"item_id": PRODUCT_A, "quantity": 1}]}
    await call_tool(server, "create_item", args)
    assert mock_gorelo.requests[0].json["UnitPrice"] == -20


@pytest.mark.parametrize("value", [-0.01, -5])
async def test_a_negative_cost_is_refused_naming_unit_cost(server, mock_gorelo, value):
    # UnitCost did not change in contract e15cb5a18ec2: a negative one is still a 400, so it never leaves this server
    text = await call_tool_error(server, "create_item", {"type": "product", "name": "Widget", "unit_cost": value})
    assert text == "unit_cost: must be a number of 0 or more"
    assert mock_gorelo.requests == []


async def test_a_negative_price_does_not_excuse_a_negative_cost(server, mock_gorelo):
    text = await call_tool_error(
        server, "create_item", {"type": "product", "name": "Widget", "unit_price": -5, "unit_cost": -1}
    )
    assert text == "unit_cost: must be a number of 0 or more"  # the price is fine, the cost is not
    assert mock_gorelo.requests == []
    route_create(mock_gorelo)
    await call_tool(server, "create_item", {"type": "product", "name": "Widget", "unit_price": -5, "unit_cost": 0})
    assert mock_gorelo.requests[0].json == {"TypeId": 1, "Name": "Widget", "UnitPrice": -5, "UnitCost": 0}


@pytest.mark.parametrize("bad", NOT_FINITE_NUMBERS)
async def test_a_price_that_is_not_a_finite_number_is_refused_naming_unit_price(client_factory, mock_gorelo, bad):
    # a direct call skips the schema: the helper still refuses (a negative price is fine, these are not numbers)
    wanted = r"unit_price: must be a finite number \(it may be negative, for a discount or credit line\)"
    for tool, args in (
        (catalog.create_item, {"type": "product", "name": "Widget"}),
        (catalog.update_item, {"item_id": ITEM_ID}),
    ):
        with pytest.raises(ToolError, match=wanted):
            await call_directly(client_factory, tool, unit_price=bad, **args)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("field", ["unit_price", "unit_cost"])
@pytest.mark.parametrize("word", ["nan", "inf", "-inf", "Infinity"])
async def test_the_words_nan_and_infinity_never_become_a_price_or_a_cost(server, mock_gorelo, field, word):
    # pydantic's lax float turns these strings into non-finite numbers: the tool still refuses them, naming the param
    wanted = {
        "unit_price": "unit_price: must be a finite number (it may be negative, for a discount or credit line)",
        "unit_cost": "unit_cost: must be a number of 0 or more",
    }[field]
    assert await call_tool_error(server, "create_item", {"type": "product", "name": "Widget", field: word}) == wanted
    assert await call_tool_error(server, "update_item", {"item_id": ITEM_ID, field: word}) == wanted
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", NOT_FINITE_NUMBERS)
async def test_a_cost_that_is_not_a_finite_number_is_refused_naming_unit_cost(client_factory, mock_gorelo, bad):
    for tool, args in (
        (catalog.create_item, {"type": "product", "name": "Widget"}),
        (catalog.update_item, {"item_id": ITEM_ID}),
    ):
        with pytest.raises(ToolError, match="unit_cost: must be a number of 0 or more"):
            await call_directly(client_factory, tool, unit_cost=bad, **args)
    assert mock_gorelo.requests == []


async def test_a_price_of_zero_is_a_real_value(server, mock_gorelo):
    route_create(mock_gorelo)
    await call_tool(server, "create_item", {"type": "product", "name": "Free sample", "unit_price": 0, "unit_cost": 0})
    assert mock_gorelo.requests[0].json == {"TypeId": 1, "Name": "Free sample", "UnitPrice": 0, "UnitCost": 0}


ID_FIELDS = ["category_id", "subcategory_id", "client_id", "location_id", "tax_id"]


@pytest.mark.parametrize("field", ID_FIELDS)
@pytest.mark.parametrize("value", [0, -1])
async def test_an_id_must_be_positive_on_create(server, mock_gorelo, field, value):
    text = await call_tool_error(server, "create_item", {"type": "product", "name": "Widget", field: value})
    assert text == f"{field}: expected a positive whole number such as 123, got zero or a negative number"
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("field", ID_FIELDS)
async def test_an_id_beyond_int64_is_refused_on_create(server, mock_gorelo, field):
    text = await call_tool_error(server, "create_item", {"type": "product", "name": "Widget", field: 2**63})
    assert text == (
        f"{field}: expected a positive whole number such as 123, got a number above 9223372036854775807, "
        "the largest Gorelo id"
    )
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("field", ID_FIELDS)
@pytest.mark.parametrize("bad", [True, "5", 5.0], ids=["json-true", "text-5", "decimal-5"])
async def test_an_id_is_strict_on_create_true_and_text_never_become_an_id(server, mock_gorelo, field, bad):
    text = await call_tool_error(server, "create_item", {"type": "product", "name": "Widget", field: bad})
    assert field in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("field", ID_FIELDS)
@pytest.mark.parametrize("bad", [True, "5", 5.0], ids=["json-true", "text-5", "decimal-5"])
async def test_an_id_is_strict_on_update_true_and_text_never_become_an_id(server, mock_gorelo, field, bad):
    text = await call_tool_error(server, "update_item", {"item_id": ITEM_ID, field: bad})
    assert field in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("field", ID_FIELDS)
async def test_calling_create_item_directly_with_a_bool_or_text_id_is_refused_by_the_helper(client_factory, mock_gorelo, field):
    for bad in (True, "5", 5.0):
        with pytest.raises(ToolError, match=f"{field}: expected a positive whole number such as 123, got "):
            await call_directly(client_factory, catalog.create_item, type="product", name="Widget", **{field: bad})
    with pytest.raises(ToolError) as info:  # what was given is described, never quoted
        await call_directly(client_factory, catalog.create_item, type="product", name="Widget", **{field: "secret-text"})
    assert "secret-text" not in str(info.value) and str(info.value).endswith("got a string")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "args, param",
    [
        ({"type": "service", "name": "x"}, "type"),
        ({"name": "x"}, "type"),
        ({"type": "product"}, "name"),
        ({"type": "product", "name": "x", "unit_price": "cheap"}, "unit_price"),
        ({"type": "product", "name": "x", "category_id": "four"}, "category_id"),
    ],
)
async def test_a_value_outside_the_schema_is_refused_with_the_param_name(server, mock_gorelo, args, param):
    assert param in await call_tool_error(server, "create_item", args)
    assert mock_gorelo.requests == []


async def test_calling_create_item_directly_with_an_unknown_type_is_refused(client_factory, mock_gorelo):
    with pytest.raises(ToolError, match="type: must be one of 'product', 'bundle', got 'service'"):
        await call_directly(client_factory, catalog.create_item, type="service", name="x")
    with pytest.raises(ToolError, match="name: is required"):
        await call_directly(client_factory, catalog.create_item, type="product", name=None)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "property_name, param",
    [("Name", "name"), ("TypeId", "type"), ("SubItems", "sub_items"), ("UnitCost", "unit_cost"), ("CategoryId", "category_id"),
     ("SubcategoryId", "subcategory_id"), ("LocationId", "location_id"), ("TaxId", "tax_id"), ("Sku", "sku"),
     ("ShowSubItemsOnInvoice", "show_sub_items_on_invoice")],
)
async def test_a_gorelo_400_names_the_snake_case_param_on_create(server, mock_gorelo, property_name, param):
    mock_gorelo.on("POST", ITEMS, error_envelope(400, [("070101", "Rejected by Gorelo.", property_name)]))
    text = await call_tool_error(server, "create_item", {"type": "product", "name": "Widget"})
    assert text == f"Gorelo rejected create_item (HTTP 400, code 070101): {param}: Rejected by Gorelo. [trace {TEST_TRACE_ID}]"
    assert mock_gorelo.calls("GET") == []  # nothing to read back


async def test_a_nested_sub_item_error_names_the_sub_items_param_and_item(server, mock_gorelo):
    body = error_envelope(400, [("070101", "A sub-item cannot itself be a bundle.", "SubItems[0].ItemId")])
    mock_gorelo.on("POST", ITEMS, body)
    args = {"type": "bundle", "name": "Kit", "sub_items": [{"item_id": PRODUCT_A, "quantity": 1}]}
    text = await call_tool_error(server, "create_item", args)
    assert "sub_items (item 1, ItemId): A sub-item cannot itself be a bundle." in text


async def test_a_failed_reread_after_a_successful_create_returns_the_id_and_a_warning(server, mock_gorelo):
    mock_gorelo.on("POST", ITEMS, envelope({"Id": ITEM_ID}))
    mock_gorelo.on("GET", ITEM_PATH, error_envelope(500, [("070001", "Read model unavailable.")]))
    result = await call_tool(server, "create_item", {"type": "product", "name": "Widget"})
    assert set(result) == {"Id", "warning"} and result["Id"] == ITEM_ID
    assert result["warning"].startswith("the write succeeded; re-reading it failed: GET /v1/items/{itemId} answered HTTP 500")
    assert "Do not repeat the write" in result["warning"]
    assert len(mock_gorelo.calls("POST")) == 1 and len(mock_gorelo.calls("GET")) == 1  # neither repeated


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
        pytest.param([{"Id": ITEM_ID}], "Data is a list of 1 item, not an object with an Id", id="list"),
    ],
)
async def test_a_create_answer_without_an_id_is_not_taken_for_a_clean_success(server, mock_gorelo, data, problem):
    mock_gorelo.on("POST", ITEMS, envelope(data))
    text = await call_tool_error(server, "create_item", {"type": "product", "name": "Widget"})
    assert text.startswith(
        "Gorelo returned an unexpected response for create_item: POST /v1/items: Gorelo reported success but the "
        f"answer carries no usable Id for the record ({problem})"
    )
    assert "the write may have been applied, so verify it with a read before repeating it" in text
    assert mock_gorelo.calls("GET") == [] and len(mock_gorelo.calls("POST")) == 1


async def test_a_create_answer_with_an_id_that_cannot_be_read_back_returns_a_warning_not_a_second_create(server, mock_gorelo):
    # an Id that is not a GUID passes the answer check but cannot name the item to re-read: the write is not repeated
    mock_gorelo.on("POST", ITEMS, envelope({"Id": 5}))
    result = await call_tool(server, "create_item", {"type": "product", "name": "Widget"})
    assert result["Id"] == 5 and result["warning"].startswith("the write succeeded; re-reading it failed: ")
    assert "Do not repeat the write" in result["warning"]
    assert mock_gorelo.calls("GET") == [] and len(mock_gorelo.calls("POST")) == 1


async def test_a_create_that_times_out_is_not_retried_and_says_to_verify(server, mock_gorelo):
    mock_gorelo.on("POST", ITEMS, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "create_item", {"type": "product", "name": "Widget"})
    assert text.startswith("Gorelo did not confirm create_item (the request timed out).")
    assert "The change may or may not have been applied. Verify with a read before retrying." in text
    assert len(mock_gorelo.requests) == 1


# --------------------------------------------------------------------------
# update_item
# --------------------------------------------------------------------------


def route_update(mock, detail=None):
    mock.on("PATCH", ITEM_PATH, envelope({"Id": ITEM_ID}))
    mock.on("GET", ITEM_PATH, envelope(detail if detail is not None else item_detail()))


async def test_an_update_sends_only_the_given_field_and_rereads_the_item(server, mock_gorelo):
    route_update(mock_gorelo, item_detail(UnitPrice=12.5))
    result = await call_tool(server, "update_item", {"item_id": ITEM_ID, "unit_price": 12.5})
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("PATCH", ITEM_PATH), ("GET", ITEM_PATH)]
    patch = mock_gorelo.requests[0]
    assert patch.json == {"UnitPrice": 12.5} and patch.query == {}
    assert result["UnitPrice"] == 12.5


async def test_an_update_never_sends_a_type_id(server, mock_gorelo):
    route_update(mock_gorelo)
    args = {
        "item_id": ITEM_ID, "name": "Widget 2", "number": "W-101", "description": "d", "sku": "S", "part_number": "P",
        "manufacturer": "M", "vendor": "V", "external_product_id": "X", "category_id": 4, "subcategory_id": 9,
        "client_id": 1, "location_id": 2, "tax_id": 3, "unit_cost": 1.5, "unit_price": 2.5, "status": "active",
    }
    await call_tool(server, "update_item", args)
    assert "TypeId" not in json.dumps(mock_gorelo.requests[0].json)


async def test_every_field_is_sent_under_its_pascal_name(server, mock_gorelo):
    route_update(mock_gorelo)
    args = {
        "item_id": ITEM_ID,
        "name": "Widget 2",
        "number": "W-101",
        "description": "New description",
        "sku": "WID-2",
        "part_number": "PN-78",
        "manufacturer": "Acme 2",
        "vendor": "Example Vendor B",
        "external_product_id": "QB-101",
        "category_id": 5,
        "subcategory_id": 10,
        "client_id": 9102,
        "location_id": 77,
        "tax_id": 3,
        "unit_cost": 41.25,
        "unit_price": 70,
        "status": "archived",
        "sub_items": [{"item_id": PRODUCT_A, "quantity": 3}],
        "show_sub_items_on_invoice": False,
        "show_sub_item_descriptions_on_invoice": True,
    }
    await call_tool(server, "update_item", args)
    assert mock_gorelo.requests[0].json == {
        "Name": "Widget 2",
        "Number": "W-101",
        "Description": "New description",
        "Sku": "WID-2",
        "PartNumber": "PN-78",
        "Manufacturer": "Acme 2",
        "Vendor": "Example Vendor B",
        "ExternalProductId": "QB-101",
        "CategoryId": 5,
        "SubcategoryId": 10,
        "ClientId": 9102,
        "LocationId": 77,
        "TaxId": 3,
        "UnitCost": 41.25,
        "UnitPrice": 70,
        "StatusId": 2,
        "SubItems": [{"ItemId": PRODUCT_A, "Quantity": 3.0}],
        "ShowSubItemsOnInvoice": False,  # false is a value, not "not given"
        "ShowSubItemDescriptionsOnInvoice": True,
    }


@pytest.mark.parametrize("status, status_id", [("active", 1), ("archived", 2)])
async def test_status_names_become_gorelo_ids(server, mock_gorelo, status, status_id):
    route_update(mock_gorelo)
    await call_tool(server, "update_item", {"item_id": ITEM_ID, "status": status})
    assert mock_gorelo.requests[0].json == {"StatusId": status_id}


@pytest.mark.parametrize(
    "field, wire, value",
    [
        ("description", "Description", ""),
        ("sku", "Sku", ""),
        ("part_number", "PartNumber", ""),
        ("manufacturer", "Manufacturer", ""),
        ("vendor", "Vendor", ""),
        ("external_product_id", "ExternalProductId", ""),
        ("category_id", "CategoryId", 0),
        ("subcategory_id", "SubcategoryId", 0),
        ("client_id", "ClientId", 0),
        ("location_id", "LocationId", 0),
        ("tax_id", "TaxId", 0),
        ("sub_items", "SubItems", []),
    ],
)
async def test_clear_fields_sends_the_clearing_value_gorelo_expects(server, mock_gorelo, field, wire, value):
    route_update(mock_gorelo)
    await call_tool(server, "update_item", {"item_id": ITEM_ID, "clear_fields": [field]})
    sent = mock_gorelo.requests[0].json
    assert sent == {wire: value} and sent[wire] == value and type(sent[wire]) is type(value)


async def test_several_fields_can_be_changed_and_cleared_in_one_call(server, mock_gorelo):
    route_update(mock_gorelo)
    args = {"item_id": ITEM_ID, "unit_price": 9, "vendor": "New Vendor", "clear_fields": ["description", "tax_id", "description"]}
    await call_tool(server, "update_item", args)
    assert mock_gorelo.requests[0].json == {"UnitPrice": 9, "Vendor": "New Vendor", "Description": "", "TaxId": 0}


async def test_clearing_a_field_does_not_touch_the_others(server, mock_gorelo):
    route_update(mock_gorelo)
    await call_tool(server, "update_item", {"item_id": ITEM_ID, "clear_fields": ["client_id"]})
    assert mock_gorelo.requests[0].json == {"ClientId": 0}  # no Name, no UnitPrice, nothing else


async def test_a_field_cannot_be_given_a_value_and_cleared_at_once(server, mock_gorelo):
    text = await call_tool_error(server, "update_item", {"item_id": ITEM_ID, "vendor": "X", "clear_fields": ["vendor"]})
    assert text == "vendor: cannot be given a value and cleared in the same call"
    text = await call_tool_error(
        server, "update_item",
        {"item_id": ITEM_ID, "sub_items": [{"item_id": PRODUCT_A, "quantity": 1}], "clear_fields": ["sub_items"]},
    )
    assert text == "sub_items: cannot be given a value and cleared in the same call"
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("field", ["name", "number", "unit_cost", "unit_price", "status", "show_sub_items_on_invoice", "type"])
async def test_a_field_gorelo_cannot_clear_is_refused_in_clear_fields(server, mock_gorelo, field):
    text = await call_tool_error(server, "update_item", {"item_id": ITEM_ID, "clear_fields": [field]})
    assert "clear_fields" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("field", ["name", "number", "unit_cost", "unit_price", "status", "type", "show_sub_items_on_invoice"])
async def test_calling_update_item_directly_cannot_clear_a_fixed_field_either(client_factory, mock_gorelo, field):
    with pytest.raises(ToolError, match=f"clear_fields: '{field}' cannot be cleared; allowed: description, sku"):
        await call_directly(client_factory, catalog.update_item, item_id=ITEM_ID, clear_fields=[field])
    assert mock_gorelo.requests == []


async def test_an_empty_clear_fields_list_is_refused(server, mock_gorelo):
    text = await call_tool_error(server, "update_item", {"item_id": ITEM_ID, "unit_price": 5, "clear_fields": []})
    assert text.startswith("clear_fields: must not be an empty list")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("field", ["name", "number"])
@pytest.mark.parametrize("blank", ["", "  "])
async def test_name_and_number_cannot_be_blanked(server, mock_gorelo, field, blank):
    text = await call_tool_error(server, "update_item", {"item_id": ITEM_ID, field: blank})
    assert text == f"{field}: must not be blank; Gorelo cannot clear it, omit it to keep the current value"
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("field", ["description", "sku", "part_number", "manufacturer", "vendor", "external_product_id"])
@pytest.mark.parametrize("blank", ["", "   "])
async def test_blank_text_is_refused_and_points_to_clear_fields(server, mock_gorelo, field, blank):
    text = await call_tool_error(server, "update_item", {"item_id": ITEM_ID, field: blank})
    assert text == f"{field}: a blank value is not accepted; to clear it pass clear_fields=['{field}']"
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("field", ["category_id", "subcategory_id", "client_id", "location_id", "tax_id"])
async def test_zero_is_not_an_id_and_points_to_clear_fields(server, mock_gorelo, field):
    text = await call_tool_error(server, "update_item", {"item_id": ITEM_ID, field: 0})
    assert text == f"{field}: 0 is not an id; to remove it pass clear_fields=['{field}']"
    text = await call_tool_error(server, "update_item", {"item_id": ITEM_ID, field: -3})
    assert text == f"{field}: expected a positive whole number such as 123, got zero or a negative number"
    text = await call_tool_error(server, "update_item", {"item_id": ITEM_ID, field: 2**63})
    assert text.startswith(f"{field}: expected a positive whole number such as 123, got a number above 9223372036854775807")
    assert mock_gorelo.requests == []


async def test_an_empty_sub_item_list_points_to_clear_fields(server, mock_gorelo):
    text = await call_tool_error(server, "update_item", {"item_id": ITEM_ID, "sub_items": []})
    assert text.startswith("sub_items: an empty list is not accepted here; to remove every sub-item pass clear_fields=['sub_items']")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("value", [-0.01, -1, -250.75])
async def test_a_negative_price_is_sent_as_given_on_update(server, mock_gorelo, value):
    # contract e15cb5a18ec2: UpdateItemCommand.UnitPrice "May be negative, for a discount or credit line"
    route_update(mock_gorelo, item_detail(UnitPrice=value))
    result = await call_tool(server, "update_item", {"item_id": ITEM_ID, "unit_price": value})
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("PATCH", ITEM_PATH), ("GET", ITEM_PATH)]
    assert mock_gorelo.requests[0].json == {"UnitPrice": value}
    assert result["UnitPrice"] == value


@pytest.mark.parametrize("value", [-0.01, -1])
async def test_a_negative_cost_is_refused_on_update_naming_unit_cost(server, mock_gorelo, value):
    # "a supplied negative value is a 400" is still what UpdateItemCommand.UnitCost says
    text = await call_tool_error(server, "update_item", {"item_id": ITEM_ID, "unit_cost": value})
    assert text == "unit_cost: must be a number of 0 or more"
    assert mock_gorelo.requests == []


async def test_a_negative_price_with_a_negative_cost_on_update_is_refused_for_the_cost_only(server, mock_gorelo):
    text = await call_tool_error(server, "update_item", {"item_id": ITEM_ID, "unit_price": -3, "unit_cost": -3})
    assert text == "unit_cost: must be a number of 0 or more"
    assert mock_gorelo.requests == []
    route_update(mock_gorelo)
    await call_tool(server, "update_item", {"item_id": ITEM_ID, "unit_price": -3, "unit_cost": 3})
    assert mock_gorelo.requests[0].json == {"UnitCost": 3, "UnitPrice": -3}


async def test_a_price_of_zero_is_a_real_change(server, mock_gorelo):
    route_update(mock_gorelo)
    await call_tool(server, "update_item", {"item_id": ITEM_ID, "unit_price": 0})
    assert mock_gorelo.requests[0].json == {"UnitPrice": 0}


@pytest.mark.parametrize("args", [{}, {"clear_fields": None}])
async def test_an_update_without_any_change_is_refused_and_sends_nothing(server, mock_gorelo, args):
    text = await call_tool_error(server, "update_item", {"item_id": ITEM_ID, **args})
    assert text == "no change requested: give at least one field to change, or name fields to remove in clear_fields"
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", ["", "7", "not-a-guid", "../taxes", f"{ITEM_ID} "])
async def test_update_item_needs_a_guid_and_names_item_id(server, mock_gorelo, bad):
    text = await call_tool_error(server, "update_item", {"item_id": bad, "unit_price": 1})
    assert text.startswith("item_id: expected a GUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b, got ")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "property_name, param",
    [("Name", "name"), ("Number", "number"), ("UnitPrice", "unit_price"), ("UnitCost", "unit_cost"), ("StatusId", "status"),
     ("SubItems", "sub_items"), ("SubcategoryId", "subcategory_id"), ("TypeId", "TypeId"), ("itemId", "item_id")],
)
async def test_a_gorelo_error_on_update_names_the_snake_case_param(server, mock_gorelo, property_name, param):
    # TypeId is not a parameter of update_item: Gorelo's own name stays
    mock_gorelo.on("PATCH", ITEM_PATH, error_envelope(400, [("070101", "Rejected by Gorelo.", property_name)]))
    text = await call_tool_error(server, "update_item", {"item_id": ITEM_ID, "unit_price": 1})
    assert text == f"Gorelo rejected update_item (HTTP 400, code 070101): {param}: Rejected by Gorelo. [trace {TEST_TRACE_ID}]"
    assert mock_gorelo.calls("GET") == []


async def test_a_404_on_update_names_item_id(server, mock_gorelo):
    mock_gorelo.on("PATCH", ITEM_PATH, error_envelope(404, [("070401", "Item not found.", "itemId")]))
    text = await call_tool_error(server, "update_item", {"item_id": ITEM_ID, "unit_price": 1})
    assert "HTTP 404, code 070401): item_id: Item not found." in text


@pytest.mark.parametrize(
    "data", [None, True, False, {}, [], [{"Id": ITEM_ID}], "ok"], ids=["null", "true", "false", "empty-object", "empty-list", "list", "text"]
)
async def test_an_update_answer_that_is_not_an_object_is_not_taken_for_a_clean_success(server, mock_gorelo, data):
    mock_gorelo.on("PATCH", ITEM_PATH, envelope(data))
    text = await call_tool_error(server, "update_item", {"item_id": ITEM_ID, "unit_price": 1})
    assert text.startswith(
        "Gorelo returned an unexpected response for update_item: PATCH /v1/items/{itemId}: "
        "expected Data to be a non-empty object but got "
    )
    assert "the write may have been applied, so verify it with a read before repeating it" in text
    assert mock_gorelo.calls("GET") == [] and len(mock_gorelo.calls("PATCH")) == 1  # nothing re-read, nothing repeated


async def test_a_failed_reread_after_a_successful_update_returns_a_warning(server, mock_gorelo):
    mock_gorelo.on("PATCH", ITEM_PATH, envelope({"Id": ITEM_ID}))
    mock_gorelo.on("GET", ITEM_PATH, httpx.ReadTimeout("slow"))
    result = await call_tool(server, "update_item", {"item_id": ITEM_ID, "unit_price": 1})
    assert set(result) == {"Id", "warning"} and result["Id"] == ITEM_ID
    assert "the write succeeded; re-reading it failed: GET /v1/items/{itemId} timed out" in result["warning"]
    assert len(mock_gorelo.calls("PATCH")) == 1 and len(mock_gorelo.calls("GET")) == 1


async def test_an_update_that_times_out_is_not_retried_and_says_to_verify(server, mock_gorelo):
    mock_gorelo.on("PATCH", ITEM_PATH, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "update_item", {"item_id": ITEM_ID, "unit_price": 1})
    assert text.startswith("Gorelo did not confirm update_item (the request timed out).")
    assert "Verify with a read before retrying." in text
    assert len(mock_gorelo.requests) == 1


# --------------------------------------------------------------------------
# delete_item
# --------------------------------------------------------------------------


async def test_delete_refuses_without_confirm_and_makes_no_http_call(server, mock_gorelo):
    for args in ({"item_id": ITEM_ID}, {"item_id": ITEM_ID, "confirm": False}):
        text = await call_tool_error(server, "delete_item", args)
        assert text.startswith(f"confirm: refusing to delete catalog item {ITEM_ID} without confirm=true.")
        assert "409" in text and "sub-item of a bundle or billed on a contract" in text
        assert f"Call again with confirm=true if you really want to delete catalog item {ITEM_ID}." in text
    assert mock_gorelo.requests == []


async def test_a_confirmed_delete_sends_one_delete_and_returns_gorelos_record(server, mock_gorelo):
    mock_gorelo.on("DELETE", ITEM_PATH, envelope({"Id": ITEM_ID}))
    result = await call_tool(server, "delete_item", {"item_id": ITEM_ID, "confirm": True})
    assert result == {"Id": ITEM_ID}
    request = mock_gorelo.last
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("DELETE", ITEM_PATH)]
    assert request.query == {} and request.json is None


@pytest.mark.parametrize(
    "data", [None, True, False, {}, [], [{"Id": ITEM_ID}], "ok", 5], ids=["null", "true", "false", "empty-object", "empty-list", "list", "text", "number"]
)
async def test_a_delete_answer_that_is_not_an_object_raises_instead_of_reporting_success(server, mock_gorelo, data):
    # the DELETE may have been applied, so the answer is never turned into {"ok": true}
    mock_gorelo.on("DELETE", ITEM_PATH, envelope(data))
    text = await call_tool_error(server, "delete_item", {"item_id": ITEM_ID, "confirm": True})
    assert text.startswith(
        "Gorelo returned an unexpected response for delete_item: DELETE /v1/items/{itemId}: "
        "expected Data to be a non-empty object but got "
    )
    assert "the write may have been applied, so verify it with a read before repeating it" in text
    assert len(mock_gorelo.calls("DELETE")) == 1  # never repeated


async def test_a_blocked_delete_shows_what_blocks_it(server, mock_gorelo):
    body = error_envelope(409, [("070409", "The item is a sub-item of the bundle 'Starter Kit'.")])
    mock_gorelo.on("DELETE", ITEM_PATH, body)
    text = await call_tool_error(server, "delete_item", {"item_id": ITEM_ID, "confirm": True})
    assert text == (
        "Gorelo rejected delete_item (HTTP 409, code 070409): The item is a sub-item of the bundle 'Starter Kit'. "
        f"[trace {TEST_TRACE_ID}]"
    )
    assert len(mock_gorelo.requests) == 1


async def test_a_blocked_delete_lists_every_blocker_gorelo_names_not_just_the_first_few(server, mock_gorelo):
    # contract e15cb5a18ec2: "every blocker that applies is listed in the one response" (bundles and contracts at once).
    # The client's own one-line summary stops at three notifications; what the model reads must not
    blockers = [
        "The item is a sub-item of the bundle 'Starter Kit'.",
        "The item is a sub-item of the bundle 'Travel Pack'.",
        "The item is billed on the contract 'Managed Services'.",
        "The item is billed on the contract 'Backup'.",
    ]
    mock_gorelo.on("DELETE", ITEM_PATH, error_envelope(409, [("070409", message) for message in blockers]))
    text = await call_tool_error(server, "delete_item", {"item_id": ITEM_ID, "confirm": True})
    assert text == f"Gorelo rejected delete_item (HTTP 409, code 070409): {'; '.join(blockers)} [trace {TEST_TRACE_ID}]"
    assert len(mock_gorelo.calls("DELETE")) == 1 and len(mock_gorelo.requests) == 1  # no status check, no retry


async def test_delete_404_names_item_id(server, mock_gorelo):
    mock_gorelo.on("DELETE", ITEM_PATH, error_envelope(404, [("070401", "Item not found.", "itemId")]))
    text = await call_tool_error(server, "delete_item", {"item_id": ITEM_ID, "confirm": True})
    assert "HTTP 404, code 070401): item_id: Item not found." in text


async def test_a_delete_that_times_out_is_not_retried_and_says_to_verify(server, mock_gorelo):
    mock_gorelo.on("DELETE", ITEM_PATH, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "delete_item", {"item_id": ITEM_ID, "confirm": True})
    assert text.startswith("Gorelo did not confirm delete_item (the request timed out).")
    assert "Verify with a read before retrying." in text
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize("bad", ["", "5", "categories", f"{ITEM_ID}/../taxes", f" {ITEM_ID}"])
async def test_delete_needs_a_guid_and_names_item_id_even_without_confirm(server, mock_gorelo, bad):
    text = await call_tool_error(server, "delete_item", {"item_id": bad, "confirm": True})
    assert text.startswith("item_id: expected a GUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b, got ")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", ["true", "yes", 1, "1"], ids=["text-true", "text-yes", "number-1", "text-1"])
async def test_confirm_is_a_strict_boolean_so_nothing_but_true_deletes(server, mock_gorelo, bad):
    text = await call_tool_error(server, "delete_item", {"item_id": ITEM_ID, "confirm": bad})
    assert "confirm" in text
    assert mock_gorelo.requests == []


async def test_delete_item_does_not_exist_unless_delete_tools_are_enabled(server_factory, mock_gorelo):
    off = server_factory()
    names = {tool.name for tool in await list_tools(off)}
    assert {"list_items", "get_item", "create_item", "update_item", "list_item_categories", "list_taxes"} <= names
    assert "delete_item" not in names
    text = await call_tool_error(off, "delete_item", {"item_id": ITEM_ID, "confirm": True})
    assert "unknown tool" in text.lower()
    assert mock_gorelo.requests == []


async def test_the_catalog_tools_belong_to_the_billing_toolset(server_factory):
    names = set(EXPECTED_TOOLS)
    assert not names & {t.name for t in await list_tools(server_factory(toolsets={"core"}, destructive=True))}
    assert names <= {t.name for t in await list_tools(server_factory(toolsets={"billing"}, destructive=True))}
