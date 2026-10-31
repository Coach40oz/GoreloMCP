"""tools/invoices.py: list_invoices, get_invoice, create_invoice, create_approved_invoice, export_invoice_pdf and
delete_invoice (offline: MockGorelo and an in-process client)."""

import base64
import inspect
import json
import logging
import re
from pathlib import Path

import httpx
import pytest
from conftest import (
    TEST_TRACE_ID,
    call_tool,
    call_tool_error,
    call_tool_raw,
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

from gorelo_client import EXPORT_NOTE, is_side_effect_get
from tools import invoices
from tools._common import MAX_ID, REGISTRY

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


# the invoices, catalog and uptime modules must stay within this many bytes of tool list JSON. The
# advertised schemas are compacted centrally (server.compact_input_schema), so what the modules still control is its
# descriptions. Measured 21473 bytes for the first 16 tools (descriptions are 7243 of them) and 28240 for
# the 19 tools since contract e15cb5a18ec2 added get_invoice, create_invoice and create_approved_invoice (the two create
# tools carry the line schema, about 1350 bytes each, and the gated one is not even in the default list); the limit is
# that plus 10 percent, rounded up to the next 100 (1.5.0: 23 tools after update_invoice, approve_invoice, list_payments and
# get_payment, measured 38327 bytes, so 42200). the budget covers the texts that say a void happens in Gorelo only.
INVOICES_MODULE_BUDGET_BYTES = 42200

INVOICES = "/v1/invoices"
INVOICE_ID = uid(5)
PDF_PATH = f"/v1/invoices/{INVOICE_ID}/pdf"
DELETE_PATH = f"/v1/invoices/{INVOICE_ID}"
MB5 = 5 * 1024 * 1024


def invoice(number=1042, status=(1, "Draft"), invoice_id=INVOICE_ID, **extra):
    """One InvoiceModel as Gorelo returns it in a list row (PascalCase, Status is a {Id, Name} code)."""
    row = {
        "Id": invoice_id,
        "Number": number,
        "DisplayNumber": f"INV-{number}",
        "ClientId": 9102,
        "ContractId": None,
        "Status": {"Id": status[0], "Name": status[1]},
        "InvoiceDate": "2026-09-01",
        "DueDate": "2026-09-30",
        "SubTotal": 100.0,
        "TotalDiscount": 0.0,
        "TotalTax": 8.0,
        "Total": 108.0,
        "AmountPaid": 0.0,
        "AmountDue": 108.0,
        "Reference": None,
        "ExternalId": None,
        "PaymentLink": None,
        "InvoiceTemplateId": 12,
        "InvoiceEmailTemplateId": 3,
        "BrandingThemeId": None,
        "IsEmailSent": False,
        "EmailSentOn": None,
        "CreatedOn": "2026-09-01T10:00:00Z",
        "UpdatedOn": None,
    }
    row.update(extra)
    return row


ITEM_A = uid(21)
ITEM_B = uid(22)
NEW_ID = uid(9)
NEW_PATH = f"/v1/invoices/{NEW_ID}"
CLIENT = 9102


def line_row(item_id=ITEM_A, **extra):
    """One InvoiceLineItemModel as Gorelo returns it in GET /v1/invoices/{invoiceId}."""
    row = {
        "Id": uid(31),
        "ItemId": item_id,
        "ItemType": {"Id": 1, "Name": "Product"},
        "Name": "Onboarding Pack",
        "Description": None,
        "Quantity": 2.0,
        "UnitCost": 90.0,
        "UnitPrice": 150.0,
        "DiscountPercent": 10.0,
        "Tax": {"Id": 2, "Name": "GST"},
        "Amount": 270.0,
        "TaxAmount": 13.5,
        "BillableStatus": {"Id": 1, "Name": "Billable"},
        "CoaCode": None,
        "SubItems": [],
    }
    row.update(extra)
    return row


def invoice_detail(number=1042, status=(1, "Draft"), invoice_id=NEW_ID, **extra):
    """One InvoiceDetailModel: the list row plus Description, Attachments, LineItems and Payments (2026-10-08)."""
    detail = invoice(number, status, invoice_id)
    detail.update({"Description": None, "Attachments": [], "LineItems": [line_row()], "Payments": []})
    detail.update(extra)
    return detail


def route_create(mock, detail=None, new_id=NEW_ID):
    """POST /v1/invoices answers {Id}, and the GET of that invoice answers `detail`."""
    mock.on("POST", INVOICES, envelope({"Id": new_id}))
    mock.on("GET", f"/v1/invoices/{new_id}", envelope(detail if detail is not None else invoice_detail(invoice_id=new_id)))


GOOD = {"client_id": CLIENT, "line_items": [{"item_id": ITEM_A, "quantity": 2}]}
ATTACHMENT = {"Name": "timesheet-august.pdf", "Url": "https://files.example.test/i/1?sig=abc"}
SUB_ITEM = {"ItemId": ITEM_B, "Name": "Router"}


def pdf_response(content=b"%PDF-1.7 invoice body", disposition='attachment; filename="INV-1042.pdf"', **headers):
    sent = {"content-type": "application/pdf", **headers}
    if disposition:
        sent["content-disposition"] = disposition
    return httpx.Response(200, content=content, headers=sent)


@pytest.fixture
def server(server_factory):
    """Every toolset on and delete tools enabled."""
    return server_factory(destructive=True)


def module_specs():
    return [spec for spec in REGISTRY.specs if spec.fn.__module__ == invoices.__name__]


def tool_def(spec_name, tools):
    return next(tool for tool in tools if tool.name == spec_name)


async def call_directly(client_factory, tool, **kwargs):
    """Call the decorated function itself (no pydantic in front of it): for the checks the schema normally pre-empts."""
    async with client_factory() as client:
        return await tool(make_ctx(client), **kwargs)


# --------------------------------------------------------------------------
# What the module declares
# --------------------------------------------------------------------------

EXPECTED_TOOLS = {
    "list_invoices": ("read", ["GET /v1/invoices"]),
    "get_invoice": ("read", ["GET /v1/invoices/{invoiceId}"]),
    "create_invoice": ("write", ["POST /v1/invoices", "GET /v1/invoices/{invoiceId}", "GET /v1/taxes"]),
    "create_approved_invoice": ("destructive", ["POST /v1/invoices", "GET /v1/invoices/{invoiceId}", "GET /v1/taxes"]),
    "export_invoice_pdf": ("write", ["GET /v1/invoices/{invoiceId}/pdf"]),
    "delete_invoice": ("destructive", ["GET /v1/invoices", "DELETE /v1/invoices/{invoiceId}"]),
    "update_invoice": ("write", ["GET /v1/invoices/{invoiceId}", "PATCH /v1/invoices/{invoiceId}", "GET /v1/taxes"]),
    "approve_invoice": ("destructive", ["GET /v1/invoices/{invoiceId}", "PATCH /v1/invoices/{invoiceId}"]),
    "list_payments": ("read", ["GET /v1/payments"]),
    "get_payment": ("read", ["GET /v1/payments/{paymentId}"]),
}


def test_the_module_declares_exactly_these_tools():
    found = {spec.name: (spec.kind, spec.ops) for spec in module_specs()}
    assert found == EXPECTED_TOOLS
    assert {spec.toolset for spec in module_specs()} == {"billing"}


def test_the_fixtures_are_shaped_like_the_spec(spec_index):
    """The mocked answers carry exactly the fields the spec's response schemas define."""
    assert set(invoice()) == set(spec_index.schema("InvoiceModel")["fields"])
    assert set(invoice()["Status"]) == set(spec_index.schema("CodeModel")["fields"])
    assert set(spec_index.schema("DeleteInvoiceResult")["fields"]) == {"Id", "StatusId"}
    # contract e15cb5a18ec2: the single record and its parts
    assert set(invoice_detail()) == set(spec_index.schema("InvoiceDetailModel")["fields"])
    assert set(invoice_detail()) == set(invoice()) | {"Description", "Attachments", "LineItems", "Payments"}  # "the fields of the list row plus"
    assert set(line_row()) == set(spec_index.schema("InvoiceLineItemModel")["fields"])
    code = set(spec_index.schema("CodeModel")["fields"])
    assert set(line_row()["ItemType"]) == set(line_row()["Tax"]) == set(line_row()["BillableStatus"]) == code
    assert set(ATTACHMENT) == set(spec_index.schema("InvoiceAttachmentModel")["fields"])
    assert set(SUB_ITEM) == set(spec_index.schema("InvoiceLineSubItemModel")["fields"])
    assert set(spec_index.schema("CreateInvoiceResult")["fields"]) == {"Id"}


def test_the_module_docstring_names_every_op_the_tools_use():
    doc = invoices.__doc__
    for _kind, ops in EXPECTED_TOOLS.values():
        for op in ops:
            assert op in doc, op


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


def test_every_param_is_described_and_the_text_stays_within_the_concision_limits():
    for spec in module_specs():
        doc = inspect.getdoc(spec.fn)
        assert 40 < len(doc) <= 900, spec.name
        schema = Tool.from_function(spec.fn).parameters
        for name, definition in schema["properties"].items():
            text = definition.get("description")
            assert text, f"{spec.name}.{name} needs a description"
            assert len(text) <= 160, f"{spec.name}.{name} description is {len(text)} characters"
    assert "Side effects:" in inspect.getdoc(invoices.export_invoice_pdf)  # the one tool with a side effect to name


async def test_the_invoice_catalog_and_uptime_tools_stay_within_the_package_size_budget(server):
    # claude.ai pays for these bytes in every conversation: growing them needs a deliberate bump of the budget
    names = {s.name for s in REGISTRY.specs if s.fn.__module__.split(".")[-1] in ("invoices", "catalog", "uptime")}
    tools = [t for t in await list_tools(server) if t.name in names]
    size = sum(len(json.dumps(t.model_dump(mode="json", exclude_none=True))) for t in tools)
    assert len(names) == 23 and len(tools) == 23  # 16 until contract e15cb5a18ec2 added three invoice tools, 19 until 1.5.0 (4 more)
    assert size <= INVOICES_MODULE_BUDGET_BYTES, size


def test_the_docstrings_state_the_facts_the_model_must_know():
    export = " ".join(inspect.getdoc(invoices.export_invoice_pdf).split())
    assert "5 MB cap" in export and "recorded on the invoice as an export event" in export
    assert "never call it speculatively or in a loop" in export
    assert "after a timeout or failure it may already be recorded and a retry records another" in export
    delete = " ".join(inspect.getdoc(invoices.delete_invoice).split())
    assert "only on the user's request" in delete and "must be the only match" in delete
    assert "have status expected_status, else nothing changes" in delete
    # what Gorelo's answer says and no more (the 2026-10-02 text says only Deleted, not that it is unrecoverable)
    assert "Draft: deleted (no longer listed)" in delete and "Approved: voided (status Void, still listed)" in delete
    # contract e15cb5a18ec2: the answer carries the status after the call, and the result says which one it was
    assert "The result's StatusId is Gorelo's answer: 6 Deleted or 4 Void" in delete
    # previous_status is read from the answer as well
    assert "previous_status is read from it too (6 was a Draft, 4 was Approved)" in delete
    # Void is refused by this tool's own gate; Gorelo would call deleting a void invoice a success
    assert "Void is refused here (Gorelo itself treats deleting a void invoice as success)" in delete
    assert "Paid, Submitted and others are refused by Gorelo with 409" in delete
    # a void happens in Gorelo ONLY, the copy in Xero stays open, so the model
    # is told before it asks the user; and a Void invoice kept its AmountDue
    assert (
        "A void happens in Gorelo ONLY: the copy already pushed to the accounting system stays open (observed with Xero), so say so when you ask the user, who must void it there too."
    ) in delete
    assert "A Void invoice can still show AmountDue: read Status first." in delete
    assert delete.index("A void happens in Gorelo ONLY") < delete.index("Ask the user first; needs confirm=true.")
    assert "Ask the user first; needs confirm=true." in delete  # the consistency rule of every destructive tool
    listing = " ".join(inspect.getdoc(invoices.list_invoices).split())
    assert "filter by number, client, status or dates" in listing
    assert "Without status_ids every status is listed; deleted invoices never are" in listing
    assert "the GUID get_invoice and export_invoice_pdf need" in listing and "only get_invoice returns the lines" in listing
    assert "no get-by-id" not in listing and "only way to find" not in listing  # get_invoice exists since 2026-10-02
    # a Void invoice can keep its AmountDue, so every tool that shows an invoice says to read Status
    assert "A Void invoice can still show AmountDue: read Status first." in listing


async def test_the_parameter_descriptions_carry_the_id_sources_and_the_confirm_rule(server):
    tools = await list_tools(server)

    def text(tool, param):
        return tool_def(tool, tools).inputSchema["properties"][param]["description"]

    assert "list_clients" in text("list_invoices", "client_ids") and "list_contracts" in text("list_invoices", "contract_ids")
    assert "1 Draft, 2 Submitted, 3 Paid, 4 Void, 5 Approved" in text("list_invoices", "status_ids")
    assert text("list_invoices", "status_ids") == (
        "1 Draft, 2 Submitted, 3 Paid, 4 Void, 5 Approved. Omitted: every status; deleted invoices are never listed."
    )
    assert "YYYY-MM-DD, inclusive" in text("list_invoices", "invoice_date_since")
    assert "YYYY-MM-DD, exclusive" in text("list_invoices", "due_date_before")
    assert "same filters" in text("list_invoices", "cursor")
    # the paging stop condition is said where the cursor is
    assert text("list_invoices", "cursor") == (
        "next_cursor from the previous call; same filters and sort; repeat until has_more is false."
    )
    assert "list_invoices" in text("export_invoice_pdf", "invoice_id")
    assert "Must be true" in text("delete_invoice", "confirm") and "user approves" in text("delete_invoice", "confirm")
    # where the number comes from, and what the expected status means and does
    number = text("delete_invoice", "invoice_number")
    assert "list_invoices row" in number and "1042 for INV-1042" in number and "not DisplayNumber" in number
    status = text("delete_invoice", "expected_status")
    assert "That row's current Status" in status and "if it differs nothing changes" in status
    assert status == (
        "That row's current Status; if it differs nothing changes. "
        "Draft: deleted (no longer listed). Approved: voided (status Void, still listed)."
    )


async def test_the_annotations_tell_a_client_what_each_tool_does(server):
    tools = await list_tools(server)
    listing = tool_def("list_invoices", tools).annotations
    assert (listing.readOnlyHint, listing.destructiveHint, listing.idempotentHint) == (True, False, True)
    export = tool_def("export_invoice_pdf", tools).annotations
    assert (export.readOnlyHint, export.destructiveHint) == (False, False)  # a write, but it destroys nothing
    delete = tool_def("delete_invoice", tools).annotations
    assert (delete.readOnlyHint, delete.destructiveHint) == (False, True)
    confirm = tool_def("delete_invoice", tools).inputSchema["properties"]["confirm"]
    assert confirm["type"] == "boolean" and confirm["default"] is False
    assert "confirm" not in tool_def("delete_invoice", tools).inputSchema["required"]
    # the new tools: reading is read-only, a Draft is a plain write that destroys nothing, approving is the gated one
    detail = tool_def("get_invoice", tools).annotations
    assert (detail.readOnlyHint, detail.destructiveHint, detail.idempotentHint) == (True, False, True)
    draft = tool_def("create_invoice", tools).annotations
    assert (draft.readOnlyHint, draft.destructiveHint, draft.idempotentHint) == (False, False, False)
    approved = tool_def("create_approved_invoice", tools)
    assert (approved.annotations.readOnlyHint, approved.annotations.destructiveHint) == (False, True)
    gate = approved.inputSchema["properties"]["confirm"]
    assert gate["type"] == "boolean" and gate["default"] is False and "confirm" not in approved.inputSchema["required"]


async def test_export_invoice_pdf_is_a_write_and_its_docstring_says_why(server):
    tools = await list_tools(server)
    export = tool_def("export_invoice_pdf", tools)
    assert "export event" in export.description and "5 MB" in export.description
    assert export.annotations.readOnlyHint is False  # a GET that Gorelo records is not read-only
    assert export.outputSchema is None  # a ToolResult: summary text plus an embedded file


# --------------------------------------------------------------------------
# list_invoices
# --------------------------------------------------------------------------


async def test_list_invoices_without_filters_sends_only_the_page_size(server, mock_gorelo):
    rows = [invoice(1043, (5, "Approved"), uid(6)), invoice(1042)]
    mock_gorelo.on("GET", INVOICES, paged_envelope(rows, total_count=2))
    result = await call_tool(server, "list_invoices")
    request = mock_gorelo.last
    assert (request.method, request.path) == ("GET", INVOICES)
    assert request.query == {"PageSize": "50"} and request.json is None
    assert result == {
        "items": rows,
        "count": 2,
        "total_count": 2,
        "has_more": False,
        "next_cursor": None,
        "page_size": 50,
        "filters": {},
    }
    assert len(mock_gorelo.requests) == 1


async def test_every_filter_goes_out_under_its_spec_name(server, mock_gorelo):
    mock_gorelo.on("GET", INVOICES, paged_envelope([invoice()]))
    args = {
        "client_ids": [9102, 9101],
        "status_ids": [1, 5],
        "contract_ids": [11],
        "number": 1042,
        "invoice_date_since": "2026-09-01",
        "invoice_date_before": "2026-10-01",
        "due_date_since": "2026-09-15",
        "due_date_before": "2026-10-15",
        "is_email_sent": False,
        "created_since": "2026-09-01T08:00:00Z",
        "created_before": "2026-09-02T08:00:00+02:00",  # instants keep an offset and are converted to UTC
        "updated_since": "2026-09-03T08:00:00Z",
        "updated_before": "2026-09-04T08:00:00Z",
        "query": "monthly",
        "sort_by": "dueDate",
        "sort_order": "asc",
        "page_size": 25,
        "cursor": "opaque-cursor-1",
    }
    result = await call_tool(server, "list_invoices", args)
    assert mock_gorelo.last.query == {
        "ClientIds": "9102,9101",
        "StatusIds": "1,5",
        "ContractIds": "11",
        "Number": "1042",
        "InvoiceDateSince": "2026-09-01T00:00:00Z",  # a calendar date goes out as midnight UTC
        "InvoiceDateBefore": "2026-10-01T00:00:00Z",
        "DueDateSince": "2026-09-15T00:00:00Z",
        "DueDateBefore": "2026-10-15T00:00:00Z",
        "IsEmailSent": "false",
        "CreatedSince": "2026-09-01T08:00:00Z",
        "CreatedBefore": "2026-09-02T06:00:00Z",
        "UpdatedSince": "2026-09-03T08:00:00Z",
        "UpdatedBefore": "2026-09-04T08:00:00Z",
        "Query": "monthly",
        "SortBy": "dueDate",
        "SortOrder": "asc",
        "PageSize": "25",
        "Cursor": "opaque-cursor-1",
    }
    # the result echoes the filters in the tool's own terms (dates as dates), so the next call can repeat them
    assert result["filters"] == {
        "client_ids": [9102, 9101],
        "status_ids": [1, 5],
        "contract_ids": [11],
        "number": 1042,
        "invoice_date_since": "2026-09-01",
        "invoice_date_before": "2026-10-01",
        "due_date_since": "2026-09-15",
        "due_date_before": "2026-10-15",
        "is_email_sent": False,
        "created_since": "2026-09-01T08:00:00Z",
        "created_before": "2026-09-02T06:00:00Z",
        "updated_since": "2026-09-03T08:00:00Z",
        "updated_before": "2026-09-04T08:00:00Z",
        "query": "monthly",
        "sort_by": "dueDate",
        "sort_order": "asc",
    }
    assert result["page_size"] == 25


async def test_is_email_sent_true_is_sent_as_true(server, mock_gorelo):
    mock_gorelo.on("GET", INVOICES, paged_envelope([]))
    await call_tool(server, "list_invoices", {"is_email_sent": True})
    assert mock_gorelo.last.query == {"IsEmailSent": "true", "PageSize": "50"}


@pytest.mark.parametrize(
    "ids, wire",
    [([1], "1"), ([2], "2"), ([3], "3"), ([4], "4"), ([5], "5"), ([1, 2, 3, 4, 5], "1,2,3,4,5")],
)
async def test_list_invoices_accepts_every_status_of_the_published_scale(server, mock_gorelo, ids, wire):
    mock_gorelo.on("GET", INVOICES, paged_envelope([]))
    result = await call_tool(server, "list_invoices", {"status_ids": ids})
    assert mock_gorelo.last.query == {"StatusIds": wire, "PageSize": "50"}
    assert result["filters"] == {"status_ids": ids}


async def test_list_invoices_direct_call_answers_a_number_outside_the_scale_with_the_scale_and_keeps_the_id_message_for_the_rest(
    client_factory, mock_gorelo
):
    for bad in (0, -1, 6, MAX_ID):  # a number outside the scale is answered with the scale, quoted back
        with pytest.raises(ToolError) as info:
            await call_directly(client_factory, invoices.list_invoices, status_ids=[bad])
        assert str(info.value) == f"status_ids[0]: expected 1 Draft, 2 Submitted, 3 Paid, 4 Void, 5 Approved, got {bad}"
    for bad, kind in ((True, "a boolean"), (5.0, "a decimal number"), ("2", "a string")):
        with pytest.raises(ToolError) as info:  # what is not a whole number keeps the positive_id message
            await call_directly(client_factory, invoices.list_invoices, status_ids=[bad])
        assert str(info.value) == f"status_ids[0]: expected a positive whole number such as 123, got {kind}"
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("given, used", [(0, 1), (-5, 1), (1, 1), (50, 50), (200, 200), (201, 200), (5000, 200)])
async def test_page_size_is_clamped_to_1_200_and_the_size_used_is_reported(server, mock_gorelo, given, used):
    mock_gorelo.on("GET", INVOICES, paged_envelope([]))
    result = await call_tool(server, "list_invoices", {"page_size": given})
    assert mock_gorelo.last.query["PageSize"] == str(used) and result["page_size"] == used


async def test_paging_follows_next_cursor_with_the_same_filters(server, mock_gorelo):
    first = [invoice(1043, invoice_id=uid(6))]
    second = [invoice(1042)]
    mock_gorelo.on(
        "GET",
        INVOICES,
        in_order(
            envelope(first, pagination("cursor-2", 2)),
            envelope(second, pagination(None, 2, has_more=False)),
        ),
    )
    filters = {"status_ids": [1], "page_size": 1}
    one = await call_tool(server, "list_invoices", filters)
    assert (one["has_more"], one["next_cursor"], one["total_count"], one["count"]) == (True, "cursor-2", 2, 1)
    two = await call_tool(server, "list_invoices", {**filters, "cursor": one["next_cursor"]})
    assert (two["has_more"], two["next_cursor"], two["count"]) == (False, None, 1)
    first_query, second_query = (r.query for r in mock_gorelo.requests)
    assert first_query == {"StatusIds": "1", "PageSize": "1"}
    assert second_query == {"StatusIds": "1", "PageSize": "1", "Cursor": "cursor-2"}
    assert one["filters"] == two["filters"] == {"status_ids": [1]}


async def test_an_empty_page_is_reported_as_one_with_its_total(server, mock_gorelo):
    mock_gorelo.on("GET", INVOICES, envelope([], pagination(None, 0, has_more=False)))
    result = await call_tool(server, "list_invoices", {"number": 99999})
    assert result["items"] == [] and result["count"] == 0 and result["total_count"] == 0 and result["has_more"] is False


async def test_list_invoices_refuses_an_answer_that_is_not_a_list(server, mock_gorelo):
    mock_gorelo.on("GET", INVOICES, envelope({"Id": INVOICE_ID}, pagination(None, 1, has_more=False)))
    text = await call_tool_error(server, "list_invoices")
    assert text.startswith("Gorelo returned an unexpected response for list_invoices") and "expected Data to be a list" in text


async def test_list_invoices_a_gorelo_400_names_the_snake_case_param(server, mock_gorelo):
    body = error_envelope(400, [("070101", "A value that does not parse as a whole number.", "Number")])
    mock_gorelo.on("GET", INVOICES, body)
    text = await call_tool_error(server, "list_invoices", {"number": 1042})
    assert text == (
        "Gorelo rejected list_invoices (HTTP 400, code 070101): number: A value that does not parse as a whole "
        f"number. [trace {TEST_TRACE_ID}]"
    )


@pytest.mark.parametrize(
    "property_name, param",
    [("StatusIds", "status_ids"), ("ClientIds", "client_ids"), ("SortBy", "sort_by"), ("Cursor", "cursor"),
     ("InvoiceDateSince", "invoice_date_since"), ("IsEmailSent", "is_email_sent"), ("PageSize", "page_size")],
)
async def test_other_gorelo_property_names_map_to_their_params_too(server, mock_gorelo, property_name, param):
    mock_gorelo.on("GET", INVOICES, error_envelope(400, [("070101", "rejected", property_name)]))
    text = await call_tool_error(server, "list_invoices")
    assert f"HTTP 400, code 070101): {param}: rejected" in text


async def test_list_invoices_a_missing_scope_reads_as_a_missing_scope(server, mock_gorelo):
    mock_gorelo.on("GET", INVOICES, error_envelope(403, [("080203", "API key does not have 'Billing' scope")]))
    text = await call_tool_error(server, "list_invoices")
    assert "the API key does not have the 'Billing' scope" in text


DAY_ERROR = "expected a calendar date as YYYY-MM-DD, for example 2026-09-01 (no time, no offset)"

LOCAL_LIST_ERRORS = [
    pytest.param(
        {"client_ids": []},
        "client_ids: expected at least one id, got an empty list (omit client_ids if you have no ids to give)",
        id="empty-client-ids",
    ),
    pytest.param({"contract_ids": []}, "contract_ids: expected at least one id, got an empty list", id="empty-contract-ids"),
    pytest.param({"status_ids": []}, "status_ids: expected at least one id, got an empty list", id="empty-status-ids"),
    pytest.param(
        {"status_ids": [1, 6]},
        "status_ids[1]: expected 1 Draft, 2 Submitted, 3 Paid, 4 Void, 5 Approved, got 6",
        id="status-id-6-is-not-listable",
    ),
    pytest.param(
        {"status_ids": [0]},
        "status_ids[0]: expected 1 Draft, 2 Submitted, 3 Paid, 4 Void, 5 Approved, got 0",
        id="status-id-zero",
    ),
    pytest.param(
        {"status_ids": [-3, 1]},
        "status_ids[0]: expected 1 Draft, 2 Submitted, 3 Paid, 4 Void, 5 Approved, got -3",
        id="status-id-negative",
    ),
    pytest.param(
        {"status_ids": [2**63]},
        "status_ids[0]: expected a positive whole number such as 123, got a number above 9223372036854775807",
        id="status-id-beyond-int64",
    ),
    pytest.param({"client_ids": [5, 0]}, "client_ids[1]: expected a positive whole number such as 123, got zero or a negative number", id="client-id-zero"),
    pytest.param({"contract_ids": [-1]}, "contract_ids[0]: expected a positive whole number such as 123, got zero or a negative number", id="contract-id-negative"),
    pytest.param({"client_ids": [2**63]}, "client_ids[0]: expected a positive whole number such as 123, got a number above 9223372036854775807", id="client-id-beyond-int64"),
    pytest.param({"number": 0}, "number: expected a positive whole number such as 123, got zero or a negative number", id="number-zero"),
    pytest.param({"number": -4}, "number: expected a positive whole number such as 123", id="number-negative"),
    pytest.param({"invoice_date_since": "2026-09-01T00:00:00"}, f"invoice_date_since: {DAY_ERROR}", id="naive-invoice-date-since"),
    pytest.param({"invoice_date_since": "2026-09-01T00:00:00Z"}, f"invoice_date_since: {DAY_ERROR}", id="datetime-invoice-date-since"),
    pytest.param({"invoice_date_before": "2026-9-1"}, f"invoice_date_before: {DAY_ERROR}", id="unpadded-invoice-date-before"),
    pytest.param({"invoice_date_before": "20260901"}, f"invoice_date_before: {DAY_ERROR}", id="compact-invoice-date-before"),
    pytest.param({"due_date_since": "next friday"}, f"due_date_since: {DAY_ERROR}", id="garbage-due-date-since"),
    pytest.param({"due_date_since": "2026-02-30"}, f"due_date_since: {DAY_ERROR}", id="a-day-that-does-not-exist"),
    pytest.param({"due_date_before": "2026-13-01"}, f"due_date_before: {DAY_ERROR}", id="month-13"),
    pytest.param({"due_date_before": "2026-09-30 "}, f"due_date_before: {DAY_ERROR}", id="trailing-space"),
    pytest.param({"due_date_before": ""}, f"due_date_before: {DAY_ERROR}", id="blank-due-date-before"),
    pytest.param({"created_since": "yesterday"}, "created_since: 'yesterday' is not an ISO 8601 datetime", id="garbage-created-since"),
    pytest.param({"created_since": "2026-09-01"}, "created_since: '2026-09-01' has no UTC offset", id="created-since-stays-an-instant"),
    pytest.param({"created_before": "2026-09-30T10:00:00"}, "created_before: ", id="naive-created-before"),
    pytest.param({"updated_since": "2026-09-30T10:00:00"}, "updated_since: ", id="naive-updated-since"),
    pytest.param({"updated_before": "2026-09-30T10:00:00"}, "updated_before: ", id="naive-updated-before"),
    pytest.param({"query": "   "}, "query: must not be empty or whitespace only", id="blank-query"),
    pytest.param({"cursor": ""}, "cursor: must not be empty or whitespace only", id="blank-cursor"),
]


@pytest.mark.parametrize("args, fragment", LOCAL_LIST_ERRORS)
async def test_list_invoices_local_validation_errors_name_the_param_and_send_nothing(server, mock_gorelo, args, fragment):
    text = await call_tool_error(server, "list_invoices", args)
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "args, param",
    [
        ({"sort_by": "name"}, "sort_by"),
        ({"sort_order": "up"}, "sort_order"),
        ({"number": "INV-1042"}, "number"),
        ({"client_ids": ["abc"]}, "client_ids"),
        ({"is_email_sent": "maybe"}, "is_email_sent"),
    ],
)
async def test_a_value_outside_the_schema_is_refused_with_the_param_name(server, mock_gorelo, args, param):
    text = await call_tool_error(server, "list_invoices", args)
    assert param in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("param", ["client_ids", "status_ids", "contract_ids"])
