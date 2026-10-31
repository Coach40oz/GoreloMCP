"""tools/product_charges.py: list_product_charges, get_product_charge, create_product_charge, update_product_charge and
delete_product_charge (offline: MockGorelo and an in-process client)."""

import inspect
import json
import logging

import httpx
import pytest
from conftest import (
    TEST_TRACE_ID,
    call_tool,
    call_tool_error,
    error_envelope,
    envelope,
    list_tools,
    make_ctx,
    paged_envelope,
    uid,
)
from fastmcp.exceptions import ToolError

from tools import product_charges
from tools._common import REGISTRY

pytestmark = pytest.mark.anyio

CHARGES = "/v1/product-charges"
CHARGE_ID = 91524
CHARGE = f"{CHARGES}/{CHARGE_ID}"
TICKET = uid(11)
TASK = uid(12)
PROJECT = uid(13)
ITEM = uid(14)
INVOICE = uid(15)


@pytest.fixture(autouse=True)
def _quiet_fastmcp_tracebacks():
    logger = logging.getLogger("fastmcp")
    level = logger.level
    logger.setLevel(logging.CRITICAL)
    yield
    logger.setLevel(level)


@pytest.fixture
def server(server_factory):
    """Every toolset on and the gated tools enabled."""
    return server_factory(destructive=True)


def trace(text):
    return f"{text} [trace {TEST_TRACE_ID}]"


def flat(text):
    return " ".join(text.split())


def charge(**extra):
    """One ProductChargeModel as Gorelo returns it (a ticket charge)."""
    row = {
        "Id": CHARGE_ID,
        "TicketId": TICKET,
        "TaskId": None,
        "ProjectId": None,
        "ItemId": ITEM,
        "Name": "USB-C Docking Station",
        "Description": "Replacement dock",
        "Quantity": 2.0,
        "UnitPrice": 189.0,
        "UnitCost": 142.5,
        "Amount": 378.0,
        "BillableStatus": {"Id": 1, "Name": "Billable"},
        "ClientId": 4983,
        "InvoiceId": None,
        "CreatedOn": "2026-10-20T10:00:00Z",
        "UpdatedOn": None,
    }
    row.update(extra)
    return row


def module_specs():
    return [spec for spec in REGISTRY.specs if spec.fn.__module__ == product_charges.__name__]


EXPECTED_TOOLS = {
    "list_product_charges": ("read", ["GET /v1/product-charges"]),
    "get_product_charge": ("read", ["GET /v1/product-charges/{productChargeId}"]),
    "create_product_charge": ("write", ["POST /v1/product-charges", "GET /v1/product-charges/{productChargeId}"]),
    "update_product_charge": ("write", ["PATCH /v1/product-charges/{productChargeId}"]),
    "delete_product_charge": ("destructive", ["DELETE /v1/product-charges/{productChargeId}"]),
}


def tool_def(name, tools):
    return next(tool for tool in tools if tool.name == name)


async def call_directly(client_factory, tool, **kwargs):
    async with client_factory() as client:
        return await tool(make_ctx(client), **kwargs)


# --------------------------------------------------------------------------
# What the module declares
# --------------------------------------------------------------------------


def test_the_module_declares_exactly_these_tools_in_the_billing_toolset():
    assert {spec.name: (spec.kind, spec.ops) for spec in module_specs()} == EXPECTED_TOOLS
    assert {spec.toolset for spec in module_specs()} == {"billing"}
    assert {spec.name for spec in module_specs() if spec.destructive_hint} == {"update_product_charge", "delete_product_charge"}


def test_the_module_docstring_names_every_op():
    for _kind, ops in EXPECTED_TOOLS.values():
        for op in ops:
            assert op in product_charges.__doc__, op


def test_the_fixture_is_shaped_like_the_spec(spec_index):
    assert set(charge()) == set(spec_index.schema("ProductChargeModel")["fields"])
    assert set(spec_index.schema("CreateProductChargeResult")["fields"]) == {"Id"}
    assert set(spec_index.schema("DeleteProductChargeResult")["fields"]) == {"Id"}


def test_every_field_map_entry_is_a_name_the_spec_knows(spec_index):
    for spec in module_specs():
        known = set()
        for op_key in spec.ops:
            op = spec_index.op(op_key)
            known |= {name.lower() for name in op.query_params}
            known |= {name.lower() for name in op.path_placeholders}
            known |= {name.lower() for name in ((op.body or {}).get("fields") or {})}
        for param, name in spec.field_map.items():
            assert name.lower() in known, f"{spec.name}: {param} maps to {name}, which no declared op has"
    assert set(product_charges.CREATE_FIELDS.values()) == set(spec_index.op("POST /v1/product-charges").body["fields"])
    assert set(product_charges.UPDATE_FIELDS.values()) == set(spec_index.op("PATCH /v1/product-charges/{productChargeId}").body["fields"])
    assert not {"TicketId", "TaskId", "ItemId"} & set(product_charges.UPDATE_FIELDS.values())  # cannot change


