"""Invoices: list, read, create (Draft or Approved), PDF export, delete or void (toolset "billing").

Tools:

    list_invoices            read         GET /v1/invoices
    get_invoice              read         GET /v1/invoices/{invoiceId}
    create_invoice           write        POST /v1/invoices, GET /v1/invoices/{invoiceId}
    create_approved_invoice  destructive  POST /v1/invoices, GET /v1/invoices/{invoiceId}
    export_invoice_pdf       write        GET /v1/invoices/{invoiceId}/pdf
    delete_invoice           destructive  GET /v1/invoices, DELETE /v1/invoices/{invoiceId}

Ops owned by this module:

    GET /v1/invoices
    GET /v1/invoices/{invoiceId}
    POST /v1/invoices
    GET /v1/invoices/{invoiceId}/pdf
    DELETE /v1/invoices/{invoiceId}

POST /v1/invoices (contract e15cb5a18ec2) raises a manual invoice and answers only {Id}. Both create tools build
the same validated request (_invoice_request) and read the new invoice back with GET /v1/invoices/{invoiceId}
(reread_after_write), so the result is the full invoice or {Id, warning}. They differ in StatusId, which the TOOL
writes and no parameter can change: create_invoice always sends 1 (Draft: not pushed to accounting, not sent) and
create_approved_invoice always sends 5 (Approved). Approving on create pushes the invoice to the connected
accounting system at once, which only an operator who enabled GORELO_ENABLE_DESTRUCTIVE can allow, so
create_approved_invoice is kind "destructive": registered only then, refusing with no HTTP call unless confirm is
true. An Approved invoice whose total is exactly 0 is created as Paid; the result's Status says which. Only the
approved tool takes recipient_emails: Gorelo does not say whether creating an invoice already emails them, so the
draft tool has no such parameter at all.

Nothing in this module takes the push back. A void through DELETE /v1/invoices/{invoiceId} happens in Gorelo ONLY:
the DELETE answers StatusId 4 and get_invoice shows
Void, while the copy in the connected accounting system stays open (observed with Xero). The texts of create_approved_invoice and delete_invoice therefore say that the user must void the invoice in
the accounting system too (the docstrings, and the confirm refusals of both tools, which name the effect before
anything is sent; delete_invoice words it for expected_status Approved only: a Draft was never pushed). A Void invoice
can keep its AmountDue, which the texts of delete_invoice, get_invoice and list_invoices
mention so that nobody reads it as money owed.

InvoiceDate and DueDate are calendar dates (YYYY-MM-DD in the tools) and go out as <date>T00:00:00Z, the way the list
filters send theirs and the way the spec's own example is written. The local clock is never consulted (the provider's
"today" is not ours), so due_date is compared with invoice_date only when both are given.

Before 2026-10-02 Gorelo had no create endpoint and no GET by id. The PDF export is a GET that Gorelo records as an
export event on the invoice, so export_invoice_pdf is kind "write", never "read". The PDF is a raw file download read
with GoreloClient.get_binary under a 5 MB cap and handed to the model as a FastMCP ToolResult: a short text summary
plus the PDF as an embedded file. delete_invoice is the "invoice void" gate: it looks the invoice up by its number,
requires exactly one match whose status is the expected one, and only then sends the DELETE (a Draft invoice is
deleted and no longer listed, an Approved invoice is voided and stays listed; the published text says no more than
that, so the tool promises no more). What it reports comes from the StatusId of Gorelo's answer (DeleteInvoiceResult:
6 Deleted for a Draft, 4 Void for an Approved invoice) and never from the status the caller expected: that holds for
the outcome and for previous_status alike (6 means the invoice was a Draft, 4 that it was Approved, and so already
pushed to the accounting system). Any other StatusId, or none, is a shape error that says the invoice may have been
deleted or voided. The invoice and due date filters of list_invoices take a calendar date (YYYY-MM-DD) and go out as
<date>T00:00:00Z; the created and updated filters are instants with a UTC offset.
"""

import math
import re
from collections.abc import Mapping
from datetime import date
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import File
from pydantic import BaseModel, ConfigDict, Field, Strict

from gorelo_client import GoreloAPIError, Page
from tools._common import (
    StrictBool,
    StrictId,
    build_body,
    clamp_page_size,
    client_of,
    created_id,
    csv_ids,
    describe_value,
    expect_object,
    gorelo_tool,
    guid,
    non_empty,
    paged_result,
    positive_id,
    positive_ids,
    reread_after_write,
    require_confirm,
    utc_iso,
)

LIST_OP = "GET /v1/invoices"
GET_OP = "GET /v1/invoices/{invoiceId}"
CREATE_OP = "POST /v1/invoices"
PDF_OP = "GET /v1/invoices/{invoiceId}/pdf"
DELETE_OP = "DELETE /v1/invoices/{invoiceId}"

PDF_MAX_BYTES = 5 * 1024 * 1024

