"""Invoices: list, read, create (Draft or Approved), update, approve, PDF export, delete or void, payments (read only)
(toolset "billing").

Tools:

    list_invoices            read         GET /v1/invoices
    get_invoice              read         GET /v1/invoices/{invoiceId}
    create_invoice           write        POST /v1/invoices, GET /v1/invoices/{invoiceId}, GET /v1/taxes (no_tax only)
    create_approved_invoice  destructive  POST /v1/invoices, GET /v1/invoices/{invoiceId}, GET /v1/taxes (no_tax only)
    update_invoice           write        GET, PATCH /v1/invoices/{invoiceId}, GET /v1/taxes (no_tax only); never sends StatusId
    approve_invoice          destructive  GET, PATCH /v1/invoices/{invoiceId}; sends StatusId 5 and nothing else
    export_invoice_pdf       write        GET /v1/invoices/{invoiceId}/pdf
    delete_invoice           destructive  GET /v1/invoices, DELETE /v1/invoices/{invoiceId}
    list_payments            read         GET /v1/payments
    get_payment              read         GET /v1/payments/{paymentId}

Ops owned by this module:

    GET /v1/invoices
    GET /v1/invoices/{invoiceId}
    POST /v1/invoices
    PATCH /v1/invoices/{invoiceId}
    GET /v1/invoices/{invoiceId}/pdf
    DELETE /v1/invoices/{invoiceId}
    GET /v1/payments
    GET /v1/payments/{paymentId}

Release 1.5.0 (contract 6c30770dffc0). update_invoice and approve_invoice share one read-and-check
(_invoice_open_for_change): they read the invoice and refuse unless it is Draft or Submitted (the spec also lets
Gorelo change an Approved invoice, which this release does not offer). The PATCH of update_invoice is built from
UPDATE_FIELDS, which has no StatusId, so it cannot approve anything; approve_invoice sends {"StatusId": 5} and nothing
else. LineItems of the PATCH replaces the set, so update_invoice builds the full list from its read: every current line
is kept as {Id} unless update_lines changes it or remove_line_ids names it, new lines (add_lines) are appended, and a
line can never be dropped silently. no_tax: TaxId null is NOT "no tax" (measured live 2026-10-30: a create line of an item taxed
9.25 percent sent with TaxId null got that tax), so no_tax is resolved ONCE per tool call, before any write, to the id of the
account's Tax Exempt rate (_resolve_no_tax: GET /v1/taxes, the ONE rate whose Percentage is exactly 0, has no non-zero SubTaxes
and whose Code or Name, trimmed and case-insensitive, is "tax exempt"). None or several: the call is refused locally and names
the 0 percent candidates. The id goes out as TaxId on every no_tax line of create, update_lines and add_lines, so it is as safe
as tax_id, which is why create_approved_invoice accepts no_tax again. The result gets a `note` naming the tax id used. A line that
still shows a tax amount in the read-back adds a `warning` (a loud one on the approved tool, whose invoice is already pushed).
Payments are read only (POST and DELETE stay excluded).

update_invoice checks its read-back against the read it made first: every kept line keeps its Quantity, UnitPrice,
DiscountPercent, tax id, billable status id and Description, every field of update_lines and add_lines shows its value (money
within a small rounding tolerance), and the set of line Ids is the kept ones plus the new ones. Each difference is listed
(field, expected, read) in the write-unconfirmed error; text values are not quoted. A cleared template id is not checked
(Gorelo says the default applies). A read-back whose Status is not Draft or Submitted adds a `note`.

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
    MAX_ID,
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
    guids,
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
UPDATE_OP = "PATCH /v1/invoices/{invoiceId}"
TAXES_OP = "GET /v1/taxes"
PAYMENTS_OP = "GET /v1/payments"
PAYMENT_OP = "GET /v1/payments/{paymentId}"

PDF_MAX_BYTES = 5 * 1024 * 1024

# Invoice status ids (the contract: match on Id, never on Name). 2 Submitted was added by the 2026-10-17 spec.
INVOICE_STATUSES = {1: "Draft", 2: "Submitted", 3: "Paid", 4: "Void", 5: "Approved"}
INVOICE_STATUS_SCALE = ", ".join(f"{status} {label}" for status, label in INVOICE_STATUSES.items())
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

# What no_tax says, on a create line and on an update line. TaxId null means "the app's tax" (measured live 2026-10-30), so no_tax
# sends the id of the account's Tax Exempt rate, looked up in list_taxes (see _resolve_no_tax).
NO_TAX_TEXT = (
    "true: bill the line with the account's Tax Exempt rate (code or name 'Tax Exempt', 0%), looked up in list_taxes; "
    "refused when there is not exactly one. Not with tax_id."
)
# The name or code (trimmed, case-insensitive) that marks the tax-exempt rate.
EXEMPT_NAME = "tax exempt"
# The id the pure validators put on a no_tax line while the lookup has not happened yet (they run first, so a bad parameter
# costs no request; the request is built again with the real id).
_UNRESOLVED_TAX_ID = 1
_CANDIDATES_SHOWN = 10


def _is_zero(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0


def _is_exempt_rate(row: Mapping[str, Any]) -> bool:
    """True when the rate is exactly 0 percent with no non-zero component and is named "Tax Exempt" by Code or Name."""
    if not _is_zero(row.get("Percentage")):
        return False
    subs = row.get("SubTaxes")
    if subs is not None:
        if not isinstance(subs, list):
            return False
        for sub in subs:
            percent = sub.get("Percentage") if isinstance(sub, Mapping) else "?"
            if percent is not None and not _is_zero(percent):
                return False
    return any(
        isinstance(row.get(key), str) and row[key].strip().casefold() == EXEMPT_NAME for key in ("Code", "Name")
    )


def _tax_id_of(row: Mapping[str, Any]) -> int | None:
    value = row.get("Id")
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_ID else None


def _candidate_text(rows: list[Mapping[str, Any]]) -> str:
    """The 0 percent rates as 'Id 12 (Code "Tax Exempt")', Id and Code only, at most _CANDIDATES_SHOWN of them."""
    shown = []
    for row in rows[:_CANDIDATES_SHOWN]:
        code = row.get("Code")
        code_text = f'Code "{code.strip()[:40]}"' if isinstance(code, str) and code.strip() else "no Code"
        shown.append(f"Id {row.get('Id')} ({code_text})")
    more = f" and {len(rows) - _CANDIDATES_SHOWN} more" if len(rows) > _CANDIDATES_SHOWN else ""
    return ", ".join(shown) + more if shown else "none"


async def _resolve_no_tax(ctx: Context, tool: str, where: str) -> int:
    """The id of the account's Tax Exempt rate, for the no_tax lines of one tool call (ONE GET /v1/taxes). `where` names the
    first no_tax line ("line_items[0]"). A refusal (ValueError) says to pass tax_id instead; a failed lookup is the normal
    Gorelo error. Either way nothing has been written: this runs before the write."""
    rows = await client_of(ctx).get_list(TAXES_OP, tool=tool)
    rates = [row for row in rows if isinstance(row, Mapping)]
    found = [row for row in rates if _is_exempt_rate(row) and _tax_id_of(row) is not None]
    if len(found) == 1:
        return _tax_id_of(found[0])  # type: ignore[return-value]
    zero = [row for row in rates if _is_zero(row.get("Percentage"))]
    why = "the account has no Tax Exempt rate" if not found else f"the account has {len(found)} Tax Exempt rates"
    raise ValueError(
        f"{where}.no_tax: {why} (a 0% rate whose Code or Name is 'Tax Exempt'), so no_tax cannot pick one; pass tax_id of "
        f"the tax-exempt rate from list_taxes instead. 0% rates: {_candidate_text(zero)}. Nothing has been sent to Gorelo."
    )


def _with_note(record: Mapping[str, Any], note: str | None) -> dict[str, Any]:
    """The record with `note` added (after the one it has, if any)."""
    if note is None:
        return dict(record)
    return {**record, "note": f"{record['note']} {note}" if record.get("note") else note}


def _no_tax_note(tax_id: int | None) -> str | None:
    return None if tax_id is None else f"no_tax used tax id {tax_id} (the account's Tax Exempt rate, 0%, from list_taxes)."


# {line param: CreateInvoiceLineItem field}. no_tax has no field of its own: it sends the Tax Exempt rate's id as TaxId.
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
    no_tax: Annotated[StrictBool, Field(description=NO_TAX_TEXT)] = False
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


def _invoice_status(param: str, value: Any) -> int:
    """One invoice status of the list filter (1 Draft ... 5 Approved); 0 and the negative numbers are answered with the scale."""
    if isinstance(value, int) and not isinstance(value, bool) and -MAX_ID <= value < 1:
        status = int(value)
    else:
        status = positive_id(param, value)
    if status not in INVOICE_STATUSES:
        raise ValueError(f"{param}: expected {INVOICE_STATUS_SCALE}, got {status}")
    return status


def _status_ids(param: str, values: Any) -> list[int] | None:
    """The status_ids filter: every item on the published scale (Gorelo answers 400 for any other id), a bad one named by its index."""
    if isinstance(values, (list, tuple)) and values:
        return [_invoice_status(f"{param}[{index}]", item) for index, item in enumerate(values)]
    return positive_ids(param, values)


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
        list[StrictId] | None,
        Field(description="1 Draft, 2 Submitted, 3 Paid, 4 Void, 5 Approved. Omitted: every status; deleted invoices are never listed."),
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
    returns the lines. Without status_ids every status is listed; deleted invoices never are. A Void invoice can still show AmountDue: read Status first.
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


def _line_fields(where: str, data: Mapping[str, Any], no_tax_id: int | None) -> dict[str, Any]:
    """The optional fields that a create line and an update line share, checked. Every problem names `<where>.<field>`.

    A field that is not in data (or is None) is left out. `no_tax_id` is the id of the Tax Exempt rate that a no_tax line sends
    as TaxId (resolved once per tool call; a pure caller passes _UNRESOLVED_TAX_ID).
    """
    line: dict[str, Any] = {}
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
            f"{where}: give tax_id or no_tax, not both (tax_id picks one tax, no_tax picks the Tax Exempt rate)"
        )
    if tax_id is not None:
        line["TaxId"] = positive_id(f"{where}.tax_id", tax_id)
    elif no_tax:
        if no_tax_id is None:
            raise RuntimeError("no_tax_id missing: a no_tax line needs the resolved Tax Exempt rate id")
        line["TaxId"] = no_tax_id
    status = data.get("billable_status_id")
    if status is not None:
        if isinstance(status, bool) or not isinstance(status, int) or status not in BILLABLE_STATUSES:
            shown = status if isinstance(status, int) and not isinstance(status, bool) else describe_value(status)
            valid = ", ".join(f"{key} {name}" for key, name in BILLABLE_STATUSES.items())
            raise ValueError(f"{where}.billable_status_id: must be 1, 2 or 3 ({valid}), got {shown}")
        line["BillableStatusId"] = status
    return line


def _invoice_line(where: str, data: Mapping[str, Any], no_tax_id: int | None = None) -> dict[str, Any]:
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
    line.update(_line_fields(where, data, no_tax_id))
    return line


def _invoice_lines(param: str, value: Any, no_tax_id: int | None = None) -> list[dict[str, Any]]:
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
        lines.append(_invoice_line(where, data, no_tax_id))
    return lines


def _no_tax_positions(value: Any) -> list[int]:
    """The positions (0 based) of the lines of a line list that ask for no_tax (objects or InvoiceLine models)."""
    found: list[int] = []
    if isinstance(value, (list, tuple)):
        for position, entry in enumerate(value):
            data = entry.model_dump() if isinstance(entry, BaseModel) else entry
            if isinstance(data, Mapping) and data.get("no_tax") is True:
                found.append(position)
    return found


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
    no_tax_id: int | None = None,
) -> dict[str, Any]:
    """The body of POST /v1/invoices WITHOUT its StatusId (each tool adds the one it is allowed to send).

    Pure: no HTTP. Raises ValueError naming the snake_case param, so a create tool can run it before it asks for
    confirmation or sends anything. `no_tax_id` is the Tax Exempt rate id for the no_tax lines: a tool first calls this with
    None-or-placeholder to validate, then (when a line has no_tax) resolves the id and calls it again.
    """
    cid = positive_id("client_id", client_id)
    lines = _invoice_lines("line_items", line_items, no_tax_id if no_tax_id is not None else _UNRESOLVED_TAX_ID)
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


def _aligned(sent: list[Mapping[str, Any]], rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any] | None]:
    """For each line that was sent (no Id), the line of the read that is most likely it: the first unused one with the same
    ItemId, else the first unused one. None where the read has fewer lines. Order only matters among equal items."""
    used: set[int] = set()
    out: list[Mapping[str, Any] | None] = []
    for line in sent:
        pick = None
        item = str(line.get("ItemId") or "").lower()
        for index, row in enumerate(rows):
            if index not in used and item and str(row.get("ItemId") or "").lower() == item:
                pick = index
                break
        if pick is None:
            pick = next((index for index in range(len(rows)) if index not in used), None)
        if pick is None:
            out.append(None)
        else:
            used.add(pick)
            out.append(rows[pick])
    return out


def _tax_shown(row: Mapping[str, Any]) -> bool:
    """True when the line shows a tax amount that is not 0 (a rounding error of a cent counts as 0)."""
    amount = row.get("TaxAmount")
    return isinstance(amount, (int, float)) and not isinstance(amount, bool) and abs(amount) >= 0.005


def _no_tax_warning(taxed: list[str], tax_id: int | None = None, approved: bool = False) -> str | None:
    """The warning for the no_tax lines that still show a tax amount, or None. `approved`: the invoice is already pushed."""
    if not taxed:
        return None
    sent = f" although TaxId {tax_id} (the Tax Exempt rate) was sent" if tax_id is not None else ""
    head = (
        f"no_tax was asked for {', '.join(taxed)}, but the invoice still shows a tax amount on "
        f"{'it' if len(taxed) == 1 else 'them'}{sent}."
    )
    if approved:
        return (
            f"WARNING: {head} This invoice is APPROVED and already pushed to the accounting system: tell the user at once, "
            "check the amounts with get_invoice and list_taxes, and note that a void with delete_invoice voids it in Gorelo "
            "ONLY (the user must void the copy in the accounting system too)."
        )
    return (
        f"{head} The invoice is not approved, so fix it with update_invoice (update_lines with line_id and tax_id of your "
        "tax-exempt rate, from list_taxes) before approving it."
    )


def _with_warning(record: Mapping[str, Any], warning: str | None) -> dict[str, Any]:
    """The record with `warning` added (after the one it has, if any)."""
    if warning is None:
        return dict(record)
    return {**record, "warning": f"{record['warning']} {warning}" if record.get("warning") else warning}


async def _post_and_read_back(
    ctx: Context,
    tool: str,
    body: dict[str, Any],
    no_tax_lines: list[int] | None = None,
    no_tax_id: int | None = None,
    approved: bool = False,
) -> dict[str, Any]:
    """POST the invoice, then read it back (POST answers only {Id}): the full invoice, or {Id, warning} if the read fails.

    Never retried: a POST that did not answer cleanly may already have created the invoice (the client reports it as
    unconfirmed and the tool text says to check list_invoices before creating it again). `no_tax_lines` (positions of the
    lines sent with no_tax) and `no_tax_id` (the Tax Exempt rate they carry) add a `note` naming the tax id, and a line that
    still shows a tax amount in the read-back adds a `warning` naming it (`approved`: a loud one, the invoice is pushed).
    """
    written = await client_of(ctx).post(CREATE_OP, json_body=body, tool=tool)
    new_id = created_id(written, CREATE_OP, tool=tool)
    record = await reread_after_write(ctx, GET_OP, path_params={"invoiceId": new_id}, tool=tool, written_id=new_id)
    if not no_tax_lines:
        return record
    record = _with_note(record, _no_tax_note(no_tax_id))
    shown = record.get("LineItems")
    if not isinstance(shown, list):
        return record
    sent = body["LineItems"]
    rows = _aligned(sent, [row for row in shown if isinstance(row, Mapping)])
    taxed = [
        f"line_items[{position}]" + (f" (Id {row.get('Id')})" if row.get("Id") else "")
        for position in no_tax_lines
        if (row := rows[position] if position < len(rows) else None) is not None and _tax_shown(row)
    ]
    return _with_warning(record, _no_tax_warning(taxed, no_tax_id, approved))


# --------------------------------------------------------------------------
# create_invoice
# --------------------------------------------------------------------------


@gorelo_tool(toolset="billing", kind="write", ops=[CREATE_OP, GET_OP, TAXES_OP], field_map=CREATE_FIELDS)
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
    and COA code fall back to the item's own values. For no tax use no_tax (the account's Tax Exempt rate) or tax_id; a no_tax line that still shows tax gets a warning.
    Side effects: creates a real invoice that gets a number; to remove a mistaken Draft use delete_invoice, when
    deletes are enabled. If the result is {Id, warning} the draft exists: do not create it again (read it with
    get_invoice). After a "did not confirm" error check list_invoices for the client (newest first) before creating
    it again.
    """
    fields = dict(
        client_id=client_id, line_items=line_items, invoice_date=invoice_date, due_date=due_date, reference=reference
    )
    request = _invoice_request(**fields)
    no_tax_lines = _no_tax_positions(line_items)
    no_tax_id = None
    if no_tax_lines:  # resolved once, before the write
        no_tax_id = await _resolve_no_tax(ctx, "create_invoice", f"line_items[{no_tax_lines[0]}]")
        request = _invoice_request(**fields, no_tax_id=no_tax_id)
    return await _post_and_read_back(
        ctx,
        "create_invoice",
        {**request, "StatusId": DRAFT_STATUS_ID},
        no_tax_lines=no_tax_lines,
        no_tax_id=no_tax_id,
    )