def test_the_texts_stay_within_the_limits():
    for spec in module_specs():
        doc = inspect.getdoc(spec.fn)
        assert 40 < len(doc) <= 900, spec.name
    from fastmcp.tools import Tool

    for spec in module_specs():
        for name, definition in Tool.from_function(spec.fn).parameters["properties"].items():
            text = definition.get("description")
            assert text and len(text) <= 220, f"{spec.name}.{name}"


def test_the_docstrings_state_the_facts_the_model_must_know():
    create = flat(inspect.getdoc(product_charges.create_product_charge))
    assert "exactly one of ticket_id and task_id" in create
    assert "closed, has no client or whose client is on credit hold" in create
    assert "task whose project is closed or has no client" in create
    assert "a second call charges twice" in create and "{Id, warning} means the charge exists: do not repeat" in create
    update = flat(inspect.getdoc(product_charges.update_product_charge))
    assert "Resolve ids first: product_charge_id -> list_product_charges." in update
    assert "What it is charged to and its item cannot change" in update
    assert "waiting to be invoiced or has been invoiced" in update and "delete_product_charge, when deletes are enabled" in update
    delete = flat(inspect.getdoc(product_charges.delete_product_charge))
    assert "waiting to be invoiced or has been invoiced" in delete and "appears on the ticket's or task's timeline" in delete
    assert delete.endswith("Ask the user first; needs confirm=true.")
    assert "Removed charges are never listed" in flat(inspect.getdoc(product_charges.list_product_charges))


# --------------------------------------------------------------------------
# list_product_charges
# --------------------------------------------------------------------------


async def test_list_without_filters_sends_only_the_page_size(server, mock_gorelo):
    mock_gorelo.on("GET", CHARGES, paged_envelope([charge()], total_count=1))
    result = await call_tool(server, "list_product_charges")
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", CHARGES, {"PageSize": "50"}, None)
    assert result == {"items": [charge()], "count": 1, "total_count": 1, "has_more": False, "next_cursor": None, "page_size": 50, "filters": {}}


async def test_every_filter_goes_out_under_its_spec_name(server, mock_gorelo):
    mock_gorelo.on("GET", CHARGES, paged_envelope([]))
    result = await call_tool(
        server, "list_product_charges",
        {
            "ticket_ids": [TICKET.upper(), uid(21)], "task_ids": [TASK], "project_ids": [PROJECT], "client_ids": [4983, 7],
            "invoice_ids": [INVOICE], "created_since": "2026-10-01T00:00:00Z", "created_before": "2026-11-01T02:00:00+02:00",
            "page_size": 999, "cursor": "c9",
        },
    )
    assert mock_gorelo.last.query == {
        "TicketIds": f"{TICKET},{uid(21)}", "TaskIds": TASK, "ProjectIds": PROJECT, "ClientIds": "4983,7", "InvoiceIds": INVOICE,
        "CreatedSince": "2026-10-01T00:00:00Z", "CreatedBefore": "2026-11-01T00:00:00Z", "PageSize": "200", "Cursor": "c9",
    }
    assert result["page_size"] == 200 and result["filters"]["ticket_ids"] == [TICKET, uid(21)]


async def test_paging_follows_next_cursor_with_the_same_filters(server, mock_gorelo):
    mock_gorelo.on("GET", CHARGES, paged_envelope([charge()], next_cursor="c1", total_count=2))
    first = await call_tool(server, "list_product_charges", {"ticket_ids": [TICKET]})
    assert first["has_more"] and first["next_cursor"] == "c1"
    await call_tool(server, "list_product_charges", {**first["filters"], "cursor": first["next_cursor"]})
    assert mock_gorelo.last.query == {"TicketIds": TICKET, "PageSize": "50", "Cursor": "c1"}