@pytest.mark.parametrize("bad", [True, "5", 5.0], ids=["json-true", "text-5", "decimal-5"])
async def test_an_id_list_is_strict_true_and_text_are_refused_instead_of_becoming_an_id(server, mock_gorelo, param, bad):
    text = await call_tool_error(server, "list_invoices", {param: [bad]})
    assert param in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", [True, "1042", 1042.0], ids=["json-true", "text-1042", "decimal-1042"])
async def test_the_invoice_number_is_a_strict_integer_too(server, mock_gorelo, bad):
    assert "number" in await call_tool_error(server, "list_invoices", {"number": bad})
    args = {"invoice_number": bad, "expected_status": "Draft", "confirm": True}
    assert "invoice_number" in await call_tool_error(server, "delete_invoice", args)
    assert mock_gorelo.requests == []


async def test_a_calendar_date_is_accepted_as_is_and_sent_as_midnight_utc(server, mock_gorelo):
    mock_gorelo.on("GET", INVOICES, paged_envelope([]))
    result = await call_tool(server, "list_invoices", {"invoice_date_before": "2026-09-01", "due_date_since": "2024-02-29"})
    assert mock_gorelo.last.query == {
        "InvoiceDateBefore": "2026-09-01T00:00:00Z",
        "DueDateSince": "2024-02-29T00:00:00Z",  # a leap day is a real day
        "PageSize": "50",
    }
    assert result["filters"] == {"invoice_date_before": "2026-09-01", "due_date_since": "2024-02-29"}


async def test_the_echoed_filters_can_be_sent_back_unchanged_for_the_next_page(server, mock_gorelo):
    mock_gorelo.on("GET", INVOICES, paged_envelope([]))
    first = await call_tool(server, "list_invoices", {"invoice_date_since": "2026-09-01", "created_since": "2026-09-01T08:00:00+02:00"})
    again = await call_tool(server, "list_invoices", {**first["filters"], "cursor": "c-2"})
    assert again["filters"] == first["filters"]
    assert mock_gorelo.requests[1].query["InvoiceDateSince"] == "2026-09-01T00:00:00Z"


async def test_sort_by_accepts_every_documented_column(server, mock_gorelo):
    mock_gorelo.on("GET", INVOICES, paged_envelope([]))
    for column in ("createdOn", "updatedOn", "date", "dueDate", "totalAmount"):
        await call_tool(server, "list_invoices", {"sort_by": column})
        assert mock_gorelo.last.query["SortBy"] == column


# --------------------------------------------------------------------------
# export_invoice_pdf
# --------------------------------------------------------------------------


async def test_export_returns_a_summary_and_the_pdf_as_an_embedded_file(server, mock_gorelo):
    content = b"%PDF-1.7 hello invoice"
    mock_gorelo.on("GET", PDF_PATH, pdf_response(content))
    result = await call_tool_raw(server, "export_invoice_pdf", {"invoice_id": INVOICE_ID})
    assert not result.is_error and result.structured_content is None
    summary, document = result.content
    assert summary.type == "text"
    assert summary.text == (
        "Exported invoice PDF INV-1042.pdf (22 bytes), attached as an embedded file. "
        "Gorelo recorded this download on the invoice as an export event."
    )
    assert document.type == "resource"
    assert document.resource.mimeType == "application/pdf"
    assert str(document.resource.uri) == "file:///INV-1042.pdf"
    assert base64.b64decode(document.resource.blob) == content
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", PDF_PATH, {}, None)
    assert request.headers["accept"] == "application/pdf, application/json"
    assert len(mock_gorelo.requests) == 1


async def test_the_invoice_id_is_sent_in_canonical_form(server, mock_gorelo):
    mock_gorelo.on("GET", PDF_PATH, pdf_response())
    await call_tool_raw(server, "export_invoice_pdf", {"invoice_id": INVOICE_ID.upper().replace("-", "")})
    assert mock_gorelo.last.path == PDF_PATH


@pytest.mark.parametrize(
    "disposition, shown, uri",
    [
        pytest.param('attachment; filename="INV-1042.pdf"', "INV-1042.pdf", "file:///INV-1042.pdf", id="plain"),
        pytest.param('attachment; filename="INV 1042 (final).PDF"', "INV 1042 (final).PDF", "file:///INV_1042_final.pdf", id="spaces-and-brackets"),
        pytest.param("attachment; filename*=UTF-8''caf%C3%A9-7.pdf", "caf\u00e9-7.pdf", "file:///caf_-7.pdf", id="non-ascii"),
        pytest.param('attachment; filename="a#b?c%d.pdf"', "a#b?c%d.pdf", "file:///a_b_c_d.pdf", id="uri-special-characters"),
        pytest.param('attachment; filename="../../etc/passwd"', "passwd", "file:///passwd.pdf", id="path-parts-dropped"),
        pytest.param('attachment; filename="..pdf"', "..pdf", f"file:///invoice-{INVOICE_ID}.pdf", id="nothing-usable-left"),
        pytest.param('attachment; filename="INV-0007"', "INV-0007", "file:///INV-0007.pdf", id="no-extension"),
        pytest.param("", f"invoice-{INVOICE_ID}.pdf", f"file:///invoice-{INVOICE_ID}.pdf", id="no-content-disposition"),
        pytest.param("inline", f"invoice-{INVOICE_ID}.pdf", f"file:///invoice-{INVOICE_ID}.pdf", id="inline-without-a-name"),
    ],
)
async def test_the_file_name_is_made_safe_for_the_embedded_resource(server, mock_gorelo, disposition, shown, uri):
    mock_gorelo.on("GET", PDF_PATH, pdf_response(b"%PDF-1.7 x", disposition=disposition))
    result = await call_tool_raw(server, "export_invoice_pdf", {"invoice_id": INVOICE_ID})
    assert not result.is_error
    summary, document = result.content
    assert f"Exported invoice PDF {shown} (" in summary.text
    assert str(document.resource.uri) == uri


async def test_a_pdf_of_exactly_the_cap_is_returned(server, mock_gorelo):
    mock_gorelo.on("GET", PDF_PATH, pdf_response(b"%" * MB5))
    result = await call_tool_raw(server, "export_invoice_pdf", {"invoice_id": INVOICE_ID})
    assert not result.is_error
    assert f"({MB5:,} bytes)" in result.content[0].text
    assert len(base64.b64decode(result.content[1].resource.blob)) == MB5


async def test_a_pdf_one_byte_over_the_cap_is_refused_and_the_export_note_is_given(server, mock_gorelo):
    mock_gorelo.on("GET", PDF_PATH, pdf_response(b"%" * (MB5 + 1)))
    text = await call_tool_error(server, "export_invoice_pdf", {"invoice_id": INVOICE_ID})
    assert text.startswith("Gorelo returned an unexpected response for export_invoice_pdf")
    assert str(MB5) in text and "larger than" in text
    assert EXPORT_NOTE in text and "Retry only if one more export event is acceptable." in text
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(200, text="<html>gateway</html>", headers={"content-type": "text/html"}), id="html"),
        pytest.param(httpx.Response(200, content=b"%PDF-1.7 x", headers={"content-type": "application/octet-stream"}), id="octet-stream"),
        pytest.param(httpx.Response(200, json=envelope({"Surprise": True})), id="json-envelope"),
        pytest.param(httpx.Response(200, content=b"", headers={"content-type": "application/pdf"}), id="empty-pdf"),
        pytest.param(httpx.Response(502, text="<html>Bad Gateway</html>"), id="502-gateway-page"),
    ],
)
async def test_an_answer_that_is_not_a_pdf_is_refused_and_says_the_export_may_be_recorded(server, mock_gorelo, response):
    mock_gorelo.on("GET", PDF_PATH, response)
    text = await call_tool_error(server, "export_invoice_pdf", {"invoice_id": INVOICE_ID})
    assert text.startswith("Gorelo returned an unexpected response for export_invoice_pdf")
    assert EXPORT_NOTE in text
    assert len(mock_gorelo.requests) == 1  # never retried


async def test_a_gorelo_404_names_the_snake_case_param(server, mock_gorelo):
    body = error_envelope(404, [("070401", "Invoice not found.", "invoiceId")])
    mock_gorelo.on("GET", PDF_PATH, body)
    text = await call_tool_error(server, "export_invoice_pdf", {"invoice_id": INVOICE_ID})
    assert text == (
        "Gorelo rejected export_invoice_pdf (HTTP 404, code 070401): invoice_id: Invoice not found. "
        f"[trace {TEST_TRACE_ID}]"
    )


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")])
async def test_a_timeout_or_lost_connection_is_not_safe_to_retry(server, mock_gorelo, failure):
    mock_gorelo.on("GET", PDF_PATH, failure)
    text = await call_tool_error(server, "export_invoice_pdf", {"invoice_id": INVOICE_ID})
    assert text.startswith("Gorelo did not confirm export_invoice_pdf")
    assert EXPORT_NOTE in text and "Retry only if one more export event is acceptable." in text
    assert "retrying is safe" not in text
    assert len(mock_gorelo.requests) == 1


async def test_a_server_error_may_already_be_recorded(server, mock_gorelo):
    mock_gorelo.on("GET", PDF_PATH, error_envelope(500, [("070001", "Rendering failed")]))
    text = await call_tool_error(server, "export_invoice_pdf", {"invoice_id": INVOICE_ID})
    assert "Gorelo rejected export_invoice_pdf (HTTP 500, code 070001): Rendering failed" in text
    assert EXPORT_NOTE in text and len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "bad",
    [
        "INV-1042", "1042", "", "   ", "../../assets/agents", f"{INVOICE_ID}/pdf",
        f" {INVOICE_ID} ", "{" + INVOICE_ID + "}", f"urn:uuid:{INVOICE_ID}",  # nothing is trimmed or guessed
    ],
)
async def test_the_invoice_id_must_be_a_guid_and_the_error_names_invoice_id(server, mock_gorelo, bad):
    text = await call_tool_error(server, "export_invoice_pdf", {"invoice_id": bad})
    assert text.startswith("invoice_id: expected a GUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b, got ")
    assert "invoiceId" not in text
    assert mock_gorelo.requests == []


async def test_a_blank_or_wrongly_typed_invoice_id_says_what_it_got_without_quoting_it(server, mock_gorelo):
    text = await call_tool_error(server, "export_invoice_pdf", {"invoice_id": ""})
    assert text == "invoice_id: expected a GUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b, got an empty string"
    text = await call_tool_error(server, "export_invoice_pdf", {"invoice_id": "INV-1042"})
    assert text.endswith("got text that is not a GUID") and "INV-1042" not in text
    assert "invoice_id" in await call_tool_error(server, "export_invoice_pdf", {"invoice_id": 1042})
    assert mock_gorelo.requests == []


async def test_the_guid_is_accepted_in_any_case_and_without_hyphens(server, mock_gorelo):
    mock_gorelo.on("GET", PDF_PATH, pdf_response())
    for given in (INVOICE_ID.upper(), INVOICE_ID.replace("-", "")):
        result = await call_tool_raw(server, "export_invoice_pdf", {"invoice_id": given})
        assert not result.is_error and mock_gorelo.last.path == PDF_PATH


# --------------------------------------------------------------------------
# delete_invoice
# --------------------------------------------------------------------------


def lookup(mock, rows, number=1042, **page):
    mock.on("GET", INVOICES, paged_envelope(rows, **page), query={"Number": str(number)})


# What a refusal without confirm says would happen, by the status the caller expects. An Approved invoice has been pushed
# to the accounting system and the void is Gorelo's only (the Xero copy stays open after a void), so
# its refusal says that and a Draft's does not: a Draft was never pushed.
DRAFT_DELETE_EFFECT = (
    "A Draft invoice is deleted (no longer listed); an Approved invoice is voided (status Void, still listed). "
    "Nothing has been sent to Gorelo."
)
APPROVED_DELETE_EFFECT = (
    "An Approved invoice is voided (status Void, still listed) in Gorelo ONLY: the copy already pushed to the "
    "accounting system stays open (observed with Xero), so the user must void it there too. "
    "Nothing has been sent to Gorelo."
)


async def test_delete_refuses_without_confirm_and_makes_no_http_call(server, mock_gorelo):
    for args in (
        {"invoice_number": 1042, "expected_status": "Draft"},
        {"invoice_number": 1042, "expected_status": "Approved", "confirm": False},
    ):
        text = await call_tool_error(server, "delete_invoice", args)
        assert text.startswith(
            f"confirm: refusing to delete or void invoice number 1042 (expected status {args['expected_status']}) "
            "without confirm=true."
        )
        expected = APPROVED_DELETE_EFFECT if args["expected_status"] == "Approved" else DRAFT_DELETE_EFFECT
        assert expected in text
        assert f"Call again with confirm=true if you really want to delete or void invoice number 1042" in text
    assert mock_gorelo.requests == []


async def test_the_refusal_for_an_approved_invoice_says_the_void_is_in_gorelo_only_and_the_one_for_a_draft_does_not(
    server, mock_gorelo
):
    approved = await call_tool_error(server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Approved"})
    draft = await call_tool_error(server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft"})
    assert invoices.DELETE_EFFECT == {"Draft": DRAFT_DELETE_EFFECT, "Approved": APPROVED_DELETE_EFFECT}
    assert set(invoices.DELETE_EFFECT) == set(invoices.REMOVABLE_STATUS_IDS)  # a status Gorelo removes needs its own effect text
    # said before the user is asked to confirm: voiding does not reach the copy in the accounting system
    assert "in Gorelo ONLY" in approved and "stays open" in approved and "the user must void it there too" in approved
    assert "observed with Xero" in approved
    assert "ONLY" not in draft and "accounting system" not in draft  # a Draft was never pushed
    assert mock_gorelo.requests == []  # both are refusals: nothing was sent


async def test_a_draft_invoice_is_found_by_number_and_deleted_by_its_id(server, mock_gorelo):
    lookup(mock_gorelo, [invoice(1042, (1, "Draft"))])
    # contract e15cb5a18ec2: a deleted Draft is answered with StatusId 6 (Deleted), a status that exists only here
    mock_gorelo.on("DELETE", DELETE_PATH, envelope({"Id": INVOICE_ID, "StatusId": 6}))
    result = await call_tool(
        server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True}
    )
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", INVOICES), ("DELETE", DELETE_PATH)]
    first, second = mock_gorelo.requests
    assert first.query == {"Number": "1042", "PageSize": "10"}  # nothing but the number: no status or client filter
    assert second.query == {} and second.json is None
    assert result == {
        "Id": INVOICE_ID,
        "StatusId": 6,
        "invoice_number": 1042,
        "previous_status": "Draft",
        "outcome": "deleted (StatusId 6 Deleted, no longer listed)",
    }


async def test_an_approved_invoice_is_voided(server, mock_gorelo):
    lookup(mock_gorelo, [invoice(1042, (5, "Approved"))])
    mock_gorelo.on("DELETE", DELETE_PATH, envelope({"Id": INVOICE_ID, "StatusId": 4}))  # 4 (Void): it stays listed
    result = await call_tool(
        server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Approved", "confirm": True}
    )
    assert mock_gorelo.calls("DELETE")[0].path == DELETE_PATH
    assert result == {
        "Id": INVOICE_ID,
        "StatusId": 4,
        "invoice_number": 1042,
        "previous_status": "Approved",
        "outcome": "voided (status Void, still listed)",
    }


async def test_a_draft_that_gorelo_reports_as_void_afterwards_was_approved_and_is_not_called_deleted(server, mock_gorelo):
    # the status can change between the lookup and the delete: say what Gorelo reports, not what was expected. StatusId 4
    # (Void) is only the answer for an Approved invoice, so an invoice that was looked up as a Draft was approved in the
    # meantime. previous_status follows the answer (Approved), not expected_status (Draft), and the note says that an
    # Approved invoice has already been pushed to the connected accounting system
    lookup(mock_gorelo, [invoice(1042, (1, "Draft"))])
    mock_gorelo.on("DELETE", DELETE_PATH, envelope({"Id": INVOICE_ID, "StatusId": 4}))
    result = await call_tool(
        server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True}
    )
    assert result["outcome"] == "voided (status Void, still listed)" and result["StatusId"] == 4
    assert result["previous_status"] == "Approved"  # not "Draft", which is only what the caller expected
    # the caller was told this was a Draft, so this note is where it learns that the void did not reach the copy in the
    # accounting system (the void is in Gorelo ONLY, observed with Xero)
    assert result["note"] == (
        "expected_status was Draft, so Gorelo should have answered StatusId 6 (Deleted), but it answered 4 (Void): "
        "the invoice changed after it was looked up. StatusId 4 means an Approved invoice was voided, so its status was "
        "Approved when the delete reached Gorelo and it had already been pushed to the connected accounting system, "
        "where the copy stays open (the void happened in Gorelo ONLY), so the user must void it there too. "
        "Check it in the Gorelo app."
    )


async def test_an_approved_invoice_that_gorelo_reports_as_deleted_is_not_called_voided(server, mock_gorelo):
    # the old reading took "Approved" to mean "voided" whatever Gorelo answered: the answer decides, never the expectation
    lookup(mock_gorelo, [invoice(1042, (5, "Approved"))])
    mock_gorelo.on("DELETE", DELETE_PATH, envelope({"Id": INVOICE_ID, "StatusId": 6}))
    result = await call_tool(
        server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Approved", "confirm": True}
    )
    assert result["outcome"] == "deleted (StatusId 6 Deleted, no longer listed)" and result["StatusId"] == 6
    assert result["previous_status"] == "Draft"  # only a Draft is answered with 6
    assert result["note"] == (
        "expected_status was Approved, so Gorelo should have answered StatusId 4 (Void), but it answered 6 (Deleted): "
        "the invoice changed after it was looked up. StatusId 6 means a Draft invoice was deleted, so its status was "
        "Draft when the delete reached Gorelo. Check it in the Gorelo app."
    )
    assert "accounting" not in result["note"]  # a Draft was never pushed


@pytest.mark.parametrize(
    "expected, answered, was",
    [
        pytest.param("Draft", 6, "Draft", id="draft-deleted"),
        pytest.param("Approved", 4, "Approved", id="approved-voided"),
        pytest.param("Draft", 4, "Approved", id="draft-looked-up-but-approved-by-then"),
        pytest.param("Approved", 6, "Draft", id="approved-looked-up-but-a-draft-by-then"),
    ],
)
async def test_previous_status_is_read_from_the_answer_and_never_from_expected_status(
    server, mock_gorelo, expected, answered, was
):
    lookup(mock_gorelo, [invoice(1042, (1, "Draft") if expected == "Draft" else (5, "Approved"))])
    mock_gorelo.on("DELETE", DELETE_PATH, envelope({"Id": INVOICE_ID, "StatusId": answered}))
    result = await call_tool(
        server, "delete_invoice", {"invoice_number": 1042, "expected_status": expected, "confirm": True}
    )
    assert result["previous_status"] == was
    assert ("note" in result) == (expected != was)  # a note exactly when the answer is not the one expected
    # only an Approved invoice has been pushed to accounting, so only the note that says the invoice was Approved
    # (although the caller expected a Draft) mentions it
    assert ("accounting system" in result.get("note", "")) == (expected == "Draft" and was == "Approved")


@pytest.mark.parametrize("expected, answered", [("Draft", 6), ("Approved", 4)])
async def test_an_answer_that_matches_the_expected_status_carries_no_note(server, mock_gorelo, expected, answered):
    status = (1, "Draft") if expected == "Draft" else (5, "Approved")
    lookup(mock_gorelo, [invoice(1042, status)])
    mock_gorelo.on("DELETE", DELETE_PATH, envelope({"Id": INVOICE_ID, "StatusId": answered}))
    result = await call_tool(
        server, "delete_invoice", {"invoice_number": 1042, "expected_status": expected, "confirm": True}
    )
    assert "note" not in result and set(result) == {"Id", "StatusId", "invoice_number", "previous_status", "outcome"}
    assert result["previous_status"] == expected


def test_the_two_delete_answers_are_the_ones_the_spec_text_gives(spec_index):
    assert (invoices.DELETED_STATUS_ID, invoices.VOID_STATUS_ID) == (6, 4)
    assert set(invoices.DELETE_ANSWERS) == {6, 4}
    assert invoices.EXPECTED_DELETE_ANSWER == {"Draft": 6, "Approved": 4}
    # 6 is no status a list can show (it exists only in this answer), 4 is one a list shows
    assert 6 not in invoices.INVOICE_STATUSES and 4 in invoices.INVOICE_STATUSES
    assert invoices.INVOICE_STATUSES == {1: "Draft", 2: "Submitted", 3: "Paid", 4: "Void", 5: "Approved"}
    assert spec_index.schema("DeleteInvoiceResult")["fields"]["StatusId"]["nullable"] is False
    # the outcome says what the answer says (Deleted, no longer listed) and promises nothing more
    assert [text for _name, text in invoices.DELETE_ANSWERS.values()] == [
        "deleted (StatusId 6 Deleted, no longer listed)",
        "voided (status Void, still listed)",
    ]
    # what an answer shows the invoice was is the inverse of what each expected status leads to
    assert invoices.ANSWER_SHOWS_WAS == {6: "Draft", 4: "Approved"}
    assert invoices.ANSWER_SHOWS_WAS == {answer: status for status, answer in invoices.EXPECTED_DELETE_ANSWER.items()}


async def test_no_text_of_delete_invoice_promises_that_a_delete_is_permanent(server, mock_gorelo):
    """the 2026-10-02 spec says only that a deleted Draft has StatusId 6 (Deleted) and is gone from the list; it no
    longer says hard-deleted. The tool must not tell the model or the user more than that: not in its module text, its
    description, its parameters, its refusals or its results."""
    texts = {"module docstring": invoices.__doc__, "docstring": inspect.getdoc(invoices.delete_invoice)}
    tool = tool_def("delete_invoice", await list_tools(server))
    texts["description"] = tool.description
    for name, definition in tool.inputSchema["properties"].items():
        texts[f"parameter {name}"] = definition["description"]
    for _name, outcome in invoices.DELETE_ANSWERS.values():
        texts[f"outcome {outcome}"] = outcome
    args = {"invoice_number": 1042, "expected_status": "Draft"}
    mock_gorelo.on(  # the three lookups below, one answer each, in order
        "GET",
        INVOICES,
        in_order(
            paged_envelope([invoice(1042, (3, "Paid"))]),
            paged_envelope([]),
            paged_envelope([invoice(1042, (1, "Draft"))]),
        ),
        query={"Number": "1042"},
    )
    mock_gorelo.on("DELETE", DELETE_PATH, envelope({"Id": INVOICE_ID, "StatusId": 6}))
    texts["confirm refusal"] = await call_tool_error(server, "delete_invoice", args)  # no request at all
    texts["status refusal"] = await call_tool_error(server, "delete_invoice", {**args, "confirm": True})
    texts["lookup refusal"] = await call_tool_error(server, "delete_invoice", {**args, "confirm": True})
    texts["result"] = json.dumps(await call_tool(server, "delete_invoice", {**args, "confirm": True}))
    for where, text in texts.items():
        assert text and not re.search(r"permanen|hard-delet|irrevers|for good", text, re.IGNORECASE), where


async def test_the_delete_goes_to_the_id_of_the_matching_row_in_canonical_form(server, mock_gorelo):
    lookup(mock_gorelo, [invoice(1042, (1, "Draft"), invoice_id=INVOICE_ID.upper().replace("-", ""))])
    mock_gorelo.on("DELETE", DELETE_PATH, envelope({"Id": INVOICE_ID, "StatusId": 6}))
    await call_tool(server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True})
    assert mock_gorelo.calls("DELETE")[0].path == DELETE_PATH


@pytest.mark.parametrize(
    "data", [None, True, False, {}, [], [{"Id": INVOICE_ID}], "ok", 5], ids=["null", "true", "false", "empty-object", "empty-list", "list", "text", "number"]
)
async def test_a_delete_answer_that_is_not_an_object_is_not_taken_for_a_success(server, mock_gorelo, data):
    # the DELETE may well have been applied: say so and never report "deleted" for an answer that proves nothing
    lookup(mock_gorelo, [invoice(1042)])
    mock_gorelo.on("DELETE", DELETE_PATH, envelope(data))
    text = await call_tool_error(server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True})
    assert text.startswith(
        "Gorelo returned an unexpected response for delete_invoice: DELETE /v1/invoices/{invoiceId}: "
        "expected Data to be a non-empty object but got "
    )
    assert "the write may have been applied, so verify it with a read before repeating it" in text
    assert "The change may or may not have been applied" not in text  # said once, not twice
    assert len(mock_gorelo.calls("DELETE")) == 1  # never repeated