# --------------------------------------------------------------------------
# create_approved_invoice
# --------------------------------------------------------------------------


@gorelo_tool(toolset="billing", kind="destructive", ops=[CREATE_OP, GET_OP, TAXES_OP], field_map=APPROVED_FIELDS)
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
    """Create an APPROVED invoice for a client and return it. Arguments work as in create_invoice (no_tax too; a no_tax
    line that still shows tax gets a loud warning).
    Side effects: approving on create pushes the invoice to the connected accounting system at once. Nothing takes
    that back: delete_invoice, when deletes are enabled, voids it in Gorelo ONLY, so the copy in the accounting
    system stays open (observed with Xero): tell the user, who must void it there too. A total of exactly 0 is created
    as Paid instead: check Status in the result.
    Emails: recipient_emails, when given, are the addresses Gorelo sends the invoice to (it does not say whether
    creating the invoice already sends it): tell the user who.
    If the result is {Id, warning} the invoice exists: do not create it again (get_invoice). After a "did not confirm"
    error check list_invoices first.
    Ask the user first; needs confirm=true.
    """
    fields = dict(
        client_id=client_id,
        line_items=line_items,
        invoice_date=invoice_date,
        due_date=due_date,
        reference=reference,
        recipient_emails=recipient_emails,
    )
    request = _invoice_request(**fields)
    no_tax_lines = _no_tax_positions(line_items)
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
    no_tax_id = None
    if no_tax_lines:  # after the confirmation (zero HTTP without it), once, before the write
        no_tax_id = await _resolve_no_tax(ctx, "create_approved_invoice", f"line_items[{no_tax_lines[0]}]")
        request = _invoice_request(**fields, no_tax_id=no_tax_id)
    return await _post_and_read_back(
        ctx,
        "create_approved_invoice",
        {**request, "StatusId": APPROVED_STATUS_ID},
        no_tax_lines=no_tax_lines,
        no_tax_id=no_tax_id,
        approved=True,
    )


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
    itself treats deleting a void invoice as success); Paid, Submitted and others are refused by Gorelo with 409.
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