@pytest.mark.parametrize(
    "args, fragment",
    [
        ({"ticket_ids": ["T-1"]}, "ticket_ids[0]: expected a GUID"),
        ({"ticket_ids": []}, "ticket_ids: expected at least one GUID"),
        ({"task_ids": ["x"]}, "task_ids[0]: expected a GUID"),
        ({"project_ids": [5]}, "project_ids[0]: expected a GUID"),
        ({"invoice_ids": ["INV-1"]}, "invoice_ids[0]: expected a GUID"),
        ({"client_ids": [0]}, "client_ids"),
        ({"created_since": "2026-10-01T00:00:00"}, "created_since"),
        ({"cursor": ""}, "cursor: must not be empty"),
    ],
)
async def test_list_local_errors_name_the_param_and_send_nothing(client_factory, mock_gorelo, args, fragment):
    with pytest.raises((ToolError, ValueError)) as info:
        await call_directly(client_factory, product_charges.list_product_charges, **args)
    assert fragment in str(info.value) and mock_gorelo.requests == []


async def test_a_bool_client_id_is_refused_by_the_schema(server, mock_gorelo):
    assert "client_ids" in await call_tool_error(server, "list_product_charges", {"client_ids": [True]})
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "property_name, param",
    [("TicketIds", "ticket_ids"), ("TaskIds", "task_ids"), ("ProjectIds", "project_ids"), ("ClientIds", "client_ids"),
     ("InvoiceIds", "invoice_ids"), ("CreatedSince", "created_since"), ("CreatedBefore", "created_before"), ("PageSize", "page_size"),
     ("Cursor", "cursor")],
)
async def test_list_a_gorelo_400_names_the_snake_case_param(server, mock_gorelo, property_name, param):
    mock_gorelo.on("GET", CHARGES, error_envelope(400, [("070101", "rejected", property_name)]))
    assert f"HTTP 400, code 070101): {param}: rejected" in await call_tool_error(server, "list_product_charges")


# --------------------------------------------------------------------------
# get_product_charge
# --------------------------------------------------------------------------


async def test_get_reads_one_charge_by_its_integer_id_and_returns_the_record_unchanged(server, mock_gorelo):
    mock_gorelo.on("GET", CHARGE, envelope(charge()))
    assert await call_tool(server, "get_product_charge", {"product_charge_id": CHARGE_ID}) == charge()
    request = mock_gorelo.last
    assert (request.method, request.path, request.query) == ("GET", CHARGE, {})


@pytest.mark.parametrize("bad", ["91524", True, 0, -1, 1.5, None])
async def test_get_needs_a_positive_integer(server, mock_gorelo, bad):
    text = await call_tool_error(server, "get_product_charge", {"product_charge_id": bad})
    assert "product_charge_id" in text and mock_gorelo.requests == []


async def test_get_a_404_names_product_charge_id_and_a_non_object_answer_is_refused(server, mock_gorelo):
    mock_gorelo.on("GET", CHARGE, error_envelope(404, [("070401", "Product charge not found.", "productChargeId")]))
    assert await call_tool_error(server, "get_product_charge", {"product_charge_id": CHARGE_ID}) == trace(
        "Gorelo rejected get_product_charge (HTTP 404, code 070401): product_charge_id: Product charge not found."
    )
    mock_gorelo.on("GET", f"{CHARGES}/7", envelope([charge()]))
    assert "unexpected response" in await call_tool_error(server, "get_product_charge", {"product_charge_id": 7})


# --------------------------------------------------------------------------
# create_product_charge
# --------------------------------------------------------------------------

NEW_ID = 91600
NEW = f"{CHARGES}/{NEW_ID}"


def route_create(mock, record=None):
    mock.on("POST", CHARGES, envelope({"Id": NEW_ID}))
    mock.on("GET", NEW, envelope(record if record is not None else charge(Id=NEW_ID)))


async def test_create_on_a_ticket_posts_the_body_then_reads_the_charge_back(server, mock_gorelo):
    route_create(mock_gorelo)
    result = await call_tool(server, "create_product_charge", {"ticket_id": TICKET.upper(), "item_id": ITEM, "quantity": 2})
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("POST", CHARGES), ("GET", NEW)]
    post, read = mock_gorelo.requests
    assert post.json == {"TicketId": TICKET, "ItemId": ITEM, "Quantity": 2} and post.query == {}
    assert read.query == {} and read.json is None
    assert result == charge(Id=NEW_ID)


async def test_create_on_a_task_with_every_optional_field(server, mock_gorelo):
    route_create(mock_gorelo, charge(Id=NEW_ID, TicketId=None, TaskId=TASK, ProjectId=PROJECT))
    await call_tool(
        server, "create_product_charge",
        {
            "task_id": TASK, "item_id": ITEM.replace("-", ""), "quantity": 1.5, "unit_price": -10.25, "unit_cost": 0,
            "description": "Text", "billable_status_id": 3,
        },
    )
    assert mock_gorelo.requests[0].json == {
        "TaskId": TASK, "ItemId": ITEM, "Quantity": 1.5, "UnitPrice": -10.25, "UnitCost": 0, "Description": "Text",
        "BillableStatusId": 3,
    }