@pytest.mark.parametrize(
    "answer, shown",
    [
        pytest.param({"Id": INVOICE_ID}, "missing", id="no-status-id"),
        pytest.param({"Id": INVOICE_ID, "StatusId": 1}, "1", id="draft-status-1-is-not-an-answer"),
        pytest.param({"Id": INVOICE_ID, "StatusId": 3}, "3", id="paid"),
        pytest.param({"Id": INVOICE_ID, "StatusId": 5}, "5", id="approved-would-mean-nothing-was-removed"),
        pytest.param({"Id": INVOICE_ID, "StatusId": 0}, "0", id="zero"),
        pytest.param({"Id": INVOICE_ID, "StatusId": 7}, "7", id="a-status-gorelo-added"),
        pytest.param({"Id": INVOICE_ID, "StatusId": -6}, "-6", id="negative"),
        pytest.param({"Id": INVOICE_ID, "StatusId": "6"}, "a string", id="text-six"),
        pytest.param({"Id": INVOICE_ID, "StatusId": "Deleted"}, "a string", id="the-name-not-the-id"),
        pytest.param({"Id": INVOICE_ID, "StatusId": 6.0}, "a number", id="decimal-six"),
        pytest.param({"Id": INVOICE_ID, "StatusId": True}, "a boolean", id="true-is-not-an-id"),
        pytest.param({"Id": INVOICE_ID, "StatusId": None}, "null", id="null"),
        pytest.param({"Id": INVOICE_ID, "StatusId": [6]}, "a list of 1 item", id="list"),
        pytest.param({"Id": INVOICE_ID, "StatusId": {"Id": 6}}, "an object", id="object"),
    ],
)
async def test_a_delete_answer_without_a_status_id_of_6_or_4_is_not_reported_as_a_success(
    server, mock_gorelo, answer, shown
):
    # DeleteInvoiceResult.StatusId is never null and is 6 (Deleted) or 4 (Void). Anything else cannot be reported as
    # "deleted" or "voided": the DELETE was sent and may well have been applied, so the model is told to check
    lookup(mock_gorelo, [invoice(1042)])
    mock_gorelo.on("DELETE", DELETE_PATH, envelope(answer))
    text = await call_tool_error(server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True})
    assert text == (
        "Gorelo returned an unexpected response for delete_invoice: DELETE /v1/invoices/{invoiceId}: "
        f"the answer's StatusId is {shown}, expected 6 (Deleted) or 4 (Void); the delete may have been applied, so "
        "verify it with a read before repeating it (list_invoices(number=1042))"
    )
    assert "The change may or may not have been applied" not in text  # said once, not twice
    assert len(mock_gorelo.calls("DELETE")) == 1  # never repeated


async def test_a_text_status_id_is_described_never_quoted(server, mock_gorelo):
    lookup(mock_gorelo, [invoice(1042)])
    mock_gorelo.on("DELETE", DELETE_PATH, envelope({"Id": INVOICE_ID, "StatusId": "secret-text"}))
    text = await call_tool_error(server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True})
    assert "secret-text" not in text and "StatusId is a string" in text


@pytest.mark.parametrize(
    "actual, expected, said",
    [
        pytest.param((3, "Paid"), "Draft", "has status Paid (id 3), not Draft", id="paid-is-not-draft"),
        pytest.param((3, "Paid"), "Approved", "has status Paid (id 3), not Approved", id="paid-is-not-approved"),
        pytest.param((2, "Submitted"), "Draft", "has status Submitted (id 2), not Draft", id="submitted-is-not-draft"),
        pytest.param((2, "Submitted"), "Approved", "has status Submitted (id 2), not Approved", id="submitted-is-not-approved"),
        pytest.param((4, "Void"), "Approved", "has status Void (id 4), not Approved", id="void-is-not-approved"),
        pytest.param((4, "Void"), "Draft", "has status Void (id 4), not Draft", id="void-is-not-draft"),
        pytest.param((5, "Approved"), "Draft", "has status Approved (id 5), not Draft", id="approved-is-not-draft"),
        pytest.param((1, "Draft"), "Approved", "has status Draft (id 1), not Approved", id="draft-is-not-approved"),
        pytest.param((9, "Mystery"), "Draft", "has status Mystery (id 9), not Draft", id="a-status-gorelo-added"),
    ],
)
async def test_a_status_other_than_the_expected_one_refuses_and_deletes_nothing(server, mock_gorelo, actual, expected, said):
    lookup(mock_gorelo, [invoice(1042, actual)])
    text = await call_tool_error(
        server, "delete_invoice", {"invoice_number": 1042, "expected_status": expected, "confirm": True}
    )
    assert text.startswith(f"expected_status: invoice 1042 {said}; nothing was deleted or voided.")
    assert "Gorelo only removes Draft invoices (deleted, no longer listed) and Approved invoices (voided, still listed)." in text
    assert mock_gorelo.calls("DELETE") == [] and len(mock_gorelo.requests) == 1


async def test_the_status_is_matched_on_its_id_not_on_its_name(server, mock_gorelo):
    # a row whose Name says Draft but whose Id is Paid must not be deleted as a Draft
    lookup(mock_gorelo, [invoice(1042, (3, "Draft"))])
    text = await call_tool_error(server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True})
    assert "has status Draft (id 3), not Draft" in text
    assert mock_gorelo.calls("DELETE") == []


async def test_no_matching_invoice_refuses(server, mock_gorelo):
    lookup(mock_gorelo, [])
    text = await call_tool_error(server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True})
    assert text == (
        "invoice_number: no invoice has the number 1042 (deleted invoices are not listed); nothing was deleted "
        "or voided. Check it with list_invoices(number=1042)."
    )
    assert mock_gorelo.calls("DELETE") == []


async def test_several_matching_invoices_refuse_and_name_them(server, mock_gorelo):
    lookup(mock_gorelo, [invoice(1042, (1, "Draft"), uid(5)), invoice(1042, (5, "Approved"), uid(6))])
    text = await call_tool_error(server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True})
    assert text.startswith("invoice_number: 1042 matches more than one invoice (")
    assert f"Id {uid(5)}, status Draft" in text and f"Id {uid(6)}, status Approved" in text
    assert "refusing to guess which one to delete or void" in text and "Nothing was deleted or voided." in text
    assert mock_gorelo.calls("DELETE") == []


async def test_a_single_row_with_more_pages_behind_it_is_still_ambiguous(server, mock_gorelo):
    mock_gorelo.on(
        "GET", INVOICES, envelope([invoice(1042)], pagination("next-1", 12)), query={"Number": "1042"}
    )
    text = await call_tool_error(server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True})
    assert "matches more than one invoice" in text
    assert mock_gorelo.calls("DELETE") == []


async def test_a_row_for_another_number_is_never_deleted(server, mock_gorelo):
    # Gorelo (or a gateway) ignoring the Number filter must not turn into the wrong invoice being removed
    lookup(mock_gorelo, [invoice(1043, (1, "Draft"))])
    text = await call_tool_error(server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True})
    assert text.startswith("Gorelo returned an unexpected response for delete_invoice: GET /v1/invoices: asked for the invoice numbered 1042")
    assert "refusing to delete or void anything" in text
    assert mock_gorelo.calls("DELETE") == []


@pytest.mark.parametrize(
    "row, fragment",
    [
        pytest.param(invoice(1042, Status=None), "the invoice row has no readable Status.Id", id="no-status"),
        pytest.param(invoice(1042, Status={"Name": "Draft"}), "the invoice row has no readable Status.Id", id="no-status-id"),
        pytest.param(invoice(1042, Status={"Id": "1", "Name": "Draft"}), "the invoice row has no readable Status.Id", id="status-id-is-text"),
        pytest.param(invoice(1042, Id=None), "the invoice row has no usable Id", id="no-id"),
        pytest.param(invoice(1042, Id="not-a-guid"), "the invoice row has no usable Id", id="id-is-not-a-guid"),
        pytest.param(invoice(1042, Id=f" {INVOICE_ID} "), "the invoice row has no usable Id", id="id-with-spaces-is-not-trimmed"),
        pytest.param(invoice(1042, Id=5), "the invoice row has no usable Id", id="id-is-a-number"),
        pytest.param(invoice(1042, Number="1042"), "asked for the invoice numbered 1042 but Gorelo returned a different one", id="number-is-text"),
        pytest.param(invoice(1042, Number=None), "asked for the invoice numbered 1042 but Gorelo returned a different one", id="no-number"),
        pytest.param("a string row", "the invoice row is not an object", id="row-is-not-an-object"),
    ],
)
async def test_a_row_that_cannot_be_trusted_is_refused_without_deleting(server, mock_gorelo, row, fragment):
    mock_gorelo.on("GET", INVOICES, envelope([row], pagination(None, 1, has_more=False)), query={"Number": "1042"})
    text = await call_tool_error(server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True})
    assert text.startswith("Gorelo returned an unexpected response for delete_invoice") and fragment in text
    assert mock_gorelo.calls("DELETE") == []


async def test_the_lookup_error_of_gorelo_names_invoice_number(server, mock_gorelo):
    mock_gorelo.on("GET", INVOICES, error_envelope(400, [("070101", "Number must be a whole number.", "Number")]))
    text = await call_tool_error(server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True})
    assert text == (
        "Gorelo rejected delete_invoice (HTTP 400, code 070101): invoice_number: Number must be a whole number. "
        f"[trace {TEST_TRACE_ID}]"
    )
    assert mock_gorelo.calls("DELETE") == []


async def test_a_gorelo_409_on_the_delete_is_reported_with_its_reason(server, mock_gorelo):
    lookup(mock_gorelo, [invoice(1042, (1, "Draft"))])
    body = error_envelope(409, [("070409", "Only a Draft or Approved invoice can be removed.")])
    mock_gorelo.on("DELETE", DELETE_PATH, body)
    text = await call_tool_error(server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True})
    assert text == (
        "Gorelo rejected delete_invoice (HTTP 409, code 070409): Only a Draft or Approved invoice can be removed. "
        f"[trace {TEST_TRACE_ID}]"
    )
    assert len(mock_gorelo.calls("DELETE")) == 1


async def test_an_invoice_that_vanished_between_lookup_and_delete_is_a_404(server, mock_gorelo):
    lookup(mock_gorelo, [invoice(1042, (1, "Draft"))])
    mock_gorelo.on("DELETE", DELETE_PATH, error_envelope(404, [("070401", "Invoice not found.")]))
    text = await call_tool_error(server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True})
    assert "Gorelo rejected delete_invoice (HTTP 404, code 070401): Invoice not found." in text


async def test_a_delete_that_times_out_is_not_retried_and_says_to_verify(server, mock_gorelo):
    lookup(mock_gorelo, [invoice(1042, (1, "Draft"))])
    mock_gorelo.on("DELETE", DELETE_PATH, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True})
    assert text.startswith("Gorelo did not confirm delete_invoice (the request timed out).")
    assert "Verify with a read before retrying." in text
    assert len(mock_gorelo.calls("DELETE")) == 1


@pytest.mark.parametrize("number", [0, -1])
async def test_a_bad_invoice_number_is_refused_before_anything_else(server, mock_gorelo, number):
    text = await call_tool_error(server, "delete_invoice", {"invoice_number": number, "expected_status": "Draft", "confirm": True})
    assert text == "invoice_number: expected a positive whole number such as 123, got zero or a negative number"
    assert mock_gorelo.requests == []


async def test_a_bad_invoice_number_is_refused_before_the_confirm_rule_is_even_reached(server, mock_gorelo):
    text = await call_tool_error(server, "delete_invoice", {"invoice_number": 0, "expected_status": "Draft"})
    assert text.startswith("invoice_number: expected a positive whole number")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "args, param",
    [
        ({"invoice_number": 1042, "expected_status": "Paid"}, "expected_status"),
        ({"invoice_number": 1042, "expected_status": "draft"}, "expected_status"),
        ({"invoice_number": 1042}, "expected_status"),
        ({"expected_status": "Draft"}, "invoice_number"),
        ({"invoice_number": "INV-1042", "expected_status": "Draft"}, "invoice_number"),
    ],
)
async def test_a_missing_or_unknown_value_is_refused_with_the_param_name(server, mock_gorelo, args, param):
    text = await call_tool_error(server, "delete_invoice", {**args, "confirm": True})
    assert param in text
    assert mock_gorelo.requests == []


async def test_calling_delete_invoice_directly_without_a_valid_status_or_number_is_refused(client_factory, mock_gorelo):
    with pytest.raises(ToolError, match="expected_status: must be 'Draft' or 'Approved'"):
        await call_directly(client_factory, invoices.delete_invoice, invoice_number=1042, expected_status="Paid", confirm=True)
    for bad in (None, True, "1042", 0, 2**63):  # a direct call skips the schema: the helper still refuses
        with pytest.raises(ToolError, match="invoice_number: expected a positive whole number such as 123, got "):
            await call_directly(client_factory, invoices.delete_invoice, invoice_number=bad, expected_status="Draft", confirm=True)
    with pytest.raises(ToolError) as info:  # what was given is described, never quoted
        await call_directly(client_factory, invoices.delete_invoice, invoice_number="secret-text", expected_status="Draft", confirm=True)
    assert "secret-text" not in str(info.value) and str(info.value).endswith("got a string")
    assert mock_gorelo.requests == []


async def test_delete_invoice_does_not_exist_unless_delete_tools_are_enabled(server_factory, mock_gorelo):
    off = server_factory()  # destructive defaults to off
    names = {tool.name for tool in await list_tools(off)}
    assert {"list_invoices", "get_invoice", "create_invoice", "export_invoice_pdf"} <= names
    assert not {"delete_invoice", "create_approved_invoice"} & names
    text = await call_tool_error(off, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True})
    assert "unknown tool" in text.lower()
    assert mock_gorelo.requests == []


async def test_the_invoice_tools_belong_to_the_billing_toolset(server_factory):
    everything = set(EXPECTED_TOOLS)
    only_core = server_factory(toolsets={"core"}, destructive=True)
    assert not everything & {t.name for t in await list_tools(only_core)}
    billing = server_factory(toolsets={"billing"}, destructive=True)
    assert everything <= {t.name for t in await list_tools(billing)}
    billing_off = server_factory(toolsets={"billing"}, destructive=False)
    gated = {"delete_invoice", "create_approved_invoice", "approve_invoice"}
    assert everything - gated <= {t.name for t in await list_tools(billing_off)}
    assert not gated & {t.name for t in await list_tools(billing_off)}


# --------------------------------------------------------------------------
# get_invoice (GET /v1/invoices/{invoiceId}, new in contract e15cb5a18ec2)
# --------------------------------------------------------------------------


def trace(text):
    return f"{text} [trace {TEST_TRACE_ID}]"


def flat(text):
    return " ".join(text.split())


async def test_get_invoice_reads_one_invoice_by_its_guid_and_returns_the_record_unchanged(server, mock_gorelo):
    bundle = line_row(
        ITEM_B, Id=uid(32), Name="Starter Kit", ItemType={"Id": 2, "Name": "Bundle"}, SubItems=[SUB_ITEM]
    )
    detail = invoice_detail(
        status=(5, "Approved"), Description="Monthly retainer", Attachments=[ATTACHMENT], LineItems=[line_row(), bundle]
    )
    mock_gorelo.on("GET", NEW_PATH, envelope(detail))
    result = await call_tool(server, "get_invoice", {"invoice_id": NEW_ID})
    assert result == detail
    assert result["Attachments"] == [ATTACHMENT] and result["LineItems"][1]["SubItems"] == [SUB_ITEM]
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", NEW_PATH, {}, None)
    assert len(mock_gorelo.requests) == 1


async def test_get_invoice_sends_the_id_in_canonical_form(server, mock_gorelo):
    mock_gorelo.on("GET", NEW_PATH, envelope(invoice_detail()))
    for given in (NEW_ID.upper(), NEW_ID.replace("-", ""), NEW_ID.upper().replace("-", "")):
        await call_tool(server, "get_invoice", {"invoice_id": given})
        assert mock_gorelo.last.path == NEW_PATH


@pytest.mark.parametrize(
    "bad",
    ["INV-1042", "1042", "", "   ", "../../assets/agents", f"{NEW_ID}/pdf", f" {NEW_ID} ", "{" + NEW_ID + "}", f"urn:uuid:{NEW_ID}"],
)
async def test_get_invoice_needs_a_guid_and_the_error_names_invoice_id(server, mock_gorelo, bad):
    text = await call_tool_error(server, "get_invoice", {"invoice_id": bad})
    assert text.startswith("invoice_id: expected a GUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b, got ")
    assert "invoiceId" not in text
    assert mock_gorelo.requests == []


async def test_get_invoice_refuses_a_number_and_a_missing_id(server, mock_gorelo):
    assert "invoice_id" in await call_tool_error(server, "get_invoice", {"invoice_id": 1042})
    assert "invoice_id" in await call_tool_error(server, "get_invoice", {})
    assert "invoice_id" in await call_tool_error(server, "get_invoice", {"invoice_id": None})
    assert mock_gorelo.requests == []


async def test_get_invoice_maps_a_404_to_the_snake_case_param(server, mock_gorelo):
    body = error_envelope(404, [("070401", "Invoice not found.", "invoiceId")])
    mock_gorelo.on("GET", NEW_PATH, body)
    text = await call_tool_error(server, "get_invoice", {"invoice_id": NEW_ID})
    assert text == trace("Gorelo rejected get_invoice (HTTP 404, code 070401): invoice_id: Invoice not found.")


async def test_get_invoice_404_for_a_missing_invoice_and_for_another_providers_reads_the_same(server, mock_gorelo):
    # the spec: "An invoice that does not exist, or belongs to another service provider, is a 404 - the two are never distinguished"
    mock_gorelo.on("GET", NEW_PATH, error_envelope(404, [("070401", "Invoice not found.")]))
    text = await call_tool_error(server, "get_invoice", {"invoice_id": NEW_ID})
    assert text == trace("Gorelo rejected get_invoice (HTTP 404, code 070401): Invoice not found.")


async def test_get_invoice_reports_a_missing_scope(server, mock_gorelo):
    mock_gorelo.on("GET", NEW_PATH, error_envelope(403, [("080203", "API key does not have 'Billing' scope")]))
    text = await call_tool_error(server, "get_invoice", {"invoice_id": NEW_ID})
    assert "the API key does not have the 'Billing' scope" in text


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")])
async def test_get_invoice_after_a_timeout_says_it_was_a_read_and_retrying_is_safe(server, mock_gorelo, failure):
    mock_gorelo.on("GET", NEW_PATH, failure)
    text = await call_tool_error(server, "get_invoice", {"invoice_id": NEW_ID})
    assert text.startswith("Gorelo did not answer get_invoice") and "This was a read, so retrying is safe." in text
    assert "export" not in text.lower()


@pytest.mark.parametrize(
    "data, said",
    [
        pytest.param(None, "Data is null; refusing to guess", id="null"),
        pytest.param([invoice_detail()], "expected Data to be a non-empty object but got a list of 1 item; refusing to guess", id="list"),
        pytest.param({}, "expected Data to be a non-empty object but got an empty object; refusing to guess", id="empty-object"),
        pytest.param("INV-1042", "expected Data to be a non-empty object but got a string; refusing to guess", id="text"),
    ],
)
async def test_get_invoice_refuses_an_answer_that_is_not_an_invoice_object(server, mock_gorelo, data, said):
    mock_gorelo.on("GET", NEW_PATH, envelope(data))
    text = await call_tool_error(server, "get_invoice", {"invoice_id": NEW_ID})
    assert text.startswith("Gorelo returned an unexpected response for get_invoice") and said in text
    assert "may have been applied" not in text  # a read changed nothing
    assert len(mock_gorelo.requests) == 1


async def test_get_invoice_declares_only_its_own_get_so_it_cannot_reach_the_list_or_the_pdf(server, mock_gorelo):
    mock_gorelo.on("GET", NEW_PATH, envelope(invoice_detail()))
    await call_tool(server, "get_invoice", {"invoice_id": NEW_ID})
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", NEW_PATH)]
    assert not any(r.path.endswith("/pdf") for r in mock_gorelo.requests)  # a read must never record an export event


def test_get_invoice_is_a_read_tool_that_is_not_a_side_effect_get():
    # decided by SHAPE (is_side_effect_get), never by the exact text of the key: under another spelling of the placeholder
    # (Gorelo renamed thirteen of them on 2026-10-02) an exact-text test would pass for the export too
    spec = next(s for s in REGISTRY.specs if s.name == "get_invoice")
    assert spec.kind == "read" and spec.destructive_hint is False
    assert not any(is_side_effect_get(op) for op in spec.ops)
    # and the check can tell the two apart: the export is a side-effect GET under any spelling, the invoice read is not
    assert is_side_effect_get("GET /v1/invoices/{invoiceId}/pdf") and is_side_effect_get("GET /v1/invoices/{id}/pdf")
    assert not is_side_effect_get("GET /v1/invoices/{invoiceId}") and not is_side_effect_get("GET /v1/invoices/{id}")


def test_the_get_invoice_docstring_says_what_a_line_and_an_attachment_are():
    doc = flat(inspect.getdoc(invoices.get_invoice))
    assert "status, totals, amount due, every line and the attachments" in doc
    assert "A bundle is one line and its parts are in SubItems" in doc
    assert "An attachment Url is a temporary link: use it now, never store it." in doc
    assert "A Void invoice can still show AmountDue: read Status first." in doc
    assert "Side effects" not in doc


# --------------------------------------------------------------------------
# create_invoice and create_approved_invoice: what they say and declare
# --------------------------------------------------------------------------

LATEST_SPEC = Path(__file__).resolve().parent.parent / "backups" / "swagger-latest.json"
LINE_PARAMS = [
    "item_id", "quantity", "description", "unit_price", "unit_cost", "discount_percent", "tax_id", "no_tax", "coa_code",
    "billable_status_id",
]
CREATE_PARAMS = ["client_id", "line_items", "invoice_date", "due_date", "reference"]
APPROVED_PARAMS = [*CREATE_PARAMS, "recipient_emails", "confirm"]


def test_both_create_tools_share_one_request_and_the_field_maps_differ_only_by_the_recipients(spec_index):
    draft = next(s for s in REGISTRY.specs if s.name == "create_invoice")
    approved = next(s for s in REGISTRY.specs if s.name == "create_approved_invoice")
    assert draft.field_map == invoices.CREATE_FIELDS == {
        "client_id": "ClientId", "line_items": "LineItems", "invoice_date": "InvoiceDate", "due_date": "DueDate",
        "reference": "Reference",
    }
    assert approved.field_map == invoices.APPROVED_FIELDS == {**draft.field_map, "recipient_emails": "RecipientEmails"}
    assert "StatusId" not in set(draft.field_map.values()) | set(approved.field_map.values())  # not a parameter
    op = spec_index.op("POST /v1/invoices")
    assert set(approved.field_map.values()) | {"StatusId"} == set(op.body["fields"])  # every field of the body is accounted for
    assert op.body["fields"]["StatusId"]["type"] == "integer" and op.body["required"] == []
    line = spec_index.schema("CreateInvoiceLineItem")["fields"]
    assert set(invoices.LINE_FIELDS.values()) == set(line)  # no line field of the spec is left out or invented
    assert set(invoices.LINE_PARAMS) == set(invoices.LINE_FIELDS) | {"no_tax"}


async def test_the_create_tools_take_the_documented_parameters(server):
    tools = await list_tools(server)
    draft = tool_def("create_invoice", tools).inputSchema
    approved = tool_def("create_approved_invoice", tools).inputSchema
    assert list(draft["properties"]) == CREATE_PARAMS and draft["required"] == ["client_id", "line_items"]
    assert list(approved["properties"]) == APPROVED_PARAMS and approved["required"] == ["client_id", "line_items"]
    for schema in (draft, approved):
        assert all(p.get("description") for p in schema["properties"].values())
    # the draft tool has no recipients and no confirm: nothing on it can email or approve
    assert "recipient_emails" not in draft["properties"] and "confirm" not in draft["properties"]
    assert not {"status_id", "status", "approve", "approved", "status_ids"} & (set(draft["properties"]) | set(approved["properties"]))


async def test_the_line_schema_has_the_documented_fields_with_the_bounds_the_spec_states(server):
    tool = tool_def("create_invoice", await list_tools(server))
    # FastMCP inlines the model: the advertised schema has no $defs and no $ref, the line sits in line_items.items
    assert "$defs" not in tool.inputSchema and "$ref" not in json.dumps(tool.inputSchema)
    assert tool.inputSchema["properties"]["line_items"]["type"] == "array"
    line = tool.inputSchema["properties"]["line_items"]["items"]
    assert list(line["properties"]) == LINE_PARAMS and line["required"] == ["item_id", "quantity"]
    assert line["additionalProperties"] is False and line["type"] == "object"
    assert all(p.get("description") for p in line["properties"].values())
    assert line["properties"]["quantity"]["exclusiveMinimum"] == 0 and line["properties"]["quantity"]["type"] == "number"
    discount = line["properties"]["discount_percent"]
    assert (discount["minimum"], discount["maximum"], discount["type"]) == (0, 100, "number")
    assert line["properties"]["no_tax"]["type"] == "boolean" and line["properties"]["no_tax"]["default"] is False
    assert line["properties"]["tax_id"]["type"] == "integer" and line["properties"]["billable_status_id"]["type"] == "integer"
    # the compacted schema keeps no null branch in the line either
    assert '"type": "null"' not in json.dumps(tool.inputSchema)
    # the line fields say where an id comes from and what an omitted field does
    text = {name: flat(p["description"]) for name, p in line["properties"].items()}
    assert "list_items" in text["item_id"] and "A bundle is one line" in text["item_id"]
    assert "list_taxes" in text["tax_id"] and "Not with tax_id" in text["no_tax"]
    assert "1 Billable (default), 2 No charge, 3 Non-billable" in text["billable_status_id"]
    for name in ("description", "unit_price", "unit_cost", "tax_id", "coa_code"):
        assert "Default:" in text[name], name
    assert "Default 0" in text["discount_percent"]  # a plain default, not the item's own discount


async def test_the_create_parameter_texts_carry_the_id_sources_dates_and_the_confirm_rule(server):
    tools = await list_tools(server)

    def text(tool, param):
        return flat(tool_def(tool, tools).inputSchema["properties"][param]["description"])

    for tool in ("create_invoice", "create_approved_invoice"):
        assert "list_clients" in text(tool, "client_id")
        assert text(tool, "invoice_date") == "YYYY-MM-DD. Default: today."
        assert text(tool, "due_date") == "YYYY-MM-DD, not before invoice_date. Default: invoice_date."
        # only description, price, cost, tax and COA code come from the item; discount and billable status
        # are plain defaults (0 and Billable), so "fields left out use the item's own values" would be wrong
        assert text(tool, "line_items") == (
            "At least one line. Description, price, cost, tax and COA code left out use the item's own values; "
            "discount defaults to 0 and billable status to Billable."
        )
        assert text(tool, "line_items") == invoices.LINE_ITEMS_TEXT
        assert "Fields left out use the item's own values" not in text(tool, "line_items")
    assert "Must be true" in text("create_approved_invoice", "confirm") and "user approves" in text("create_approved_invoice", "confirm")
    emails = text("create_approved_invoice", "recipient_emails")
    assert "may email them at once" in emails and "Omit for none" in emails