# --------------------------------------------------------------------------
# update_invoice and approve_invoice: the read they share
# --------------------------------------------------------------------------

# The statuses Gorelo's PATCH can change that this release offers: Draft and Submitted. (The spec also allows an
# Approved invoice; editing one is not part of this release.)
CHANGEABLE_STATUS_IDS = (1, 2)
CHANGEABLE_TEXT = "Draft (1) and Submitted (2)"
# StatusId of approving is APPROVED_STATUS_ID (5): the only value the PATCH accepts. approve_invoice is the only code that sends it.
PAID_STATUS_ID = 3
# LineItems of the PATCH: "send every line you want to keep (at most 100)".
MAX_INVOICE_LINES = 100

# {tool param: UpdateInvoiceCommand field} of update_invoice. StatusId is NOT here and no code path adds it: only
# approve_invoice sends one.
UPDATE_FIELDS = {
    "invoice_id": "invoiceId",
    "invoice_date": "InvoiceDate",
    "due_date": "DueDate",
    "reference": "Reference",
    "invoice_template_id": "InvoiceTemplateId",
    "invoice_email_template_id": "InvoiceEmailTemplateId",
    "branding_theme_id": "BrandingThemeId",
}
# The line parameters are NOT in the map on purpose: update_lines, add_lines and remove_line_ids all become one LineItems
# array (kept lines first, in invoice order, then the new ones), so a Gorelo error about LineItems[n] cannot be turned back
# into one parameter and keeps Gorelo's own name.
# What clear_fields of update_invoice may clear: the four fields the spec clears with null.
UPDATE_CLEARABLE = ("reference", "invoice_template_id", "invoice_email_template_id", "branding_theme_id")
InvoiceClearable = Literal["reference", "invoice_template_id", "invoice_email_template_id", "branding_theme_id"]
HEADER_CLEAR_VALUES: dict[str, Any] = {name: None for name in UPDATE_CLEARABLE}
# {line param: UpdateInvoiceLineItem field} of an existing line; ItemId is only for a new line.
LINE_CHANGE_PARAMS = (
    "line_id",
    "quantity",
    "description",
    "unit_price",
    "unit_cost",
    "discount_percent",
    "tax_id",
    "no_tax",
    "coa_code",
    "billable_status_id",
    "clear_fields",
)
LINE_CLEARABLE = ("description", "coa_code")
LineClearable = Literal["description", "coa_code"]