async def test_a_field_the_caller_did_not_give_is_never_sent(server, mock_gorelo):
    route_create(mock_gorelo)
    await call_tool(server, "create_product_charge", {"ticket_id": TICKET, "item_id": ITEM, "quantity": 1})
    assert set(mock_gorelo.requests[0].json) == {"TicketId", "ItemId", "Quantity"}


@pytest.mark.parametrize("status", [1, 2, 3])
async def test_every_billable_status_a_new_charge_can_have_is_accepted(server, mock_gorelo, status):
    route_create(mock_gorelo)
    await call_tool(server, "create_product_charge", {"ticket_id": TICKET, "item_id": ITEM, "quantity": 1, "billable_status_id": status})
    assert mock_gorelo.requests[0].json["BillableStatusId"] == status


async def test_create_refuses_5_void_before_any_request_and_says_where_void_is_set(server, mock_gorelo):
    # a charge created void is pointless, so create refuses 5; update_product_charge keeps it
    text = await call_tool_error(
        server, "create_product_charge", {"ticket_id": TICKET, "item_id": ITEM, "quantity": 1, "billable_status_id": 5}
    )
    assert text.startswith("billable_status_id: 5 (Void) is not accepted when a charge is created")
    assert "use 1 (Billable), 2 (No charge), 3 (Non-billable)" in text and "update_product_charge can set Void" in text
    assert mock_gorelo.requests == []


async def test_the_create_texts_no_longer_offer_void_but_the_update_texts_do(server_factory):
    tools = {t.name: t for t in await list_tools(server_factory(destructive=True))}
    create = tools["create_product_charge"].inputSchema["properties"]["billable_status_id"]["description"]
    update = tools["update_product_charge"].inputSchema["properties"]["billable_status_id"]["description"]
    assert "Void" not in create and create == "1 Billable, 2 No charge, 3 Non-billable. Default 1."
    assert update == "1 Billable, 2 No charge, 3 Non-billable, 5 Void."


BASE = {"ticket_id": TICKET, "item_id": ITEM, "quantity": 1}


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"ticket_id": None}, "ticket_id and task_id: give exactly one of them"),
        ({"task_id": TASK}, "ticket_id and task_id: give exactly one of them"),
        ({"ticket_id": "T-123"}, "ticket_id: expected a GUID"),
        ({"ticket_id": None, "task_id": "x"}, "task_id: expected a GUID"),
        ({"item_id": "SKU-1"}, "item_id: expected a GUID"),
        ({"quantity": 0}, "quantity: must be above 0"),
        ({"quantity": -1}, "quantity: must be above 0"),
        ({"quantity": 100_000_000}, "quantity: must be above 0 and at most 99,999,999"),
        ({"quantity": 1.00005}, "quantity: at most 4 decimal places"),
        ({"quantity": float("nan")}, "quantity: must be a finite number"),
        ({"unit_price": 100_000_000}, "unit_price: must be from -99,999,999 to 99,999,999"),
        ({"unit_price": -100_000_000}, "unit_price: must be from -99,999,999 to 99,999,999"),
        ({"unit_cost": -0.01}, "unit_cost: must be from 0 to 99,999,999"),
        ({"description": "   "}, "description: must not be empty or whitespace only"),
        ({"billable_status_id": 4}, "billable_status_id: must be one of 1 (Billable), 2 (No charge), 3 (Non-billable), got 4"),
        ({"billable_status_id": 0}, "billable_status_id: must be one of"),
    ],
)
async def test_create_local_errors_name_the_param_and_send_nothing(client_factory, mock_gorelo, overrides, fragment):
    with pytest.raises((ToolError, ValueError)) as info:
        await call_directly(client_factory, product_charges.create_product_charge, **{**BASE, **overrides})
    assert fragment in str(info.value)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "args, param",
    [
        ({"quantity": "2"}, "quantity"),
        ({"quantity": True}, "quantity"),
        ({"unit_price": "5"}, "unit_price"),
        ({"billable_status_id": "1"}, "billable_status_id"),
        ({"billable_status_id": True}, "billable_status_id"),
        ({"item_id": 5}, "item_id"),
    ],
)
async def test_the_schema_refuses_wrongly_typed_values_with_the_param_name(server, mock_gorelo, args, param):
    text = await call_tool_error(server, "create_product_charge", {**BASE, **args})
    assert param in text and mock_gorelo.requests == []