def test_the_create_invoice_docstring_states_the_facts_the_model_must_know():
    doc = flat(inspect.getdoc(invoices.create_invoice))
    assert doc.startswith("Create a DRAFT invoice for a client from catalog items and return it with its lines and totals.")
    assert "A Draft is not pushed to accounting or sent to anyone" in doc
    assert "nothing here makes it Approved" in doc and "create_approved_invoice, when enabled" in doc
    assert "client_id -> list_clients, item_id -> list_items, tax_id -> list_taxes" in doc
    assert "Unset price, cost, tax and COA code fall back to the item's own values" in doc
    assert "creates a real invoice that gets a number" in doc
    assert "to remove a mistaken Draft use delete_invoice, when deletes are enabled" in doc
    assert "If the result is {Id, warning} the draft exists: do not create it again (read it with get_invoice)" in doc
    assert 'After a "did not confirm" error check list_invoices for the client (newest first) before creating it again' in doc
    assert "recipient" not in doc.lower() and "email" not in doc.lower()  # nobody is emailed by a draft
    assert 40 < len(inspect.getdoc(invoices.create_invoice)) <= 900


def test_the_create_approved_invoice_docstring_states_the_facts_the_model_must_know():
    doc = flat(inspect.getdoc(invoices.create_approved_invoice))
    assert doc.startswith("Create an APPROVED invoice for a client and return it.")
    assert "Arguments work as in create_invoice (no_tax too; a no_tax line that still shows tax gets a loud warning)." in doc
    assert "no_tax is refused" not in doc and "not accepted" not in doc
    assert "Side effects: approving on create pushes the invoice to the connected accounting system at once" in doc
    # voiding with delete_invoice does not reach the copy in Xero, so the old
    # "cannot be undone except by voiding it" is gone: nothing takes the push back, and the user voids the copy by hand
    assert "cannot be undone except by voiding" not in doc
    assert (
        "Nothing takes that back: delete_invoice, when deletes are enabled, voids it in Gorelo ONLY, so the copy in the "
        "accounting system stays open (observed with Xero): tell the user, who must void it there too."
    ) in doc
    assert "A total of exactly 0 is created as Paid instead: check Status in the result" in doc
    assert "recipient_emails, when given, are the addresses Gorelo sends the invoice to" in doc
    assert "it does not say whether creating the invoice already sends it): tell the user who" in doc
    assert "If the result is {Id, warning} the invoice exists: do not create it again (get_invoice)" in doc
    assert 'After a "did not confirm" error check list_invoices first' in doc
    assert doc.endswith("Ask the user first; needs confirm=true.")  # the consistency rule of every destructive tool
    assert 40 < len(inspect.getdoc(invoices.create_approved_invoice)) <= 900


@pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")
def test_the_create_promises_are_the_ones_in_the_spec_text():
    """spec/spec_index.json keeps no description text, so what the create tools promise is pinned to the text of the
    full OpenAPI snapshot of contract e15cb5a18ec2."""
    document = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))
    schemas = document["components"]["schemas"]
    command = schemas["CreateInvoiceCommand"]["properties"]
    line = schemas["CreateInvoiceLineItem"]["properties"]
    assert "1 = Draft (default) or 5 = Approved. Any other value is rejected" in flat(command["StatusId"]["description"])
    assert "Approving on create pushes the invoice to the connected accounting system" in flat(command["StatusId"]["description"])
    assert "exactly 0 is recorded as Paid instead" in flat(command["StatusId"]["description"])
    assert "Defaults to today" in flat(command["InvoiceDate"]["description"])
    assert "Defaults to InvoiceDate when omitted. Must not be earlier than InvoiceDate" in flat(command["DueDate"]["description"])
    assert "Email addresses to send the invoice to" in flat(command["RecipientEmails"]["description"])
    assert "At least one is required" in flat(command["LineItems"]["description"])
    assert "Required, must be greater than 0" in flat(line["Quantity"]["description"])
    assert "0-100" in flat(line["DiscountPercent"]["description"]) and "Defaults to 0" in flat(line["DiscountPercent"]["description"])
    # contract 6c30770dffc0 (2026-10-29): null no longer forces no tax on a create line; the tax-exempt rate's id does
    assert "When omitted or null the line takes the tax the app would give it" in flat(line["TaxId"]["description"])
    assert "To bill no tax, send the tax-exempt rate's id" in flat(line["TaxId"]["description"])
    assert "send explicit null to force no tax" not in flat(line["TaxId"]["description"])
    assert "1 = Billable, 2 = No charge, 3 = Non-billable" in flat(line["BillableStatusId"]["description"])
    assert "Falls back to the item's own name" in flat(line["Description"]["description"])
    # what a left-out line field does, as the field texts say it (the operation text lumps all seven together)
    assert "Defaults to Billable when omitted" in flat(line["BillableStatusId"]["description"])
    assert "Defaults to 0 when omitted" in flat(line["DiscountPercent"]["description"])
    # (TaxId is not here any more: its text says "When omitted or null the line takes the tax the app would give it")
    for field, what in (("UnitPrice", "price"), ("UnitCost", "cost"), ("CoaCode", "COA code")):
        assert f"Falls back to the item's own {what}" in flat(line[field]["description"]), field
    assert "A line whose ItemId is a bundle is billed as one line" in flat(command["LineItems"]["description"])
    operation = flat(document["paths"]["/v1/invoices"]["post"]["description"])
    assert "`StatusId` is `1` (Draft) or `5` (Approved)" in operation
    assert "Approving on create also pushes the invoice to the connected accounting system" in operation
    assert "exactly `0` is created as Paid instead" in operation
    assert "The response is just the created invoice's `Id`" in operation
    # the read-back and the detail
    detail = flat(document["paths"]["/v1/invoices/{invoiceId}"]["get"]["description"])
    assert "A bundle is one line" in detail and "`Url` is a temporary download link" in detail
    assert flat(schemas["InvoiceAttachmentModel"]["properties"]["Url"]["description"]).startswith("Temporary secure download link")


@pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")
def test_the_delete_answer_is_read_the_way_the_spec_text_gives_it():
    """What delete_invoice reports (StatusId 6 Deleted for a Draft, 4 Void for an Approved invoice) is pinned to the
    text of the full OpenAPI snapshot of contract e15cb5a18ec2."""
    document = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))
    status = flat(document["components"]["schemas"]["DeleteInvoiceResult"]["properties"]["StatusId"]["description"])
    assert "6 (Deleted) when a Draft invoice was deleted, or 4 (Void) when an Approved invoice was voided" in status
    assert "Deleted (6) appears only here" in status
    operation = flat(document["paths"]["/v1/invoices/{invoiceId}"]["delete"]["description"])
    assert "the response's `StatusId` is 6 (Deleted)" in operation and "the response's `StatusId` is 4 (Void)" in operation
    assert "already deleted, or already void, is treated as success" in operation  # why this tool refuses Void itself
    assert "only a Draft or Approved invoice can be removed this way" in operation
    # the published text says Deleted and gone from the list, and nothing about the delete being permanent (the
    # 2026-10-01 text said hard-deleted). If a later spec says more, re-read it before the tool's wording changes.
    assert "the invoice is gone from `GET /v1/invoices` entirely" in operation
    assert not re.search(r"permanen|hard-delet|irrevers", operation + " " + status, re.IGNORECASE)


needs_spec_text = pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")


def operation(method, path):
    return json.loads(LATEST_SPEC.read_text(encoding="utf-8"))["paths"][path][method]


def param_text(method, path, name):
    return flat(next(p["description"] for p in operation(method, path)["parameters"] if p["name"] == name))


def scale_of(text):  # {id: label} from "1 Draft, 2 Submitted, 3 Paid"
    return {int(n): label for n, label in re.findall(r"(\d+) ([A-Z][a-z]+)", text)}


@needs_spec_text
def test_the_invoice_status_scale_is_the_one_the_spec_text_documents():
    """The StatusIds scale of list_invoices is the one the full OpenAPI snapshot documents. Deleted (6) is only a
    DELETE answer, so the list's statuses stay 1 to 5."""
    text = param_text("get", "/v1/invoices", "StatusIds")
    assert scale_of(text) == invoices.INVOICE_STATUSES == {1: "Draft", 2: "Submitted", 3: "Paid", 4: "Void", 5: "Approved"}
    assert "Any other id is a 400" in text
    assert "Omitted returns every status; deleted invoices are never listed" in text
    document = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))
    answer = flat(document["components"]["schemas"]["DeleteInvoiceResult"]["properties"]["StatusId"]["description"])
    assert "so the list's status set stays 1-5" in answer and set(invoices.INVOICE_STATUSES) == {1, 2, 3, 4, 5}
    delete = flat(operation("delete", "/v1/invoices/{invoiceId}")["description"])
    assert "Refused with 409 - only a Draft or Approved invoice can be removed this way" in delete


# --------------------------------------------------------------------------
# create_invoice: what goes out and what comes back
# --------------------------------------------------------------------------

BOTH = ["create_invoice", "create_approved_invoice"]


def args_for(tool, **extra):
    """The call arguments of one create tool: the gated one also needs confirm=true to get anywhere."""
    base = {**GOOD, **({"confirm": True} if tool == "create_approved_invoice" else {})}
    return {**base, **extra}


def line(**extra):
    return {"item_id": ITEM_A, "quantity": 1, **extra}


# The taxes as measured live on 2026-10-30 on a test account: six active rates at 0 percent, one of them Id 202 "Tax Exempt TAX"
# with Code "Tax Exempt" (the default), plus a taxed rate. Names of the other 0 percent rates are invented.
EXEMPT_ID = 202


def tax_row(tax_id, name, code, percentage=0.0, is_default=False, sub_taxes=None):
    """One TaxModel as GET /v1/taxes returns it."""
    return {"Id": tax_id, "Name": name, "Code": code, "Percentage": percentage, "IsDefault": is_default,
            "SubTaxes": sub_taxes if sub_taxes is not None else []}


LIVE_TAXES = [
    tax_row(197, "Sales Tax 9.25", "ST925", 9.25),
    tax_row(198, "Zero Rated", "Zero Rated"),
    tax_row(199, "Export", "Export"),
    tax_row(200, "No VAT", "NOVAT"),
    tax_row(201, "Out of Scope", "OOS"),
    tax_row(EXEMPT_ID, "Tax Exempt TAX", "Tax Exempt", 0.0, True),
    tax_row(203, "Non Taxable", None),
]


def route_taxes(mock, rows=None):
    """GET /v1/taxes answers `rows` (default: the live tenant's rates)."""
    mock.on("GET", "/v1/taxes", envelope(LIVE_TAXES if rows is None else rows))


async def call_create_directly(client_factory, tool, **kwargs):
    """Call the decorated function itself (no pydantic in front of it): for what the schema normally pre-empts."""
    async with client_factory() as client:
        return await getattr(invoices, tool)(make_ctx(client), **kwargs)


async def test_create_invoice_posts_a_draft_then_reads_it_back_and_returns_the_invoice(server, mock_gorelo):
    detail = invoice_detail()
    route_create(mock_gorelo, detail)
    result = await call_tool(server, "create_invoice", GOOD)
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("POST", INVOICES), ("GET", NEW_PATH)]
    post, read = mock_gorelo.requests
    assert post.json == {"ClientId": CLIENT, "StatusId": 1, "LineItems": [{"ItemId": ITEM_A, "Quantity": 2}]}
    assert post.query == {} and post.raw_path == "/v1/invoices"
    assert read.query == {} and read.json is None and read.content == b""
    assert result == detail and result["Status"] == {"Id": 1, "Name": "Draft"}
    assert result["LineItems"] == detail["LineItems"] and result["Id"] == NEW_ID


async def test_create_invoice_sends_every_line_field_under_its_spec_name(server, mock_gorelo):
    route_create(mock_gorelo)
    route_taxes(mock_gorelo)
    args = {
        "client_id": CLIENT,
        "line_items": [
            {
                "item_id": ITEM_A.upper(),  # canonical lowercase on the wire
                "quantity": 2,
                "description": "Onboarding, week 1",
                "unit_price": 150,
                "unit_cost": 90.5,
                "discount_percent": 10,
                "tax_id": 2,
                "coa_code": "200",
                "billable_status_id": 2,
            },
            {"item_id": ITEM_B.replace("-", ""), "quantity": 0.5, "no_tax": True},
        ],
        "invoice_date": "2026-09-15",
        "due_date": "2026-10-15",
        "reference": "PO-4471",
    }
    await call_tool(server, "create_invoice", args)
    assert mock_gorelo.calls("POST")[0].json == {
        "ClientId": CLIENT,
        "StatusId": 1,
        "LineItems": [
            {
                "ItemId": ITEM_A,
                "Quantity": 2,
                "Description": "Onboarding, week 1",
                "UnitPrice": 150,
                "UnitCost": 90.5,
                "DiscountPercent": 10,
                "TaxId": 2,
                "CoaCode": "200",
                "BillableStatusId": 2,
            },
            {"ItemId": ITEM_B, "Quantity": 0.5, "TaxId": EXEMPT_ID},
        ],
        "InvoiceDate": "2026-09-15T00:00:00Z",
        "DueDate": "2026-10-15T00:00:00Z",
        "Reference": "PO-4471",
    }


async def test_a_field_the_caller_did_not_give_is_never_sent(server, mock_gorelo):
    route_create(mock_gorelo)
    await call_tool(server, "create_invoice", GOOD)
    body = mock_gorelo.requests[0].json
    assert set(body) == {"ClientId", "StatusId", "LineItems"}  # no dates, no reference, no recipients
    assert set(body["LineItems"][0]) == {"ItemId", "Quantity"}  # no price, cost, tax, discount, COA code or description
    assert "TaxId" not in body["LineItems"][0]  # no key at all: the item's own tax applies


@pytest.mark.parametrize(
    "given, wire_name, wire_value",
    [
        pytest.param({"description": "Setup"}, "Description", "Setup", id="description"),
        pytest.param({"unit_price": 150}, "UnitPrice", 150, id="unit-price"),
        pytest.param({"unit_price": 0}, "UnitPrice", 0, id="a-free-line-is-a-price-of-zero"),
        pytest.param({"unit_price": 19.99}, "UnitPrice", 19.99, id="decimal-price"),
        pytest.param({"unit_price": -5}, "UnitPrice", -5, id="a-credit-line-is-the-callers-choice"),
        pytest.param({"unit_cost": 90.5}, "UnitCost", 90.5, id="unit-cost"),
        pytest.param({"unit_cost": 0}, "UnitCost", 0, id="a-cost-of-zero"),
        pytest.param({"discount_percent": 0}, "DiscountPercent", 0, id="discount-0"),
        pytest.param({"discount_percent": 12.5}, "DiscountPercent", 12.5, id="discount-12.5"),
        pytest.param({"discount_percent": 100}, "DiscountPercent", 100, id="discount-100"),
        pytest.param({"tax_id": 2}, "TaxId", 2, id="tax-id"),
        pytest.param({"coa_code": "200"}, "CoaCode", "200", id="coa-code"),
        pytest.param({"billable_status_id": 1}, "BillableStatusId", 1, id="billable"),
        pytest.param({"billable_status_id": 2}, "BillableStatusId", 2, id="no-charge"),
        pytest.param({"billable_status_id": 3}, "BillableStatusId", 3, id="non-billable"),
        pytest.param({"quantity": 0.25}, "Quantity", 0.25, id="a-quarter-of-an-item"),
        pytest.param({"quantity": 1000000}, "Quantity", 1000000, id="a-million"),
    ],
)
async def test_each_optional_line_field_alone_goes_out_as_its_own_key(server, mock_gorelo, given, wire_name, wire_value):
    route_create(mock_gorelo)
    await call_tool(server, "create_invoice", {"client_id": CLIENT, "line_items": [line(**given)]})
    sent = mock_gorelo.requests[0].json["LineItems"][0]
    assert sent[wire_name] == wire_value
    assert set(sent) == ({"ItemId", "Quantity"} | {wire_name})  # and nothing else came along


async def test_no_tax_sends_the_exempt_rate_id_and_the_other_ways_send_nothing_or_the_id(server, mock_gorelo):
    route_create(mock_gorelo)
    route_taxes(mock_gorelo)
    lines = [line(no_tax=True), line(no_tax=False), line(), line(tax_id=7), line(no_tax=False, tax_id=7), line(no_tax=True)]
    result = await call_tool(server, "create_invoice", {"client_id": CLIENT, "line_items": lines})
    (post,) = mock_gorelo.calls("POST")
    sent = post.json["LineItems"]
    assert sent[0]["TaxId"] == EXEMPT_ID and sent[5]["TaxId"] == EXEMPT_ID  # the Tax Exempt rate, never null
    assert "TaxId" not in sent[1] and "TaxId" not in sent[2]  # false and absent are the same: the item's own tax
    assert sent[3]["TaxId"] == 7 and sent[4]["TaxId"] == 7
    assert b"null" not in post.content
    assert len(mock_gorelo.calls("GET", "/v1/taxes")) == 1  # resolved ONCE for two no_tax lines
    assert f"tax id {EXEMPT_ID}" in result["note"]


async def test_lines_keep_their_order_and_the_same_item_twice_is_two_lines(server, mock_gorelo):
    route_create(mock_gorelo)
    lines = [line(), {"item_id": ITEM_B, "quantity": 3}, line(quantity=4, description="Second use")]
    await call_tool(server, "create_invoice", {"client_id": CLIENT, "line_items": lines})
    sent = mock_gorelo.requests[0].json["LineItems"]
    assert [entry["ItemId"] for entry in sent] == [ITEM_A, ITEM_B, ITEM_A]
    assert [entry["Quantity"] for entry in sent] == [1, 3, 4]
    assert sent[2]["Description"] == "Second use" and "Description" not in sent[0]


async def test_a_bundle_is_one_line_and_its_parts_are_never_sent(server, mock_gorelo):
    # the spec: "A line whose ItemId is a bundle is billed as one line ... the caller never sends the parts"
    detail = invoice_detail(
        LineItems=[line_row(ITEM_B, ItemType={"Id": 2, "Name": "Bundle"}, Name="Starter Kit", SubItems=[SUB_ITEM])]
    )
    route_create(mock_gorelo, detail)
    result = await call_tool(server, "create_invoice", {"client_id": CLIENT, "line_items": [{"item_id": ITEM_B, "quantity": 1}]})
    assert mock_gorelo.requests[0].json["LineItems"] == [{"ItemId": ITEM_B, "Quantity": 1}]
    assert result["LineItems"][0]["SubItems"] == [SUB_ITEM]


@pytest.mark.parametrize(
    "dates, wire",
    [
        pytest.param({"invoice_date": "2026-09-15"}, {"InvoiceDate": "2026-09-15T00:00:00Z"}, id="invoice-date-only"),
        pytest.param({"due_date": "2026-10-15"}, {"DueDate": "2026-10-15T00:00:00Z"}, id="due-date-only"),
        pytest.param(
            {"invoice_date": "2026-09-15", "due_date": "2026-09-15"},
            {"InvoiceDate": "2026-09-15T00:00:00Z", "DueDate": "2026-09-15T00:00:00Z"},
            id="due-on-the-invoice-date",
        ),
        pytest.param(
            {"invoice_date": "2024-02-29", "due_date": "2024-03-30"},
            {"InvoiceDate": "2024-02-29T00:00:00Z", "DueDate": "2024-03-30T00:00:00Z"},
            id="a-leap-day-is-a-real-day",
        ),
        pytest.param(
            {"invoice_date": "2026-12-31", "due_date": "2027-01-01"},
            {"InvoiceDate": "2026-12-31T00:00:00Z", "DueDate": "2027-01-01T00:00:00Z"},
            id="across-a-year",
        ),
        pytest.param({"due_date": "2000-01-01"}, {"DueDate": "2000-01-01T00:00:00Z"}, id="due-alone-is-not-compared-with-today"),
    ],
)
async def test_calendar_dates_go_out_as_midnight_utc_and_only_when_given(server, mock_gorelo, dates, wire):
    route_create(mock_gorelo)
    await call_tool(server, "create_invoice", {**GOOD, **dates})
    body = mock_gorelo.requests[0].json
    assert {k: v for k, v in body.items() if k in ("InvoiceDate", "DueDate")} == wire


async def test_the_reference_goes_out_as_given_and_only_when_given(server, mock_gorelo):
    route_create(mock_gorelo)
    await call_tool(server, "create_invoice", {**GOOD, "reference": "  PO-4471 / Q3  "})
    assert mock_gorelo.requests[0].json["Reference"] == "  PO-4471 / Q3  "  # free text: not trimmed or reshaped
    await call_tool(server, "create_invoice", GOOD)
    assert "Reference" not in mock_gorelo.requests[2].json


async def test_the_reread_goes_to_the_id_gorelo_answered_with_in_canonical_form(server, mock_gorelo):
    new_id = uid(77)
    mock_gorelo.on("POST", INVOICES, envelope({"Id": new_id.upper().replace("-", "")}))
    mock_gorelo.on("GET", f"/v1/invoices/{new_id}", envelope(invoice_detail(invoice_id=new_id)))
    result = await call_tool(server, "create_invoice", GOOD)
    assert result["Id"] == new_id and mock_gorelo.requests[1].path == f"/v1/invoices/{new_id}"


async def test_a_model_line_and_a_dict_line_make_the_same_request_when_called_directly(client_factory, mock_gorelo):
    route_create(mock_gorelo)
    route_taxes(mock_gorelo)
    as_model = invoices.InvoiceLine(item_id=ITEM_A, quantity=2, unit_price=10, no_tax=True)
    as_dict = {"item_id": ITEM_A, "quantity": 2, "unit_price": 10, "no_tax": True}
    await call_create_directly(client_factory, "create_invoice", client_id=CLIENT, line_items=[as_model])
    await call_create_directly(client_factory, "create_invoice", client_id=CLIENT, line_items=[as_dict])
    posts = [r.json for r in mock_gorelo.calls("POST")]
    assert posts[0] == posts[1] == {
        "ClientId": CLIENT,
        "StatusId": 1,
        "LineItems": [{"ItemId": ITEM_A, "Quantity": 2, "UnitPrice": 10, "TaxId": EXEMPT_ID}],
    }


def test_the_line_model_refuses_what_pydantic_can_refuse_before_the_tool_runs():
    from pydantic import ValidationError

    good = {"item_id": ITEM_A, "quantity": 1}
    assert invoices.InvoiceLine(**good).no_tax is False
    for bad in (
        {"quantity": 0},
        {"quantity": -1},
        {"quantity": True},  # strict: JSON true is not a quantity of 1
        {"quantity": "2"},
        {"discount_percent": 100.5},
        {"discount_percent": -1},
        {"discount_percent": True},
        {"unit_price": "free"},
        {"unit_price": True},
        {"unit_cost": "9"},
        {"tax_id": True},
        {"tax_id": "2"},
        {"tax_id": 2.5},
        {"billable_status_id": True},
        {"no_tax": "true"},
        {"no_tax": 1},
        {"price": 3},  # an unknown field
        {"status_id": 5},
    ):
        with pytest.raises(ValidationError):
            invoices.InvoiceLine(**{**good, **bad})
    with pytest.raises(ValidationError):
        invoices.InvoiceLine(item_id=ITEM_A)  # quantity is required
    with pytest.raises(ValidationError):
        invoices.InvoiceLine(quantity=1)  # item_id is required


# --------------------------------------------------------------------------
# The status: 1 from create_invoice, 5 only from the gated tool
# --------------------------------------------------------------------------

STATUS_SHAPES = [
    pytest.param({}, id="minimal"),
    pytest.param({"invoice_date": "2026-09-01", "due_date": "2026-09-30", "reference": "PO-1"}, id="dated-and-referenced"),
    pytest.param({"line_items": [line(), line(tax_id=4), line(tax_id=3, discount_percent=100)]}, id="many-lines"),
    pytest.param({"line_items": [line(unit_price=0, quantity=1)]}, id="a-total-of-zero"),
]


@pytest.mark.parametrize("extra", STATUS_SHAPES)
async def test_create_invoice_always_sends_status_1_and_never_5(server, mock_gorelo, extra):
    route_create(mock_gorelo)
    await call_tool(server, "create_invoice", {**GOOD, **extra})
    body = mock_gorelo.requests[0].json
    assert body["StatusId"] == 1 and isinstance(body["StatusId"], int) and not isinstance(body["StatusId"], bool)
    assert b"5" not in json.dumps(body["StatusId"]).encode() and list(body).count("StatusId") == 1


@pytest.mark.parametrize("extra", STATUS_SHAPES)
async def test_create_approved_invoice_always_sends_status_5_and_never_1(server, mock_gorelo, extra):
    route_create(mock_gorelo)
    await call_tool(server, "create_approved_invoice", {**GOOD, **extra, "confirm": True})
    body = mock_gorelo.requests[0].json
    assert body["StatusId"] == 5 and isinstance(body["StatusId"], int) and not isinstance(body["StatusId"], bool)


@pytest.mark.parametrize("tool", BOTH)
@pytest.mark.parametrize(
    "extra",
    [
        {"status_id": 5}, {"StatusId": 5}, {"status": 5}, {"status": "approved"}, {"approved": True}, {"approve": True},
        {"status_ids": [5]}, {"statusId": 5}, {"state": "Approved"},
    ],
)
async def test_no_argument_can_change_the_status(server, mock_gorelo, tool, extra):
    text = await call_tool_error(server, tool, args_for(tool, **extra))
    assert next(iter(extra)) in text and mock_gorelo.requests == []


async def test_a_line_cannot_carry_a_status_either(server, mock_gorelo, client_factory):
    for tool in BOTH:
        for bad in ({"status_id": 5}, {"StatusId": 5}, {"status": "Approved"}):
            text = await call_tool_error(server, tool, args_for(tool, line_items=[line(**bad)]))
            assert next(iter(bad)) in text
    with pytest.raises(ToolError, match=r"line_items\[0\]: unknown field\(s\) StatusId; allowed: "):
        await call_create_directly(client_factory, "create_invoice", client_id=CLIENT, line_items=[line(StatusId=5)])
    assert mock_gorelo.requests == []


def test_the_shared_request_never_contains_a_status_and_only_the_gated_tool_names_the_approved_one():
    request = invoices._invoice_request(
        client_id=CLIENT, line_items=[line()], invoice_date="2026-09-01", due_date="2026-09-30", reference="r",
        recipient_emails=["a@example.test"],
    )
    assert "StatusId" not in request
    assert set(request) == {"ClientId", "LineItems", "InvoiceDate", "DueDate", "Reference", "RecipientEmails"}
    draft = inspect.getsource(invoices.create_invoice)
    approved = inspect.getsource(invoices.create_approved_invoice)
    assert "APPROVED_STATUS_ID" not in draft and "DRAFT_STATUS_ID" in draft
    assert "APPROVED_STATUS_ID" in approved and "DRAFT_STATUS_ID" not in approved
    # the constant is defined once and used by the two tools that approve (create_approved_invoice and approve_invoice),
    # never by the Draft create nor by update_invoice
    assert "APPROVED_STATUS_ID" not in inspect.getsource(invoices.update_invoice)
    assert "APPROVED_STATUS_ID" in inspect.getsource(invoices.approve_invoice)
    assert (invoices.DRAFT_STATUS_ID, invoices.APPROVED_STATUS_ID) == (1, 5)
    # a bad parameter is reported before the user is asked, and the user is asked before anything is posted
    assert approved.index("_invoice_request(") < approved.index("require_confirm(") < approved.index("_post_and_read_back(")
    assert "require_confirm" not in draft and "recipient" not in draft.lower()


