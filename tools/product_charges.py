"""Product charges: catalog items charged to a ticket or to a project task (toolset "billing").

Five tools:

    list_product_charges   read         GET /v1/product-charges (paged)
    get_product_charge     read         GET /v1/product-charges/{productChargeId}
    create_product_charge  write        POST /v1/product-charges, then GET /v1/product-charges/{productChargeId}
    update_product_charge  write        PATCH /v1/product-charges/{productChargeId}; destructive_hint
    delete_product_charge  destructive  DELETE /v1/product-charges/{productChargeId}; needs confirm=true

Ops owned by this module (keep the list in step with the ops=[...] of the declarations below):

    GET /v1/product-charges
    POST /v1/product-charges
    GET /v1/product-charges/{productChargeId}
    PATCH /v1/product-charges/{productChargeId}
    DELETE /v1/product-charges/{productChargeId}

Rules that shape the code (contract 6c30770dffc0, the descriptions of the raw spec):

* A product charge is billed to the client of its ticket (or of its task's project) and is invoiced with that
  ticket's or project's other charges. It is the sibling of a time entry: POST answers only {Id}, so create reads the
  charge back with the single GET (reread_after_write); PATCH answers the full charge.
* Exactly one of ticket_id and task_id (both GUIDs, like create_time_entry). The item is a product or bundle from
  GET /v1/items. What a charge is charged to and its item cannot change: remove it and add another.
* Gorelo refuses with 409 (plain refusals, nothing is written): create on a ticket that is closed, has no client or whose
  client is on credit hold, and on a task whose project is closed or has no client; update and delete while the ticket is
  closed, the task's project is closed, or the charge is waiting to be invoiced or has been invoiced. The tool texts say so
  before the call; Gorelo's own 409 text reaches the model unchanged.
* BillableStatusId is 1 Billable, 2 No charge, 3 Non-billable or 5 Void (the spec text of both the create and the update
  field; a time entry has no 5). create_product_charge refuses 5 (a charge created void is
  pointless), update_product_charge keeps it. Quantity is above 0, at most QUANTITY_MAX and has up to 4 decimals; UnitPrice is from
  -PRICE_MAX to PRICE_MAX; UnitCost is 0 to PRICE_MAX. A left-out price, cost or description on create falls back to the item's own.
* Only Description clears (null): clear_description=true. Quantity, UnitPrice, UnitCost and BillableStatusId do not clear.
* delete_product_charge is the gated tool: the removal appears on the ticket's or task's timeline and no tool puts the
  charge back. Its answer is {Id}, which must be the id that was asked for, else a write-unconfirmed shape error.
"""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastmcp import Context
from pydantic import Field, Strict

from gorelo_client import GoreloAPIError
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
    guids,
    non_empty,
    paged_result,
    positive_id,
    positive_ids,
    reread_after_write,
    require_confirm,
    utc_iso,
)

LIST_OP = "GET /v1/product-charges"
GET_OP = "GET /v1/product-charges/{productChargeId}"
CREATE_OP = "POST /v1/product-charges"
UPDATE_OP = "PATCH /v1/product-charges/{productChargeId}"
DELETE_OP = "DELETE /v1/product-charges/{productChargeId}"
TOOLSET = "billing"

# BillableStatusId of a product charge. The spec text of the create and update fields lists 5 Void as well; a time
# entry and an invoice line have no 5.
BILLABLE_STATUSES = {1: "Billable", 2: "No charge", 3: "Non-billable", 5: "Void"}
BILLABLE_TEXT = "1 Billable, 2 No charge, 3 Non-billable, 5 Void"
# create_product_charge refuses 5 Void (a charge created void is pointless: do not create it); update keeps it.
VOID_STATUS_ID = 5
CREATE_BILLABLE_TEXT = "1 Billable, 2 No charge, 3 Non-billable"

QUANTITY_MAX = 99_999_999
QUANTITY_DECIMALS = 4
PRICE_MAX = 99_999_999
QUANTITY_TEXT = f"Above 0, at most {QUANTITY_MAX:,}, up to {QUANTITY_DECIMALS} decimals."