async def test_quantity_and_item_id_are_required(server, mock_gorelo):
    for missing in ("item_id", "quantity"):
        args = {key: value for key, value in BASE.items() if key != missing}
        assert missing in await call_tool_error(server, "create_product_charge", args)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "property_name, param",
    [("TicketId", "ticket_id"), ("TaskId", "task_id"), ("ItemId", "item_id"), ("Quantity", "quantity"), ("UnitPrice", "unit_price"),
     ("UnitCost", "unit_cost"), ("Description", "description"), ("BillableStatusId", "billable_status_id")],
)
async def test_create_a_gorelo_400_names_the_snake_case_param(server, mock_gorelo, property_name, param):
    mock_gorelo.on("POST", CHARGES, error_envelope(400, [("070101", "Not an active product.", property_name)]))
    text = await call_tool_error(server, "create_product_charge", BASE)
    assert text == trace(f"Gorelo rejected create_product_charge (HTTP 400, code 070101): {param}: Not an active product.")
    assert mock_gorelo.calls("GET") == []


@pytest.mark.parametrize("reason", ["The ticket is closed.", "The client is on credit hold.", "The project is closed."])
async def test_a_409_is_reported_with_its_reason_and_is_not_an_unconfirmed_write(server, mock_gorelo, reason):
    mock_gorelo.on("POST", CHARGES, error_envelope(409, [("070901", reason)]))
    text = await call_tool_error(server, "create_product_charge", BASE)
    assert text == trace(f"Gorelo rejected create_product_charge (HTTP 409, code 070901): {reason}")
    assert "may or may not" not in text and len(mock_gorelo.requests) == 1


async def test_an_unknown_ticket_is_a_404_that_names_ticket_id(server, mock_gorelo):
    mock_gorelo.on("POST", CHARGES, error_envelope(404, [("070401", "Ticket not found.", "TicketId")]))
    assert "ticket_id: Ticket not found." in await call_tool_error(server, "create_product_charge", BASE)


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")], ids=["timeout", "connection"])
async def test_a_post_that_is_not_answered_is_never_retried_and_says_to_verify(server, mock_gorelo, failure):
    mock_gorelo.on("POST", CHARGES, failure)
    text = await call_tool_error(server, "create_product_charge", BASE)
    assert text.startswith("Gorelo did not confirm create_product_charge (")
    assert "Verify with a read before retrying." in text
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("POST", CHARGES)]


async def test_a_5xx_may_have_created_the_charge(server, mock_gorelo):
    mock_gorelo.on("POST", CHARGES, error_envelope(500, [("070500", "Boom.")]))
    text = await call_tool_error(server, "create_product_charge", BASE)
    assert "Gorelo may have applied the change before failing. Verify with a read before retrying." in text


@pytest.mark.parametrize("data", [{}, {"Id": None}, {"Id": 0}, {"Id": True}, [], None])
async def test_an_answer_without_a_usable_id_is_not_taken_for_a_success(server, mock_gorelo, data):
    mock_gorelo.on("POST", CHARGES, envelope(data))
    text = await call_tool_error(server, "create_product_charge", BASE)
    assert "verify it with a read before repeating it" in text or "Verify with a read before retrying." in text
    assert [r.method for r in mock_gorelo.requests] == ["POST"]


async def test_a_failed_read_back_returns_the_id_and_a_warning_never_a_second_post(server, mock_gorelo):
    mock_gorelo.on("POST", CHARGES, envelope({"Id": NEW_ID}))
    mock_gorelo.on("GET", NEW, error_envelope(500, [("070500", "Down.")]))
    result = await call_tool(server, "create_product_charge", BASE)
    assert result["Id"] == NEW_ID and "the write succeeded; re-reading it failed" in result["warning"]
    assert "Do not repeat the write" in result["warning"]
    assert [r.method for r in mock_gorelo.requests] == ["POST", "GET"]


# --------------------------------------------------------------------------
# update_product_charge
# --------------------------------------------------------------------------


async def test_update_sends_only_the_fields_given_under_their_spec_names_and_returns_the_answer(server, mock_gorelo):
    answer = charge(Quantity=3.0, UnitPrice=179.0, UnitCost=140.0, Description="New", BillableStatus={"Id": 2, "Name": "No charge"})
    mock_gorelo.on("PATCH", CHARGE, envelope(answer))
    result = await call_tool(
        server, "update_product_charge",
        {"product_charge_id": CHARGE_ID, "quantity": 3, "unit_price": 179, "unit_cost": 140, "description": "New", "billable_status_id": 2},
    )
    request = mock_gorelo.last
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("PATCH", CHARGE)]  # PATCH answers the charge: no extra read
    assert request.json == {"Quantity": 3, "UnitPrice": 179, "UnitCost": 140, "Description": "New", "BillableStatusId": 2}
    assert request.query == {} and result == answer