async def test_both_tools_build_the_same_request_apart_from_the_status_and_the_recipients(server, mock_gorelo):
    route_create(mock_gorelo)
    extra = {
        "line_items": [line(unit_price=9, tax_id=4), line(tax_id=2, billable_status_id=3, coa_code="400")],
        "invoice_date": "2026-09-01",
        "due_date": "2026-09-30",
        "reference": "PO-9",
    }
    await call_tool(server, "create_invoice", {**GOOD, **extra})
    await call_tool(server, "create_approved_invoice", {**GOOD, **extra, "confirm": True})
    draft, approved = (r.json for r in mock_gorelo.calls("POST"))
    assert {k: v for k, v in draft.items() if k != "StatusId"} == {k: v for k, v in approved.items() if k != "StatusId"}
    assert (draft["StatusId"], approved["StatusId"]) == (1, 5)


# --------------------------------------------------------------------------
# Local validation: every problem names its param, nothing is sent
# --------------------------------------------------------------------------

DAY = "expected a calendar date as YYYY-MM-DD, for example 2026-09-01 (no time, no offset)"
LINES_SHAPE = "expected a list of lines, each an object with item_id and quantity"
POSITIVE = "expected a positive whole number such as 123"
ALLOWED = (
    "item_id, quantity, description, unit_price, unit_cost, discount_percent, tax_id, coa_code, billable_status_id, no_tax"
)

LOCAL_ERRORS = [
    pytest.param({"line_items": "a string"}, f"line_items: {LINES_SHAPE}, got a string", id="lines-not-a-list"),
    pytest.param({"line_items": None}, f"line_items: {LINES_SHAPE}, got null", id="lines-null"),
    pytest.param({"line_items": {"item_id": ITEM_A, "quantity": 1}}, f"line_items: {LINES_SHAPE}, got an object", id="lines-one-object"),
    pytest.param({"line_items": []}, "line_items: at least one line is required, each an object with item_id and quantity", id="no-lines"),
    pytest.param({"line_items": ["x"]}, "line_items[0]: must be an object with item_id and quantity, got a string", id="line-is-text"),
    pytest.param({"line_items": [line(), 5]}, "line_items[1]: must be an object with item_id and quantity, got a number", id="second-line-is-a-number"),
    pytest.param({"line_items": [None]}, "line_items[0]: must be an object with item_id and quantity, got null", id="line-is-null"),
    pytest.param({"line_items": [line(price=3)]}, f"line_items[0]: unknown field(s) price; allowed: {ALLOWED}", id="unknown-field"),
    pytest.param({"line_items": [line(price=3, StatusId=5)]}, f"line_items[0]: unknown field(s) StatusId, price; allowed: {ALLOWED}", id="unknown-fields-sorted"),
    pytest.param({"line_items": [{"item_id": ITEM_A}]}, "line_items[0]: missing quantity", id="missing-quantity"),
    pytest.param({"line_items": [{"quantity": 1}]}, "line_items[0]: missing item_id", id="missing-item-id"),
    pytest.param({"line_items": [{}]}, "line_items[0]: missing item_id and quantity", id="missing-both"),
    pytest.param({"line_items": [{"item_id": None, "quantity": None}]}, "line_items[0]: missing item_id and quantity", id="both-null"),
    pytest.param({"line_items": [line(), {"item_id": ITEM_B}]}, "line_items[1]: missing quantity", id="second-line-missing-quantity"),
    pytest.param({"line_items": [{"item_id": "12", "quantity": 1}]}, "line_items[0].item_id: expected a GUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b, got text that is not a GUID", id="item-id-not-a-guid"),
    pytest.param({"line_items": [{"item_id": 12, "quantity": 1}]}, "line_items[0].item_id: expected a GUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b, got a number", id="item-id-number"),
    pytest.param({"line_items": [{"item_id": "", "quantity": 1}]}, "line_items[0].item_id: expected a GUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b, got an empty string", id="item-id-empty"),
    pytest.param({"line_items": [line(), {"item_id": f" {ITEM_B}", "quantity": 1}]}, "line_items[1].item_id: expected a GUID", id="item-id-with-a-space-is-not-trimmed"),
    pytest.param({"line_items": [line(quantity=0)]}, "line_items[0].quantity: must be a number greater than 0", id="quantity-0"),
    pytest.param({"line_items": [line(quantity=-1)]}, "line_items[0].quantity: must be a number greater than 0", id="quantity-negative"),
    pytest.param({"line_items": [line(quantity=float("nan"))]}, "line_items[0].quantity: must be a number greater than 0", id="quantity-nan"),
    pytest.param({"line_items": [line(quantity=float("inf"))]}, "line_items[0].quantity: must be a number greater than 0", id="quantity-infinite"),
    pytest.param({"line_items": [line(quantity=True)]}, "line_items[0].quantity: must be a number greater than 0", id="quantity-true"),
    pytest.param({"line_items": [line(quantity="2")]}, "line_items[0].quantity: must be a number greater than 0", id="quantity-text"),
    pytest.param({"line_items": [line(quantity=10**400)]}, "line_items[0].quantity: must be a number greater than 0", id="quantity-too-large-for-a-float"),
    pytest.param({"line_items": [line(), line(quantity=0)]}, "line_items[1].quantity: must be a number greater than 0", id="second-line-quantity-0"),
    pytest.param({"line_items": [line(unit_price="free")]}, "line_items[0].unit_price: must be a number", id="unit-price-text"),
    pytest.param({"line_items": [line(unit_price=float("nan"))]}, "line_items[0].unit_price: must be a number", id="unit-price-nan"),
    pytest.param({"line_items": [line(unit_price=True)]}, "line_items[0].unit_price: must be a number", id="unit-price-true"),
    pytest.param({"line_items": [line(unit_cost="9")]}, "line_items[0].unit_cost: must be a number", id="unit-cost-text"),
    pytest.param({"line_items": [line(unit_cost=float("-inf"))]}, "line_items[0].unit_cost: must be a number", id="unit-cost-infinite"),
    pytest.param({"line_items": [line(discount_percent=100.01)]}, "line_items[0].discount_percent: must be a number from 0 to 100", id="discount-over-100"),
    pytest.param({"line_items": [line(discount_percent=-0.5)]}, "line_items[0].discount_percent: must be a number from 0 to 100", id="discount-negative"),
    pytest.param({"line_items": [line(discount_percent=True)]}, "line_items[0].discount_percent: must be a number from 0 to 100", id="discount-true"),
    pytest.param({"line_items": [line(discount_percent="10")]}, "line_items[0].discount_percent: must be a number from 0 to 100", id="discount-text"),
    pytest.param({"line_items": [line(discount_percent=float("nan"))]}, "line_items[0].discount_percent: must be a number from 0 to 100", id="discount-nan"),
    pytest.param({"line_items": [line(tax_id=0)]}, f"line_items[0].tax_id: {POSITIVE}, got zero or a negative number", id="tax-id-0"),
    pytest.param({"line_items": [line(tax_id=-3)]}, f"line_items[0].tax_id: {POSITIVE}, got zero or a negative number", id="tax-id-negative"),
    pytest.param({"line_items": [line(tax_id=True)]}, f"line_items[0].tax_id: {POSITIVE}, got a boolean", id="tax-id-true"),
    pytest.param({"line_items": [line(tax_id="2")]}, f"line_items[0].tax_id: {POSITIVE}, got a string", id="tax-id-text"),
    pytest.param({"line_items": [line(tax_id=2.5)]}, f"line_items[0].tax_id: {POSITIVE}, got a decimal number", id="tax-id-decimal"),
    pytest.param({"line_items": [line(tax_id=2**63)]}, f"line_items[0].tax_id: {POSITIVE}, got a number above 9223372036854775807", id="tax-id-beyond-int64"),
    pytest.param({"line_items": [line(tax_id=2, no_tax=True)]}, "line_items[0]: give tax_id or no_tax, not both (tax_id picks one tax, no_tax picks the Tax Exempt rate)", id="tax-id-and-no-tax"),
    pytest.param({"line_items": [line(), line(tax_id=2, no_tax=True)]}, "line_items[1]: give tax_id or no_tax, not both", id="second-line-tax-id-and-no-tax"),
    pytest.param({"line_items": [line(no_tax="yes")]}, "line_items[0].no_tax: must be true or false, got a string", id="no-tax-text"),
    pytest.param({"line_items": [line(no_tax=1)]}, "line_items[0].no_tax: must be true or false, got a number", id="no-tax-number"),
    pytest.param({"line_items": [line(coa_code="")]}, "line_items[0].coa_code: must be text that is not empty or whitespace only", id="coa-empty"),
    pytest.param({"line_items": [line(coa_code="   ")]}, "line_items[0].coa_code: must be text that is not empty or whitespace only", id="coa-blank"),
    pytest.param({"line_items": [line(coa_code=200)]}, "line_items[0].coa_code: must be text that is not empty or whitespace only", id="coa-number"),
    pytest.param({"line_items": [line(description="")]}, "line_items[0].description: must be text that is not empty or whitespace only", id="description-empty"),
    pytest.param({"line_items": [line(description=" \t ")]}, "line_items[0].description: must be text that is not empty or whitespace only", id="description-blank"),
    pytest.param({"line_items": [line(billable_status_id=0)]}, "line_items[0].billable_status_id: must be 1, 2 or 3 (1 Billable, 2 No charge, 3 Non-billable), got 0", id="billable-0"),
    pytest.param({"line_items": [line(billable_status_id=4)]}, "line_items[0].billable_status_id: must be 1, 2 or 3 (1 Billable, 2 No charge, 3 Non-billable), got 4", id="billable-4"),
    pytest.param({"line_items": [line(billable_status_id=-1)]}, "line_items[0].billable_status_id: must be 1, 2 or 3 (1 Billable, 2 No charge, 3 Non-billable), got -1", id="billable-negative"),
    pytest.param({"line_items": [line(billable_status_id=True)]}, "line_items[0].billable_status_id: must be 1, 2 or 3 (1 Billable, 2 No charge, 3 Non-billable), got a boolean", id="billable-true"),
    pytest.param({"line_items": [line(billable_status_id="1")]}, "line_items[0].billable_status_id: must be 1, 2 or 3 (1 Billable, 2 No charge, 3 Non-billable), got a string", id="billable-text"),
    pytest.param({"line_items": [line(billable_status_id=1.0)]}, "line_items[0].billable_status_id: must be 1, 2 or 3 (1 Billable, 2 No charge, 3 Non-billable), got a number", id="billable-decimal"),
    pytest.param({"client_id": 0}, f"client_id: {POSITIVE}, got zero or a negative number", id="client-0"),
    pytest.param({"client_id": -9102}, f"client_id: {POSITIVE}, got zero or a negative number", id="client-negative"),
    pytest.param({"client_id": True}, f"client_id: {POSITIVE}, got a boolean", id="client-true"),
    pytest.param({"client_id": "9102"}, f"client_id: {POSITIVE}, got a string", id="client-text"),
    pytest.param({"client_id": None}, f"client_id: {POSITIVE}, got null", id="client-null"),
    pytest.param({"client_id": 2**63}, f"client_id: {POSITIVE}, got a number above 9223372036854775807", id="client-beyond-int64"),
    pytest.param({"invoice_date": "2026-09-01T00:00:00"}, f"invoice_date: {DAY}", id="invoice-date-naive-datetime"),
    pytest.param({"invoice_date": "2026-09-01T00:00:00Z"}, f"invoice_date: {DAY}", id="invoice-date-instant"),
    pytest.param({"invoice_date": "2026-9-1"}, f"invoice_date: {DAY}", id="invoice-date-unpadded"),
    pytest.param({"invoice_date": "20260901"}, f"invoice_date: {DAY}", id="invoice-date-compact"),
    pytest.param({"invoice_date": "01/09/2026"}, f"invoice_date: {DAY}", id="invoice-date-day-first"),
    pytest.param({"invoice_date": "2026-02-30"}, f"invoice_date: {DAY}", id="invoice-date-that-does-not-exist"),
    pytest.param({"invoice_date": "2026-13-01"}, f"invoice_date: {DAY}", id="invoice-date-month-13"),
    pytest.param({"invoice_date": "today"}, f"invoice_date: {DAY}", id="invoice-date-a-word"),
    pytest.param({"invoice_date": ""}, f"invoice_date: {DAY}", id="invoice-date-empty"),
    pytest.param({"invoice_date": " 2026-09-01"}, f"invoice_date: {DAY}", id="invoice-date-leading-space"),
    pytest.param({"invoice_date": 20260901}, f"invoice_date: {DAY}", id="invoice-date-number"),
    pytest.param({"due_date": "2026-10-15T00:00:00+02:00"}, f"due_date: {DAY}", id="due-date-with-an-offset"),
    pytest.param({"due_date": "next friday"}, f"due_date: {DAY}", id="due-date-words"),
    pytest.param({"due_date": "2027-02-29"}, f"due_date: {DAY}", id="due-date-leap-day-of-a-common-year"),
    pytest.param(
        {"invoice_date": "2026-09-15", "due_date": "2026-09-14"},
        "due_date: 2026-09-14 is before invoice_date 2026-09-15; the due date must be the invoice date or later",
        id="due-a-day-before-the-invoice-date",
    ),
    pytest.param(
        {"invoice_date": "2026-09-15", "due_date": "2025-09-15"},
        "due_date: 2025-09-15 is before invoice_date 2026-09-15",
        id="due-a-year-before",
    ),
    pytest.param({"reference": ""}, "reference: must not be empty or whitespace only", id="reference-empty"),
    pytest.param({"reference": "   "}, "reference: must not be empty or whitespace only", id="reference-blank"),
    pytest.param({"reference": 4471}, "reference: must be text, got a number", id="reference-number"),
    pytest.param(
        {"client_id": 0, "line_items": [], "due_date": "x", "reference": ""},
        f"client_id: {POSITIVE}, got zero or a negative number",
        id="several-problems-the-first-in-the-parameter-order-is-reported",
    ),
    pytest.param(
        {"line_items": [], "due_date": "x"}, "line_items: at least one line is required", id="lines-before-dates"
    ),
]


@pytest.mark.parametrize("tool", BOTH)
@pytest.mark.parametrize("overrides, fragment", LOCAL_ERRORS)
async def test_local_validation_errors_name_the_param_and_send_nothing(client_factory, mock_gorelo, tool, overrides, fragment):
    kwargs = {**GOOD, **overrides, **({"confirm": True} if tool == "create_approved_invoice" else {})}
    with pytest.raises(ToolError) as info:
        await call_create_directly(client_factory, tool, **kwargs)
    assert str(info.value).startswith(fragment) or fragment in str(info.value)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("tool", BOTH)
async def test_a_value_the_text_gave_is_never_echoed_in_a_line_error(client_factory, mock_gorelo, tool):
    kwargs = args_for(tool, line_items=[line(coa_code=" "), line()])
    kwargs["line_items"][0]["description"] = "Confidential client wording"
    with pytest.raises(ToolError) as info:
        await call_create_directly(client_factory, tool, **kwargs)
    assert "Confidential" not in str(info.value)
    with pytest.raises(ToolError) as info:
        await call_create_directly(client_factory, tool, **args_for(tool, line_items=[{"item_id": "SECRET-TEXT", "quantity": 1}]))
    assert "SECRET-TEXT" not in str(info.value) and str(info.value).endswith("got text that is not a GUID")
    assert mock_gorelo.requests == []


SCHEMA_REFUSALS = [
    pytest.param({"line_items": [line(quantity=0)]}, "greater than 0", id="quantity-0"),
    pytest.param({"line_items": [line(quantity=True)]}, "valid number", id="quantity-true"),
    pytest.param({"line_items": [line(quantity="2")]}, "valid number", id="quantity-text"),
    pytest.param({"line_items": [line(discount_percent=101)]}, "less than or equal to 100", id="discount-101"),
    pytest.param({"line_items": [line(discount_percent=-1)]}, "greater than or equal to 0", id="discount-negative"),
    pytest.param({"line_items": [line(unit_price="free")]}, "valid number", id="unit-price-text"),
    pytest.param({"line_items": [line(unit_cost=True)]}, "valid number", id="unit-cost-true"),
    pytest.param({"line_items": [line(tax_id=True)]}, "valid integer", id="tax-id-true"),
    pytest.param({"line_items": [line(tax_id="2")]}, "valid integer", id="tax-id-text"),
    pytest.param({"line_items": [line(tax_id=2.0)]}, "valid integer", id="tax-id-decimal"),
    pytest.param({"line_items": [line(billable_status_id=True)]}, "valid integer", id="billable-true"),
    pytest.param({"line_items": [line(no_tax="true")]}, "valid boolean", id="no-tax-text"),
    pytest.param({"line_items": [line(no_tax=1)]}, "valid boolean", id="no-tax-number"),
    pytest.param({"line_items": [line(price=3)]}, "Extra inputs are not permitted", id="unknown-line-field"),
    pytest.param({"line_items": [{"item_id": ITEM_A}]}, "Field required", id="missing-quantity"),
    pytest.param({"line_items": [{"quantity": 1}]}, "Field required", id="missing-item-id"),
    pytest.param({"line_items": "one line"}, "valid list", id="lines-text"),
    pytest.param({"line_items": [[ITEM_A, 1]]}, "valid dictionary", id="line-is-a-list"),
    pytest.param({"client_id": True}, "valid integer", id="client-true"),
    pytest.param({"client_id": "9102"}, "valid integer", id="client-text"),
    pytest.param({"client_id": 9102.0}, "valid integer", id="client-decimal"),
    pytest.param({"invoice_date": 20260901}, "valid string", id="date-number"),
    pytest.param({"reference": 4471}, "valid string", id="reference-number"),
]


@pytest.mark.parametrize("tool", BOTH)
@pytest.mark.parametrize("overrides, phrase", SCHEMA_REFUSALS)
async def test_the_schema_refuses_a_wrongly_typed_value_with_the_param_name_before_the_tool_runs(
    server, mock_gorelo, tool, overrides, phrase
):
    text = await call_tool_error(server, tool, args_for(tool, **overrides))
    assert next(iter(overrides)) in text and phrase in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("tool", BOTH)
@pytest.mark.parametrize("missing", ["client_id", "line_items"])
async def test_client_id_and_line_items_are_required(server, mock_gorelo, tool, missing):
    arguments = {k: v for k, v in args_for(tool).items() if k != missing}
    assert missing in await call_tool_error(server, tool, arguments)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("tool", BOTH)
async def test_a_date_on_the_same_day_and_a_date_in_the_past_or_future_are_not_refused_locally(server, mock_gorelo, tool):
    route_create(mock_gorelo)
    for dates in (
        {"invoice_date": "1999-12-31", "due_date": "1999-12-31"},
        {"invoice_date": "2099-01-01", "due_date": "2099-12-31"},
    ):
        await call_tool(server, tool, args_for(tool, **dates))
    assert len(mock_gorelo.calls("POST")) == 2


# --------------------------------------------------------------------------
# What Gorelo can answer to the POST and to the read-back
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tool", BOTH)
@pytest.mark.parametrize(
    "property_name, param",
    [
        ("ClientId", "client_id"),
        ("LineItems", "line_items"),
        ("InvoiceDate", "invoice_date"),
        ("DueDate", "due_date"),
        ("Reference", "reference"),
        ("clientid", "client_id"),  # names are matched case-insensitively
    ],
)
async def test_a_gorelo_400_names_the_snake_case_param(server, mock_gorelo, tool, property_name, param):
    mock_gorelo.on("POST", INVOICES, error_envelope(400, [("070101", "Rejected by Gorelo.", property_name)]))
    text = await call_tool_error(server, tool, args_for(tool))
    assert text == trace(f"Gorelo rejected {tool} (HTTP 400, code 070101): {param}: Rejected by Gorelo.")
    assert mock_gorelo.calls("GET") == []  # nothing to read back
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize("tool", BOTH)
@pytest.mark.parametrize(
    "property_name, label",
    [
        ("LineItems[0].ItemId", "line_items (item 1, ItemId)"),
        ("LineItems[1].Quantity", "line_items (item 2, Quantity)"),
        ("LineItems[2].TaxId", "line_items (item 3, TaxId)"),
        ("LineItems[0].DiscountPercent", "line_items (item 1, DiscountPercent)"),
        ("LineItems[3].BillableStatusId", "line_items (item 4, BillableStatusId)"),
    ],
)
async def test_a_gorelo_error_inside_a_line_names_the_line_and_the_field(server, mock_gorelo, tool, property_name, label):
    mock_gorelo.on("POST", INVOICES, error_envelope(400, [("070101", "The item does not exist.", property_name)]))
    text = await call_tool_error(server, tool, args_for(tool))
    assert text == trace(f"Gorelo rejected {tool} (HTTP 400, code 070101): {label}: The item does not exist.")


@pytest.mark.parametrize("tool", BOTH)
async def test_an_unknown_client_is_a_404_that_names_client_id(server, mock_gorelo, tool):
    mock_gorelo.on("POST", INVOICES, error_envelope(404, [("070401", "Client not found.", "ClientId")]))
    text = await call_tool_error(server, tool, args_for(tool))
    assert text == trace(f"Gorelo rejected {tool} (HTTP 404, code 070401): client_id: Client not found.")


@pytest.mark.parametrize("tool", BOTH)
async def test_a_409_is_reported_with_its_reason_and_is_not_an_unconfirmed_write(server, mock_gorelo, tool):
    mock_gorelo.on("POST", INVOICES, error_envelope(409, [("070901", "The client is on hold for billing.")]))
    text = await call_tool_error(server, tool, args_for(tool))
    assert text == trace(f"Gorelo rejected {tool} (HTTP 409, code 070901): The client is on hold for billing.")
    assert "may or may not" not in text and "Verify with a read" not in text  # a 4xx applied nothing
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize("tool", BOTH)
async def test_every_notification_is_shown_and_the_status_id_keeps_gorelos_own_name(server, mock_gorelo, tool):
    notes = [("070101", "Bad date.", "DueDate"), ("070101", "Bad status.", "StatusId"), ("070101", "No tax.", "LineItems[0].TaxId")]
    mock_gorelo.on("POST", INVOICES, error_envelope(400, notes))
    text = await call_tool_error(server, tool, args_for(tool))
    assert "due_date: Bad date.; StatusId: Bad status.; line_items (item 1, TaxId): No tax." in text


@pytest.mark.parametrize("tool", BOTH)
async def test_a_missing_scope_reads_as_a_missing_scope(server, mock_gorelo, tool):
    mock_gorelo.on("POST", INVOICES, error_envelope(403, [("080203", "API key does not have 'Billing' scope")]))
    text = await call_tool_error(server, tool, args_for(tool))
    assert "the API key does not have the 'Billing' scope" in text and "may or may not" not in text


@pytest.mark.parametrize("tool", BOTH)
@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")], ids=["timeout", "connection"])
async def test_a_post_that_is_not_answered_is_never_retried_and_says_to_verify(server, mock_gorelo, tool, failure):
    mock_gorelo.on("POST", INVOICES, failure)
    text = await call_tool_error(server, tool, args_for(tool))
    assert text.startswith(f"Gorelo did not confirm {tool} (")
    assert "The change may or may not have been applied. Verify with a read before retrying." in text
    assert "retrying is safe" not in text
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("POST", INVOICES)]  # one POST, no read-back, no retry


@pytest.mark.parametrize("tool", BOTH)
async def test_a_5xx_answer_to_the_post_may_have_created_the_invoice(server, mock_gorelo, tool):
    mock_gorelo.on("POST", INVOICES, error_envelope(500, [("070500", "Accounting sync failed.")]))
    text = await call_tool_error(server, tool, args_for(tool))
    assert f"Gorelo rejected {tool} (HTTP 500, code 070500): Accounting sync failed." in text
    assert "Gorelo may have applied the change before failing. Verify with a read before retrying." in text
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize("tool", BOTH)
async def test_a_200_that_is_not_an_envelope_may_have_created_the_invoice(server, mock_gorelo, tool):
    mock_gorelo.on("POST", INVOICES, httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"}))
    text = await call_tool_error(server, tool, args_for(tool))
    assert text.startswith(f"Gorelo returned an unexpected response for {tool}")
    assert "the write may have been applied" in text or "may or may not have been applied" in text
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize("tool", BOTH)
@pytest.mark.parametrize(
    "data, problem",
    [
        pytest.param(None, "Data is null, not an object with an Id", id="null"),
        pytest.param(True, "Data is a boolean, not an object with an Id", id="true"),
        pytest.param({}, "Data is an object without an Id", id="empty-object"),
        pytest.param({"Ok": True}, "Data is an object without an Id", id="object-without-id"),
        pytest.param({"Id": ""}, "Data.Id is blank", id="blank-id"),
        pytest.param({"Id": None}, "Data.Id is null", id="null-id"),
        pytest.param({"Id": 0}, "Data.Id is zero or negative", id="zero-id"),
        pytest.param({"Id": False}, "Data.Id is a boolean", id="boolean-id"),
        pytest.param([{"Id": NEW_ID}], "Data is a list of 1 item, not an object with an Id", id="list"),
    ],
)
async def test_a_create_answer_without_a_usable_id_is_not_taken_for_a_clean_success(server, mock_gorelo, tool, data, problem):
    mock_gorelo.on("POST", INVOICES, envelope(data))
    text = await call_tool_error(server, tool, args_for(tool))
    assert text.startswith(
        f"Gorelo returned an unexpected response for {tool}: POST /v1/invoices: Gorelo reported success but the "
        f"answer carries no usable Id for the record ({problem})"
    )
    assert "the write may have been applied, so verify it with a read before repeating it" in text
    assert mock_gorelo.calls("GET") == [] and len(mock_gorelo.calls("POST")) == 1  # never repeated


# (answer, the reason in the warning, how many times the read is asked): a refused connection is a read's one retry (1.4.2).
REREAD_FAILURES = [
    pytest.param(error_envelope(404, [("070401", "Invoice not found.")]), "GET /v1/invoices/{invoiceId} answered HTTP 404", 1, id="404"),
    pytest.param(error_envelope(500, [("070500", "Read model unavailable.")]), "GET /v1/invoices/{invoiceId} answered HTTP 500", 1, id="500"),
    pytest.param(error_envelope(403, [("080203", "API key does not have 'Billing' scope")]), "GET /v1/invoices/{invoiceId} answered HTTP 403", 1, id="scope"),
    pytest.param(httpx.ReadTimeout("slow"), "GET /v1/invoices/{invoiceId} timed out", 1, id="timeout"),
    pytest.param(httpx.ConnectError("refused"), "the connection to Gorelo failed during GET /v1/invoices/{invoiceId}", 2, id="connection"),
    pytest.param(envelope([invoice_detail()]), "GET /v1/invoices/{invoiceId} returned a list instead of the record", 1, id="list"),
    pytest.param(envelope("INV-1042"), "GET /v1/invoices/{invoiceId} returned a string instead of the record", 1, id="text"),
    pytest.param(
        envelope(None), "GET /v1/invoices/{invoiceId}: Gorelo reported success but Data is null; refusing to guess", 1, id="null"
    ),
]


@pytest.mark.parametrize("tool", BOTH)
@pytest.mark.parametrize("answer, reason, reads", REREAD_FAILURES)
async def test_a_failed_reread_after_a_successful_create_returns_the_id_and_a_warning_never_a_second_post(
    server, mock_gorelo, tool, answer, reason, reads
):
    mock_gorelo.on("POST", INVOICES, envelope({"Id": NEW_ID}))
    mock_gorelo.on("GET", NEW_PATH, answer)
    result = await call_tool(server, tool, args_for(tool))
    assert set(result) == {"Id", "warning"} and result["Id"] == NEW_ID
    assert result["warning"].startswith(f"the write succeeded; re-reading it failed: {reason}")
    assert result["warning"].endswith("Do not repeat the write; read it again later.")
    assert len(mock_gorelo.calls("POST")) == 1  # the write is never repeated
    assert len(mock_gorelo.calls("GET")) == reads  # the read is asked once more only after a refused connection


@pytest.mark.parametrize("tool", BOTH)
async def test_an_id_that_cannot_name_an_invoice_to_read_gives_a_warning_not_a_second_post(server, mock_gorelo, tool):
    mock_gorelo.on("POST", INVOICES, envelope({"Id": "not-a-guid"}))
    result = await call_tool(server, tool, args_for(tool))
    assert result["Id"] == "not-a-guid" and result["warning"].startswith("the write succeeded; re-reading it failed: ")
    assert "Do not repeat the write" in result["warning"]
    assert mock_gorelo.calls("GET") == [] and len(mock_gorelo.calls("POST")) == 1


@pytest.mark.parametrize("tool", BOTH)
async def test_a_create_makes_exactly_one_post_and_one_read_and_nothing_else(server, mock_gorelo, tool):
    route_create(mock_gorelo)
    await call_tool(server, tool, args_for(tool, line_items=[line(), line(), line()]))
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("POST", INVOICES), ("GET", NEW_PATH)]
    assert not any(r.path.endswith("/pdf") or r.method in ("DELETE", "PATCH") for r in mock_gorelo.requests)