class InvoiceLineChange(BaseModel):
    # A change to one line the invoice already has. Only the fields given change; the rest of the line is kept.
    # No docstring on purpose (see InvoiceLine).

    model_config = ConfigDict(extra="forbid")

    line_id: Annotated[str, Field(description="Id of a line of this invoice (get_invoice LineItems[].Id).")]
    quantity: Annotated[
        float | None, Strict(), Field(gt=0, description="Above 0. Gorelo refuses it on a line billing a time entry.")
    ] = None
    description: Annotated[str | None, Field(description="New line text. To remove it use clear_fields.")] = None
    unit_price: Annotated[float | None, Strict(), Field(description="New unit price.")] = None
    unit_cost: Annotated[float | None, Strict(), Field(description="New unit cost.")] = None
    discount_percent: Annotated[float | None, Strict(), Field(ge=0, le=100, description="0-100.")] = None
    tax_id: Annotated[StrictId | None, Field(description="From list_taxes.")] = None
    no_tax: Annotated[StrictBool, Field(description=NO_TAX_TEXT)] = False
    coa_code: Annotated[str | None, Field(description="Chart of accounts code. To remove it use clear_fields.")] = None
    billable_status_id: Annotated[StrictId | None, Field(description="1 Billable, 2 No charge, 3 Non-billable.")] = None
    clear_fields: Annotated[
        list[LineClearable] | None, Field(description="Line fields to remove: description, coa_code. Not with a value for the same field.")
    ] = None


def _unconfirmed_write(op_key: str, detail: str, check_with: str) -> GoreloAPIError:
    """The write-unconfirmed shape error: Gorelo answered 2xx, but the answer does not show the effect of the write (the
    shape of _delete_report's and of tools/documents.py's)."""
    return GoreloAPIError(
        f"{op_key}: {detail}; the write may have been applied, so verify it with a read before repeating it "
        f"({check_with})",
        status=200,
        op_key=op_key,
        kind="shape",
        write_unconfirmed=True,
    )


def _read_shape_error(detail: str) -> GoreloAPIError:
    """Gorelo's read of the invoice cannot be used: nothing was written."""
    return GoreloAPIError(f"{GET_OP}: {detail}; nothing was changed", status=200, op_key=GET_OP, kind="shape")


def _invoice_status_id(record: Mapping[str, Any]) -> int | None:
    status = record.get("Status")
    status_id = status.get("Id") if isinstance(status, Mapping) else None
    return status_id if isinstance(status_id, int) and not isinstance(status_id, bool) else None


def _invoice_name(record: Mapping[str, Any]) -> str:
    shown = record.get("DisplayNumber") or record.get("Number")
    return str(shown) if isinstance(shown, (str, int)) and not isinstance(shown, bool) else "this invoice"


async def _invoice_open_for_change(ctx: Context, tool: str, invoice_guid: str, *, verb: str) -> dict[str, Any]:
    """Read the invoice and refuse (ValueError naming invoice_id, nothing sent) unless it is Draft or Submitted."""
    data = await client_of(ctx).get_one(GET_OP, path_params={"invoiceId": invoice_guid}, tool=tool)
    record = expect_object(data, GET_OP, tool=tool)
    status_id = _invoice_status_id(record)
    if status_id is None:
        raise _read_shape_error("the invoice has no readable Status.Id")
    if status_id not in CHANGEABLE_STATUS_IDS:
        label = INVOICE_STATUSES.get(status_id, "an unknown status")
        already = " It is already Approved." if status_id == APPROVED_STATUS_ID else ""
        raise ValueError(
            f"invoice_id: {_invoice_name(record)} has status {label} (id {status_id}); {tool} {verb} only "
            f"{CHANGEABLE_TEXT} invoices.{already} Nothing was changed."
        )
    return record


def _final_record(answer: Mapping[str, Any], read_back: Mapping[str, Any]) -> dict[str, Any]:
    """The invoice to return and check: the read-back, or the answer of the PATCH (also a full invoice) plus the warning
    when the read-back failed."""
    if "warning" in read_back:
        return {**answer, "warning": read_back["warning"]}
    return dict(read_back)


# --------------------------------------------------------------------------
# update_invoice
# --------------------------------------------------------------------------


def _current_lines(record: Mapping[str, Any]) -> list[str]:
    """The line Ids of the read, canonical, in order. A read with no usable LineItems is a shape error (nothing sent)."""
    lines = record.get("LineItems")
    if not isinstance(lines, list):
        raise _read_shape_error("the invoice has no LineItems list, so the lines cannot be kept safely")
    found: list[str] = []
    for position, row in enumerate(lines):
        try:
            found.append(guid("Id", row.get("Id") if isinstance(row, Mapping) else None))
        except ValueError:
            raise _read_shape_error(f"line {position + 1} of the invoice has no usable Id") from None
    return found


def _line_ids(param: str, values: Any) -> list[str] | None:
    """Canonical line ids, none twice. [] is an error (omit the parameter)."""
    if values is None:
        return None
    non_empty(param, values)
    if isinstance(values, (str, bytes, Mapping)) or not isinstance(values, (list, tuple)):
        raise ValueError(f"{param}: expected a list of line ids (GUIDs), got {describe_value(values)}")
    found = [guid(f"{param}[{index}]", item) for index, item in enumerate(values)]
    if len(set(found)) != len(found):
        raise ValueError(f"{param}: the same line id is listed twice")
    return found


def _line_change(where: str, data: Mapping[str, Any], no_tax_id: int | None = None) -> tuple[str, dict[str, Any]]:
    """(canonical line id, the fields to change) of one update_lines entry. Every problem names `<where>.<field>`."""
    unknown = sorted(str(key) for key in data if key not in LINE_CHANGE_PARAMS)
    if unknown:
        raise ValueError(f"{where}: unknown field(s) {', '.join(unknown)}; allowed: {', '.join(LINE_CHANGE_PARAMS)}")
    if data.get("line_id") is None:
        raise ValueError(f"{where}: missing line_id (the Id of a line of the invoice; a new line goes in add_lines)")
    line_id = guid(f"{where}.line_id", data["line_id"])
    change: dict[str, Any] = {}
    quantity = data.get("quantity")
    if quantity is not None:
        if not _is_number(quantity) or quantity <= 0:
            raise ValueError(f"{where}.quantity: must be a number greater than 0")
        change["Quantity"] = quantity
    change.update(_line_fields(where, data, no_tax_id))
    clears = data.get("clear_fields")
    if clears is not None:
        non_empty(f"{where}.clear_fields", clears)
        if isinstance(clears, (str, bytes, Mapping)) or not isinstance(clears, (list, tuple)):
            raise ValueError(f"{where}.clear_fields: expected a list of field names, got {describe_value(clears)}")
        for name in dict.fromkeys(clears):
            if name not in LINE_CLEARABLE:
                raise ValueError(f"{where}.clear_fields: {describe_value(name)} cannot be cleared; allowed: {', '.join(LINE_CLEARABLE)}")
            if LINE_FIELDS[name] in change:
                raise ValueError(f"{where}.{name}: cannot be given a value and cleared in the same call")
            change[LINE_FIELDS[name]] = None
    if not change:
        raise ValueError(f"{where}: give at least one field to change besides line_id")
    return line_id, change