# Invoice status ids (the contract: match on Id, never on Name).
INVOICE_STATUSES = {1: "Draft", 3: "Paid", 4: "Void", 5: "Approved"}
# The only statuses Gorelo removes: a Draft is deleted (no longer listed), an Approved invoice is voided (still listed).
REMOVABLE_STATUS_IDS = {"Draft": 1, "Approved": 5}
# What DELETE /v1/invoices/{invoiceId} answers (DeleteInvoiceResult, contract e15cb5a18ec2): {Id, StatusId}, the status the
# invoice has AFTER the call. A Draft is deleted (6, a status that exists only in this answer: a deleted invoice is never
# listed) and an Approved invoice is voided (4, it stays listed). Nothing else is a valid answer. The spec says nothing
# about a deleted Draft being unrecoverable, so the outcome text for 6 says what the answer says and no more.
DELETED_STATUS_ID = 6
VOID_STATUS_ID = 4
DELETE_ANSWERS = {
    DELETED_STATUS_ID: ("Deleted", "deleted (StatusId 6 Deleted, no longer listed)"),
    VOID_STATUS_ID: ("Void", "voided (status Void, still listed)"),
}
# The answer each expected_status normally gets.
EXPECTED_DELETE_ANSWER = {"Draft": DELETED_STATUS_ID, "Approved": VOID_STATUS_ID}
# What an answer shows the invoice WAS when the DELETE reached Gorelo (DeleteInvoiceResult.StatusId: 6 "when a Draft invoice
# was deleted", 4 "when an Approved invoice was voided"). It is the previous_status of the result, read from the answer and
# never from expected_status: the invoice can change between the lookup and the delete, and an invoice approved in the
# meantime is voided (4) although it was looked up as a Draft.
ANSWER_SHOWS_WAS = {DELETED_STATUS_ID: "Draft", VOID_STATUS_ID: "Approved"}
# delete_invoice only needs to know whether Number matched zero, one or several invoices.
LOOKUP_PAGE_SIZE = 10
# What a delete_invoice call without confirm=true says would happen, by the status the caller expects. An Approved invoice
# has been pushed to the connected accounting system, and the void happens in Gorelo ONLY: the copy in the
# accounting system stayed open after the DELETE answered StatusId 4 (observed with Xero). The user has to
# hear that BEFORE confirming, so the Approved text says it; a Draft was never pushed, so its text does not.
DELETE_EFFECT = {
    "Draft": (
        "A Draft invoice is deleted (no longer listed); an Approved invoice is voided (status Void, still listed). "
        "Nothing has been sent to Gorelo."
    ),
    "Approved": (
        "An Approved invoice is voided (status Void, still listed) in Gorelo ONLY: the copy already pushed to the "
        "accounting system stays open (observed with Xero), so the user must void it there too. "
        "Nothing has been sent to Gorelo."
    ),
}

# {tool param: Gorelo query name} for every filter of GET /v1/invoices. The map also turns the
# PropertyName of a Gorelo 400 back into the snake_case param in error messages.
FILTER_FIELDS = {
    "client_ids": "ClientIds",
    "status_ids": "StatusIds",
    "contract_ids": "ContractIds",
    "number": "Number",
    "invoice_date_since": "InvoiceDateSince",
    "invoice_date_before": "InvoiceDateBefore",
    "due_date_since": "DueDateSince",
    "due_date_before": "DueDateBefore",
    "is_email_sent": "IsEmailSent",
    "created_since": "CreatedSince",
    "created_before": "CreatedBefore",
    "updated_since": "UpdatedSince",
    "updated_before": "UpdatedBefore",
    "query": "Query",
    "sort_by": "SortBy",
    "sort_order": "SortOrder",
}
LIST_FIELD_MAP = {**FILTER_FIELDS, "page_size": "PageSize", "cursor": "Cursor"}

# The four filters that compare calendar dates. The tool takes YYYY-MM-DD and sends <date>T00:00:00Z.
DAY_FILTERS = ("invoice_date_since", "invoice_date_before", "due_date_since", "due_date_before")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

# The StatusId each create tool writes. It is NOT a parameter of either tool: create_invoice can only make a Draft and
# only create_approved_invoice (destructive, confirm=true) can make an Approved invoice.
DRAFT_STATUS_ID = 1
APPROVED_STATUS_ID = 5
# BillableStatusId of an invoice line (the same scale as a time entry's).
BILLABLE_STATUSES = {1: "Billable", 2: "No charge", 3: "Non-billable"}

# {tool param: CreateInvoiceCommand field}. A Gorelo PropertyName turns back into the snake_case param through it, and
# one that points into a line ("LineItems[1].Quantity") reads "line_items (item 2, Quantity)". StatusId is left out on
# purpose: it is not a param, so an error about it keeps Gorelo's own name.
CREATE_FIELDS = {
    "client_id": "ClientId",
    "line_items": "LineItems",
    "invoice_date": "InvoiceDate",
    "due_date": "DueDate",
    "reference": "Reference",
}
APPROVED_FIELDS = {**CREATE_FIELDS, "recipient_emails": "RecipientEmails"}