# --------------------------------------------------------------------------
# create_approved_invoice: the gate (it pushes to accounting, so nothing leaves before confirm=true)
# --------------------------------------------------------------------------

EFFECT = (
    "Approving on create pushes the invoice to the connected accounting system at once, and nothing takes that back: "
    "voiding it with delete_invoice voids it in Gorelo ONLY, so the copy in the accounting system stays open (observed with "
    "Xero) and the user must void it there too."
)


def refusal(lines, client=CLIENT, recipients=0):
    action = f"create an APPROVED invoice for client {client} with {lines} line{'' if lines == 1 else 's'}"
    mail = ""
    if recipients:
        mail = f" It may also email the invoice to the {recipients} address{'' if recipients == 1 else 'es'} in recipient_emails."
    return (
        f"confirm: refusing to {action} without confirm=true. {EFFECT}{mail} Nothing has been sent to Gorelo. "
        f"Call again with confirm=true if you really want to {action}."
    )


async def test_approved_refuses_without_confirm_and_makes_no_http_call(server, mock_gorelo):
    for extra in ({}, {"confirm": False}):
        text = await call_tool_error(server, "create_approved_invoice", {**GOOD, **extra})
        assert text == refusal(1)
    assert mock_gorelo.requests == []


async def test_the_refusal_says_how_many_lines_and_which_client_and_how_many_recipients(server, mock_gorelo):
    many = {"client_id": 9101, "line_items": [line(), line(), line()]}
    assert await call_tool_error(server, "create_approved_invoice", many) == refusal(3, client=9101)
    mailed = {**GOOD, "recipient_emails": ["ops@example.test"]}
    assert await call_tool_error(server, "create_approved_invoice", mailed) == refusal(1, recipients=1)
    mailed_two = {**GOOD, "recipient_emails": ["ops@example.test", "finance@example.test"]}
    text = await call_tool_error(server, "create_approved_invoice", mailed_two)
    assert text == refusal(1, recipients=2)
    assert "example.test" not in text  # an address is personal data: counted, never echoed
    assert mock_gorelo.requests == []


async def test_every_text_read_before_approving_or_voiding_says_the_void_is_in_gorelo_only(server, mock_gorelo):
    """Gorelo pushes an Approved invoice to Xero, and the DELETE that voids it
    (StatusId 4, get_invoice shows Void) leaves the Xero copy open. Nothing in this server takes
    the push back, so every text the model reads before it approves or voids says so: the docstrings (read at the start
    of every conversation) and the refusals without confirm (read at the moment the user is asked)."""
    tools = await list_tools(server)
    said = {name: flat(tool_def(name, tools).description) for name in ("create_approved_invoice", "delete_invoice")}
    for name, text in said.items():
        assert "Gorelo ONLY" in text and "accounting system" in text and "stays open" in text, name
        assert "observed with Xero" in text and "who must void it there too" in text, name
        assert "cannot be undone except by voiding" not in text and "except by voiding it" not in text, name  # the old claim
    assert said["delete_invoice"].index("Gorelo ONLY") < said["delete_invoice"].index("Ask the user first")
    assert said["create_approved_invoice"].index("Gorelo ONLY") < said["create_approved_invoice"].index("Ask the user first")
    # the refusals (zero HTTP calls) name the same effect, for the approval and for the void of an Approved invoice
    approve = await call_tool_error(server, "create_approved_invoice", GOOD)
    void = await call_tool_error(server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Approved"})
    for refusal_text in (approve, void):
        assert "in Gorelo ONLY" in refusal_text and "stays open" in refusal_text, refusal_text
        assert "the user must void it there too" in refusal_text and "observed with Xero" in refusal_text
        assert "cannot be undone except by voiding" not in refusal_text
    # and the module text records the observed behavior
    for fragment in ("happens in Gorelo ONLY", "stays open (observed with Xero)", "can keep its AmountDue"):
        assert fragment in " ".join(invoices.__doc__.split()), fragment
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("confirm", ["true", "True", "yes", "on", "1", 1, 0, 1.0, None, "false", [True]])
async def test_confirm_is_strict_text_and_numbers_never_count_as_yes(server, mock_gorelo, confirm):
    text = await call_tool_error(server, "create_approved_invoice", {**GOOD, "confirm": confirm})
    assert "confirm" in text and "valid boolean" in text
    assert mock_gorelo.requests == []


async def test_a_bad_parameter_is_reported_before_the_user_is_asked_to_confirm(server, mock_gorelo):
    # asking the user to approve something that would be refused anyway wastes their answer
    text = await call_tool_error(server, "create_approved_invoice", {**GOOD, "due_date": "2026-13-01"})
    assert text == f"due_date: {DAY}"
    text = await call_tool_error(server, "create_approved_invoice", {"client_id": CLIENT, "line_items": []})
    assert text == "line_items: at least one line is required, each an object with item_id and quantity"
    assert mock_gorelo.requests == []


async def test_with_confirm_true_it_posts_status_5_and_reads_the_invoice_back(server, mock_gorelo):
    detail = invoice_detail(status=(5, "Approved"), ExternalId="XERO-77", Total=300.0)
    route_create(mock_gorelo, detail)
    args = {
        "client_id": CLIENT,
        "line_items": [line(quantity=2, unit_price=150, tax_id=2)],
        "invoice_date": "2026-09-15",
        "due_date": "2026-10-15",
        "reference": "PO-4471",
        "confirm": True,
    }
    result = await call_tool(server, "create_approved_invoice", args)
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("POST", INVOICES), ("GET", NEW_PATH)]
    post = mock_gorelo.requests[0]
    assert post.json == {
        "ClientId": CLIENT,
        "StatusId": 5,
        "LineItems": [{"ItemId": ITEM_A, "Quantity": 2, "UnitPrice": 150, "TaxId": 2}],
        "InvoiceDate": "2026-09-15T00:00:00Z",
        "DueDate": "2026-10-15T00:00:00Z",
        "Reference": "PO-4471",
    }
    assert "confirm" not in post.json and "RecipientEmails" not in post.json  # confirm is the tool's, never Gorelo's
    assert post.query == {}
    assert result == detail and result["Status"] == {"Id": 5, "Name": "Approved"} and result["ExternalId"] == "XERO-77"


async def test_recipient_emails_are_sent_stripped_in_order_and_only_when_given(server, mock_gorelo):
    route_create(mock_gorelo)
    mails = [" ops@example.test ", "finance@example.test", "ops@example.test"]
    await call_tool(server, "create_approved_invoice", {**GOOD, "recipient_emails": mails, "confirm": True})
    assert mock_gorelo.requests[0].json["RecipientEmails"] == ["ops@example.test", "finance@example.test", "ops@example.test"]
    await call_tool(server, "create_approved_invoice", {**GOOD, "confirm": True})
    assert "RecipientEmails" not in mock_gorelo.calls("POST")[1].json


async def test_the_draft_tool_has_no_recipients_so_it_cannot_email_anyone(server, mock_gorelo):
    for extra in ({"recipient_emails": ["ops@example.test"]}, {"RecipientEmails": ["ops@example.test"]}, {"email": "ops@example.test"}):
        text = await call_tool_error(server, "create_invoice", {**GOOD, **extra})
        assert next(iter(extra)) in text
    assert mock_gorelo.requests == []


async def test_a_total_of_exactly_zero_comes_back_as_paid_and_the_tool_does_not_hide_it(server, mock_gorelo):
    free = line_row(UnitPrice=0.0, Amount=0.0, TaxAmount=0.0)
    detail = invoice_detail(status=(3, "Paid"), SubTotal=0.0, Total=0.0, AmountDue=0.0, AmountPaid=0.0, LineItems=[free])
    route_create(mock_gorelo, detail)
    result = await call_tool(
        server, "create_approved_invoice", {**GOOD, "line_items": [line(unit_price=0)], "confirm": True}
    )
    assert mock_gorelo.requests[0].json["StatusId"] == 5  # what was sent
    assert result["Status"] == {"Id": 3, "Name": "Paid"} and result["Total"] == 0.0  # what Gorelo made of it


async def test_create_approved_invoice_does_not_exist_unless_deletes_are_enabled(server_factory, mock_gorelo):
    off = server_factory()
    assert "create_approved_invoice" not in {t.name for t in await list_tools(off)}
    text = await call_tool_error(off, "create_approved_invoice", {**GOOD, "confirm": True})
    assert "unknown tool" in text.lower()
    assert mock_gorelo.requests == []
    on = server_factory(destructive=True)
    assert "create_approved_invoice" in {t.name for t in await list_tools(on)}


EMAIL_SHAPE = "expected exactly one email address such as name@example.com (no display name, no list of addresses)"
EMAIL_ERRORS = [
    pytest.param([], "recipient_emails: must not be an empty list; omit it to name no recipients", id="empty-list"),
    pytest.param("ops@example.test", "recipient_emails: expected a list of email addresses such as ['a@example.com']", id="one-text"),
    pytest.param({"a": "ops@example.test"}, "recipient_emails: expected a list of email addresses such as ['a@example.com']", id="an-object"),
    pytest.param([""], f"recipient_emails[0]: {EMAIL_SHAPE}", id="empty-address"),
    pytest.param(["   "], f"recipient_emails[0]: {EMAIL_SHAPE}", id="blank-address"),
    pytest.param(["ops@example.test", "no-at-sign"], f"recipient_emails[1]: {EMAIL_SHAPE}", id="second-has-no-at-sign"),
    pytest.param(["Ops <ops@example.test>"], f"recipient_emails[0]: {EMAIL_SHAPE}", id="display-name"),
    pytest.param(["ops@example.test, finance@example.test"], f"recipient_emails[0]: {EMAIL_SHAPE}", id="two-in-one-comma"),
    pytest.param(["ops@example.test;finance@example.test"], f"recipient_emails[0]: {EMAIL_SHAPE}", id="two-in-one-semicolon"),
    pytest.param(["ops @example.test"], f"recipient_emails[0]: {EMAIL_SHAPE}", id="space-inside"),
    pytest.param(["@example.test"], f"recipient_emails[0]: {EMAIL_SHAPE}", id="no-local-part"),
    pytest.param(["ops@"], f"recipient_emails[0]: {EMAIL_SHAPE}", id="no-domain"),
    pytest.param(["ops@@example.test"], f"recipient_emails[0]: {EMAIL_SHAPE}", id="two-at-signs"),
    pytest.param([5], f"recipient_emails[0]: {EMAIL_SHAPE}", id="number"),
    pytest.param([None], f"recipient_emails[0]: {EMAIL_SHAPE}", id="null"),
    pytest.param([True], f"recipient_emails[0]: {EMAIL_SHAPE}", id="true"),
]


@pytest.mark.parametrize("emails, fragment", EMAIL_ERRORS)
async def test_recipient_emails_errors_name_the_param_and_send_nothing(client_factory, mock_gorelo, emails, fragment):
    with pytest.raises(ToolError) as info:
        await call_create_directly(client_factory, "create_approved_invoice", **GOOD, recipient_emails=emails, confirm=True)
    assert str(info.value) == fragment
    assert mock_gorelo.requests == []


async def test_a_bad_address_is_never_echoed_in_the_error(client_factory, mock_gorelo):
    for bad in ("secret-person-at-example", "Secret Person <secret@example.test>"):
        with pytest.raises(ToolError) as info:
            await call_create_directly(
                client_factory, "create_approved_invoice", **GOOD, recipient_emails=["ops@example.test", bad], confirm=True
            )
        text = str(info.value)
        assert "ecret" not in text and "Person" not in text and "ops@" not in text  # neither the bad one nor its neighbour
        assert text == f"recipient_emails[1]: {EMAIL_SHAPE}"
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "emails, phrase",
    [("ops@example.test", "valid list"), ([5], "valid string"), ([["a@example.test"]], "valid string"), ({"a": 1}, "valid list")],
)
async def test_the_schema_refuses_recipient_emails_of_the_wrong_type(server, mock_gorelo, emails, phrase):
    text = await call_tool_error(server, "create_approved_invoice", {**GOOD, "recipient_emails": emails, "confirm": True})
    assert "recipient_emails" in text and phrase in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "property_name, label",
    [("RecipientEmails", "recipient_emails"), ("RecipientEmails[1]", "recipient_emails"), ("recipientemails", "recipient_emails")],
)
async def test_a_gorelo_error_about_a_recipient_names_recipient_emails(server, mock_gorelo, property_name, label):
    mock_gorelo.on("POST", INVOICES, error_envelope(400, [("070101", "Not an email address.", property_name)]))
    text = await call_tool_error(server, "create_approved_invoice", {**GOOD, "recipient_emails": ["a@example.test"], "confirm": True})
    assert text == trace(f"Gorelo rejected create_approved_invoice (HTTP 400, code 070101): {label}: Not an email address.")


async def test_a_confirmed_approval_that_times_out_is_not_repeated_and_the_text_says_to_look_first(server, mock_gorelo):
    mock_gorelo.on("POST", INVOICES, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "create_approved_invoice", {**GOOD, "confirm": True})
    assert text.startswith("Gorelo did not confirm create_approved_invoice (the request timed out).")
    assert "Verify with a read before retrying." in text
    assert len(mock_gorelo.requests) == 1
    doc = flat(inspect.getdoc(invoices.create_approved_invoice))
    assert 'After a "did not confirm" error check list_invoices first' in doc


def test_the_gated_tool_is_declared_destructive_and_the_draft_tool_is_not():
    draft = next(s for s in REGISTRY.specs if s.name == "create_invoice")
    approved = next(s for s in REGISTRY.specs if s.name == "create_approved_invoice")
    assert (draft.toolset, draft.kind, draft.destructive_hint) == ("billing", "write", False)
    assert (approved.toolset, approved.kind, approved.destructive_hint) == ("billing", "destructive", True)
    parameter = inspect.signature(approved.fn).parameters["confirm"]
    assert parameter.default is False and ("StrictBool" in repr(parameter.annotation) or "Strict(" in repr(parameter.annotation))
    assert "confirm" not in inspect.signature(draft.fn).parameters


async def test_recipient_addresses_references_and_line_text_never_reach_the_logs(server, mock_gorelo, caplog):
    route_create(mock_gorelo)
    args = {
        **GOOD,
        "line_items": [line(description="SECRET-LINE-WORDING", coa_code="SECRET-COA")],
        "reference": "SECRET-REFERENCE",
        "recipient_emails": ["secret-person@example.test"],
        "confirm": True,
    }
    with caplog.at_level(logging.DEBUG):
        await call_tool(server, "create_approved_invoice", args)
        await call_tool(server, "create_invoice", {k: v for k, v in args.items() if k not in ("recipient_emails", "confirm")})
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "create_approved_invoice" in logged and "create_invoice" in logged  # the client did log both calls ...
    for secret in ("SECRET-LINE-WORDING", "SECRET-COA", "SECRET-REFERENCE", "secret-person", "example.test", ITEM_A):
        assert secret not in logged, secret  # ... without a single value of the request
    assert "LineItems" in logged and "RecipientEmails" in logged  # field names are fine


# --------------------------------------------------------------------------
# 1.5.0 (contract 6c30770dffc0): no_tax on create, update_invoice, approve_invoice, list_payments, get_payment
# --------------------------------------------------------------------------

L1, L2, L3 = uid(31), uid(32), uid(33)
PATCH_PATH = f"/v1/invoices/{INVOICE_ID}"
TAXES = "/v1/taxes"


def lines3():
    return [line_row(Id=L1, Name="One"), line_row(Id=L2, Name="Two"), line_row(Id=L3, Name="Three")]


def open_invoice(status=(1, "Draft"), lines=None, **extra):
    return invoice_detail(status=status, invoice_id=INVOICE_ID, LineItems=lines if lines is not None else lines3(), **extra)


def route_update(mock, before=None, after=None, patch=None):
    """GET answers `before` the first time and `after` the second; PATCH answers `patch` (default `after`)."""
    before = before if before is not None else open_invoice()
    after = after if after is not None else before
    mock.on("GET", PATCH_PATH, in_order(envelope(before), envelope(after)))
    answer = patch if patch is not None else after
    mock.on("PATCH", PATCH_PATH, answer if "IsSuccess" in answer else envelope(answer))  # a ready-made error envelope passes


def sent_patch(mock):
    (patch,) = mock.calls("PATCH")
    return patch.json


def test_a_no_tax_line_takes_the_resolved_id_and_never_builds_without_one():
    wire = invoices._invoice_line("line_items[0]", {"item_id": ITEM_A, "quantity": 1, "no_tax": True}, EXEMPT_ID)
    assert wire == {"ItemId": ITEM_A, "Quantity": 1, "TaxId": EXEMPT_ID}
    with pytest.raises(RuntimeError, match="no_tax_id missing"):
        invoices._invoice_line("line_items[0]", {"item_id": ITEM_A, "quantity": 1, "no_tax": True})
    assert not hasattr(invoices, "_no_tax_wire")  # the null is gone: it meant "the app's tax" (measured live 2026-10-30)
    for spec in REGISTRY.specs:
        if spec.name in (*BOTH, "update_invoice"):
            assert "GET /v1/taxes" in spec.ops  # the lookup of the Tax Exempt rate


async def test_the_no_tax_text_says_the_tax_exempt_rule_and_drops_the_hedge(server, mock_gorelo):
    tools = await list_tools(server)
    for name in ("create_invoice", "create_approved_invoice"):
        shown = tool_def(name, tools).inputSchema["properties"]["line_items"]["items"]["properties"]
        assert shown["no_tax"]["description"] == invoices.NO_TAX_TEXT
    assert invoices.NO_TAX_TEXT == (
        "true: bill the line with the account's Tax Exempt rate (code or name 'Tax Exempt', 0%), looked up in list_taxes; "
        "refused when there is not exactly one. Not with tax_id."
    )
    assert len(invoices.NO_TAX_TEXT) <= 220
    for hedged in ("Gorelo then applies", "item's or default tax", "send no tax rate"):
        assert hedged not in invoices.NO_TAX_TEXT
    for fn in (invoices.create_invoice, invoices.create_approved_invoice, invoices.update_invoice):
        assert "no tax rate sent" not in flat(inspect.getdoc(fn)) and "only sends no rate" not in flat(inspect.getdoc(fn))


# ---- update_invoice: the request ------------------------------------------------------------------------------


async def test_update_invoice_reads_the_invoice_patches_only_the_header_fields_given_and_reads_it_back(server, mock_gorelo):
    after = open_invoice(Reference="PO-1", InvoiceDate="2026-09-02", DueDate="2026-09-20")
    route_update(mock_gorelo, after=after)
    result = await call_tool(
        server, "update_invoice",
        {"invoice_id": INVOICE_ID.upper(), "invoice_date": "2026-09-02", "due_date": "2026-09-20", "reference": "PO-1"},
    )
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", PATCH_PATH), ("PATCH", PATCH_PATH), ("GET", PATCH_PATH)]
    assert sent_patch(mock_gorelo) == {
        "InvoiceDate": "2026-09-02T00:00:00Z", "DueDate": "2026-09-20T00:00:00Z", "Reference": "PO-1",
    }  # no LineItems, no StatusId
    assert result == after and mock_gorelo.requests[1].query == {}


async def test_the_template_ids_and_the_branding_theme_go_out_under_their_spec_names(server, mock_gorelo):
    theme = uid(77)
    route_update(mock_gorelo, after=open_invoice(InvoiceTemplateId=12, InvoiceEmailTemplateId=4, BrandingThemeId=theme))
    await call_tool(
        server, "update_invoice",
        {"invoice_id": INVOICE_ID, "invoice_template_id": 12, "invoice_email_template_id": 4, "branding_theme_id": theme.upper()},
    )
    assert sent_patch(mock_gorelo) == {"InvoiceTemplateId": 12, "InvoiceEmailTemplateId": 4, "BrandingThemeId": theme}


async def test_clear_fields_sends_an_explicit_null_for_the_four_clearable_fields_only(server, mock_gorelo):
    route_update(mock_gorelo, after=open_invoice(Reference=None, BrandingThemeId=None))
    names = ["reference", "invoice_template_id", "invoice_email_template_id", "branding_theme_id"]
    await call_tool(server, "update_invoice", {"invoice_id": INVOICE_ID, "clear_fields": names})
    assert sent_patch(mock_gorelo) == {
        "Reference": None, "InvoiceTemplateId": None, "InvoiceEmailTemplateId": None, "BrandingThemeId": None,
    }
    assert b'"Reference":null' in mock_gorelo.calls("PATCH")[0].content.replace(b" ", b"")


async def test_a_cleared_template_may_read_back_as_the_providers_default_and_is_not_an_error(server, mock_gorelo):
    route_update(mock_gorelo, after=open_invoice(InvoiceTemplateId=1, Reference=None))  # "the default applies"
    result = await call_tool(server, "update_invoice", {"invoice_id": INVOICE_ID, "clear_fields": ["invoice_template_id", "reference"]})
    assert result["InvoiceTemplateId"] == 1


@pytest.mark.parametrize(
    "args, fragment",
    [
        ({}, "nothing to change"),
        ({"reference": "x", "clear_fields": ["reference"]}, "reference: cannot be given a value and cleared in the same call"),
        ({"reference": "  "}, 'reference: must not be blank; to remove it pass clear_fields=["reference"]'),
        ({"invoice_date": "2026-09-01T00:00:00Z"}, "invoice_date: expected a calendar date as YYYY-MM-DD"),
        ({"due_date": "soon"}, "due_date: expected a calendar date as YYYY-MM-DD"),
        ({"invoice_date": "2026-09-10", "due_date": "2026-09-09"}, "due_date: 2026-09-09 is before invoice_date 2026-09-10"),
        ({"invoice_template_id": 0}, "invoice_template_id"),
        ({"branding_theme_id": "not-a-guid"}, "branding_theme_id: expected a GUID"),
        ({"clear_fields": []}, "clear_fields: must not be an empty list"),
        ({"clear_fields": ["invoice_date"]}, "clear_fields"),
        ({"update_lines": []}, "update_lines: must not be an empty list"),
        ({"add_lines": []}, "add_lines: at least one line is required"),
        ({"remove_line_ids": []}, "remove_line_ids: must not be an empty list"),
    ],
)
async def test_update_invoice_local_errors_name_the_param_and_send_nothing(client_factory, mock_gorelo, args, fragment):
    with pytest.raises((ToolError, ValueError)) as info:
        await call_directly(client_factory, invoices.update_invoice, invoice_id=INVOICE_ID, **args)
    assert fragment in str(info.value)
    assert mock_gorelo.requests == []


async def test_a_bad_invoice_id_names_invoice_id_and_sends_nothing(server, mock_gorelo):
    text = await call_tool_error(server, "update_invoice", {"invoice_id": "INV-1042", "reference": "x"})
    assert "invoice_id" in text and mock_gorelo.requests == []


@pytest.mark.parametrize(
    "held, args, fragment",
    [
        ({"InvoiceDate": "2026-09-10", "DueDate": "2026-09-30"}, {"due_date": "2026-09-01"}, "due_date: 2026-09-01 is before the invoice date 2026-09-10"),
        (
            {"InvoiceDate": "2026-09-01", "DueDate": "2026-09-05"},
            {"invoice_date": "2026-09-10"},
            "invoice_date: 2026-09-10 is after the invoice's due date 2026-09-05",
        ),
    ],
)
async def test_the_due_date_is_checked_against_the_date_the_invoice_already_has(server, mock_gorelo, held, args, fragment):
    route_update(mock_gorelo, before=open_invoice(**held))
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, **args})
    assert fragment in text
    assert mock_gorelo.calls("PATCH") == []


# ---- update_invoice: only Draft and Submitted ---------------------------------------------------------------------


@pytest.mark.parametrize("status", [(1, "Draft"), (2, "Submitted")])
async def test_draft_and_submitted_invoices_can_be_changed(server, mock_gorelo, status):
    route_update(mock_gorelo, before=open_invoice(status), after=open_invoice(status, Reference="r"))
    result = await call_tool(server, "update_invoice", {"invoice_id": INVOICE_ID, "reference": "r"})
    assert result["Status"]["Id"] == status[0] and len(mock_gorelo.calls("PATCH")) == 1


@pytest.mark.parametrize("status", [(3, "Paid"), (4, "Void"), (5, "Approved"), (6, "Deleted"), (99, "Other")])
async def test_any_other_status_is_refused_before_a_patch_and_the_message_names_the_status_and_the_two_ids(server, mock_gorelo, status):
    route_update(mock_gorelo, before=open_invoice(status))
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "reference": "r"})
    assert text.startswith("invoice_id: INV-1042 has status ") and f"(id {status[0]})" in text
    assert "update_invoice changes only Draft (1) and Submitted (2) invoices." in text and "Nothing was changed." in text
    assert [r.method for r in mock_gorelo.requests] == ["GET"]  # one read, never a PATCH


async def test_an_invoice_read_without_a_status_is_a_shape_error_and_nothing_is_written(server, mock_gorelo):
    row = open_invoice()
    row["Status"] = None
    route_update(mock_gorelo, before=row)
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "reference": "r"})
    assert "no readable Status.Id" in text and mock_gorelo.calls("PATCH") == []


async def test_update_invoice_cannot_send_a_status_by_construction(server):
    spec = next(s for s in REGISTRY.specs if s.name == "update_invoice")
    assert "StatusId" not in set(spec.field_map.values()) and "StatusId" not in invoices.UPDATE_FIELDS.values()
    assert not {"status_id", "status", "approve", "approved"} & set(inspect.signature(spec.fn).parameters)
    tool = tool_def("update_invoice", await list_tools(server))
    assert not {"status_id", "status", "approve", "approved", "confirm"} & set(tool.inputSchema["properties"])
    source = inspect.getsource(spec.fn)
    assert "APPROVED_STATUS_ID" not in source and '"StatusId":' not in source and "StatusId=" not in source
    # the line models refuse a status too
    assert "status_id" not in invoices.InvoiceLineChange.model_fields and "status_id" not in invoices.InvoiceLine.model_fields