@pytest.mark.parametrize("field, wire, value", [("quantity", "Quantity", 4), ("unit_price", "UnitPrice", 5.5), ("unit_cost", "UnitCost", 0), ("billable_status_id", "BillableStatusId", 3)])
async def test_each_field_alone_goes_out_as_its_own_key(server, mock_gorelo, field, wire, value):
    mock_gorelo.on("PATCH", CHARGE, envelope(charge(Quantity=4.0, UnitPrice=5.5, UnitCost=0.0, BillableStatus={"Id": 3, "Name": "Non-billable"})))
    await call_tool(server, "update_product_charge", {"product_charge_id": CHARGE_ID, field: value})
    assert mock_gorelo.last.json == {wire: value}


async def test_update_still_sets_5_void_on_an_existing_charge(server, mock_gorelo):
    mock_gorelo.on("PATCH", CHARGE, envelope(charge(BillableStatus={"Id": 5, "Name": "Void"})))
    result = await call_tool(server, "update_product_charge", {"product_charge_id": CHARGE_ID, "billable_status_id": 5})
    assert mock_gorelo.last.json == {"BillableStatusId": 5} and result["BillableStatus"]["Id"] == 5


async def test_clear_description_sends_an_explicit_null(server, mock_gorelo):
    mock_gorelo.on("PATCH", CHARGE, envelope(charge(Description=None)))
    result = await call_tool(server, "update_product_charge", {"product_charge_id": CHARGE_ID, "clear_description": True})
    assert mock_gorelo.last.json == {"Description": None} and result["Description"] is None
    assert b'"Description":null' in mock_gorelo.last.content.replace(b" ", b"")


@pytest.mark.parametrize(
    "args, fragment",
    [
        ({}, "give at least one of them, there is nothing to change"),
        ({"clear_description": False}, "give at least one of them"),
        ({"description": "x", "clear_description": True}, "clear_description: cannot be true together with description"),
        ({"description": " "}, "description: must not be empty or whitespace only"),
        ({"quantity": 0}, "quantity: must be above 0"),
        ({"quantity": 0.12345}, "quantity: at most 4 decimal places"),
        ({"unit_price": 1e9}, "unit_price: must be from"),
        ({"unit_cost": -1}, "unit_cost: must be from 0"),
        ({"billable_status_id": 9}, "billable_status_id: must be one of"),
    ],
)
async def test_update_local_errors_name_the_param_and_send_nothing(client_factory, mock_gorelo, args, fragment):
    with pytest.raises((ToolError, ValueError)) as info:
        await call_directly(client_factory, product_charges.update_product_charge, product_charge_id=CHARGE_ID, **args)
    assert fragment in str(info.value) and mock_gorelo.requests == []


async def test_update_cannot_carry_what_the_charge_is_charged_to_or_its_item(server, mock_gorelo):
    for name in ("ticket_id", "task_id", "item_id"):
        text = await call_tool_error(server, "update_product_charge", {"product_charge_id": CHARGE_ID, "quantity": 1, name: TICKET})
        assert name in text
    assert mock_gorelo.requests == []
    tool = tool_def("update_product_charge", await list_tools(server))
    assert not {"ticket_id", "task_id", "item_id"} & set(tool.inputSchema["properties"])


@pytest.mark.parametrize("bad", ["5", True, 0, None])
async def test_update_needs_a_positive_integer_id(server, mock_gorelo, bad):
    text = await call_tool_error(server, "update_product_charge", {"product_charge_id": bad, "quantity": 1})
    assert "product_charge_id" in text and mock_gorelo.requests == []


@pytest.mark.parametrize(
    "property_name, param",
    [("Quantity", "quantity"), ("UnitPrice", "unit_price"), ("UnitCost", "unit_cost"), ("Description", "description"),
     ("BillableStatusId", "billable_status_id"), ("productChargeId", "product_charge_id")],
)
async def test_update_a_gorelo_400_names_the_snake_case_param(server, mock_gorelo, property_name, param):
    mock_gorelo.on("PATCH", CHARGE, error_envelope(400, [("070101", "Rejected.", property_name)]))
    text = await call_tool_error(server, "update_product_charge", {"product_charge_id": CHARGE_ID, "quantity": 1})
    assert text == trace(f"Gorelo rejected update_product_charge (HTTP 400, code 070101): {param}: Rejected.")