# What the line_items parameter of both create tools says about a line field that is left out. It follows the field
# texts of CreateInvoiceLineItem (contract e15cb5a18ec2): the description, the price, the cost, the tax and the COA code
# come from the item itself, but the discount and the billable status are plain defaults (0, and Billable). One text for
# both tools, so they cannot drift apart.
LINE_ITEMS_TEXT = (
    "At least one line. Description, price, cost, tax and COA code left out use the item's own values; "
    "discount defaults to 0 and billable status to Billable."
)

# {line param: CreateInvoiceLineItem field}. no_tax has no field of its own: it sends TaxId as an explicit null.
LINE_FIELDS = {
    "item_id": "ItemId",
    "quantity": "Quantity",
    "description": "Description",
    "unit_price": "UnitPrice",
    "unit_cost": "UnitCost",
    "discount_percent": "DiscountPercent",
    "tax_id": "TaxId",
    "coa_code": "CoaCode",
    "billable_status_id": "BillableStatusId",
}
LINE_PARAMS = (*LINE_FIELDS, "no_tax")
# A single address, nothing else: no display name, no list. (Same shape the ticket tools accept.)
_EMAIL = re.compile(r"[^@\s,;<>()\[\]\"]+@[^@\s,;<>()\[\]\"]+")


class InvoiceLine(BaseModel):
    # One line of a new invoice (CreateInvoiceLineItem in snake_case). A field left out falls back to the item's own value.
    # No docstring on purpose: pydantic would copy it into the advertised schema, which claude.ai pays for in every
    # conversation, and the field descriptions already say what the line needs.

    model_config = ConfigDict(extra="forbid")

    item_id: Annotated[str, Field(description="Item GUID (list_items). A bundle is one line.")]
    quantity: Annotated[float, Strict(), Field(gt=0, description="Above 0.")]
    description: Annotated[str | None, Field(description="Line text. Default: the item's name.")] = None
    unit_price: Annotated[float | None, Strict(), Field(description="Default: the item's price.")] = None
    unit_cost: Annotated[float | None, Strict(), Field(description="Default: the item's cost.")] = None
    discount_percent: Annotated[float | None, Strict(), Field(ge=0, le=100, description="0-100. Default 0.")] = None
    tax_id: Annotated[StrictId | None, Field(description="From list_taxes. Default: the item's tax.")] = None
    no_tax: Annotated[StrictBool, Field(description="true: no tax, whatever the item says. Not with tax_id.")] = False
    coa_code: Annotated[str | None, Field(description="Chart of accounts code. Default: the item's.")] = None
    billable_status_id: Annotated[
        StrictId | None, Field(description="1 Billable (default), 2 No charge, 3 Non-billable.")
    ] = None


# --------------------------------------------------------------------------
# Local validation helpers (each raises ValueError naming the snake_case param)
# --------------------------------------------------------------------------


def _calendar_date(param: str, value: Any) -> str | None:
    """A calendar date as YYYY-MM-DD, returned as given. A time, an offset or any other text is refused."""
    if value is None:
        return None
    if isinstance(value, str) and _DAY.fullmatch(value):
        try:
            date.fromisoformat(value)
        except ValueError:
            pass
        else:
            return value
    raise ValueError(f"{param}: expected a calendar date as YYYY-MM-DD, for example 2026-09-01 (no time, no offset)")


def _status_ids(param: str, values: Any) -> list[int] | None:
    """Status ids must be on the published scale: an unknown id would come back as a silent empty page."""
    ids = positive_ids(param, values)
    if ids is None:
        return None
    unknown = [value for value in ids if value not in INVOICE_STATUSES]
    if unknown:
        valid = ", ".join(f"{key} {name}" for key, name in INVOICE_STATUSES.items())
        raise ValueError(f"{param}: unknown status id(s) {unknown}; valid ids: {valid}")
    return ids


def _to_query(filters: dict[str, Any]) -> dict[str, Any]:
    """Gorelo's query names; an id list becomes one comma separated value (csv_ids names the param on error).

    Unset filters stay in the dict as None on purpose: the client checks every NAME before it drops them.
    """
    return {
        FILTER_FIELDS[param]: csv_ids(param, value) if isinstance(value, (list, tuple)) else value
        for param, value in filters.items()
    }


def _pdf_names(filename: str | None, invoice_guid: str) -> tuple[str, str]:
    """(filename to show, file stem for the embedded file).

    The stem ends up in the resource URI (file:///<stem>.pdf), so it keeps only A-Z a-z 0-9 . _ -. A
    missing or unusable name falls back to invoice-<guid>.
    """
    shown = "".join(char for char in (filename or "") if char.isprintable()).strip()[:120]
    stem = shown[:-4] if shown.lower().endswith(".pdf") else shown
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    if not stem:
        stem = f"invoice-{invoice_guid}"
    return shown or f"{stem}.pdf", stem


# --------------------------------------------------------------------------
# list_invoices
# --------------------------------------------------------------------------