# ---- update_invoice: lines -----------------------------------------------------------------------------------------


async def test_the_lines_the_caller_does_not_mention_are_kept_by_id_and_new_lines_are_appended(server, mock_gorelo):
    new_line = line_row(Id=uid(34), item_id=ITEM_B, Quantity=2.0, UnitPrice=5.0)
    route_update(mock_gorelo, after=open_invoice(lines=[*lines3(), new_line]))
    await call_tool(
        server, "update_invoice",
        {"invoice_id": INVOICE_ID, "add_lines": [{"item_id": ITEM_B.replace("-", ""), "quantity": 2, "unit_price": 5}]},
    )
    assert sent_patch(mock_gorelo) == {
        "LineItems": [{"Id": L1}, {"Id": L2}, {"Id": L3}, {"ItemId": ITEM_B, "Quantity": 2, "UnitPrice": 5}]
    }


async def test_update_lines_change_a_line_in_place_and_every_other_line_stays_as_just_its_id(server, mock_gorelo):
    changed = line_row(
        Id=L2, Name="Two", Quantity=3.0, Description="New text", UnitPrice=10.5, UnitCost=4.0, DiscountPercent=5.0,
        Tax={"Id": 7, "Name": "VAT"}, CoaCode="200", BillableStatus={"Id": 2, "Name": "No charge"},
    )
    route_update(mock_gorelo, after=open_invoice(lines=[line_row(Id=L1, Name="One"), changed, line_row(Id=L3, Name="Three")]))
    await call_tool(
        server, "update_invoice",
        {
            "invoice_id": INVOICE_ID,
            "update_lines": [
                {
                    "line_id": L2.upper(), "quantity": 3, "description": "New text", "unit_price": 10.5, "unit_cost": 4,
                    "discount_percent": 5, "tax_id": 7, "coa_code": "200", "billable_status_id": 2,
                }
            ],
        },
    )
    assert sent_patch(mock_gorelo) == {
        "LineItems": [
            {"Id": L1},
            {
                "Id": L2, "Quantity": 3, "Description": "New text", "UnitPrice": 10.5, "UnitCost": 4, "DiscountPercent": 5,
                "TaxId": 7, "CoaCode": "200", "BillableStatusId": 2,
            },
            {"Id": L3},
        ]
    }


async def test_remove_line_ids_drops_only_the_lines_named_and_the_text_says_what_happens_to_their_work(server, mock_gorelo):
    route_update(mock_gorelo, after=open_invoice(lines=[line_row(Id=L1), line_row(Id=L3)]))
    await call_tool(server, "update_invoice", {"invoice_id": INVOICE_ID, "remove_line_ids": [L2]})
    assert sent_patch(mock_gorelo) == {"LineItems": [{"Id": L1}, {"Id": L3}]}
    tool = tool_def("update_invoice", await list_tools(server))
    assert "waiting to be invoiced" in tool.inputSchema["properties"]["remove_line_ids"]["description"]


async def test_update_remove_and_add_work_together_in_one_request(server, mock_gorelo):
    route_update(
        mock_gorelo,
        after=open_invoice(lines=[line_row(Id=L1), line_row(Id=L3, Quantity=9.0), line_row(Id=uid(35), item_id=ITEM_A, Quantity=1.0)]),
    )
    await call_tool(
        server, "update_invoice",
        {
            "invoice_id": INVOICE_ID,
            "remove_line_ids": [L2],
            "update_lines": [{"line_id": L3, "quantity": 9}],
            "add_lines": [{"item_id": ITEM_A, "quantity": 1}],
        },
    )
    assert sent_patch(mock_gorelo)["LineItems"] == [{"Id": L1}, {"Id": L3, "Quantity": 9}, {"ItemId": ITEM_A, "Quantity": 1}]


async def test_no_tax_on_an_update_line_sends_the_exempt_id_and_a_clear_sends_null_for_description_and_coa_code(server, mock_gorelo):
    # the read-back shows the exempt rate and no tax on the two no_tax lines, so there is no warning
    untaxed = {"TaxAmount": 0.0, "Tax": {"Id": EXEMPT_ID, "Name": "Tax Exempt TAX"}}
    route_taxes(mock_gorelo)
    route_update(
        mock_gorelo,
        after=open_invoice(
            lines=[line_row(Id=L1, Name="One", **untaxed), line_row(Id=L2, Name="Two"), line_row(Id=L3, Name="Three"),
                   line_row(Id=uid(34), Quantity=1.0, **untaxed)]
        ),
    )
    result = await call_tool(
        server, "update_invoice",
        {
            "invoice_id": INVOICE_ID,
            "update_lines": [{"line_id": L1, "no_tax": True}, {"line_id": L2, "clear_fields": ["description", "coa_code"]}],
            "add_lines": [{"item_id": ITEM_A, "quantity": 1, "no_tax": True}],
        },
    )
    sent = sent_patch(mock_gorelo)["LineItems"]
    assert sent[0] == {"Id": L1, "TaxId": EXEMPT_ID}
    assert sent[1] == {"Id": L2, "Description": None, "CoaCode": None}
    assert sent[2] == {"Id": L3}
    assert sent[3] == {"ItemId": ITEM_A, "Quantity": 1, "TaxId": EXEMPT_ID}
    assert "warning" not in result and f"tax id {EXEMPT_ID}" in result["note"]
    assert len(mock_gorelo.calls("GET", TAXES)) == 1  # ONE lookup for the update line and the new line
    tools = {t.name: t for t in await list_tools(server)}
    change = tools["update_invoice"].inputSchema["properties"]["update_lines"]["items"]["properties"]
    assert change["no_tax"]["description"] == invoices.NO_TAX_TEXT


@pytest.mark.parametrize(
    "args, fragment",
    [
        ({"update_lines": [{"line_id": uid(99), "quantity": 1}]}, "update_lines[0].line_id: " + uid(99) + " is not a line of this invoice"),
        ({"remove_line_ids": [uid(99)]}, "remove_line_ids[0]: " + uid(99) + " is not a line of this invoice"),
        ({"remove_line_ids": [L1, L1]}, "remove_line_ids: the same line id is listed twice"),
        ({"remove_line_ids": ["x"]}, "remove_line_ids[0]: expected a GUID"),
        ({"remove_line_ids": [L1], "update_lines": [{"line_id": L1, "quantity": 2}]}, "remove_line_ids[0]: this line is also in update_lines"),
        ({"remove_line_ids": [L1, L2, L3]}, "remove_line_ids: this would remove every line of the invoice"),
        ({"update_lines": [{"line_id": L1}]}, "update_lines[0]: give at least one field to change besides line_id"),
        ({"update_lines": [{"line_id": L1, "quantity": 0}]}, "update_lines[0].quantity: must be a number greater than 0"),
        ({"update_lines": [{"line_id": L1, "description": "x", "clear_fields": ["description"]}]}, "update_lines[0].description: cannot be given a value and cleared"),
        ({"update_lines": [{"line_id": L1, "tax_id": 3, "no_tax": True}]}, "update_lines[0]: give tax_id or no_tax, not both"),
        ({"update_lines": [{"line_id": L1, "billable_status_id": 4}]}, "update_lines[0].billable_status_id: must be 1, 2 or 3"),
        ({"update_lines": [{"line_id": L1, "discount_percent": 101}]}, "update_lines[0].discount_percent"),
        ({"update_lines": [{"line_id": L1, "quantity": 1}, {"line_id": L1.upper(), "quantity": 2}]}, "update_lines[1].line_id: this line is already changed"),
        ({"update_lines": [{"line_id": L1, "item_id": ITEM_A, "quantity": 2}]}, "update_lines[0]: unknown field(s) item_id"),
        ({"update_lines": [{"quantity": 2}]}, "update_lines[0]: missing line_id"),
        ({"update_lines": [{"line_id": L1, "clear_fields": ["tax_id"]}]}, "update_lines[0].clear_fields"),
        ({"add_lines": [{"item_id": ITEM_A}]}, "add_lines[0]: missing quantity"),
        ({"add_lines": [{"item_id": "x", "quantity": 1}]}, "add_lines[0].item_id: expected a GUID"),
    ],
)
async def test_line_errors_name_the_param_and_the_item_and_never_reach_a_patch(client_factory, mock_gorelo, args, fragment):
    route_update(mock_gorelo)
    with pytest.raises((ToolError, ValueError)) as info:
        await call_directly(client_factory, invoices.update_invoice, invoice_id=INVOICE_ID, **args)
    assert fragment in str(info.value)
    assert mock_gorelo.calls("PATCH") == []


async def test_a_hundred_lines_is_the_limit_and_the_message_names_add_lines_when_lines_are_added(client_factory, mock_gorelo):
    rows = [line_row(Id=uid(1000 + n)) for n in range(100)]
    route_update(mock_gorelo, before=open_invoice(lines=rows))
    with pytest.raises((ToolError, ValueError)) as info:
        await call_directly(
            client_factory, invoices.update_invoice, invoice_id=INVOICE_ID, add_lines=[{"item_id": ITEM_A, "quantity": 1}]
        )
    assert "add_lines: the invoice would have 101 lines (100 kept plus 1 added) and Gorelo accepts at most 100" in str(info.value)
    assert mock_gorelo.calls("PATCH") == []


@pytest.mark.parametrize("lines, fragment", [([{"Name": "x"}], "line 1 of the invoice has no usable Id"), (None, "no LineItems list")])
async def test_an_invoice_whose_read_has_no_usable_line_ids_is_never_patched_on_its_lines(server, mock_gorelo, lines, fragment):
    before = open_invoice(lines=lines if lines is not None else [])
    if lines is None:
        before["LineItems"] = None
    route_update(mock_gorelo, before=before)
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "remove_line_ids": [L1]})
    assert fragment in text and mock_gorelo.calls("PATCH") == []


async def test_a_line_billing_a_time_entry_is_left_to_gorelo_and_the_text_says_quantity_cannot_change(server, mock_gorelo):
    # the read of a line carries nothing that says it bills a time entry, so the tool cannot refuse it locally
    assert "Name" in line_row() and not any("Time" in key for key in line_row())
    route_update(
        mock_gorelo,
        patch=error_envelope(400, [("070101", "Quantity cannot change on a line billing a time entry.", "LineItems[0].Quantity")]),
    )
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "update_lines": [{"line_id": L1, "quantity": 5}]})
    assert text == trace(
        "Gorelo rejected update_invoice (HTTP 400, code 070101): LineItems[0].Quantity: Quantity cannot change on a line billing a time entry."
    )
    tools = {t.name: t for t in await list_tools(server)}
    assert "billing a time entry" in tools["update_invoice"].description
    assert "billing a time entry" in tools["update_invoice"].inputSchema["properties"]["update_lines"]["items"]["properties"]["quantity"]["description"]


# ---- update_invoice: what Gorelo answers ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "property_name, param",
    [("Reference", "reference"), ("InvoiceDate", "invoice_date"), ("DueDate", "due_date"), ("InvoiceTemplateId", "invoice_template_id"),
     ("InvoiceEmailTemplateId", "invoice_email_template_id"), ("BrandingThemeId", "branding_theme_id"), ("invoiceId", "invoice_id")],
)
async def test_update_invoice_a_gorelo_400_names_the_snake_case_param(server, mock_gorelo, property_name, param):
    route_update(mock_gorelo, patch=error_envelope(400, [("070101", "Rejected.", property_name)]))
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "reference": "r"})
    assert text == trace(f"Gorelo rejected update_invoice (HTTP 400, code 070101): {param}: Rejected.")


async def test_update_invoice_a_409_is_a_plain_refusal(server, mock_gorelo):
    route_update(mock_gorelo, patch=error_envelope(409, [("070901", "The invoice was paid meanwhile.")]))
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "reference": "r"})
    assert text == trace("Gorelo rejected update_invoice (HTTP 409, code 070901): The invoice was paid meanwhile.")
    assert "may or may not" not in text


async def test_update_invoice_a_404_on_the_read_is_reported_and_nothing_is_patched(server, mock_gorelo):
    mock_gorelo.on("GET", PATCH_PATH, error_envelope(404, [("070401", "Invoice not found.", "invoiceId")]))
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "reference": "r"})
    assert text == trace("Gorelo rejected update_invoice (HTTP 404, code 070401): invoice_id: Invoice not found.")
    assert [r.method for r in mock_gorelo.requests] == ["GET"]


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")], ids=["timeout", "connection"])
async def test_a_patch_that_is_not_answered_is_never_retried_and_says_to_verify(server, mock_gorelo, failure):
    mock_gorelo.on("GET", PATCH_PATH, envelope(open_invoice()))
    mock_gorelo.on("PATCH", PATCH_PATH, failure)
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "reference": "r"})
    assert text.startswith("Gorelo did not confirm update_invoice (")
    assert "Verify with a read before retrying." in text
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH"]


async def test_a_failed_read_back_returns_the_answer_of_the_patch_with_a_warning(server, mock_gorelo):
    after = open_invoice(Reference="PO-1")
    mock_gorelo.on("GET", PATCH_PATH, in_order(envelope(open_invoice()), error_envelope(500, [("070500", "Down.")])))
    mock_gorelo.on("PATCH", PATCH_PATH, envelope(after))
    result = await call_tool(server, "update_invoice", {"invoice_id": INVOICE_ID, "reference": "PO-1"})
    assert result["Reference"] == "PO-1" and "re-reading it failed" in result["warning"] and "Do not repeat the write" in result["warning"]


async def test_a_read_back_that_does_not_show_the_change_is_a_write_unconfirmed_error(server, mock_gorelo):
    route_update(mock_gorelo, after=open_invoice(Reference="OLD"), patch=open_invoice(Reference="OLD"))
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "reference": "NEW"})
    assert "the invoice does not show the change (Reference is not the text that was sent)" in text
    assert "verify it with a read before repeating it (get_invoice)" in text


async def test_a_read_back_with_a_different_number_of_lines_is_not_taken_for_a_success(server, mock_gorelo):
    route_update(mock_gorelo, after=open_invoice(lines=lines3()))
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "remove_line_ids": [L1]})
    assert "LineItems shows 3 lines, 2 were sent" in text


async def test_a_patch_answer_that_is_not_an_invoice_is_write_unconfirmed(server, mock_gorelo):
    mock_gorelo.on("GET", PATCH_PATH, envelope(open_invoice()))
    mock_gorelo.on("PATCH", PATCH_PATH, envelope(None))
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "reference": "r"})
    assert "Verify with a read before retrying." in text or "verify it with a read" in text


async def test_update_invoice_is_a_write_with_the_destructive_hint_and_a_text_that_says_what_it_does(server):
    spec = next(s for s in REGISTRY.specs if s.name == "update_invoice")
    assert (spec.toolset, spec.kind, spec.destructive_hint) == ("billing", "write", True)
    tool = tool_def("update_invoice", await list_tools(server))
    assert tool.annotations.destructiveHint is True and tool.annotations.readOnlyHint is False
    text = flat(inspect.getdoc(invoices.update_invoice))
    assert 40 < len(text) <= 900
    assert "Draft or Submitted" in text and "never changes the status (approve_invoice, when enabled)" in text
    assert "Gorelo replaces the whole set, so the tool resends them" in text
    assert "update_lines changes lines by line_id, add_lines appends new ones, remove_line_ids removes some" in text
    assert "no_tax (the account's Tax Exempt rate" in text and "or tax_id" in text
    for name, definition in tool.inputSchema["properties"].items():
        assert definition.get("description") and len(definition["description"]) <= 220, name


# ---- approve_invoice ---------------------------------------------------------------------------------------------------


async def test_approve_refuses_without_confirm_with_no_http_and_says_what_would_happen(server, mock_gorelo):
    text = await call_tool_error(server, "approve_invoice", {"invoice_id": INVOICE_ID})
    assert text.startswith(f"confirm: refusing to approve invoice {INVOICE_ID} without confirm=true.")
    assert "pushes the invoice to the connected accounting system at once" in text
    assert "voids it in Gorelo ONLY" in text and "the user must void it there too" in text and "observed with Xero" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("confirm", ["true", 1, "yes", None, 0])
async def test_approve_confirm_is_strict(server, mock_gorelo, confirm):
    text = await call_tool_error(server, "approve_invoice", {"invoice_id": INVOICE_ID, "confirm": confirm})
    assert "confirm" in text and mock_gorelo.requests == []


async def test_a_bad_invoice_id_is_reported_before_the_user_is_asked_to_confirm(server, mock_gorelo):
    text = await call_tool_error(server, "approve_invoice", {"invoice_id": "nope"})
    assert text.startswith("invoice_id: expected a GUID") and mock_gorelo.requests == []


@pytest.mark.parametrize("status", [(1, "Draft"), (2, "Submitted")])
async def test_approve_reads_patches_status_5_and_nothing_else_and_reads_it_back(server, mock_gorelo, status):
    approved = open_invoice((5, "Approved"), ExternalId="xero-1")
    route_update(mock_gorelo, before=open_invoice(status), after=approved)
    result = await call_tool(server, "approve_invoice", {"invoice_id": INVOICE_ID.upper(), "confirm": True})
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", PATCH_PATH), ("PATCH", PATCH_PATH), ("GET", PATCH_PATH)]
    assert sent_patch(mock_gorelo) == {"StatusId": 5}
    assert result == approved and "note" not in result


@pytest.mark.parametrize("status", [(3, "Paid"), (4, "Void"), (5, "Approved")])
async def test_approve_refuses_any_status_but_draft_and_submitted_and_sends_no_patch(server, mock_gorelo, status):
    route_update(mock_gorelo, before=open_invoice(status))
    text = await call_tool_error(server, "approve_invoice", {"invoice_id": INVOICE_ID, "confirm": True})
    assert f"(id {status[0]}); approve_invoice approves only Draft (1) and Submitted (2) invoices." in text
    assert ("It is already Approved." in text) == (status[0] == 5)
    assert [r.method for r in mock_gorelo.requests] == ["GET"]


async def test_a_total_of_zero_comes_back_paid_with_a_note_and_is_not_an_error(server, mock_gorelo):
    paid = open_invoice((3, "Paid"), Total=0.0, AmountDue=0.0)
    route_update(mock_gorelo, after=paid)
    result = await call_tool(server, "approve_invoice", {"invoice_id": INVOICE_ID, "confirm": True})
    assert result["Status"] == {"Id": 3, "Name": "Paid"} and "Status is Paid, not Approved" in result["note"]


async def test_an_answer_that_still_shows_draft_is_write_unconfirmed(server, mock_gorelo):
    route_update(mock_gorelo, after=open_invoice((1, "Draft")))
    text = await call_tool_error(server, "approve_invoice", {"invoice_id": INVOICE_ID, "confirm": True})
    assert "the invoice shows Draft, expected Approved or Paid after approving" in text
    assert "verify it with a read before repeating it (get_invoice)" in text


async def test_a_failed_read_back_after_approving_returns_the_patch_answer_with_a_warning(server, mock_gorelo):
    mock_gorelo.on("GET", PATCH_PATH, in_order(envelope(open_invoice()), error_envelope(500, [("070500", "Down.")])))
    mock_gorelo.on("PATCH", PATCH_PATH, envelope(open_invoice((5, "Approved"))))
    result = await call_tool(server, "approve_invoice", {"invoice_id": INVOICE_ID, "confirm": True})
    assert result["Status"]["Id"] == 5 and "re-reading it failed" in result["warning"]


async def test_a_confirmed_approval_that_times_out_is_not_repeated_and_the_text_says_to_look_first_with_get_invoice(server, mock_gorelo):
    mock_gorelo.on("GET", PATCH_PATH, envelope(open_invoice()))
    mock_gorelo.on("PATCH", PATCH_PATH, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "approve_invoice", {"invoice_id": INVOICE_ID, "confirm": True})
    assert text.startswith("Gorelo did not confirm approve_invoice (the request timed out).")
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH"]
    assert 'After a "did not confirm" error read get_invoice before repeating.' in flat(inspect.getdoc(invoices.approve_invoice))


async def test_approve_a_gorelo_409_is_a_plain_refusal(server, mock_gorelo):
    route_update(mock_gorelo, patch=error_envelope(409, [("070901", "Not approvable.")]))
    text = await call_tool_error(server, "approve_invoice", {"invoice_id": INVOICE_ID, "confirm": True})
    assert text == trace("Gorelo rejected approve_invoice (HTTP 409, code 070901): Not approvable.")


async def test_approve_invoice_is_gated_and_says_the_same_things_as_create_approved_invoice(server_factory):
    spec = next(s for s in REGISTRY.specs if s.name == "approve_invoice")
    assert (spec.toolset, spec.kind, spec.ops) == ("billing", "destructive", ["GET /v1/invoices/{invoiceId}", "PATCH /v1/invoices/{invoiceId}"])
    off = {t.name for t in await list_tools(server_factory(destructive=False))}
    assert "approve_invoice" not in off and "update_invoice" in off
    listed = server_factory(destructive=True, destructive_tools=frozenset({"approve_invoice"}))
    assert "approve_invoice" in {t.name for t in await list_tools(listed)}
    doc = flat(inspect.getdoc(invoices.approve_invoice))
    assert 40 < len(doc) <= 900
    assert "pushes the invoice to the connected accounting system at once" in doc
    assert "voids it in Gorelo ONLY, so the copy in the accounting system stays open (observed with Xero)" in doc
    assert "who must void it there too" in doc and "A total of exactly 0 is recorded as Paid instead: check Status" in doc
    assert doc.index("Gorelo ONLY") < doc.index("Ask the user first")
    assert doc.endswith("Ask the user first; needs confirm=true.")
    parameter = inspect.signature(spec.fn).parameters["confirm"]
    assert parameter.default is False and "Strict" in repr(parameter.annotation)


# ---- payments (read only) ------------------------------------------------------------------------------------------------

PAYMENT_ID = uid(41)


def payment(**extra):
    row = {
        "Id": PAYMENT_ID, "Amount": 250.0, "PaymentDate": "2026-10-01", "Reference": "BANK-1", "ClientId": 4983,
        "Invoices": [{"InvoiceId": INVOICE_ID, "Amount": 250.0}],
    }
    row.update(extra)
    return row


def test_the_payment_fixture_is_shaped_like_the_spec(spec_index):
    assert set(payment()) == set(spec_index.schema("PaymentModel")["fields"])
    assert set(payment()["Invoices"][0]) == set(spec_index.schema("PaymentInvoiceModel")["fields"])