LineRequest = tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[str]]


def _line_request(
    update_lines: Any, add_lines: Any, remove_line_ids: Any, no_tax_id: int | None = None
) -> LineRequest | None:
    """The three line parameters, checked without the invoice: ({line id: fields to change}, new lines, ids to remove), or
    None when none was given. Pure: no HTTP, so a bad entry costs no request. `no_tax_id`: the Tax Exempt rate id a no_tax line sends
    (the placeholder while the lookup has not happened)."""
    if update_lines is None and add_lines is None and remove_line_ids is None:
        return None
    changes: dict[str, dict[str, Any]] = {}
    if update_lines is not None:
        non_empty("update_lines", update_lines)
        if isinstance(update_lines, (str, bytes, Mapping)) or not isinstance(update_lines, (list, tuple)):
            raise ValueError(f"update_lines: expected a list of line changes, got {describe_value(update_lines)}")
        for position, entry in enumerate(update_lines):
            where = f"update_lines[{position}]"
            data = entry.model_dump() if isinstance(entry, BaseModel) else entry
            if not isinstance(data, Mapping):
                raise ValueError(f"{where}: must be an object with line_id and the fields to change, got {describe_value(entry)}")
            line_id, change = _line_change(where, data, no_tax_id)
            if line_id in changes:
                raise ValueError(f"{where}.line_id: this line is already changed by an earlier entry of update_lines")
            changes[line_id] = change
    new_lines = _invoice_lines("add_lines", add_lines, no_tax_id) if add_lines is not None else []
    removals = _line_ids("remove_line_ids", remove_line_ids) or []
    return changes, new_lines, removals


def _plan_lines(current: list[str], request: LineRequest | None) -> list[dict[str, Any]] | None:
    """The complete LineItems of the PATCH, or None when no line parameter was given.

    Every current line is kept as {Id} unless update_lines changes it (the same Id plus the fields to change) or
    remove_line_ids names it; new lines (add_lines, no Id) are appended. A line is never dropped without being named, and
    a line id that is not on the invoice is refused. Pure: no HTTP.
    """
    if request is None:
        return None
    changes, new_lines, removals = request
    known = set(current)
    for position, line_id in enumerate(changes):
        if line_id not in known:
            raise ValueError(
                f"update_lines[{position}].line_id: {line_id} is not a line of this invoice; "
                f"its lines are {', '.join(current[:10]) or 'none'}{' ...' if len(current) > 10 else ''} (get_invoice shows them)"
            )
    for position, line_id in enumerate(removals):
        if line_id not in known:
            raise ValueError(f"remove_line_ids[{position}]: {line_id} is not a line of this invoice (get_invoice shows its lines)")
        if line_id in changes:
            raise ValueError(f"remove_line_ids[{position}]: this line is also in update_lines; remove it or change it, not both")
    final: list[dict[str, Any]] = []
    for line_id in current:
        if line_id in removals:
            continue
        final.append({"Id": line_id, **changes.get(line_id, {})})
    final.extend(new_lines)
    if not final:
        raise ValueError(
            "remove_line_ids: this would remove every line of the invoice and Gorelo refuses an empty list; "
            "keep or add at least one line (to get rid of a Draft use delete_invoice, when deletes are enabled)"
        )
    if len(final) > MAX_INVOICE_LINES:
        kept = len(final) - len(new_lines)
        if new_lines:
            raise ValueError(
                f"add_lines: the invoice would have {len(final)} lines ({kept} kept plus {len(new_lines)} added) and "
                f"Gorelo accepts at most {MAX_INVOICE_LINES}; add fewer lines or remove some with remove_line_ids"
            )
        raise ValueError(
            f"remove_line_ids: the invoice would still have {len(final)} lines and Gorelo accepts at most "
            f"{MAX_INVOICE_LINES}; remove at least {len(final) - MAX_INVOICE_LINES} more with remove_line_ids"
        )
    return final


def _date_of(record: Mapping[str, Any], key: str) -> date | None:
    value = record.get(key)
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _header_dates(record: Mapping[str, Any], invoice_date: Any, due_date: Any) -> tuple[str | None, str | None]:
    """The checked dates, also against the one the invoice already holds when only one of the two is given."""
    start, due = _invoice_dates(invoice_date, due_date)
    if start is not None and due is None:
        current_due = _date_of(record, "DueDate")
        if current_due is not None and current_due < date.fromisoformat(start):
            raise ValueError(
                f"invoice_date: {start} is after the invoice's due date {current_due.isoformat()}; "
                "give due_date too (it must be the invoice date or later)"
            )
    if due is not None and start is None:
        current_start = _date_of(record, "InvoiceDate")
        if current_start is not None and date.fromisoformat(due) < current_start:
            raise ValueError(
                f"due_date: {due} is before the invoice date {current_start.isoformat()}; "
                "the due date must be the invoice date or later"
            )
    return start, due


# Tolerances of the read-back: the invoice rounds prices and costs to the cent, quantities to four decimals and discounts to
# two (a small margin, so a rounding never raises a false "not confirmed").
_QUANTITY_TOLERANCE = 0.0001
_MONEY_TOLERANCE = 0.005
_PERCENT_TOLERANCE = 0.01
# (field of InvoiceLineItemModel, tolerance): the numbers that update_invoice compares on a line.
LINE_NUMBERS = (
    ("Quantity", _QUANTITY_TOLERANCE),
    ("UnitPrice", _MONEY_TOLERANCE),
    ("UnitCost", _MONEY_TOLERANCE),
    ("DiscountPercent", _PERCENT_TOLERANCE),
)
LINE_TEXTS = ("Description", "CoaCode")
# (field of InvoiceLineItemModel that holds a code object, field of the update line that sends its id)
LINE_CODES = (("Tax", "TaxId"), ("BillableStatus", "BillableStatusId"))


def _is_plain_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _quoted(value: Any) -> str:
    """A number or None as it is; anything else only by its type (never the value)."""
    return "none" if value is None else str(value) if _is_plain_number(value) else describe_value(value)


def _line_id_of(row: Any) -> str | None:
    value = row.get("Id") if isinstance(row, Mapping) else None
    return value.lower() if isinstance(value, str) and value else None