@pytest.mark.parametrize("reason", ["The ticket is closed.", "The charge has been invoiced.", "The charge is waiting to be invoiced."])
async def test_update_a_409_is_a_plain_refusal(server, mock_gorelo, reason):
    mock_gorelo.on("PATCH", CHARGE, error_envelope(409, [("070901", reason)]))
    text = await call_tool_error(server, "update_product_charge", {"product_charge_id": CHARGE_ID, "quantity": 1})
    assert text == trace(f"Gorelo rejected update_product_charge (HTTP 409, code 070901): {reason}")


async def test_update_a_404_names_the_id(server, mock_gorelo):
    mock_gorelo.on("PATCH", CHARGE, error_envelope(404, [("070401", "Not found.", "productChargeId")]))
    assert "product_charge_id: Not found." in await call_tool_error(server, "update_product_charge", {"product_charge_id": CHARGE_ID, "quantity": 1})


async def test_update_a_timeout_is_not_retried_and_says_to_verify(server, mock_gorelo):
    mock_gorelo.on("PATCH", CHARGE, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "update_product_charge", {"product_charge_id": CHARGE_ID, "quantity": 1})
    assert text.startswith("Gorelo did not confirm update_product_charge (") and "Verify with a read before retrying." in text
    assert len(mock_gorelo.requests) == 1


async def test_an_answer_that_does_not_show_the_change_is_write_unconfirmed(server, mock_gorelo):
    mock_gorelo.on("PATCH", CHARGE, envelope(charge(Quantity=2.0)))  # still the old quantity
    text = await call_tool_error(server, "update_product_charge", {"product_charge_id": CHARGE_ID, "quantity": 5, "billable_status_id": 2})
    assert "the answer does not show the change (Quantity is not the one that was sent; BillableStatus is not the one that was sent)" in text
    assert "verify it with a read before repeating it (get_product_charge)" in text


async def test_a_description_that_is_still_set_after_a_clear_is_write_unconfirmed(server, mock_gorelo):
    mock_gorelo.on("PATCH", CHARGE, envelope(charge(Description="still here")))
    text = await call_tool_error(server, "update_product_charge", {"product_charge_id": CHARGE_ID, "clear_description": True})
    assert "Description is still set" in text


async def test_a_price_rounded_to_cents_is_not_a_false_alarm(server, mock_gorelo):
    mock_gorelo.on("PATCH", CHARGE, envelope(charge(UnitPrice=10.13)))
    result = await call_tool(server, "update_product_charge", {"product_charge_id": CHARGE_ID, "unit_price": 10.1304})
    assert result["UnitPrice"] == 10.13


@pytest.mark.parametrize("data", [None, {}, [charge()], "ok"])
async def test_an_update_answer_that_is_not_a_charge_is_refused(server, mock_gorelo, data):
    mock_gorelo.on("PATCH", CHARGE, envelope(data))
    text = await call_tool_error(server, "update_product_charge", {"product_charge_id": CHARGE_ID, "quantity": 1})
    assert "verify it with a read" in text or "Verify with a read before retrying." in text


async def test_update_is_a_write_with_the_destructive_hint(server):
    tool = tool_def("update_product_charge", await list_tools(server))
    assert tool.annotations.destructiveHint is True and tool.annotations.readOnlyHint is False
    assert tool.inputSchema["required"] == ["product_charge_id"]


# --------------------------------------------------------------------------
# delete_product_charge (gated)
# --------------------------------------------------------------------------


async def test_delete_refuses_without_confirm_with_no_http_and_says_what_would_happen(server, mock_gorelo):
    text = await call_tool_error(server, "delete_product_charge", {"product_charge_id": CHARGE_ID})
    assert text.startswith(f"confirm: refusing to remove product charge {CHARGE_ID} without confirm=true.")
    assert "removed from its ticket or task and no longer billed" in text and "appears on the timeline" in text
    assert "no tool puts it back" in text and "Nothing has been sent to Gorelo." in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("confirm", ["true", 1, "yes", None, 0, "True"])
async def test_confirm_is_strict_and_a_bad_value_sends_nothing(server, mock_gorelo, confirm):
    text = await call_tool_error(server, "delete_product_charge", {"product_charge_id": CHARGE_ID, "confirm": confirm})
    assert "confirm" in text and mock_gorelo.requests == []


async def test_a_bad_id_is_refused_before_the_confirm_rule(server, mock_gorelo):
    text = await call_tool_error(server, "delete_product_charge", {"product_charge_id": "x"})
    assert "product_charge_id" in text and "confirm" not in text and mock_gorelo.requests == []