# {tool param: Gorelo query name} of GET /v1/product-charges. The same map turns a Gorelo PropertyName back into the param.
LIST_QUERY = {
    "ticket_ids": "TicketIds",
    "task_ids": "TaskIds",
    "project_ids": "ProjectIds",
    "client_ids": "ClientIds",
    "invoice_ids": "InvoiceIds",
    "created_since": "CreatedSince",
    "created_before": "CreatedBefore",
}
LIST_FIELDS = {**LIST_QUERY, "page_size": "PageSize", "cursor": "Cursor"}
# {tool param: CreateProductChargeCommand field}: every field of the spec and nothing else.
CREATE_FIELDS = {
    "ticket_id": "TicketId",
    "task_id": "TaskId",
    "item_id": "ItemId",
    "quantity": "Quantity",
    "unit_price": "UnitPrice",
    "unit_cost": "UnitCost",
    "description": "Description",
    "billable_status_id": "BillableStatusId",
}
# {tool param: UpdateProductChargeCommand field}. TicketId, TaskId and ItemId cannot change (a body that carries one is
# rejected as malformed), so they are not here.
UPDATE_FIELDS = {
    "quantity": "Quantity",
    "unit_price": "UnitPrice",
    "unit_cost": "UnitCost",
    "description": "Description",
    "billable_status_id": "BillableStatusId",
}
ID_MAP = {"product_charge_id": "productChargeId"}

CHARGE_ID_TEXT = "Id of a list_product_charges row (a whole number)."
CONFIRM_TEXT = "Must be true; only after the user approves."


# --------------------------------------------------------------------------
# Local validation (every message names the snake_case parameter)
# --------------------------------------------------------------------------


def _number(param: str, value: Any) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{param}: must be a number, got {describe_value(value)}")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{param}: must be a finite number")
    return value


def _quantity(param: str, value: Any) -> float | int | None:
    """Above 0, at most QUANTITY_MAX, up to QUANTITY_DECIMALS decimal places."""
    if value is None:
        return None
    number = _number(param, value)
    if number <= 0 or number > QUANTITY_MAX:
        raise ValueError(f"{param}: must be above 0 and at most {QUANTITY_MAX:,}, got {number!r}")
    try:
        exponent = Decimal(repr(number)).as_tuple().exponent
    except InvalidOperation:  # pragma: no cover - repr of a finite number is always a decimal
        exponent = 0
    if isinstance(exponent, int) and exponent < -QUANTITY_DECIMALS:
        raise ValueError(f"{param}: at most {QUANTITY_DECIMALS} decimal places are accepted, got {number!r}")
    return number


def _price(param: str, value: Any, *, low: int) -> float | int | None:
    if value is None:
        return None
    number = _number(param, value)
    if number < low or number > PRICE_MAX:
        raise ValueError(f"{param}: must be from {low:,} to {PRICE_MAX:,}, got {number!r}")
    return number


def _billable_status(param: str, value: Any, *, void: bool = True) -> int | None:
    """The billable status; `void` False (a create) leaves 5 Void out of the accepted ones."""
    if value is None:
        return None
    statuses = {status: label for status, label in BILLABLE_STATUSES.items() if void or status != VOID_STATUS_ID}
    accepted = ", ".join(f"{status} ({label})" for status, label in statuses.items())
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{param}: must be one of {accepted}, got {describe_value(value)}")
    if value == VOID_STATUS_ID and not void:
        raise ValueError(
            f"{param}: 5 (Void) is not accepted when a charge is created (a charge created void is pointless); use "
            f"{accepted}, or do not create the charge. update_product_charge can set Void on an existing one"
        )
    if value not in statuses:
        raise ValueError(f"{param}: must be one of {accepted}, got {value}")
    return value