@gorelo_tool(toolset="billing", kind="read", ops=[LIST_OP], field_map=LIST_FIELD_MAP)
async def list_invoices(
    ctx: Context,
    client_ids: Annotated[list[StrictId] | None, Field(description="From list_clients.")] = None,
    status_ids: Annotated[
        list[StrictId] | None, Field(description="1 Draft, 3 Paid, 4 Void, 5 Approved.")
    ] = None,
    contract_ids: Annotated[list[StrictId] | None, Field(description="From list_contracts.")] = None,
    number: Annotated[StrictId | None, Field(description="Bare number: 1042 for INV-1042.")] = None,
    invoice_date_since: Annotated[str | None, Field(description="YYYY-MM-DD, inclusive.")] = None,
    invoice_date_before: Annotated[str | None, Field(description="YYYY-MM-DD, exclusive.")] = None,
    due_date_since: Annotated[str | None, Field(description="YYYY-MM-DD, inclusive.")] = None,
    due_date_before: Annotated[str | None, Field(description="YYYY-MM-DD, exclusive.")] = None,
    is_email_sent: Annotated[bool | None, Field(description="true: emailed; false: not yet.")] = None,
    created_since: Annotated[str | None, Field(description="At or after.")] = None,
    created_before: Annotated[str | None, Field(description="At or before.")] = None,
    updated_since: Annotated[str | None, Field(description="At or after.")] = None,
    updated_before: Annotated[str | None, Field(description="At or before.")] = None,
    query: Annotated[str | None, Field(description="Number, reference or name.")] = None,
    sort_by: Annotated[
        Literal["createdOn", "updatedOn", "date", "dueDate", "totalAmount"] | None,
        Field(description="Default createdOn."),
    ] = None,
    sort_order: Annotated[Literal["asc", "desc"] | None, Field(description="Default desc.")] = None,
    page_size: Annotated[int, Field(description="1-200, clamped.")] = 50,
    cursor: Annotated[
        str | None, Field(description="next_cursor from the previous call; same filters and sort; repeat until has_more is false.")
    ] = None,
) -> dict:
    """List invoices, one page at a time; filter by number, client, status or dates.
    Rows carry Id (the GUID get_invoice and export_invoice_pdf need), Number, Status and amounts; only get_invoice
    returns the lines. A Void invoice can still show AmountDue: read Status first.
    """
    filters = {
        "client_ids": positive_ids("client_ids", client_ids),
        "status_ids": _status_ids("status_ids", status_ids),
        "contract_ids": positive_ids("contract_ids", contract_ids),
        "number": None if number is None else positive_id("number", number),
        "invoice_date_since": _calendar_date("invoice_date_since", invoice_date_since),
        "invoice_date_before": _calendar_date("invoice_date_before", invoice_date_before),
        "due_date_since": _calendar_date("due_date_since", due_date_since),
        "due_date_before": _calendar_date("due_date_before", due_date_before),
        "is_email_sent": is_email_sent,
        "created_since": utc_iso("created_since", created_since),
        "created_before": utc_iso("created_before", created_before),
        "updated_since": utc_iso("updated_since", updated_since),
        "updated_before": utc_iso("updated_before", updated_before),
        "query": non_empty("query", query),
        "sort_by": sort_by,
        "sort_order": sort_order,
    }
    # the result echoes `filters` (what the next call can repeat); Gorelo gets the dates as midnight UTC
    wire = {
        **filters,
        **{param: f"{filters[param]}T00:00:00Z" for param in DAY_FILTERS if filters[param] is not None},
    }
    page = await client_of(ctx).get_page(
        LIST_OP,
        query=_to_query(wire),
        page_size=clamp_page_size(page_size),
        cursor=non_empty("cursor", cursor),
        tool="list_invoices",
    )
    return paged_result(page, filters)


# --------------------------------------------------------------------------
# get_invoice
# --------------------------------------------------------------------------


@gorelo_tool(toolset="billing", kind="read", ops=[GET_OP], field_map={"invoice_id": "invoiceId"})
async def get_invoice(
    ctx: Context,
    invoice_id: Annotated[str, Field(description="Id of a list_invoices row (GUID).")],
) -> dict:
    """Get one invoice in full: status, totals, amount due, every line and the attachments.
    A bundle is one line and its parts are in SubItems. An attachment Url is a temporary link: use it now, never store it.
    A Void invoice can still show AmountDue: read Status first.
    """
    data = await client_of(ctx).get_one(
        GET_OP, path_params={"invoiceId": guid("invoice_id", invoice_id)}, tool="get_invoice"
    )
    return expect_object(data, GET_OP, tool="get_invoice")


# --------------------------------------------------------------------------
# create_invoice and create_approved_invoice: the request they share
# --------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    """A finite int or float, never a bool."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