async def test_delete_with_confirm_sends_one_delete_and_returns_the_id(server, mock_gorelo):
    mock_gorelo.on("DELETE", CHARGE, envelope({"Id": CHARGE_ID}))
    result = await call_tool(server, "delete_product_charge", {"product_charge_id": CHARGE_ID, "confirm": True})
    assert result == {"Id": CHARGE_ID}
    assert [(r.method, r.path, r.query, r.json) for r in mock_gorelo.requests] == [("DELETE", CHARGE, {}, None)]


@pytest.mark.parametrize("data", [{"Id": 7}, {"Id": "91524"}, {"Id": None}, {"Other": 1}])
async def test_an_answer_with_another_id_is_not_taken_for_the_removal(server, mock_gorelo, data):
    mock_gorelo.on("DELETE", CHARGE, envelope(data))
    text = await call_tool_error(server, "delete_product_charge", {"product_charge_id": CHARGE_ID, "confirm": True})
    assert "not the charge that was asked for" in text and "verify it with a read before repeating it (get_product_charge)" in text


@pytest.mark.parametrize("data", [None, {}, [], 5])
async def test_a_delete_answer_that_is_not_an_object_is_write_unconfirmed(server, mock_gorelo, data):
    mock_gorelo.on("DELETE", CHARGE, envelope(data))
    text = await call_tool_error(server, "delete_product_charge", {"product_charge_id": CHARGE_ID, "confirm": True})
    assert "verify it with a read" in text or "Verify with a read before retrying." in text


@pytest.mark.parametrize("reason", ["The ticket is closed.", "The project is closed.", "The charge has been invoiced."])
async def test_delete_a_409_is_a_plain_refusal(server, mock_gorelo, reason):
    mock_gorelo.on("DELETE", CHARGE, error_envelope(409, [("070901", reason)]))
    text = await call_tool_error(server, "delete_product_charge", {"product_charge_id": CHARGE_ID, "confirm": True})
    assert text == trace(f"Gorelo rejected delete_product_charge (HTTP 409, code 070901): {reason}")


async def test_delete_a_404_names_the_id(server, mock_gorelo):
    mock_gorelo.on("DELETE", CHARGE, error_envelope(404, [("070401", "Already removed.", "productChargeId")]))
    text = await call_tool_error(server, "delete_product_charge", {"product_charge_id": CHARGE_ID, "confirm": True})
    assert "product_charge_id: Already removed." in text


async def test_delete_a_timeout_is_not_retried(server, mock_gorelo):
    mock_gorelo.on("DELETE", CHARGE, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "delete_product_charge", {"product_charge_id": CHARGE_ID, "confirm": True})
    assert text.startswith("Gorelo did not confirm delete_product_charge (") and len(mock_gorelo.requests) == 1


async def test_delete_exists_only_when_the_gated_tools_are_enabled(server_factory):
    off = {t.name for t in await list_tools(server_factory(destructive=False))}
    assert "delete_product_charge" not in off
    assert {"list_product_charges", "get_product_charge", "create_product_charge", "update_product_charge"} <= off
    listed = server_factory(destructive=True, destructive_tools=frozenset({"delete_product_charge"}))
    assert "delete_product_charge" in {t.name for t in await list_tools(listed)}
    spec = next(s for s in REGISTRY.specs if s.name == "delete_product_charge")
    assert (spec.kind, spec.toolset) == ("destructive", "billing")
    assert "Strict" in repr(inspect.signature(spec.fn).parameters["confirm"].annotation)


async def test_the_tools_belong_to_the_billing_toolset(server_factory):
    core = {t.name for t in await list_tools(server_factory(toolsets={"core"}, destructive=True))}
    assert not set(EXPECTED_TOOLS) & core
    billing = {t.name for t in await list_tools(server_factory(toolsets={"billing"}, destructive=True))}
    assert set(EXPECTED_TOOLS) <= billing


async def test_no_text_or_description_value_reaches_the_log(server, mock_gorelo, caplog):
    route = {"Id": NEW_ID}
    mock_gorelo.on("POST", CHARGES, envelope(route))
    mock_gorelo.on("GET", NEW, envelope(charge(Id=NEW_ID)))
    with caplog.at_level(logging.DEBUG):
        await call_tool(server, "create_product_charge", {**BASE, "description": "SECRET-DESCRIPTION-TEXT"})
    assert "SECRET-DESCRIPTION-TEXT" not in "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("gorelo"))
    assert json.loads(mock_gorelo.requests[0].content)["Description"] == "SECRET-DESCRIPTION-TEXT"