def _description(param: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{param}: must be text, got {describe_value(value)}")
    if not value.strip():
        raise ValueError(f"{param}: must not be empty or whitespace only (to remove the description use clear_description=true)")
    return value


def _unconfirmed(op_key: str, detail: str, check_with: str) -> GoreloAPIError:
    """The write-unconfirmed shape error (the shape of tools/invoices.py's _delete_report)."""
    return GoreloAPIError(
        f"{op_key}: {detail}; the write may have been applied, so verify it with a read before repeating it "
        f"({check_with})",
        status=200,
        op_key=op_key,
        kind="shape",
        write_unconfirmed=True,
    )


def _close(shown: Any, want: float | int, tolerance: float) -> bool:
    return isinstance(shown, (int, float)) and not isinstance(shown, bool) and abs(shown - want) <= tolerance


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


@gorelo_tool(toolset=TOOLSET, kind="read", ops=[LIST_OP], field_map=LIST_FIELDS)
async def list_product_charges(
    ctx: Context,
    ticket_ids: Annotated[list[str] | None, Field(description="Ticket GUIDs (list_tickets), not display numbers.")] = None,
    task_ids: Annotated[list[str] | None, Field(description="Task GUIDs (list_project_tasks, projects toolset).")] = None,
    project_ids: Annotated[list[str] | None, Field(description="Project GUIDs: every charge of a task of these projects.")] = None,
    client_ids: Annotated[list[StrictId] | None, Field(description="Client ids (list_clients): charges billed to them.")] = None,
    invoice_ids: Annotated[list[str] | None, Field(description="Invoice GUIDs (list_invoices): charges billed on them.")] = None,
    created_since: Annotated[str | None, Field(description="Added at or after this instant (ISO 8601 with UTC offset).")] = None,
    created_before: Annotated[str | None, Field(description="Added before this instant.")] = None,
    page_size: Annotated[int, Field(description="Rows per page, 1-200.")] = 50,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous call; same filters; repeat until has_more is false.")] = None,
) -> dict:
    """List products charged to tickets and project tasks, one page at a time; every filter given must match.
    A row has Id, TicketId or TaskId (with ProjectId), ItemId, Quantity, UnitPrice, UnitCost, Amount (0 unless Billable), BillableStatus, ClientId and InvoiceId (null until invoiced). Removed charges are never listed.
    Paging: pass next_cursor back as cursor with the SAME filters until has_more is false.
    """
    filters: dict[str, Any] = {
        "ticket_ids": guids("ticket_ids", ticket_ids),
        "task_ids": guids("task_ids", task_ids),
        "project_ids": guids("project_ids", project_ids),
        "client_ids": positive_ids("client_ids", client_ids),
        "invoice_ids": guids("invoice_ids", invoice_ids),
        "created_since": utc_iso("created_since", created_since),
        "created_before": utc_iso("created_before", created_before),
    }
    query = {
        LIST_QUERY[param]: csv_ids(param, value) if isinstance(value, list) else value for param, value in filters.items()
    }
    page = await client_of(ctx).get_page(
        LIST_OP,
        query=query,
        page_size=clamp_page_size(page_size),
        cursor=non_empty("cursor", cursor),
        tool="list_product_charges",
    )
    return paged_result(page, filters)


@gorelo_tool(toolset=TOOLSET, kind="read", ops=[GET_OP], field_map=ID_MAP)
async def get_product_charge(
    ctx: Context,
    product_charge_id: Annotated[StrictId, Field(description=CHARGE_ID_TEXT)],
) -> dict:
    """Get one product charge: the same record as a list_product_charges row.
    A charge that does not exist, was removed or belongs to another account is a 404.
    """
    data = await client_of(ctx).get_one(
        GET_OP, path_params={"productChargeId": positive_id("product_charge_id", product_charge_id)}, tool="get_product_charge"
    )
    return expect_object(data, GET_OP, tool="get_product_charge")


# --------------------------------------------------------------------------
# create_product_charge
# --------------------------------------------------------------------------


@gorelo_tool(toolset=TOOLSET, kind="write", ops=[CREATE_OP, GET_OP], field_map=CREATE_FIELDS)
async def create_product_charge(
    ctx: Context,
    item_id: Annotated[str, Field(description="Item GUID (list_items): an active product or bundle.")],
    quantity: Annotated[float, Strict(), Field(description=QUANTITY_TEXT)],
    ticket_id: Annotated[str | None, Field(description="Ticket GUID (list_tickets), not the display number.")] = None,
    task_id: Annotated[str | None, Field(description="Task GUID (list_project_tasks, projects toolset).")] = None,
    unit_price: Annotated[float | None, Strict(), Field(description="Price of one unit. Default: the item's price.")] = None,
    unit_cost: Annotated[float | None, Strict(), Field(description="Cost of one unit, 0 or more. Default: the item's cost.")] = None,
    description: Annotated[str | None, Field(description="Text shown on the ticket and the invoice. Default: the item's.")] = None,
    billable_status_id: Annotated[
        StrictId | None, Field(description=f"{CREATE_BILLABLE_TEXT}. Default 1.")
    ] = None,
) -> dict:
    """Charge a catalog product or bundle to ONE ticket or ONE project task and return the created charge.
    Give exactly one of ticket_id and task_id. It is billed to the ticket's client (the task's project's client) and shows on the timeline. Gorelo refuses (409) a ticket that is closed, has no client or whose client is on credit hold, and a task whose project is closed or has no client.
    Resolve ids first: ticket_id -> list_tickets, task_id -> list_project_tasks (projects toolset), item_id -> list_items.
    Side effects: a real charge that will be invoiced; a second call charges twice. After a "did not confirm" error check list_product_charges before repeating. {Id, warning} means the charge exists: do not repeat.
    """
    ticket = guid("ticket_id", ticket_id) if ticket_id is not None else None
    task = guid("task_id", task_id) if task_id is not None else None
    if (ticket is None) == (task is None):
        got = "both" if ticket is not None else "neither"
        raise ValueError(
            f"ticket_id and task_id: give exactly one of them (a product is charged to one ticket or one task), got {got}"
        )
    values = {
        "ticket_id": ticket,
        "task_id": task,
        "item_id": guid("item_id", item_id),
        "quantity": _quantity("quantity", quantity),
        "unit_price": _price("unit_price", unit_price, low=-PRICE_MAX),
        "unit_cost": _price("unit_cost", unit_cost, low=0),
        "description": _description("description", description),
        "billable_status_id": _billable_status("billable_status_id", billable_status_id, void=False),
    }
    body = build_body(values, CREATE_FIELDS)
    written = await client_of(ctx).post(CREATE_OP, json_body=body, tool="create_product_charge")
    new_id = created_id(written, CREATE_OP, tool="create_product_charge")
    return await reread_after_write(
        ctx, GET_OP, path_params={"productChargeId": new_id}, tool="create_product_charge", written_id=new_id
    )


# --------------------------------------------------------------------------
# update_product_charge
# --------------------------------------------------------------------------


def _update_problems(record: Mapping[str, Any], body: Mapping[str, Any]) -> list[str]:
    """What the answer does not show of the update that was sent (numbers within the rounding Gorelo may apply)."""
    problems = []
    if "Quantity" in body and not _close(record.get("Quantity"), body["Quantity"], 0.00005):
        problems.append("Quantity is not the one that was sent")
    for key in ("UnitPrice", "UnitCost"):
        if key in body and not _close(record.get(key), body[key], 0.005):
            problems.append(f"{key} is not the one that was sent")
    if "BillableStatusId" in body:
        status = record.get("BillableStatus")
        if not isinstance(status, Mapping) or status.get("Id") != body["BillableStatusId"]:
            problems.append("BillableStatus is not the one that was sent")
    if "Description" in body:
        shown = record.get("Description")
        if body["Description"] is None:
            if shown not in (None, ""):
                problems.append("Description is still set")
        elif not isinstance(shown, str) or shown.strip() != body["Description"].strip():
            problems.append("Description is not the text that was sent")
    return problems


@gorelo_tool(
    toolset=TOOLSET,
    kind="write",
    ops=[UPDATE_OP],
    field_map={**UPDATE_FIELDS, **ID_MAP},
    destructive_hint=True,
)
async def update_product_charge(
    ctx: Context,
    product_charge_id: Annotated[StrictId, Field(description=CHARGE_ID_TEXT)],
    quantity: Annotated[float | None, Strict(), Field(description="New quantity. " + QUANTITY_TEXT)] = None,
    unit_price: Annotated[float | None, Strict(), Field(description="New price of one unit.")] = None,
    unit_cost: Annotated[float | None, Strict(), Field(description="New cost of one unit, 0 or more.")] = None,
    description: Annotated[str | None, Field(description="New text. To remove it use clear_description.")] = None,
    billable_status_id: Annotated[StrictId | None, Field(description=BILLABLE_TEXT + ".")] = None,
    clear_description: Annotated[StrictBool, Field(description="true removes the description. Not with description.")] = False,
) -> dict:
    """Change a product charge and return the updated charge. Send only what changes; send at least one field.
    What it is charged to and its item cannot change: remove the charge (delete_product_charge, when deletes are enabled) and add another. Gorelo refuses (409) while the ticket is closed, the task's project is closed, or the charge is waiting to be invoiced or has been invoiced.
    Resolve ids first: product_charge_id -> list_product_charges.
    Side effects: overwrites the fields given; the charge's amount and the ticket's product total change with them.
    """
    charge = positive_id("product_charge_id", product_charge_id)
    if clear_description is True and description is not None:
        raise ValueError(
            "clear_description: cannot be true together with description (description replaces the text, "
            "clear_description removes it); give one of them"
        )
    values = {
        "quantity": _quantity("quantity", quantity),
        "unit_price": _price("unit_price", unit_price, low=-PRICE_MAX),
        "unit_cost": _price("unit_cost", unit_cost, low=0),
        "description": _description("description", description),
        "billable_status_id": _billable_status("billable_status_id", billable_status_id),
    }
    body = build_body(
        values,
        UPDATE_FIELDS,
        clear=["description"] if clear_description is True else [],
        clear_values={"description": None},
    )
    if not body:
        raise ValueError(
            "quantity, unit_price, unit_cost, description, billable_status_id and clear_description: give at least "
            "one of them, there is nothing to change"
        )
    answer = await client_of(ctx).patch(
        UPDATE_OP, path_params={"productChargeId": charge}, json_body=body, tool="update_product_charge"
    )
    record = expect_object(answer, UPDATE_OP, tool="update_product_charge")
    problems = _update_problems(record, body)
    if problems:
        raise _unconfirmed(
            UPDATE_OP, f"the answer does not show the change ({'; '.join(problems)})", "get_product_charge"
        )
    return record


# --------------------------------------------------------------------------
# delete_product_charge
# --------------------------------------------------------------------------


@gorelo_tool(toolset=TOOLSET, kind="destructive", ops=[DELETE_OP], field_map=ID_MAP)
async def delete_product_charge(
    ctx: Context,
    product_charge_id: Annotated[StrictId, Field(description=CHARGE_ID_TEXT)],
    confirm: Annotated[StrictBool, Field(description=CONFIRM_TEXT)] = False,
) -> dict:
    """Remove one product charge from its ticket or task and return {Id}.
    Gorelo refuses (409) while the ticket is closed, the task's project is closed, or the charge is waiting to be invoiced or has been invoiced. A charge that is already removed is a 404.
    Side effects: the charge is no longer listed or billed, and the removal appears on the ticket's or task's timeline. No tool puts it back; charge it again with create_product_charge.
    Ask the user first; needs confirm=true.
    """
    charge = positive_id("product_charge_id", product_charge_id)
    require_confirm(
        confirm,
        action=f"remove product charge {charge}",
        effect=(
            "The charge is removed from its ticket or task and no longer billed, and the removal appears on the "
            "timeline; no tool puts it back. Nothing has been sent to Gorelo."
        ),
    )
    data = await client_of(ctx).delete(DELETE_OP, path_params={"productChargeId": charge}, tool="delete_product_charge")
    record = expect_object(data, DELETE_OP, tool="delete_product_charge")
    if record.get("Id") != charge:
        shown = "missing" if "Id" not in record else describe_value(record.get("Id"))
        raise _unconfirmed(
            DELETE_OP, f"the answer's Id is {shown}, not the charge that was asked for", "get_product_charge"
        )
    return record