async def test_list_payments_sends_only_the_page_size_and_returns_the_house_shape(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/payments", paged_envelope([payment()], total_count=1))
    result = await call_tool(server, "list_payments")
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", "/v1/payments", {"PageSize": "50"}, None)
    assert result == {"items": [payment()], "count": 1, "total_count": 1, "has_more": False, "next_cursor": None, "page_size": 50, "filters": {}}


async def test_every_payment_filter_goes_out_under_its_spec_name_and_the_dates_as_midnight_utc(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/payments", paged_envelope([]))
    result = await call_tool(
        server, "list_payments",
        {
            "client_ids": [4983, 7], "invoice_ids": [INVOICE_ID.upper(), uid(6)], "payment_date_since": "2026-10-01",
            "payment_date_before": "2026-11-01", "sort_order": "asc", "page_size": 500, "cursor": "abc",
        },
    )
    assert mock_gorelo.last.query == {
        "ClientIds": "4983,7", "InvoiceIds": f"{INVOICE_ID},{uid(6)}", "PaymentDateSince": "2026-10-01T00:00:00Z",
        "PaymentDateBefore": "2026-11-01T00:00:00Z", "SortOrder": "asc", "PageSize": "200", "Cursor": "abc",
    }
    assert result["filters"]["payment_date_since"] == "2026-10-01" and result["page_size"] == 200  # echoed as given, clamped size


@pytest.mark.parametrize(
    "args, fragment",
    [
        ({"client_ids": []}, "client_ids"),
        ({"invoice_ids": ["INV-1"]}, "invoice_ids[0]: expected a GUID"),
        ({"invoice_ids": []}, "invoice_ids: expected at least one GUID"),
        ({"payment_date_since": "2026-10-01T00:00:00Z"}, "payment_date_since: expected a calendar date"),
        ({"payment_date_before": "tomorrow"}, "payment_date_before: expected a calendar date"),
        ({"cursor": " "}, "cursor: must not be empty"),
    ],
)
async def test_list_payments_local_errors_name_the_param_and_send_nothing(client_factory, mock_gorelo, args, fragment):
    with pytest.raises((ToolError, ValueError)) as info:
        await call_directly(client_factory, invoices.list_payments, **args)
    assert fragment in str(info.value) and mock_gorelo.requests == []


async def test_list_payments_refuses_a_sort_order_outside_the_schema_and_a_bool_client_id(server, mock_gorelo):
    assert "sort_order" in await call_tool_error(server, "list_payments", {"sort_order": "newest"})
    assert "client_ids" in await call_tool_error(server, "list_payments", {"client_ids": [True]})
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("property_name, param", [("SortOrder", "sort_order"), ("ClientIds", "client_ids"), ("InvoiceIds", "invoice_ids"),
                                                    ("PaymentDateSince", "payment_date_since"), ("PaymentDateBefore", "payment_date_before"),
                                                    ("PageSize", "page_size"), ("Cursor", "cursor")])
async def test_list_payments_a_gorelo_400_names_the_snake_case_param(server, mock_gorelo, property_name, param):
    mock_gorelo.on("GET", "/v1/payments", error_envelope(400, [("070101", "rejected", property_name)]))
    text = await call_tool_error(server, "list_payments")
    assert f"HTTP 400, code 070101): {param}: rejected" in text


async def test_list_payments_follows_next_cursor_with_the_same_filters(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/payments", paged_envelope([payment()], next_cursor="c1", total_count=2))
    first = await call_tool(server, "list_payments", {"client_ids": [4983]})
    assert first["has_more"] is True and first["next_cursor"] == "c1"
    await call_tool(server, "list_payments", {**first["filters"], "cursor": first["next_cursor"]})
    assert mock_gorelo.last.query == {"ClientIds": "4983", "PageSize": "50", "Cursor": "c1"}


async def test_get_payment_reads_one_payment_by_its_guid_in_canonical_form(server, mock_gorelo):
    mock_gorelo.on("GET", f"/v1/payments/{PAYMENT_ID}", envelope(payment()))
    result = await call_tool(server, "get_payment", {"payment_id": PAYMENT_ID.upper()})
    assert result == payment()
    assert (mock_gorelo.last.method, mock_gorelo.last.path, mock_gorelo.last.query) == ("GET", f"/v1/payments/{PAYMENT_ID}", {})


@pytest.mark.parametrize("bad", ["12", "", "pay-1"])
async def test_get_payment_needs_a_guid_and_names_payment_id(server, mock_gorelo, bad):
    text = await call_tool_error(server, "get_payment", {"payment_id": bad})
    assert "payment_id" in text and mock_gorelo.requests == []


async def test_get_payment_a_404_names_payment_id_and_a_non_object_answer_is_refused(server, mock_gorelo):
    mock_gorelo.on("GET", f"/v1/payments/{PAYMENT_ID}", error_envelope(404, [("070401", "Payment not found.", "paymentId")]))
    assert await call_tool_error(server, "get_payment", {"payment_id": PAYMENT_ID}) == trace(
        "Gorelo rejected get_payment (HTTP 404, code 070401): payment_id: Payment not found."
    )
    mock_gorelo.on("GET", f"/v1/payments/{uid(42)}", envelope([payment()]))
    assert "unexpected response" in await call_tool_error(server, "get_payment", {"payment_id": uid(42)})


async def test_the_payment_tools_are_reads_and_say_that_no_tool_records_or_deletes_a_payment(server):
    for name in ("list_payments", "get_payment"):
        spec = next(s for s in REGISTRY.specs if s.name == name)
        assert (spec.toolset, spec.kind) == ("billing", "read")
        assert all(op.startswith("GET ") for op in spec.ops)
        doc = flat(inspect.getdoc(spec.fn))
        assert "Read only: payments are recorded money, and no tool records or deletes one." in doc
    # nothing in this module reaches POST or DELETE /v1/payments
    assert "POST /v1/payments" not in inspect.getsource(invoices) and "DELETE /v1/payments" not in inspect.getsource(invoices).replace(
        "(POST and DELETE stay excluded)", ""
    )
    assert all("/v1/payments" not in op or op.startswith("GET ") for spec in module_specs() for op in spec.ops)


# --------------------------------------------------------------------------
# 1.5.0 regressions: no_tax, the update read-back and the other billing text fixes
# --------------------------------------------------------------------------

UNTAXED = {"TaxAmount": 0.0, "Tax": {"Id": EXEMPT_ID, "Name": "Tax Exempt TAX"}}
STILL_TAXED = {"Tax": {"Id": EXEMPT_ID, "Name": "Tax Exempt TAX"}}  # keeps the default TaxAmount 13.5 of line_row


@pytest.mark.parametrize("position", [0, 1])
async def test_no_tax_on_create_approved_invoice_sends_the_exempt_id_in_the_one_post(server, mock_gorelo, position):
    lines = [line(tax_id=4), line(tax_id=4)]
    lines[position] = line(no_tax=True)
    route_create(mock_gorelo, invoice_detail(LineItems=[line_row(Id=L1, **UNTAXED), line_row(Id=L2, **UNTAXED)]))
    route_taxes(mock_gorelo)
    result = await call_tool(server, "create_approved_invoice", {**GOOD, "line_items": lines, "confirm": True})
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", TAXES), ("POST", INVOICES), ("GET", NEW_PATH)]
    sent = mock_gorelo.calls("POST")[0].json
    assert sent["StatusId"] == 5
    assert [entry["TaxId"] for entry in sent["LineItems"]] == [EXEMPT_ID if n == position else 4 for n in range(2)]
    assert f"tax id {EXEMPT_ID}" in result["note"] and "warning" not in result


async def test_the_approved_tool_confirms_before_it_looks_up_the_rate_and_a_tax_id_line_needs_no_lookup(server, mock_gorelo):
    text = await call_tool_error(server, "create_approved_invoice", {**GOOD, "line_items": [line(no_tax=True)]})  # no confirm
    assert text.startswith("confirm: refusing to create an APPROVED invoice") and mock_gorelo.requests == []
    route_create(mock_gorelo)
    await call_tool(server, "create_approved_invoice", {**GOOD, "line_items": [line(tax_id=9)], "confirm": True})
    assert mock_gorelo.requests[0].json["LineItems"] == [{"ItemId": ITEM_A, "Quantity": 1, "TaxId": 9}]
    assert mock_gorelo.calls("GET", TAXES) == []  # no no_tax line, no lookup


async def test_an_approved_no_tax_line_that_still_shows_tax_gets_a_loud_warning(server, mock_gorelo):
    route_taxes(mock_gorelo)
    route_create(mock_gorelo, invoice_detail(status=(5, "Approved"), LineItems=[line_row(Id=L1, Quantity=1.0, **STILL_TAXED)]))
    result = await call_tool(server, "create_approved_invoice", {**GOOD, "line_items": [line(no_tax=True)], "confirm": True})
    assert result["Status"] == {"Id": 5, "Name": "Approved"}
    assert result["warning"].startswith("WARNING: no_tax was asked for line_items[0]")
    assert f"TaxId {EXEMPT_ID} (the Tax Exempt rate) was sent" in result["warning"]
    assert "APPROVED and already pushed to the accounting system" in result["warning"]
    assert "voids it in Gorelo ONLY" in result["warning"]
    assert f"tax id {EXEMPT_ID}" in result["note"]


async def test_a_draft_with_a_no_tax_line_that_still_shows_tax_gets_a_warning_naming_the_line(server, mock_gorelo):
    taxed = line_row(Id=L1, Quantity=1.0, **STILL_TAXED)  # TaxAmount 13.5
    route_taxes(mock_gorelo)
    route_create(mock_gorelo, invoice_detail(LineItems=[line_row(Id=L2, Quantity=1.0, **UNTAXED), taxed]))
    result = await call_tool(server, "create_invoice", {**GOOD, "line_items": [line(tax_id=9), line(no_tax=True)]})
    assert result["Id"] == NEW_ID and result["LineItems"][1]["Id"] == L1  # the invoice itself, unchanged
    assert "line_items[1] (Id " + L1 + ")" in result["warning"] and "line_items[0]" not in result["warning"]
    assert "tax_id of your tax-exempt rate" in result["warning"] and "update_invoice" in result["warning"]
    assert "WARNING" not in result["warning"] and "not approved" in result["warning"]
    assert f"tax id {EXEMPT_ID}" in result["note"]


async def test_a_no_tax_draft_line_that_shows_no_tax_has_no_warning(server, mock_gorelo):
    route_taxes(mock_gorelo)
    route_create(mock_gorelo, invoice_detail(LineItems=[line_row(Id=L1, Quantity=1.0, **UNTAXED)]))
    result = await call_tool(server, "create_invoice", {**GOOD, "line_items": [line(no_tax=True)]})
    assert "warning" not in result and f"tax id {EXEMPT_ID}" in result["note"]


async def test_a_failed_read_back_keeps_its_own_warning_and_adds_none(server, mock_gorelo):
    route_taxes(mock_gorelo)
    mock_gorelo.on("POST", INVOICES, envelope({"Id": NEW_ID}))
    mock_gorelo.on("GET", f"/v1/invoices/{NEW_ID}", error_envelope(500, [("070001", "Internal error.")]))
    result = await call_tool(server, "create_invoice", {**GOOD, "line_items": [line(no_tax=True)]})
    assert set(result) == {"Id", "warning", "note"} and "no_tax was asked for" not in result["warning"]


async def test_update_invoice_warns_about_a_no_tax_line_and_a_new_no_tax_line_that_show_tax(server, mock_gorelo):
    new_id = uid(34)
    route_taxes(mock_gorelo)
    route_update(
        mock_gorelo,
        after=open_invoice(
            lines=[line_row(Id=L1, Name="One"), line_row(Id=L2, Name="Two", **STILL_TAXED), line_row(Id=L3, Name="Three"),
                   line_row(Id=new_id, Quantity=1.0, **STILL_TAXED)]
        ),
    )
    result = await call_tool(
        server, "update_invoice",
        {"invoice_id": INVOICE_ID, "update_lines": [{"line_id": L2, "no_tax": True}], "add_lines": [line(no_tax=True)]},
    )
    assert f"update_lines[0] (line {L2})" in result["warning"] and f"add_lines[0] (line {new_id})" in result["warning"]
    assert f"TaxId {EXEMPT_ID} (the Tax Exempt rate) was sent" in result["warning"]
    assert result["LineItems"][3]["Id"] == new_id and f"tax id {EXEMPT_ID}" in result["note"]


async def test_update_invoice_note_keeps_the_status_note_and_adds_the_tax_note(server, mock_gorelo):
    route_taxes(mock_gorelo)
    route_update(
        mock_gorelo,
        after=open_invoice(status=(5, "Approved"), lines=[line_row(Id=L1, **UNTAXED), line_row(Id=L2), line_row(Id=L3)]),
    )
    result = await call_tool(server, "update_invoice", {"invoice_id": INVOICE_ID, "update_lines": [{"line_id": L1, "no_tax": True}]})
    assert "after the change, not Draft or Submitted" in result["note"] and f"tax id {EXEMPT_ID}" in result["note"]


async def test_the_read_back_of_update_invoice_must_show_the_exempt_rate_on_a_no_tax_line(server, mock_gorelo):
    route_taxes(mock_gorelo)
    route_update(mock_gorelo, after=open_invoice())  # L1 still shows Tax Id 2 (GST)
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "update_lines": [{"line_id": L1, "no_tax": True}]})
    assert f"line {L1}: Tax id expected {EXEMPT_ID}, read 2" in text and "the write may have been applied" in text


async def test_the_texts_name_the_new_rule():
    assert "Tax Exempt rate" in invoices.NO_TAX_TEXT
    approved = flat(inspect.getdoc(invoices.create_approved_invoice))
    assert "no_tax too" in approved and "loud warning" in approved
    assert "no_tax (the account's Tax Exempt rate) or tax_id" in flat(inspect.getdoc(invoices.create_invoice))
    assert "a no_tax line that still shows tax gets a warning" in flat(inspect.getdoc(invoices.create_invoice))


# ---- the lookup of the Tax Exempt rate: ONE rate, refused locally otherwise, before any write --------------------------


def candidates(rows):
    return {row["Id"] for row in rows}


@pytest.mark.parametrize("tool", ["create_invoice", "create_approved_invoice"])
@pytest.mark.parametrize(
    "rows, why",
    [
        pytest.param([tax_row(1, "GST", "GST", 10.0), tax_row(2, "Zero", "Zero")], "the account has no Tax Exempt rate", id="none"),
        pytest.param([], "the account has no Tax Exempt rate", id="no-taxes-at-all"),
        pytest.param(
            [tax_row(1, "Tax Exempt TAX", "Tax Exempt"), tax_row(2, "x", " tax EXEMPT ")],
            "the account has 2 Tax Exempt rates", id="two-by-code",
        ),
        pytest.param(
            [tax_row(1, "Tax Exempt", "A"), tax_row(2, "Other", "tax exempt")],
            "the account has 2 Tax Exempt rates", id="one-by-name-one-by-code",
        ),
        pytest.param([tax_row(1, "Tax Exempt", "Tax Exempt", 5.0)], "the account has no Tax Exempt rate", id="not-zero-percent"),
        pytest.param([tax_row(1, "Tax Exempt", "Tax Exempt", None)], "the account has no Tax Exempt rate", id="percentage-not-set"),
        pytest.param(
            [tax_row(1, "Tax Exempt", "Tax Exempt", 0.0, sub_taxes=[{"Id": 5, "Name": "GST", "Code": "GST", "Percentage": 5.0}])],
            "the account has no Tax Exempt rate", id="a-non-zero-subtax",
        ),
        pytest.param([tax_row(1, "Tax Exempt TAX", "Exempt")], "the account has no Tax Exempt rate", id="name-must-equal-exactly"),
    ],
)
async def test_no_tax_is_refused_before_any_write_when_there_is_not_exactly_one_exempt_rate(server, mock_gorelo, tool, rows, why):
    route_taxes(mock_gorelo, rows)
    text = await call_tool_error(server, tool, args_for(tool, line_items=[line(tax_id=3), line(no_tax=True)]))
    assert text.startswith("line_items[1].no_tax: " + why)
    assert "pass tax_id of the tax-exempt rate from list_taxes instead" in text and "Nothing has been sent to Gorelo." in text
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", TAXES)]  # the lookup only: no POST


async def test_the_refusal_lists_the_zero_percent_rates_by_id_and_code_only(server, mock_gorelo):
    rows = [tax_row(1, "Secret Name A", "Z1"), tax_row(2, "Secret Name B", None), tax_row(3, "Taxed", "T", 7.0)]
    route_taxes(mock_gorelo, rows)
    text = await call_tool_error(server, "create_invoice", {**GOOD, "line_items": [line(no_tax=True)]})
    assert '0% rates: Id 1 (Code "Z1"), Id 2 (no Code).' in text
    assert "Secret Name" not in text and "Id 3" not in text  # names never, taxed rates are not candidates


async def test_the_refusal_lists_at_most_ten_candidates(server, mock_gorelo):
    route_taxes(mock_gorelo, [tax_row(n, "z", f"C{n}") for n in range(1, 14)])
    text = await call_tool_error(server, "create_invoice", {**GOOD, "line_items": [line(no_tax=True)]})
    assert "Id 10 (" in text and "Id 11 (" not in text and "and 3 more" in text


def test_the_exempt_rule_matches_code_or_name_trimmed_and_case_insensitive_and_exact_zero():
    assert invoices._is_exempt_rate(tax_row(1, "x", "Tax Exempt"))
    assert invoices._is_exempt_rate(tax_row(1, "  TAX exempt ", None))
    assert invoices._is_exempt_rate(tax_row(1, "x", "tax exempt", 0))  # an integer 0 is exactly 0
    assert invoices._is_exempt_rate(tax_row(1, "x", "Tax Exempt", sub_taxes=[{"Id": 1, "Name": "a", "Code": None, "Percentage": 0.0}]))
    assert not invoices._is_exempt_rate(tax_row(1, "Tax Exempt TAX", "Tax Exempt", 0.01))
    assert not invoices._is_exempt_rate(tax_row(1, "Tax Exempt TAX", "TaxExempt"))
    assert not invoices._is_exempt_rate(tax_row(1, "x", "Tax Exempt", False))  # a boolean is not a rate
    assert not invoices._is_exempt_rate({"Id": 1, "Name": "Tax Exempt", "Percentage": 0.0, "SubTaxes": "none"})


async def test_the_live_tenant_resolves_to_id_202_in_one_get_with_no_query(server, mock_gorelo):
    route_taxes(mock_gorelo)
    route_create(mock_gorelo)
    await call_tool(server, "create_invoice", {**GOOD, "line_items": [line(no_tax=True)]})
    (lookup,) = mock_gorelo.calls("GET", TAXES)
    assert lookup.query == {} and mock_gorelo.requests[0] is lookup  # before the write
    assert mock_gorelo.calls("POST")[0].json["LineItems"] == [{"ItemId": ITEM_A, "Quantity": 1, "TaxId": 202}]


async def test_a_failed_tax_lookup_is_the_normal_error_and_stops_the_call_before_the_write(server, mock_gorelo):
    mock_gorelo.on("GET", TAXES, error_envelope(500, [("070001", "Internal error.")]))
    text = await call_tool_error(server, "create_invoice", {**GOOD, "line_items": [line(no_tax=True)]})
    assert "Gorelo" in text and "070001" in text
    assert [r.method for r in mock_gorelo.requests] == ["GET"]
    mock_gorelo.requests.clear()
    text = await call_tool_error(server, "create_approved_invoice", {**GOOD, "line_items": [line(no_tax=True)], "confirm": True})
    assert "070001" in text and mock_gorelo.calls("POST") == []


async def test_update_invoice_stops_before_the_patch_when_the_tax_lookup_fails(server, mock_gorelo):
    mock_gorelo.on("GET", TAXES, error_envelope(500, [("070001", "Internal error.")]))
    route_update(mock_gorelo)
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "add_lines": [line(no_tax=True)]})
    assert "070001" in text and mock_gorelo.calls("PATCH") == []


async def test_update_invoice_refuses_no_tax_before_the_patch_and_names_the_first_no_tax_line(server, mock_gorelo):
    route_taxes(mock_gorelo, [tax_row(1, "GST", "GST", 10.0)])
    route_update(mock_gorelo)
    text = await call_tool_error(
        server, "update_invoice",
        {"invoice_id": INVOICE_ID, "update_lines": [{"line_id": L1, "quantity": 2}], "add_lines": [line(no_tax=True)]},
    )
    assert text.startswith("add_lines[0].no_tax: the account has no Tax Exempt rate")
    assert mock_gorelo.calls("PATCH") == []
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", PATCH_PATH), ("GET", TAXES)]  # status read, lookup


async def test_update_invoice_names_an_update_line_when_it_is_the_first_no_tax_line(server, mock_gorelo):
    route_taxes(mock_gorelo, [])
    route_update(mock_gorelo)
    text = await call_tool_error(
        server, "update_invoice", {"invoice_id": INVOICE_ID, "update_lines": [{"line_id": L2, "no_tax": True}]}
    )
    assert text.startswith("update_lines[0].no_tax: the account has no Tax Exempt rate") and "0% rates: none." in text


async def test_update_invoice_looks_nothing_up_without_a_no_tax_line(server, mock_gorelo):
    route_update(mock_gorelo, after=open_invoice(Reference="PO-1"))
    await call_tool(server, "update_invoice", {"invoice_id": INVOICE_ID, "reference": "PO-1"})
    assert mock_gorelo.calls("GET", TAXES) == []


async def test_update_invoice_checks_the_status_before_it_looks_the_rate_up(server, mock_gorelo):
    mock_gorelo.on("GET", PATCH_PATH, envelope(open_invoice(status=(3, "Paid"))))
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "update_lines": [{"line_id": L1, "no_tax": True}]})
    assert "has status Paid" in text and mock_gorelo.calls("GET", TAXES) == []  # the status refusal comes first


def changed_by(update_lines=None, add_lines=None, remove_line_ids=None):
    return {"invoice_id": INVOICE_ID, "update_lines": update_lines, "add_lines": add_lines, "remove_line_ids": remove_line_ids}


def only_given(args):
    return {key: value for key, value in args.items() if value is not None}


def a_kept_line_changed(row):
    row["UnitPrice"] = 0.0
    return row


async def test_a_kept_line_whose_price_changed_is_reported_with_the_field_expected_and_read(server, mock_gorelo):
    # regression: line "a" is kept as {Id}, yet the read-back shows a price of 0
    after = open_invoice(lines=[a_kept_line_changed(line_row(Id=L1, Name="One")), line_row(Id=L2, Name="Two", Quantity=9.0), line_row(Id=L3)])
    route_update(mock_gorelo, after=after)
    text = await call_tool_error(server, "update_invoice", only_given(changed_by(update_lines=[{"line_id": L2, "quantity": 9}])))
    assert text.startswith("Gorelo returned an unexpected response for update_invoice")
    assert f"line {L1}: UnitPrice expected 150.0, read 0.0" in text
    assert "the write may have been applied" in text and "(get_invoice)" in text
    assert len(mock_gorelo.calls("PATCH")) == 1


@pytest.mark.parametrize(
    "key, sent, read, fragment",
    [
        ("quantity", 3, {"Quantity": 2.0}, "Quantity expected 3.0, read 2.0"),
        ("unit_price", 10.5, {"UnitPrice": 150.0}, "UnitPrice expected 10.5, read 150.0"),
        ("unit_cost", 4, {"UnitCost": 90.0}, "UnitCost expected 4.0, read 90.0"),
        ("discount_percent", 5, {"DiscountPercent": 10.0}, "DiscountPercent expected 5.0, read 10.0"),
        ("tax_id", 7, {"Tax": {"Id": 2, "Name": "GST"}}, "Tax id expected 7, read 2"),
        ("billable_status_id", 2, {"BillableStatus": {"Id": 1, "Name": "Billable"}}, "BillableStatus id expected 2, read 1"),
        ("description", "New text", {"Description": "Old text"}, "Description is not the text it should be (text not shown)"),
        ("coa_code", "200", {"CoaCode": "100"}, "CoaCode is not the text it should be (text not shown)"),
    ],
)
async def test_every_update_lines_field_that_was_sent_must_show_its_value(server, mock_gorelo, key, sent, read, fragment):
    after = open_invoice(lines=[line_row(Id=L1, Name="One"), line_row(Id=L2, Name="Two", **read), line_row(Id=L3)])
    route_update(mock_gorelo, after=after)
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "update_lines": [{"line_id": L2, key: sent}]})
    assert f"line {L2}: {fragment}" in text and "Old text" not in text and "New text" not in text  # text is never quoted
    assert text.startswith("Gorelo returned an unexpected response for update_invoice")


async def test_a_price_within_the_rounding_is_accepted(server, mock_gorelo):
    ok = open_invoice(lines=[line_row(Id=L1, Name="One"), line_row(Id=L2, Name="Two", UnitPrice=10.504), line_row(Id=L3)])
    route_update(mock_gorelo, after=ok)
    await call_tool(server, "update_invoice", {"invoice_id": INVOICE_ID, "update_lines": [{"line_id": L2, "unit_price": 10.5}]})


async def test_a_price_more_than_a_cent_off_is_not(server, mock_gorelo):
    off = open_invoice(lines=[line_row(Id=L1, Name="One"), line_row(Id=L2, Name="Two", UnitPrice=10.52), line_row(Id=L3)])
    route_update(mock_gorelo, after=off)
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "update_lines": [{"line_id": L2, "unit_price": 10.5}]})
    assert "UnitPrice expected 10.5, read 10.52" in text


async def test_a_cleared_description_and_coa_code_must_read_back_empty(server, mock_gorelo):
    before = open_invoice(lines=[line_row(Id=L1, Name="One"), line_row(Id=L2, Name="Two", Description="x", CoaCode="1"), line_row(Id=L3)])
    still = open_invoice(lines=[line_row(Id=L1, Name="One"), line_row(Id=L2, Name="Two", Description="x", CoaCode=None), line_row(Id=L3)])
    route_update(mock_gorelo, before=before, after=still)
    text = await call_tool_error(
        server, "update_invoice", {"invoice_id": INVOICE_ID, "update_lines": [{"line_id": L2, "clear_fields": ["description", "coa_code"]}]}
    )
    assert f"line {L2}: Description is not the text it should be" in text and "CoaCode" not in text


async def test_a_tax_that_the_call_did_not_send_must_stay_the_same_on_a_changed_line(server, mock_gorelo):
    after = open_invoice(lines=[line_row(Id=L1, Name="One"), line_row(Id=L2, Name="Two", Quantity=5.0, Tax={"Id": 3, "Name": "Other"}), line_row(Id=L3)])
    route_update(mock_gorelo, after=after)
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "update_lines": [{"line_id": L2, "quantity": 5}]})
    assert f"line {L2}: Tax id expected 2, read 3" in text


async def test_a_new_line_must_show_what_was_sent(server, mock_gorelo):
    new = line_row(Id=uid(34), item_id=ITEM_B, Quantity=2.0, UnitPrice=99.0)
    route_update(mock_gorelo, after=open_invoice(lines=[*lines3(), new]))
    text = await call_tool_error(
        server, "update_invoice",
        {"invoice_id": INVOICE_ID, "add_lines": [{"item_id": ITEM_B, "quantity": 2, "unit_price": 5, "billable_status_id": 3}]},
    )
    assert "new line 1: UnitPrice expected 5.0, read 99.0" in text and "new line 1: BillableStatus id expected 3, read 1" in text


async def test_the_line_ids_of_the_read_back_are_compared_with_the_first_read_plus_the_new_lines(server, mock_gorelo):
    # L2 was asked to go but is still there, L3 was kept but is gone
    route_update(mock_gorelo, after=open_invoice(lines=[line_row(Id=L1, Name="One"), line_row(Id=L2, Name="Two")]))
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "remove_line_ids": [L2]})
    assert f"line {L2} was removed but the invoice still shows it" in text
    assert f"line {L3} should stay but the invoice does not show it" in text


async def test_a_line_that_was_not_added_is_reported(server, mock_gorelo):
    route_update(mock_gorelo, after=open_invoice(lines=[*lines3(), line_row(Id=uid(34)), line_row(Id=uid(35))]))
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "add_lines": [line()]})
    assert "the invoice shows 2 new line(s), 1 were added" in text and "LineItems shows 5 lines, 4 were sent" in text


@pytest.mark.parametrize("status", [(5, "Approved"), (3, "Paid"), (4, "Void")])
async def test_a_read_back_that_is_no_longer_draft_or_submitted_adds_a_note(server, mock_gorelo, status):
    route_update(mock_gorelo, after=open_invoice(status=status, Reference="r"))
    result = await call_tool(server, "update_invoice", {"invoice_id": INVOICE_ID, "reference": "r"})
    assert result["Status"]["Id"] == status[0]
    assert f"shows {status[1]} after the change, not Draft or Submitted" in result["note"]
    assert "copy already pushed to the accounting system may not show this change" in result["note"]


@pytest.mark.parametrize("status", [(1, "Draft"), (2, "Submitted")])
async def test_a_draft_or_submitted_read_back_has_no_note(server, mock_gorelo, status):
    route_update(mock_gorelo, before=open_invoice(status), after=open_invoice(status, Reference="r"))
    assert "note" not in await call_tool(server, "update_invoice", {"invoice_id": INVOICE_ID, "reference": "r"})


async def test_the_probe_scenario_through_update_problems_directly():
    body = {"LineItems": [{"Id": "a"}, {"Id": "b", "Quantity": 9, "UnitPrice": 10.0}]}
    after = {"LineItems": [{"Id": "a", "UnitPrice": 0.0, "Quantity": 1}, {"Id": "b", "Quantity": 1, "UnitPrice": 99.0}]}
    before = {"LineItems": [{"Id": "a", "UnitPrice": 5.0, "Quantity": 1}, {"Id": "b", "Quantity": 1, "UnitPrice": 5.0}]}
    problems = invoices._update_problems(after, body, before)
    assert "line a: UnitPrice expected 5.0, read 0.0" in problems
    assert "line b: Quantity expected 9, read 1" in problems and "line b: UnitPrice expected 10.0, read 99.0" in problems


async def test_a_header_only_update_does_not_need_a_usable_line_id(server, mock_gorelo):
    # regression: a line the read shows with no Id must not refuse a change of the reference
    before = open_invoice(lines=[{"Name": "x"}])
    route_update(mock_gorelo, before=before, after={**before, "Reference": "PO-7"})
    result = await call_tool(server, "update_invoice", {"invoice_id": INVOICE_ID, "reference": "PO-7"})
    assert sent_patch(mock_gorelo) == {"Reference": "PO-7"} and result["Reference"] == "PO-7"


async def test_a_line_parameter_still_needs_the_line_ids(server, mock_gorelo):
    route_update(mock_gorelo, before=open_invoice(lines=[{"Name": "x"}]))
    text = await call_tool_error(server, "update_invoice", {"invoice_id": INVOICE_ID, "add_lines": [line()]})
    assert "line 1 of the invoice has no usable Id" in text and mock_gorelo.calls("PATCH") == []


async def test_over_100_lines_without_add_lines_does_not_name_add_lines(client_factory, mock_gorelo):
    rows = [line_row(Id=uid(1000 + n)) for n in range(101)]
    route_update(mock_gorelo, before=open_invoice(lines=rows))
    with pytest.raises((ToolError, ValueError)) as info:
        await call_directly(
            client_factory, invoices.update_invoice, invoice_id=INVOICE_ID, update_lines=[{"line_id": uid(1000), "quantity": 2}]
        )
    text = str(info.value)
    assert "add_lines" not in text
    assert text.startswith("remove_line_ids: the invoice would still have 101 lines and Gorelo accepts at most 100")
    assert "remove at least 1 more" in text and mock_gorelo.calls("PATCH") == []


def test_approve_invoice_and_list_payments_say_what_to_resolve_first():
    assert "Resolve ids first: invoice_id -> list_invoices." in flat(inspect.getdoc(invoices.approve_invoice))
    assert "Resolve ids first: client_ids -> list_clients, invoice_ids -> list_invoices." in flat(inspect.getdoc(invoices.list_payments))