def _expected_line(before: Mapping[str, Any] | None, change: Mapping[str, Any]) -> dict[str, Any]:
    """What the read must show on one line: the values the first read had (a field it did not show is not compared),
    overlaid with what the call sent (a no_tax line carries the Tax Exempt rate's id, which the read must show)."""
    expected: dict[str, Any] = {}
    if before is not None:
        for key, _ in LINE_NUMBERS:
            if key in before:
                expected[key] = before[key]
        for key in LINE_TEXTS:
            if key in before:
                expected[key] = before[key]
        for model, _ in LINE_CODES:
            if model in before:
                code = before[model]
                expected[model] = code.get("Id") if isinstance(code, Mapping) else None
    for key, _ in LINE_NUMBERS:
        if key in change:
            expected[key] = change[key]
    for key in LINE_TEXTS:
        if key in change:
            expected[key] = change[key]
    for model, field in LINE_CODES:
        if field in change:
            if change[field] is None:
                expected.pop(model, None)
            else:
                expected[model] = change[field]
    return expected


def _line_differences(label: str, expected: Mapping[str, Any], shown: Mapping[str, Any]) -> list[str]:
    """Each field of `expected` that the line `shown` does not show: "<label>: <field> expected <x>, read <y>". Numbers and
    ids are quoted; text is not (it is client data and errors reach the server log)."""
    found: list[str] = []
    for key, tolerance in LINE_NUMBERS:
        if key not in expected:
            continue
        want, got = expected[key], shown.get(key)
        if want is None or got is None:
            same = want is None and got is None
        else:
            same = _is_plain_number(want) and _is_plain_number(got) and abs(got - want) <= tolerance
        if not same:
            found.append(f"{label}: {key} expected {_quoted(want)}, read {_quoted(got)}")
    for key in LINE_TEXTS:
        if key not in expected:
            continue
        want, got = expected[key], shown.get(key)
        want_text = want.strip() if isinstance(want, str) else ""
        got_text = got.strip() if isinstance(got, str) else ""
        if want_text != got_text:
            found.append(f"{label}: {key} is not the text it should be (text not shown)")
    for model, _ in LINE_CODES:
        if model not in expected:
            continue
        code = shown.get(model)
        got = code.get("Id") if isinstance(code, Mapping) else None
        if got != expected[model]:
            found.append(f"{label}: {model} id expected {expected[model]}, read {got}")
    return found


def _line_set_problems(
    shown_lines: list[Any], wire_lines: list[Mapping[str, Any]], before: Mapping[str, Any] | None
) -> list[str]:
    """The lines of the read-back against the LineItems that were sent and the lines the first read had."""
    problems: list[str] = []
    rows = [row for row in shown_lines if isinstance(row, Mapping)]
    by_id = {line_id: row for row in rows if (line_id := _line_id_of(row))}
    first = before.get("LineItems") if before is not None else None
    before_rows = {
        line_id: row for row in (first if isinstance(first, list) else []) if (line_id := _line_id_of(row))
    }
    sent_old = [line for line in wire_lines if _line_id_of(line)]
    sent_new = [line for line in wire_lines if not _line_id_of(line)]
    kept_ids = {_line_id_of(line) for line in sent_old}
    for line_id in kept_ids:
        if line_id not in by_id:
            problems.append(f"line {line_id} should stay but the invoice does not show it")
    for line_id in before_rows:
        if line_id not in kept_ids and line_id in by_id:
            problems.append(f"line {line_id} was removed but the invoice still shows it")
    fresh = [row for row in rows if _line_id_of(row) not in kept_ids and _line_id_of(row) not in before_rows]
    if len(fresh) != len(sent_new):
        problems.append(f"the invoice shows {len(fresh)} new line(s), {len(sent_new)} were added")
    for change in sent_old:
        line_id = _line_id_of(change)
        row = by_id.get(line_id)
        if row is not None:
            problems.extend(_line_differences(f"line {line_id}", _expected_line(before_rows.get(line_id), change), row))
    if len(fresh) == len(sent_new):
        for number, (change, row) in enumerate(zip(sent_new, _aligned(sent_new, fresh)), start=1):
            if row is not None:
                expected = _expected_line(None, change)
                problems.extend(_line_differences(f"new line {number}", expected, row))
    return problems


def _update_problems(
    record: Mapping[str, Any], wire: Mapping[str, Any], before: Mapping[str, Any] | None = None
) -> list[str]:
    """What the read-back does not show of the update that was sent. A cleared template id is not checked: Gorelo says the
    service provider's default then applies, so the invoice may show that one. With `before` (the first read) every kept
    line is compared with it and the set of line Ids is checked; without it only what was sent is checked."""
    problems: list[str] = []
    for key in ("InvoiceDate", "DueDate"):
        if key in wire:
            shown = record.get(key)
            if not isinstance(shown, str) or shown[:10] != wire[key][:10]:
                problems.append(f"{key} is not the date that was sent")
    if "Reference" in wire:
        want, shown = wire["Reference"], record.get("Reference")
        if want is None:
            if shown not in (None, ""):
                problems.append("Reference is still set")
        elif not isinstance(shown, str) or shown.strip() != want.strip():
            problems.append("Reference is not the text that was sent")
    for key in ("InvoiceTemplateId", "InvoiceEmailTemplateId"):
        if wire.get(key) is not None and record.get(key) != wire[key]:
            problems.append(f"{key} is not the id that was sent")
    if wire.get("BrandingThemeId") is not None:
        shown = record.get("BrandingThemeId")
        if not isinstance(shown, str) or shown.lower() != wire["BrandingThemeId"].lower():
            problems.append("BrandingThemeId is not the id that was sent")
    if "LineItems" in wire:
        shown_lines = record.get("LineItems")
        if not isinstance(shown_lines, list) or len(shown_lines) != len(wire["LineItems"]):
            count = len(shown_lines) if isinstance(shown_lines, list) else "no list of"
            problems.append(f"LineItems shows {count} lines, {len(wire['LineItems'])} were sent")
        if isinstance(shown_lines, list):
            problems.extend(_line_set_problems(shown_lines, wire["LineItems"], before))
    return problems