def _line_text(where: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: must be text that is not empty or whitespace only")
    return value


def _invoice_line(where: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """One entry of LineItems from one line_items entry. Every problem names `line_items[<n>].<field>`."""
    unknown = sorted(str(key) for key in data if key not in LINE_PARAMS)
    if unknown:
        raise ValueError(f"{where}: unknown field(s) {', '.join(unknown)}; allowed: {', '.join(LINE_PARAMS)}")
    missing = [key for key in ("item_id", "quantity") if data.get(key) is None]
    if missing:
        raise ValueError(f"{where}: missing {' and '.join(missing)}")
    quantity = data["quantity"]
    if not _is_number(quantity) or quantity <= 0:
        raise ValueError(f"{where}.quantity: must be a number greater than 0")
    line: dict[str, Any] = {"ItemId": guid(f"{where}.item_id", data["item_id"]), "Quantity": quantity}
    for key in ("description", "coa_code"):
        if data.get(key) is not None:
            line[LINE_FIELDS[key]] = _line_text(f"{where}.{key}", data[key])
    for key in ("unit_price", "unit_cost"):
        if data.get(key) is not None:
            if not _is_number(data[key]):
                raise ValueError(f"{where}.{key}: must be a number")
            line[LINE_FIELDS[key]] = data[key]
    discount = data.get("discount_percent")
    if discount is not None:
        if not _is_number(discount) or not 0 <= discount <= 100:
            raise ValueError(f"{where}.discount_percent: must be a number from 0 to 100")
        line["DiscountPercent"] = discount
    tax_id, no_tax = data.get("tax_id"), data.get("no_tax")
    if no_tax is not None and not isinstance(no_tax, bool):
        raise ValueError(f"{where}.no_tax: must be true or false, got {describe_value(no_tax)}")
    if tax_id is not None and no_tax:
        raise ValueError(
            f"{where}: give tax_id or no_tax, not both (tax_id picks one tax, no_tax sends the line with no tax at all)"
        )
    if tax_id is not None:
        line["TaxId"] = positive_id(f"{where}.tax_id", tax_id)
    elif no_tax:
        line["TaxId"] = None  # an explicit null is how Gorelo is told "no tax", whatever the item's own tax is
    status = data.get("billable_status_id")
    if status is not None:
        if isinstance(status, bool) or not isinstance(status, int) or status not in BILLABLE_STATUSES:
            shown = status if isinstance(status, int) and not isinstance(status, bool) else describe_value(status)
            valid = ", ".join(f"{key} {name}" for key, name in BILLABLE_STATUSES.items())
            raise ValueError(f"{where}.billable_status_id: must be 1, 2 or 3 ({valid}), got {shown}")
        line["BillableStatusId"] = status
    return line


def _invoice_lines(param: str, value: Any) -> list[dict[str, Any]]:
    """The LineItems array of the request from the tool's line_items (objects or InvoiceLine models)."""
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, (list, tuple)):
        raise ValueError(
            f"{param}: expected a list of lines, each an object with item_id and quantity, got {describe_value(value)}"
        )
    if not value:
        raise ValueError(f"{param}: at least one line is required, each an object with item_id and quantity")
    lines: list[dict[str, Any]] = []
    for position, entry in enumerate(value):
        where = f"{param}[{position}]"
        data = entry.model_dump() if isinstance(entry, BaseModel) else entry
        if not isinstance(data, Mapping):
            raise ValueError(f"{where}: must be an object with item_id and quantity, got {describe_value(entry)}")
        lines.append(_invoice_line(where, data))
    return lines


def _invoice_dates(invoice_date: Any, due_date: Any) -> tuple[str | None, str | None]:
    """invoice_date and due_date as checked YYYY-MM-DD text. Compared only when both are given: the provider's
    own "today" (what an omitted invoice_date means) is not something this server can know."""
    start = _calendar_date("invoice_date", invoice_date)
    due = _calendar_date("due_date", due_date)
    if start is not None and due is not None and date.fromisoformat(due) < date.fromisoformat(start):
        raise ValueError(f"due_date: {due} is before invoice_date {start}; the due date must be the invoice date or later")
    return start, due


def _recipient_emails(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, (list, tuple)):
        raise ValueError("recipient_emails: expected a list of email addresses such as ['a@example.com']")
    if not value:
        raise ValueError("recipient_emails: must not be an empty list; omit it to name no recipients")
    found: list[str] = []
    for position, item in enumerate(value):
        text = item.strip() if isinstance(item, str) else ""
        if not _EMAIL.fullmatch(text):
            # never echoed: an address is personal data and errors end up in the server log
            raise ValueError(
                f"recipient_emails[{position}]: expected exactly one email address such as name@example.com "
                "(no display name, no list of addresses)"
            )
        found.append(text)
    return found


def _invoice_request(
    *,
    client_id: Any,
    line_items: Any,
    invoice_date: Any,
    due_date: Any,
    reference: Any,
    recipient_emails: Any = None,
) -> dict[str, Any]:
    """The body of POST /v1/invoices WITHOUT its StatusId (each tool adds the one it is allowed to send).

    Pure: no HTTP. Raises ValueError naming the snake_case param, so a create tool can run it before it asks for
    confirmation or sends anything.
    """
    cid = positive_id("client_id", client_id)
    lines = _invoice_lines("line_items", line_items)
    start, due = _invoice_dates(invoice_date, due_date)
    if reference is not None and not isinstance(reference, str):
        raise ValueError(f"reference: must be text, got {describe_value(reference)}")
    return build_body(
        {
            "client_id": cid,
            "line_items": lines,
            "invoice_date": None if start is None else f"{start}T00:00:00Z",
            "due_date": None if due is None else f"{due}T00:00:00Z",
            "reference": non_empty("reference", reference),
            "recipient_emails": _recipient_emails(recipient_emails),
        },
        APPROVED_FIELDS,
    )


async def _post_and_read_back(ctx: Context, tool: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST the invoice, then read it back (POST answers only {Id}): the full invoice, or {Id, warning} if the read fails.

    Never retried: a POST that did not answer cleanly may already have created the invoice (the client reports it as
    unconfirmed and the tool text says to check list_invoices before creating it again).
    """
    written = await client_of(ctx).post(CREATE_OP, json_body=body, tool=tool)
    new_id = created_id(written, CREATE_OP, tool=tool)
    return await reread_after_write(ctx, GET_OP, path_params={"invoiceId": new_id}, tool=tool, written_id=new_id)


# --------------------------------------------------------------------------
# create_invoice
# --------------------------------------------------------------------------


@gorelo_tool(toolset="billing", kind="write", ops=[CREATE_OP, GET_OP], field_map=CREATE_FIELDS)
async def create_invoice(
    ctx: Context,
    client_id: Annotated[StrictId, Field(description="Client to bill (list_clients).")],
    line_items: Annotated[list[InvoiceLine], Field(description=LINE_ITEMS_TEXT)],
    invoice_date: Annotated[str | None, Field(description="YYYY-MM-DD. Default: today.")] = None,
    due_date: Annotated[
        str | None, Field(description="YYYY-MM-DD, not before invoice_date. Default: invoice_date.")
    ] = None,
    reference: Annotated[str | None, Field(description="Free text on the invoice, e.g. a PO number.")] = None,
) -> dict:
    """Create a DRAFT invoice for a client from catalog items and return it with its lines and totals.
    A Draft is not pushed to accounting or sent to anyone, and nothing here makes it Approved: for that use
    create_approved_invoice, when enabled.
    Resolve ids first: client_id -> list_clients, item_id -> list_items, tax_id -> list_taxes. Unset price, cost, tax
    and COA code fall back to the item's own values.
    Side effects: creates a real invoice that gets a number; to remove a mistaken Draft use delete_invoice, when
    deletes are enabled. If the result is {Id, warning} the draft exists: do not create it again (read it with
    get_invoice). After a "did not confirm" error check list_invoices for the client (newest first) before creating
    it again.
    """
    request = _invoice_request(
        client_id=client_id,
        line_items=line_items,
        invoice_date=invoice_date,
        due_date=due_date,
        reference=reference,
    )
    return await _post_and_read_back(ctx, "create_invoice", {**request, "StatusId": DRAFT_STATUS_ID})


# --------------------------------------------------------------------------
# create_approved_invoice
# --------------------------------------------------------------------------


@gorelo_tool(toolset="billing", kind="destructive", ops=[CREATE_OP, GET_OP], field_map=APPROVED_FIELDS)
async def create_approved_invoice(
    ctx: Context,
    client_id: Annotated[StrictId, Field(description="Client to bill (list_clients).")],
    line_items: Annotated[list[InvoiceLine], Field(description=LINE_ITEMS_TEXT)],
    invoice_date: Annotated[str | None, Field(description="YYYY-MM-DD. Default: today.")] = None,
    due_date: Annotated[
        str | None, Field(description="YYYY-MM-DD, not before invoice_date. Default: invoice_date.")
    ] = None,
    reference: Annotated[str | None, Field(description="Free text on the invoice, e.g. a PO number.")] = None,
    recipient_emails: Annotated[
        list[str] | None,
        Field(description="Addresses Gorelo is to send the invoice to; it may email them at once. Omit for none."),
    ] = None,
    confirm: Annotated[StrictBool, Field(description="Must be true; only after the user approves.")] = False,
) -> dict:
    """Create an APPROVED invoice for a client and return it. Ids, lines, dates and reference work exactly as in
    create_invoice.
    Side effects: approving on create pushes the invoice to the connected accounting system at once. Nothing takes
    that back: delete_invoice, when deletes are enabled, voids it in Gorelo ONLY, so the copy in the accounting
    system stays open (observed with Xero): tell the user, who must void it there too. A total of exactly 0 is created
    as Paid instead: check Status in the result.
    Emails: recipient_emails, when given, are the addresses Gorelo sends the invoice to (it does not say whether
    creating the invoice already sends it): tell the user who.
    If the result is {Id, warning} the invoice exists: do not create it again (read it with get_invoice). After a
    "did not confirm" error check list_invoices for the client (newest first) first.
    Ask the user first; needs confirm=true.
    """
    request = _invoice_request(
        client_id=client_id,
        line_items=line_items,
        invoice_date=invoice_date,
        due_date=due_date,
        reference=reference,
        recipient_emails=recipient_emails,
    )
    lines = len(request["LineItems"])
    recipients = len(request.get("RecipientEmails", []))
    # the addresses are counted, never quoted: they are personal data and errors end up in the server log
    mail = (
        f" It may also email the invoice to the {recipients} address{'' if recipients == 1 else 'es'} in recipient_emails."
        if recipients
        else ""
    )
    require_confirm(
        confirm,
        action=f"create an APPROVED invoice for client {request['ClientId']} with {lines} line{'' if lines == 1 else 's'}",
        effect=(
            "Approving on create pushes the invoice to the connected accounting system at once, and nothing takes that "
            "back: voiding it with delete_invoice voids it in Gorelo ONLY, so the copy in the accounting system stays "
            f"open (observed with Xero) and the user must void it there too.{mail} Nothing has been sent to "
            "Gorelo."
        ),
    )
    return await _post_and_read_back(ctx, "create_approved_invoice", {**request, "StatusId": APPROVED_STATUS_ID})


# --------------------------------------------------------------------------
# export_invoice_pdf
# --------------------------------------------------------------------------


@gorelo_tool(toolset="billing", kind="write", ops=[PDF_OP], field_map={"invoice_id": "InvoiceId"})
async def export_invoice_pdf(
    ctx: Context,
    invoice_id: Annotated[str, Field(description="Id of a list_invoices row.")],
) -> ToolResult:
    """Download one invoice PDF (5 MB cap): a short summary plus the PDF as an embedded file.
    Side effects: each download is recorded on the invoice as an export event, so never call it
    speculatively or in a loop; after a timeout or failure it may already be recorded and a retry
    records another.
    """
    invoice_guid = guid("invoice_id", invoice_id)
    download = await client_of(ctx).get_binary(
        PDF_OP,
        path_params={"invoiceId": invoice_guid},
        max_bytes=PDF_MAX_BYTES,
        tool="export_invoice_pdf",
    )
    shown, stem = _pdf_names(download.filename, invoice_guid)
    summary = (
        f"Exported invoice PDF {shown} ({len(download.content):,} bytes), attached as an embedded file. "
        "Gorelo recorded this download on the invoice as an export event."
    )
    return ToolResult(content=[summary, File(data=download.content, format="pdf", name=stem)])


# --------------------------------------------------------------------------
# delete_invoice
# --------------------------------------------------------------------------


def _lookup_shape_error(detail: str) -> GoreloAPIError:
    """Gorelo's answer to the Number lookup cannot be trusted: nothing has been deleted or voided."""
    return GoreloAPIError(
        f"{LIST_OP}: {detail}; refusing to delete or void anything",
        status=200,
        op_key=LIST_OP,
        kind="shape",
    )


def _describe_invoice(row: Any) -> str:
    if not isinstance(row, dict):
        return "an unreadable row"
    status = row.get("Status")
    status_name = status.get("Name") if isinstance(status, dict) else None
    return f"{row.get('DisplayNumber') or row.get('Number')} (Id {row.get('Id')}, status {status_name})"


def _the_invoice_to_remove(page: Page, number: int, expected_status: str) -> str:
    """The Id of the one invoice numbered `number`, after every safety check.

    Raises ValueError (naming invoice_number or expected_status) when there is no match, several
    matches or a different status, and GoreloAPIError kind "shape" when the row is not what the
    lookup asked for. Nothing has been sent to Gorelo to change anything at that point.
    """
    rows = page.items
    if not rows:
        raise ValueError(
            f"invoice_number: no invoice has the number {number} (deleted invoices are not listed); "
            f"nothing was deleted or voided. Check it with list_invoices(number={number})."
        )
    if len(rows) > 1 or page.has_more:
        found = "; ".join(_describe_invoice(row) for row in rows[:5])
        raise ValueError(
            f"invoice_number: {number} matches more than one invoice ({found}); refusing to guess which one "
            "to delete or void. Nothing was deleted or voided. Handle it in the Gorelo app."
        )
    row = rows[0]
    if not isinstance(row, dict):
        raise _lookup_shape_error("the invoice row is not an object")
    returned_number = row.get("Number")
    if isinstance(returned_number, bool) or returned_number != number:
        raise _lookup_shape_error(f"asked for the invoice numbered {number} but Gorelo returned a different one")
    status = row.get("Status")
    status_id = status.get("Id") if isinstance(status, dict) else None
    if isinstance(status_id, bool) or not isinstance(status_id, int):
        raise _lookup_shape_error("the invoice row has no readable Status.Id")
    if status_id != REMOVABLE_STATUS_IDS[expected_status]:
        status_name = status.get("Name") if isinstance(status.get("Name"), str) else None
        actual = status_name or INVOICE_STATUSES.get(status_id) or "an unknown status"
        raise ValueError(
            f"expected_status: invoice {number} has status {actual} (id {status_id}), not {expected_status}; "
            "nothing was deleted or voided. Gorelo only removes Draft invoices (deleted, no longer listed) and "
            "Approved invoices (voided, still listed)."
        )
    try:
        return guid("Id", row.get("Id"))
    except ValueError:
        raise _lookup_shape_error("the invoice row has no usable Id") from None


def _delete_report(record: Mapping[str, Any], number: int, expected_status: str) -> dict[str, Any]:
    """What the DELETE did, read from the StatusId of Gorelo's answer and from nothing else:
    {previous_status, outcome[, note]}.

    6 (Deleted) means a Draft was deleted and 4 (Void) means an Approved invoice was voided, so the answer also says
    what the invoice was when the DELETE reached Gorelo (previous_status, see ANSWER_SHOWS_WAS). The status the caller
    expected never describes the outcome or the previous status: it can have changed between the lookup and the delete
    (a Draft approved in the meantime is voided, not deleted, and by then it was an Approved invoice that had already
    been pushed to the accounting system), and only the answer says what happened. When the answer is not the one
    expected_status leads to, a `note` says so and says what the answer shows. Any other StatusId, or none, raises a
    shape error that says the invoice may have been deleted or voided: the DELETE was sent and answered 2xx, so it must
    not be repeated blindly, and nothing is reported that Gorelo did not say.
    """
    status = record.get("StatusId")
    if isinstance(status, bool) or not isinstance(status, int) or status not in DELETE_ANSWERS:
        if "StatusId" not in record:
            shown = "missing"
        elif isinstance(status, int) and not isinstance(status, bool):
            shown = str(status)
        else:
            shown = describe_value(status)  # never quotes what Gorelo sent
        raise GoreloAPIError(
            f"{DELETE_OP}: the answer's StatusId is {shown}, expected {DELETED_STATUS_ID} (Deleted) or "
            f"{VOID_STATUS_ID} (Void); the delete may have been applied, so verify it with a read before repeating it "
            f"(list_invoices(number={number}))",
            status=200,
            op_key=DELETE_OP,
            kind="shape",
            write_unconfirmed=True,
        )
    was = ANSWER_SHOWS_WAS[status]
    report = {"previous_status": was, "outcome": DELETE_ANSWERS[status][1]}
    normal = EXPECTED_DELETE_ANSWER[expected_status]
    if status != normal:
        # an Approved invoice has been pushed to the connected accounting system (approving does that, in the app and
        # on create alike), so a voided invoice that was looked up as a Draft is no longer a harmless Draft
        meaning = "a Draft invoice was deleted" if status == DELETED_STATUS_ID else "an Approved invoice was voided"
        pushed = (
            " and it had already been pushed to the connected accounting system, where the copy stays open (the void "
            "happened in Gorelo ONLY), so the user must void it there too"
            if was == "Approved"
            else ""
        )
        report["note"] = (
            f"expected_status was {expected_status}, so Gorelo should have answered StatusId {normal} "
            f"({DELETE_ANSWERS[normal][0]}), but it answered {status} ({DELETE_ANSWERS[status][0]}): the invoice "
            f"changed after it was looked up. StatusId {status} means {meaning}, so its status was {was} when the "
            f"delete reached Gorelo{pushed}. Check it in the Gorelo app."
        )
    return report


@gorelo_tool(
    toolset="billing",
    kind="destructive",
    ops=[LIST_OP, DELETE_OP],
    field_map={"invoice_number": "Number"},
)
async def delete_invoice(
    ctx: Context,
    invoice_number: Annotated[
        StrictId, Field(description="Number field of the list_invoices row, e.g. 1042 for INV-1042 (not DisplayNumber).")
    ],
    expected_status: Annotated[
        Literal["Draft", "Approved"],
        Field(
            description="That row's current Status; if it differs nothing changes. "
            "Draft: deleted (no longer listed). Approved: voided (status Void, still listed)."
        ),
    ],
    confirm: Annotated[StrictBool, Field(description="Must be true; only after the user approves.")] = False,
) -> dict:
    """Delete a Draft invoice or void an Approved one, by invoice number, only on the user's request. It
    must be the only match and have status expected_status, else nothing changes. Draft: deleted (no longer
    listed). Approved: voided (status Void, still listed). The result's StatusId is Gorelo's answer: 6 Deleted
    or 4 Void; previous_status is read from it too (6 was a Draft, 4 was Approved). Void is refused here (Gorelo
    itself treats deleting a void invoice as success); Paid and others are refused by Gorelo with 409.
    A void happens in Gorelo ONLY: the copy already pushed to the accounting system stays open (observed with Xero), so say so when you ask the user, who must void it there too. A Void invoice can still show
    AmountDue: read Status first.
    Ask the user first; needs confirm=true.
    """
    number = positive_id("invoice_number", invoice_number)
    if expected_status not in REMOVABLE_STATUS_IDS:
        raise ValueError("expected_status: must be 'Draft' or 'Approved'")
    require_confirm(
        confirm,
        action=f"delete or void invoice number {number} (expected status {expected_status})",
        effect=DELETE_EFFECT[expected_status],
    )
    client = client_of(ctx)
    page = await client.get_page(
        LIST_OP, query={"Number": str(number)}, page_size=LOOKUP_PAGE_SIZE, tool="delete_invoice"
    )
    invoice_id = _the_invoice_to_remove(page, number, expected_status)
    data = await client.delete(DELETE_OP, path_params={"invoiceId": invoice_id}, tool="delete_invoice")
    record = expect_object(data, DELETE_OP, tool="delete_invoice")
    # previous_status comes from the answer (via _delete_report), never from expected_status
    return {**record, "invoice_number": number, **_delete_report(record, number, expected_status)}