@gorelo_tool(
    toolset="billing",
    kind="write",
    ops=[GET_OP, UPDATE_OP, TAXES_OP],
    field_map=UPDATE_FIELDS,
    destructive_hint=True,
)
async def update_invoice(
    ctx: Context,
    invoice_id: Annotated[str, Field(description="Id of a list_invoices row (GUID). Must be Draft or Submitted.")],
    invoice_date: Annotated[str | None, Field(description="YYYY-MM-DD.")] = None,
    due_date: Annotated[str | None, Field(description="YYYY-MM-DD, not before the invoice date.")] = None,
    reference: Annotated[str | None, Field(description="Free text, e.g. a PO number. To remove it use clear_fields.")] = None,
    invoice_template_id: Annotated[StrictId | None, Field(description="PDF template id.")] = None,
    invoice_email_template_id: Annotated[StrictId | None, Field(description="Email template id.")] = None,
    branding_theme_id: Annotated[str | None, Field(description="Branding theme GUID of the accounting system.")] = None,
    update_lines: Annotated[
        list[InvoiceLineChange] | None,
        Field(description="Changes to lines the invoice has, by line_id. Fields left out keep their value."),
    ] = None,
    add_lines: Annotated[
        list[InvoiceLine] | None,
        Field(description="New lines: item_id and quantity; the rest as in create_invoice."),
    ] = None,
    remove_line_ids: Annotated[
        list[str] | None,
        Field(description="Line ids to remove; the time or charges they billed return to waiting to be invoiced."),
    ] = None,
    clear_fields: Annotated[
        list[InvoiceClearable] | None,
        Field(description="Fields to remove: reference, invoice_template_id, invoice_email_template_id, branding_theme_id."),
    ] = None,
) -> dict:
    """Change a Draft or Submitted invoice and return it read back. Send only what changes. It refuses any other status and never changes the status (approve_invoice, when enabled).
    Lines: update_lines changes lines by line_id, add_lines appends new ones, remove_line_ids removes some; every other line is kept (Gorelo replaces the whole set, so the tool resends them). Quantity cannot change on a line billing a time entry (Gorelo refuses).
    Resolve ids first: invoice_id -> list_invoices, line ids -> get_invoice, item_id -> list_items, tax_id -> list_taxes. For no tax use no_tax (the account's Tax Exempt rate, looked up in list_taxes; refused unless there is exactly one) or tax_id.
    Side effects: overwrites what you send; a new price or billable status on a time line carries to the time entry. After a "did not confirm" error read get_invoice before repeating.
    """
    invoice_guid = guid("invoice_id", invoice_id)
    clears = _clear_names(clear_fields)
    for name, value in (
        ("reference", reference),
        ("invoice_template_id", invoice_template_id),
        ("invoice_email_template_id", invoice_email_template_id),
        ("branding_theme_id", branding_theme_id),
    ):
        if name in clears and value is not None:
            raise ValueError(f"{name}: cannot be given a value and cleared in the same call")
    # everything that needs no read is checked first, so a bad parameter costs no request
    start, due = _invoice_dates(invoice_date, due_date)
    if reference is not None and not isinstance(reference, str):
        raise ValueError(f"reference: must be text, got {describe_value(reference)}")
    if isinstance(reference, str) and not reference.strip():
        raise ValueError('reference: must not be blank; to remove it pass clear_fields=["reference"]')
    values = {
        "invoice_date": None if start is None else f"{start}T00:00:00Z",
        "due_date": None if due is None else f"{due}T00:00:00Z",
        "reference": reference,
        "invoice_template_id": None if invoice_template_id is None else positive_id("invoice_template_id", invoice_template_id),
        "invoice_email_template_id": (
            None if invoice_email_template_id is None else positive_id("invoice_email_template_id", invoice_email_template_id)
        ),
        "branding_theme_id": None if branding_theme_id is None else guid("branding_theme_id", branding_theme_id),
    }
    body_map = {key: name for key, name in UPDATE_FIELDS.items() if key in values}
    body = build_body(values, body_map, clear=clears, clear_values=HEADER_CLEAR_VALUES)
    line_request = _line_request(update_lines, add_lines, remove_line_ids, _UNRESOLVED_TAX_ID)
    if not body and line_request is None:
        raise ValueError(
            "nothing to change: give at least one of invoice_date, due_date, reference, invoice_template_id, "
            "invoice_email_template_id, branding_theme_id, update_lines, add_lines, remove_line_ids or clear_fields"
        )
    record = await _invoice_open_for_change(ctx, "update_invoice", invoice_guid, verb="changes")
    if start is not None or due is not None:
        _header_dates(record, invoice_date, due_date)
    # the lines are read only when a line parameter is given: a header-only update must not depend on them
    no_tax_id = None
    no_tax_where = [
        *(f"update_lines[{n}]" for n in _no_tax_positions(update_lines)),
        *(f"add_lines[{n}]" for n in _no_tax_positions(add_lines)),
    ]
    if no_tax_where:  # resolved once, after the status check and before the write
        no_tax_id = await _resolve_no_tax(ctx, "update_invoice", no_tax_where[0])
        line_request = _line_request(update_lines, add_lines, remove_line_ids, no_tax_id)
    lines = _plan_lines(_current_lines(record), line_request) if line_request is not None else None
    if lines is not None:
        body["LineItems"] = lines
    if "StatusId" in body:  # unreachable by construction (UPDATE_FIELDS has no StatusId); a guard, not a feature
        raise ValueError("update_invoice cannot change the status; use approve_invoice (when enabled) to approve")
    client = client_of(ctx)
    answer = await client.patch(UPDATE_OP, path_params={"invoiceId": invoice_guid}, json_body=body, tool="update_invoice")
    changed = expect_object(answer, UPDATE_OP, tool="update_invoice")
    read_back = await reread_after_write(
        ctx, GET_OP, path_params={"invoiceId": invoice_guid}, tool="update_invoice", written_id=invoice_guid
    )
    result = _final_record(changed, read_back)
    problems = _update_problems(result, body, record if lines is not None else None)
    if problems:
        raise _unconfirmed_write(UPDATE_OP, f"the invoice does not show the change ({'; '.join(problems)})", "get_invoice")
    if lines is not None:
        result = _with_warning(result, _update_no_tax_warning(result, record, update_lines, add_lines, lines, no_tax_id))
        result = _with_note(result, _no_tax_note(no_tax_id))
    status_id = _invoice_status_id(result)
    if status_id not in CHANGEABLE_STATUS_IDS:
        shown = INVOICE_STATUSES.get(status_id, "an unknown status") if status_id is not None else "no readable status"
        result = _with_note(
            result,
            f"The invoice shows {shown} after the change, not Draft or Submitted: it was Approved when the change "
            "reached Gorelo, so the copy already pushed to the accounting system may not show this change. Check it "
            "in the accounting system.",
        )
    return result


def _update_no_tax_warning(
    result: Mapping[str, Any],
    before: Mapping[str, Any],
    update_lines: Any,
    add_lines: Any,
    wire_lines: list[Mapping[str, Any]],
    no_tax_id: int | None = None,
) -> str | None:
    """The warning for the no_tax lines of update_lines and add_lines that the read-back still shows with a tax amount."""
    shown = result.get("LineItems")
    if not isinstance(shown, list):
        return None
    rows = [row for row in shown if isinstance(row, Mapping)]
    by_id = {line_id: row for row in rows if (line_id := _line_id_of(row))}
    taxed: list[str] = []
    for position in _no_tax_positions(update_lines):
        entry = update_lines[position]
        data = entry.model_dump() if isinstance(entry, BaseModel) else entry
        line_id = str(data.get("line_id") or "").lower()
        row = by_id.get(line_id)
        if row is not None and _tax_shown(row):
            taxed.append(f"update_lines[{position}] (line {line_id})")
    added = _no_tax_positions(add_lines)
    if added:
        first = before.get("LineItems")
        old_ids = {_line_id_of(row) for row in (first if isinstance(first, list) else [])}
        fresh = [row for row in rows if _line_id_of(row) not in old_ids]
        new_wire = [line for line in wire_lines if not _line_id_of(line)]
        if len(fresh) == len(new_wire):
            aligned = _aligned(new_wire, fresh)
            for position in added:
                row = aligned[position] if position < len(aligned) else None
                if row is not None and _tax_shown(row):
                    where = f"add_lines[{position}]" + (f" (line {row.get('Id')})" if row.get("Id") else "")
                    taxed.append(where)
    return _no_tax_warning(taxed, no_tax_id)


def _clear_names(clear_fields: Any) -> list[str]:
    """The distinct names in clear_fields, each one update_invoice can clear. [] is an error."""
    if clear_fields is None:
        return []
    non_empty("clear_fields", clear_fields)
    if isinstance(clear_fields, (str, bytes, Mapping)) or not isinstance(clear_fields, (list, tuple)):
        raise ValueError(f"clear_fields: expected a list of field names, got {describe_value(clear_fields)}")
    names = list(dict.fromkeys(clear_fields))
    for name in names:
        if name not in UPDATE_CLEARABLE:
            raise ValueError(f"clear_fields: {describe_value(name)} cannot be cleared; allowed: {', '.join(UPDATE_CLEARABLE)}")
    return names


# --------------------------------------------------------------------------
# approve_invoice
# --------------------------------------------------------------------------


@gorelo_tool(toolset="billing", kind="destructive", ops=[GET_OP, UPDATE_OP], field_map={"invoice_id": "invoiceId"})
async def approve_invoice(
    ctx: Context,
    invoice_id: Annotated[str, Field(description="Id of a list_invoices row (GUID). Must be Draft or Submitted.")],
    confirm: Annotated[StrictBool, Field(description="Must be true; only after the user approves.")] = False,
) -> dict:
    """Approve a Draft or Submitted invoice and return it read back. It reads the invoice first and refuses any other status.
    Resolve ids first: invoice_id -> list_invoices.
    Side effects: approving pushes the invoice to the connected accounting system at once. Nothing takes that back: delete_invoice, when deletes are enabled, voids it in Gorelo ONLY, so the copy in the accounting system stays open (observed with Xero): tell the user, who must void it there too. A total of exactly 0 is recorded as Paid instead: check Status in the result.
    After a "did not confirm" error read get_invoice before repeating.
    Ask the user first; needs confirm=true.
    """
    invoice_guid = guid("invoice_id", invoice_id)
    require_confirm(
        confirm,
        action=f"approve invoice {invoice_guid}",
        effect=(
            "Approving pushes the invoice to the connected accounting system at once, and nothing takes that back: "
            "voiding it with delete_invoice voids it in Gorelo ONLY, so the copy in the accounting system stays open "
            "(observed with Xero) and the user must void it there too. Nothing has been sent to Gorelo."
        ),
    )
    await _invoice_open_for_change(ctx, "approve_invoice", invoice_guid, verb="approves")
    answer = await client_of(ctx).patch(
        UPDATE_OP, path_params={"invoiceId": invoice_guid}, json_body={"StatusId": APPROVED_STATUS_ID}, tool="approve_invoice"
    )
    changed = expect_object(answer, UPDATE_OP, tool="approve_invoice")
    read_back = await reread_after_write(
        ctx, GET_OP, path_params={"invoiceId": invoice_guid}, tool="approve_invoice", written_id=invoice_guid
    )
    result = _final_record(changed, read_back)
    status_id = _invoice_status_id(result)
    if status_id not in (APPROVED_STATUS_ID, PAID_STATUS_ID):
        shown = INVOICE_STATUSES.get(status_id, "no usable status") if status_id is not None else "no usable status"
        raise _unconfirmed_write(
            UPDATE_OP, f"the invoice shows {shown}, expected Approved or Paid after approving", "get_invoice"
        )
    if status_id == PAID_STATUS_ID:
        result = {
            **result,
            "note": (
                "Status is Paid, not Approved: Gorelo records an approved invoice whose total is 0 as Paid. "
                "Check Total and AmountDue."
            ),
        }
    return result


# --------------------------------------------------------------------------
# list_payments and get_payment (read only: no tool records or deletes a payment)
# --------------------------------------------------------------------------

PAYMENT_FILTERS = {
    "client_ids": "ClientIds",
    "invoice_ids": "InvoiceIds",
    "payment_date_since": "PaymentDateSince",
    "payment_date_before": "PaymentDateBefore",
    "sort_order": "SortOrder",
}
PAYMENTS_FIELD_MAP = {**PAYMENT_FILTERS, "page_size": "PageSize", "cursor": "Cursor"}


@gorelo_tool(toolset="billing", kind="read", ops=[PAYMENTS_OP], field_map=PAYMENTS_FIELD_MAP)
async def list_payments(
    ctx: Context,
    client_ids: Annotated[list[StrictId] | None, Field(description="Payments received from these clients (list_clients).")] = None,
    invoice_ids: Annotated[list[str] | None, Field(description="Payments applied to these invoices (GUIDs from list_invoices).")] = None,
    payment_date_since: Annotated[str | None, Field(description="YYYY-MM-DD, inclusive.")] = None,
    payment_date_before: Annotated[str | None, Field(description="YYYY-MM-DD, exclusive.")] = None,
    sort_order: Annotated[Literal["asc", "desc"] | None, Field(description="By payment date. Default desc.")] = None,
    page_size: Annotated[int, Field(description="1-200, clamped.")] = 50,
    cursor: Annotated[
        str | None, Field(description="next_cursor from the previous call; same filters and sort; repeat until has_more is false.")
    ] = None,
) -> dict:
    """List payments received from clients, newest first, one page at a time.
    Read only: payments are recorded money, and no tool records or deletes one. A row has Amount (the whole payment), Reference, ClientId and Invoices (each invoice it was applied to with its share). Each filter narrows the result.
    Resolve ids first: client_ids -> list_clients, invoice_ids -> list_invoices.
    Paging: pass next_cursor back as cursor with the SAME filters until has_more is false.
    """
    filters = {
        "client_ids": positive_ids("client_ids", client_ids),
        "invoice_ids": guids("invoice_ids", invoice_ids),
        "payment_date_since": _calendar_date("payment_date_since", payment_date_since),
        "payment_date_before": _calendar_date("payment_date_before", payment_date_before),
        "sort_order": sort_order,
    }
    wire = {
        **filters,
        **{
            param: f"{filters[param]}T00:00:00Z"
            for param in ("payment_date_since", "payment_date_before")
            if filters[param] is not None
        },
    }
    query = {
        PAYMENT_FILTERS[param]: csv_ids(param, value) if isinstance(value, (list, tuple)) else value
        for param, value in wire.items()
    }
    page = await client_of(ctx).get_page(
        PAYMENTS_OP,
        query=query,
        page_size=clamp_page_size(page_size),
        cursor=non_empty("cursor", cursor),
        tool="list_payments",
    )
    return paged_result(page, filters)


@gorelo_tool(toolset="billing", kind="read", ops=[PAYMENT_OP], field_map={"payment_id": "paymentId"})
async def get_payment(
    ctx: Context,
    payment_id: Annotated[str, Field(description="Id of a list_payments row (GUID).")],
) -> dict:
    """Get one payment: Amount, PaymentDate, Reference, ClientId and the invoices it was applied to.
    Read only: payments are recorded money, and no tool records or deletes one.
    """
    data = await client_of(ctx).get_one(
        PAYMENT_OP, path_params={"paymentId": guid("payment_id", payment_id)}, tool="get_payment"
    )
    return expect_object(data, PAYMENT_OP, tool="get_payment")
