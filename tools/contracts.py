"""Contracts: list, read, create and update (toolset "billing").

Tools:

    list_contracts   read         GET /v1/contracts (paged; each contract carries its service lines)
    get_contract     read         GET /v1/contracts/{contractId} (service lines with labor detail and line items)
    create_contract  destructive  POST /v1/contracts, GET /v1/contracts/{contractId}
    update_contract  destructive  GET + PATCH /v1/contracts/{contractId}, GET /v1/contracts/{contractId}

Ops owned by this module (each must exist in spec/spec_index.json, none may be in FORBIDDEN_OPS; keep the list in
step with the ops=[...] of the declarations below):

    GET /v1/contracts
    GET /v1/contracts/{contractId}
    POST /v1/contracts
    PATCH /v1/contracts/{contractId}

DELETE /v1/contracts/{contractId} is in FORBIDDEN_OPS (it destroys billing configuration), so it can never be declared
or called. A contract is archived (StatusId 3) and restored (StatusId 1) with update_contract instead.

Why create and update are gated (kind "destructive", registered only when GORELO_ENABLE_DESTRUCTIVE enables them, each
refused with no HTTP call unless confirm is true): a Block Hours line raises a Draft invoice for the block at once (the
POST text; on update a NEW line and a No Labor Terms line CONVERTED to Block Hours both do), and AutoApproveAndSend makes
Gorelo approve and send every invoice the contract raises on its own: each is emailed to the client (IsEmailSent was true
about 2 minutes after creation, live 2026-10-04) and pushed to the connected accounting system. Contracts cannot be deleted, so a mistaken one can only
be archived.

A service line's Id is the identifier time entries call ServiceLineId: the list tool's docstring says so because
create_time_entry and update_time_entry take it as service_line_id.

The 2026-10-29 contract (6c30770dffc0) changed the record: InvoiceDue is DaysUntilDue, the line item Cost is UnitCost,
UnlimitedHoursDetails.AutoApprove moved to the service line as AutoApproveTime, and the record gained ProrateType,
AutoInvoiceTicketProducts, InvoiceTemplateId, InvoiceEmailTemplateId, BrandingThemeId, AutoInvoiceTime, IncludedTimeGoesTo,
HourlyRate and CustomRates (Per Hour and Limited Hours) and TemplateId and ShowRemainingHoursInClientPortal (Block Hours).
get_contract passes the record through unchanged, so nothing here names a removed field.

Nested input (service lines, line items, the four labor details objects) is accepted as snake_case objects. The pydantic
models refuse an unknown field (additionalProperties false) and the pure validators below check the rules the spec states
before any request, naming the snake_case path (service_lines[0].block_hours_details.balance). Gorelo numbers the items of
a list it refuses in the list it RECEIVED, which for update_contract is the complete list the tool builds (current lines
first, then the new ones; the same for the line items of a line). _retarget therefore rewrites the PropertyName of such an
error to the caller's own entry (service_lines item N, line_items item M) or, for a line or line item the caller did not
name, to "kept service line Id N" / "kept line item Id N". An error of a call that sent no ServiceLines keeps Gorelo's name.

update_contract replaces lists, so it reads the contract first. ServiceLines replaces the set matched by Id and a line left
out is REMOVED (409 while it has un-invoiced time); each line's LineItems replace its set the same way. The tool therefore
sends every current line (as {"Id": n} alone) unless the call updates it by id or names it in remove_service_line_ids, and
does the same for the line items of a line whose line_items or remove_line_item_ids it was given. Nothing is dropped
silently. A line with no line_items and no remove_line_item_ids is sent without LineItems ("leave it out to keep the line
items as they are"). After the PATCH the contract is read back and the tool checks that the read shows the change: the
ids, counts and names, and then field by field (_field_problems) every kept line against the first read (labor terms, auto
flags, where included time goes, work types and roles, the details object, each line item's quantity, prices, discount,
tax, billable status, description and dates) and every value that was sent against what the read shows, with a small
rounding margin. Each difference is listed (field, expected, read) in the write-unconfirmed error; a name or a description
is never quoted. A field the first read did not show is not compared, and Price (not on the read model) never is.

Integer ids are StrictId parameters (JSON true, "5" or 5.0 is refused by the schema) and are then range-checked by
positive_id / positive_ids; the single-record answer goes through expect_object.

status_ids is checked against 1 Active, 2 Expired, 3 Archived before any HTTP call (the spec says any other id is a 400);
omitted, Gorelo returns Active and Expired.

Spec rules this module cannot check locally (Gorelo answers them): that a client, location, contact, work type, work role,
item, tax, template or branding theme id exists and belongs to the client or provider; a Block Hours Template id; that a
StartDate or RepeatPeriodId change is refused with 409 once an invoice was generated; that a line with un-invoiced time
cannot be removed (409); that only a No Labor Terms line can change its labor terms, and only once; whether Balance may
change on a Block Hours line that uses a template; and that IsForAllLocations=false needs LocationIds "sent or already
stored" on an update.
"""

import math
import re
from collections.abc import Mapping
from datetime import date
from typing import Annotated, Any, Literal

from fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field, Strict

from gorelo_client import GoreloAPIError
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
    non_empty,
    paged_result,
    positive_id,
    positive_ids,
    reread_after_write,
    require_confirm,
    utc_iso,
)

LIST_OP = "GET /v1/contracts"
GET_OP = "GET /v1/contracts/{contractId}"
CREATE_OP = "POST /v1/contracts"
UPDATE_OP = "PATCH /v1/contracts/{contractId}"

# snake_case tool parameter -> Gorelo query name. The same map turns a Gorelo PropertyName back into the
# parameter to fix in error messages.
LIST_FIELDS = {
    "client_ids": "ClientIds",
    "status_ids": "StatusIds",
    "query": "Query",
    "created_since": "CreatedSince",
    "created_before": "CreatedBefore",
    "updated_since": "UpdatedSince",
    "updated_before": "UpdatedBefore",
    "end_date_since": "EndDateSince",
    "end_date_before": "EndDateBefore",
    "sort_by": "SortBy",
    "sort_order": "SortOrder",
    "page_size": "PageSize",
    "cursor": "Cursor",
}
# The two filters that compare calendar dates (a contract's EndDate is one). The tool takes YYYY-MM-DD and sends <date>T00:00:00Z.
DAY_FILTERS = ("end_date_since", "end_date_before")
QUERY_MAX = 200  # "Max 200 characters" (the Query text of GET /v1/contracts)

# The contract status scale of GET /v1/contracts (StatusIds). Gorelo derives the status (a contract has none stored);
# omitted, it returns Active and Expired, so archived contracts are left out.
CONTRACT_STATUS_LABELS = {1: "Active", 2: "Expired", 3: "Archived"}
CONTRACT_STATUS_SCALE = ", ".join(f"{status} {label}" for status, label in CONTRACT_STATUS_LABELS.items())
CONTRACT_STATUSES = (
    f"{CONTRACT_STATUS_SCALE}. Omitted: Active and Expired (archived left out). [1] gives live contracts only."
)

# What update_contract's `status` writes: StatusId 3 archives and 1 restores. 2 Expired follows from EndDate and cannot be set.
STATUS_WRITES = {"active": 1, "archived": 3}

LABOR_TERMS = {1: "Unlimited Hours", 2: "Hourly", 3: "Block Hours", 4: "No Labor Terms", 5: "Limited Hours"}
LABOR_TERMS_SCALE = ", ".join(f"{key} {label}" for key, label in LABOR_TERMS.items())
NO_LABOR_TERMS = 4
# labor terms id -> (the snake_case details parameter, the Gorelo field). No Labor Terms takes none.
DETAILS = {
    1: ("unlimited_hours_details", "UnlimitedHoursDetails"),
    2: ("per_hour_details", "PerHourDetails"),
    3: ("block_hours_details", "BlockHoursDetails"),
    5: ("limited_hours_details", "LimitedHoursDetails"),
}
DETAILS_KEYS = tuple(key for key, _ in DETAILS.values())
# IncludedTimeGoesToId: "Unlimited, Block and Limited Hours only".
INCLUDED_TIME_TERMS = (1, 3, 5)
INCLUDED_TIME = {1: "Complete", 2: "Waiting to invoice"}
RATE_TYPES = {1: "Use default labor rates", 2: "Use fixed labor rate", 3: "Custom labor rates"}
RATE_TYPE_FIXED = 2
PRORATE_TYPES = {0: "Do not prorate", 1: "Prorate and add to next invoice", 2: "Prorate and generate standalone invoice"}
REPEAT_PERIODS = {1: "Monthly", 2: "Quarterly", 3: "Yearly", 4: "Half Yearly"}
BILLABLE_STATUSES = {1: "Billable", 2: "No charge", 3: "Non-billable"}
BLOCK_CUSTOM = 0  # BlockHoursDetailsInput.TemplateId 0 means Custom: no template
WARNING_THRESHOLD_MAX = 90
OVERRUN_THRESHOLD_MAX = 50
DAYS_BEFORE_MAX = 31
INT32_MAX = 2**31 - 1  # DaysUntilDue is an int32

# {tool param: CreateContractCommand field}. A Gorelo PropertyName turns back into the snake_case param through it; one that
# points into a list ("ServiceLines[1].BlockHoursDetails.Balance") reads "service_lines (item 2, BlockHoursDetails, Balance)".
CONTRACT_FIELDS = {
    "name": "Name",
    "is_for_all_locations": "IsForAllLocations",
    "location_ids": "LocationIds",
    "reference": "Reference",
    "start_date": "StartDate",
    "end_date": "EndDate",
    "prorate_type_id": "ProrateTypeId",
    "auto_invoice_ticket_products": "AutoInvoiceTicketProducts",
    "repeat_period_id": "RepeatPeriodId",
    "days_before_invoice_creation": "DaysBeforeInvoiceCreation",
    "days_until_due": "DaysUntilDue",
    "auto_approve_and_send": "AutoApproveAndSend",
    "contact_ids": "ContactIds",
    "invoice_template_id": "InvoiceTemplateId",
    "invoice_email_template_id": "InvoiceEmailTemplateId",
    "branding_theme_id": "BrandingThemeId",
    "service_lines": "ServiceLines",
}
CREATE_FIELDS = {**CONTRACT_FIELDS, "client_id": "ClientId"}
UPDATE_FIELDS = {**CONTRACT_FIELDS, "status": "StatusId", "contract_id": "contractId"}
ID_MAP = {"contract_id": "contractId"}

# What clear_fields of update_contract may remove, and what Gorelo expects on the wire (UpdateContractCommand: Reference,
# EndDate, InvoiceTemplateId, InvoiceEmailTemplateId and BrandingThemeId clear with null; ContactIds [] removes every
# auto-approve contact).
CONTRACT_CLEAR_VALUES: dict[str, Any] = {
    "reference": None,
    "end_date": None,
    "invoice_template_id": None,
    "invoice_email_template_id": None,
    "branding_theme_id": None,
    "contact_ids": [],
}
ContractClearable = Literal[
    "reference", "end_date", "invoice_template_id", "invoice_email_template_id", "branding_theme_id", "contact_ids"
]
# A line item field that clears with null (ContractLineItemInput: Description "Send null to clear it", EndDate "Send null to
# make it open-ended").
ITEM_CLEARABLE = ("description", "end_date")
ItemClearable = Literal["description", "end_date"]

# {line item param: ContractLineItemInput field}. item_id is accepted on a NEW item only.
ITEM_FIELDS = {
    "item_id": "ItemId",
    "quantity": "Quantity",
    "description": "Description",
    "unit_price": "UnitPrice",
    "unit_cost": "UnitCost",
    "discount_percent": "DiscountPercent",
    "tax_id": "TaxId",
    "billable_status_id": "BillableStatusId",
    "start_date": "StartDate",
    "end_date": "EndDate",
}
LINE_KEYS = (
    "name",
    "labor_terms_id",
    "work_type_ids",
    "work_role_ids",
    "auto_approve_time",
    "auto_invoice_time",
    "included_time_goes_to_id",
    *DETAILS_KEYS,
    "line_items",
)

_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

# What auto_approve_and_send=true does, as the confirm refusals of both gated tools say it. Gorelo does not document the email, but
# the approved invoices that contracts raised were seen with IsEmailSent true about 2 minutes after creation (live, 2026-10-04).
AUTO_SEND_EFFECT = (
    "auto_approve_and_send=true makes Gorelo approve and send every invoice the contract raises on its own: each one is "
    "EMAILED to the client (seen live: sent about 2 minutes after it was created), and approving pushes each one to the "
    "connected accounting system."
)
LINE_ITEMS_TEXT = "Products billed on the line. Price, cost, tax and description left out use the item's."
TEMPLATES_TEXT = "Ask the user: no tool lists them."
CONFIRM_TEXT = "Must be true; only after the user approves."


# --------------------------------------------------------------------------
# Input models (snake_case, additionalProperties refused). No docstrings on purpose: pydantic would copy them into the
# advertised schema, which claude.ai pays for in every conversation; the field descriptions say what each needs.
# --------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkRoleRate(_Strict):
    work_role_id: Annotated[StrictId, Field(description="From list_billing_roles.")]
    hourly_rate: Annotated[float | None, Strict(), Field(description="Per hour.")] = None
    coa_code: Annotated[str | None, Field(description="Accounting code.")] = None


class WorkTypeRate(_Strict):
    work_type_id: Annotated[StrictId, Field(description="From list_work_types.")]
    hourly_multiplier: Annotated[float | None, Strict(), Field(description="Multiplier on the role's rate.")] = None
    coa_code: Annotated[str | None, Field(description="Accounting code.")] = None


class CustomRates(_Strict):
    work_roles: Annotated[list[WorkRoleRate] | None, Field(description="Roles to reprice.")] = None
    work_types: Annotated[list[WorkTypeRate] | None, Field(description="Work types to reprice.")] = None


class UnlimitedHoursDetails(_Strict):
    pass


class PerHourDetails(_Strict):
    rate_type_id: Annotated[StrictId | None, Field(description="1 default rates (default), 2 fixed, 3 custom.")] = None
    hourly_rate: Annotated[float | None, Strict(), Field(description="Only with rate_type_id 2, where it is required.")] = None
    custom_rates: Annotated[CustomRates | None, Field(description="Overrides for rate_type_id 3.")] = None


class LimitedHoursDetails(_Strict):
    allocated_hours: Annotated[float | None, Strict(), Field(description="Hours per period. Required on a new line.")] = None
    show_remaining_hours_in_client_portal: Annotated[StrictBool | None, Field(description="Default false.")] = None
    rate_type_id: Annotated[StrictId | None, Field(description="1 default rates (default), 2 fixed, 3 custom.")] = None
    hourly_rate: Annotated[float | None, Strict(), Field(description="Only with rate_type_id 2, where it is required.")] = None
    custom_rates: Annotated[CustomRates | None, Field(description="Overrides for rate_type_id 3.")] = None


class BlockHoursDetails(_Strict):
    template_id: Annotated[
        StrictId | None,
        Field(description="0 Custom, else a Block Hours Template id (ask the user). Required on a new line, fixed after."),
    ] = None
    balance: Annotated[float | None, Strict(), Field(description="Hours. Custom only, and then required on a new line.")] = None
    price: Annotated[
        float | None, Strict(), Field(description="Billed once on a Draft invoice. Custom only, then required; fixed after.")
    ] = None
    warning_threshold: Annotated[StrictId | None, Field(description="Percent left that warns, 0-90.")] = None
    overrun_threshold: Annotated[StrictId | None, Field(description="Percent allowed past zero, 0-50.")] = None
    show_remaining_hours_in_client_portal: Annotated[StrictBool | None, Field(description="Default false.")] = None


class NewLineItem(_Strict):
    item_id: Annotated[str, Field(description="From list_items.")]
    quantity: Annotated[float, Strict(), Field(description="Above 0.")]
    description: Annotated[str | None, Field(description="Default: the item's.")] = None
    unit_price: Annotated[float | None, Strict(), Field(description="Default: the item's.")] = None
    unit_cost: Annotated[float | None, Strict(), Field(description="Default: the item's.")] = None
    discount_percent: Annotated[float | None, Strict(), Field(description="0-100. Default 0.")] = None
    tax_id: Annotated[StrictId | None, Field(description="From list_taxes. Default: the item's tax.")] = None
    billable_status_id: Annotated[StrictId | None, Field(description="1 Billable (default), 2 No charge, 3 Non-billable.")] = None
    start_date: Annotated[str | None, Field(description="YYYY-MM-DD. Default: today.")] = None
    end_date: Annotated[str | None, Field(description="YYYY-MM-DD. Default: open-ended.")] = None


class LineItemChange(_Strict):
    id: Annotated[StrictId | None, Field(description="Current line item to update (get_contract). Leave out to add one.")] = None
    item_id: Annotated[str | None, Field(description="New line item only (list_items).")] = None
    quantity: Annotated[float | None, Strict(), Field(description="Above 0. Required on a new line item.")] = None
    description: Annotated[str | None, Field(description="To clear it use clear_fields.")] = None
    unit_price: Annotated[float | None, Strict(), Field(description="Not negative.")] = None
    unit_cost: Annotated[float | None, Strict(), Field(description="Not negative.")] = None
    discount_percent: Annotated[float | None, Strict(), Field(description="0-100.")] = None
    tax_id: Annotated[StrictId | None, Field(description="From list_taxes. Left out keeps the tax.")] = None
    billable_status_id: Annotated[StrictId | None, Field(description="1 Billable, 2 No charge, 3 Non-billable.")] = None
    start_date: Annotated[str | None, Field(description="YYYY-MM-DD.")] = None
    end_date: Annotated[str | None, Field(description="YYYY-MM-DD. To clear it use clear_fields.")] = None
    clear_fields: Annotated[list[ItemClearable] | None, Field(description="Existing items only.")] = None


class _LineSettings(_Strict):
    work_type_ids: Annotated[
        list[StrictId] | None, Field(description="From list_work_types. New line: omitted covers all. Not on No Labor Terms.")
    ] = None
    work_role_ids: Annotated[list[StrictId] | None, Field(description="From list_billing_roles. Same rules.")] = None
    auto_approve_time: Annotated[StrictBool | None, Field(description="Not on No Labor Terms. Default false.")] = None
    auto_invoice_time: Annotated[StrictBool | None, Field(description="Not on No Labor Terms. Default false.")] = None
    included_time_goes_to_id: Annotated[
        StrictId | None, Field(description="1 Complete (default), 2 Waiting to invoice. Labor terms 1, 3 and 5 only.")
    ] = None
    unlimited_hours_details: Annotated[UnlimitedHoursDetails | None, Field(description="Labor terms 1: send {}.")] = None
    per_hour_details: Annotated[PerHourDetails | None, Field(description="Labor terms 2.")] = None
    block_hours_details: Annotated[BlockHoursDetails | None, Field(description="Labor terms 3.")] = None
    limited_hours_details: Annotated[LimitedHoursDetails | None, Field(description="Labor terms 5.")] = None


class NewServiceLine(_LineSettings):
    name: Annotated[str, Field(description="Service line name.")]
    labor_terms_id: Annotated[StrictId, Field(description="1 Unlimited, 2 Hourly, 3 Block, 4 No Labor Terms, 5 Limited.")]
    line_items: Annotated[list[NewLineItem] | None, Field(description=LINE_ITEMS_TEXT)] = None


class ServiceLineChange(_LineSettings):
    id: Annotated[StrictId | None, Field(description="Current line to update (get_contract). Leave out to add a line.")] = None
    name: Annotated[str | None, Field(description="Required on a new line.")] = None
    labor_terms_id: Annotated[
        StrictId | None, Field(description="1 Unlimited, 2 Hourly, 3 Block, 4 No Labor Terms, 5 Limited. Required on a new line; only No Labor Terms can change, once.")
    ] = None
    line_items: Annotated[
        list[LineItemChange] | None, Field(description="Items to add or update; others stay unless in remove_line_item_ids.")
    ] = None
    remove_line_item_ids: Annotated[list[StrictId] | None, Field(description="Current line items to remove.")] = None


# --------------------------------------------------------------------------
# Private helpers (shared helpers live in tools/_common.py only)
# --------------------------------------------------------------------------


def _status(param: str, value: Any) -> int:
    """One contract status: 1 Active, 2 Expired or 3 Archived.

    Every whole number outside the scale is refused here, before any HTTP call, and answered with the scale and quoted
    back, 0 and the negative numbers included (positive_id's "expected a positive whole number such as 123" would
    suggest that any id will do). Whatever else is not an id (a boolean, a decimal, text, a list, a number beyond
    int64) keeps the message of positive_id."""
    if isinstance(value, int) and not isinstance(value, bool) and -MAX_ID <= value < 1:
        status = int(value)
    else:
        status = positive_id(param, value)
    if status not in CONTRACT_STATUS_LABELS:
        raise ValueError(f"{param}: expected {CONTRACT_STATUS_SCALE}, got {status}")
    return status


def _statuses(param: str, values: Any) -> list[int] | None:
    """The status_ids filter: every item on the contract scale, a bad one named by its index.

    None, an empty list and a value that is not a list keep the messages of positive_ids."""
    if isinstance(values, (list, tuple)) and values:
        return [_status(f"{param}[{index}]", item) for index, item in enumerate(values)]
    return positive_ids(param, values)


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


def _number(where: str, value: Any, *, minimum: float | None = None, above: float | None = None, maximum: float | None = None) -> float:
    """A finite number (never a bool); `minimum` is inclusive, `above` exclusive."""
    if not _is_number(value):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            raise ValueError(f"{where}: expected a finite number, got NaN, infinity or a number too large")
        raise ValueError(f"{where}: expected a number, got {describe_value(value)}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{where}: expected a number of {minimum:g} or more")
    if above is not None and value <= above:
        raise ValueError(f"{where}: expected a number above {above:g}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{where}: expected a number of at most {maximum:g}")
    return value


def _whole(where: str, value: Any, low: int, high: int) -> int:
    """A whole number from `low` to `high` (never a bool or a decimal)."""
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        shown = value if isinstance(value, int) and not isinstance(value, bool) else describe_value(value)
        raise ValueError(f"{where}: expected a whole number from {low} to {high}, got {shown}")
    return value


def _scale(where: str, value: Any, labels: Mapping[int, str]) -> int:
    """A value from a published id scale, answered with the scale when it is not on it."""
    if isinstance(value, bool) or not isinstance(value, int) or value not in labels:
        shown = value if isinstance(value, int) and not isinstance(value, bool) else describe_value(value)
        raise ValueError(f"{where}: expected {', '.join(f'{key} {label}' for key, label in labels.items())}, got {shown}")
    return value


def _flag(where: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{where}: must be true or false, got {describe_value(value)}")
    return value


def _text(where: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{where}: expected text, got {describe_value(value)}")
    if not value.strip():
        raise ValueError(f"{where}: must not be empty or whitespace only")
    return value


def _calendar_date(where: str, value: Any) -> str:
    """A calendar date as YYYY-MM-DD, returned as given. A time, an offset or any other text is refused."""
    if isinstance(value, str) and _DAY.fullmatch(value):
        try:
            date.fromisoformat(value)
        except ValueError:
            pass
        else:
            return value
    raise ValueError(f"{where}: expected a calendar date as YYYY-MM-DD, for example 2026-09-01 (no time, no offset)")


def _midnight(day: str) -> str:
    return f"{day}T00:00:00Z"


def _distinct_ids(where: str, values: Any) -> list[int]:
    """Ids with no repeat: a repeated id in a list that names things to change or remove is a mistake, not a no-op."""
    ids = positive_ids(where, values)
    assert ids is not None
    seen: set[int] = set()
    for index, number in enumerate(ids):
        if number in seen:
            raise ValueError(f"{where}[{index}]: the id appears more than once")
        seen.add(number)
    return ids


def _plain(value: Any) -> Any:
    """A pydantic model as a plain dict, anything else unchanged."""
    return value.model_dump() if isinstance(value, BaseModel) else value


def _mapping(where: str, value: Any, what: str) -> Mapping[str, Any]:
    data = _plain(value)
    if not isinstance(data, Mapping):
        raise ValueError(f"{where}: must be {what}, got {describe_value(value)}")
    return data


def _no_unknown(where: str, data: Mapping[str, Any], allowed: tuple[str, ...]) -> None:
    unknown = sorted(str(key) for key in data if key not in allowed)
    if unknown:
        raise ValueError(f"{where}: unknown field(s) {', '.join(unknown)}; allowed: {', '.join(allowed)}")


def _unconfirmed(op_key: str, detail: str, check_with: str) -> GoreloAPIError:
    """The write-unconfirmed shape error: Gorelo answered 2xx, but the read-back does not show the effect of the write.

    Same shape as the one of tools/documents.py: kind "shape", write_unconfirmed true. The sentence "verify it with a
    read before repeating it" is the one tools._common recognises, so the generic advice is not added twice."""
    return GoreloAPIError(
        f"{op_key}: {detail}; the write may have been applied, so verify it with a read before repeating it "
        f"({check_with})",
        status=200,
        op_key=op_key,
        kind="shape",
        write_unconfirmed=True,
    )


def _unreadable(detail: str) -> GoreloAPIError:
    """The contract read before an update cannot be used: nothing was sent, so this is not an unconfirmed write."""
    return GoreloAPIError(
        f"{GET_OP}: {detail}; refusing to guess, so nothing was changed", status=200, op_key=GET_OP, kind="shape"
    )


# --------------------------------------------------------------------------
# Service line input: details objects and settings
# --------------------------------------------------------------------------


def _custom_rates(where: str, value: Any) -> dict[str, Any]:
    data = _mapping(where, value, "an object with work_roles and/or work_types")
    _no_unknown(where, data, ("work_roles", "work_types"))
    out: dict[str, Any] = {}
    for key, wire_key, id_key, id_wire, number_key, number_wire in (
        ("work_roles", "WorkRoles", "work_role_id", "WorkRoleId", "hourly_rate", "HourlyRate"),
        ("work_types", "WorkTypes", "work_type_id", "WorkTypeId", "hourly_multiplier", "HourlyMultiplier"),
    ):
        rows = data.get(key)
        if rows is None:
            continue
        if isinstance(rows, (str, bytes, Mapping)) or not isinstance(rows, (list, tuple)):
            raise ValueError(f"{where}.{key}: expected a list of rows, got {describe_value(rows)}")
        if not rows:
            raise ValueError(f"{where}.{key}: expected at least one row, got an empty list (omit {key} to change none)")
        wired: list[dict[str, Any]] = []
        seen: set[int] = set()
        for position, row in enumerate(rows):
            at = f"{where}.{key}[{position}]"
            fields = _mapping(at, row, f"an object with {id_key}")
            _no_unknown(at, fields, (id_key, number_key, "coa_code"))
            if fields.get(id_key) is None:
                raise ValueError(f"{at}: missing {id_key}")
            row_id = positive_id(f"{at}.{id_key}", fields[id_key])
            if row_id in seen:
                raise ValueError(f"{at}.{id_key}: the id appears more than once")
            seen.add(row_id)
            entry: dict[str, Any] = {id_wire: row_id}
            if fields.get(number_key) is not None:
                entry[number_wire] = _number(f"{at}.{number_key}", fields[number_key])
            if fields.get("coa_code") is not None:
                entry["CoaCode"] = _text(f"{at}.coa_code", fields["coa_code"])
            wired.append(entry)
        out[wire_key] = wired
    if not out:
        raise ValueError(f"{where}: give work_roles, work_types or both")
    return out


def _rate_fields(where: str, data: Mapping[str, Any], *, fresh: bool, current_rate_type: Any) -> dict[str, Any]:
    """RateTypeId, HourlyRate and CustomRates of a Per Hour or Limited Hours details object.

    HourlyRate is "Required with RateTypeId 2, refused with any other". A new line's rate type defaults to 1, so an
    hourly_rate without rate_type_id 2 is refused there; on an existing line the rate type it has now counts when the call
    does not give one."""
    out: dict[str, Any] = {}
    rate_type = data.get("rate_type_id")
    if rate_type is not None:
        out["RateTypeId"] = _scale(f"{where}.rate_type_id", rate_type, RATE_TYPES)
    effective = rate_type if rate_type is not None else (1 if fresh else current_rate_type)
    rate = data.get("hourly_rate")
    if rate is not None:
        if effective != RATE_TYPE_FIXED:
            raise ValueError(
                f"{where}.hourly_rate: only with rate_type_id 2 (fixed labor rate), refused with any other; "
                "pass rate_type_id 2 with it"
            )
        out["HourlyRate"] = _number(f"{where}.hourly_rate", rate, minimum=0)
    elif rate_type == RATE_TYPE_FIXED:
        raise ValueError(f"{where}.hourly_rate: required with rate_type_id 2 (fixed labor rate)")
    if data.get("custom_rates") is not None:
        out["CustomRates"] = _custom_rates(f"{where}.custom_rates", data["custom_rates"])
    return out


def _details_wire(
    where: str, key: str, data: Mapping[str, Any], *, fresh: bool, current: Mapping[str, Any] | None
) -> dict[str, Any]:
    """One labor details object (PerHourDetailsInput and the others) as its Gorelo body."""
    if key == "unlimited_hours_details":
        _no_unknown(where, data, ())
        return {}
    shows = "show_remaining_hours_in_client_portal"
    if key == "block_hours_details":
        _no_unknown(
            where, data, ("template_id", "balance", "price", "warning_threshold", "overrun_threshold", shows)
        )
        out: dict[str, Any] = {}
        template, balance, price = data.get("template_id"), data.get("balance"), data.get("price")
        if fresh:
            if template is None:
                raise ValueError(
                    f"{where}.template_id: required on a new Block Hours line: 0 for Custom, or a Block Hours Template id"
                )
            template = _whole(f"{where}.template_id", template, 0, MAX_ID)
            out["TemplateId"] = template
            if template == BLOCK_CUSTOM:
                if balance is None or price is None:
                    raise ValueError(f"{where}: template_id 0 (Custom) needs balance and price")
            else:
                for field, shown in (("balance", balance), ("price", price)):
                    if shown is not None:
                        raise ValueError(
                            f"{where}.{field}: refused with a Block Hours Template (balance and price come from the "
                            "template); use template_id 0 for a custom block"
                        )
        else:
            for field, shown in (("template_id", template), ("price", price)):
                if shown is not None:
                    raise ValueError(
                        f"{where}.{field}: cannot change on an existing Block Hours line (only balance, the thresholds and "
                        f"{shows} can); leave it out"
                    )
        if balance is not None:
            out["Balance"] = _number(f"{where}.balance", balance, minimum=0)
        if price is not None:
            out["Price"] = _number(f"{where}.price", price, minimum=0)
        if data.get("warning_threshold") is not None:
            out["WarningThreshold"] = _whole(f"{where}.warning_threshold", data["warning_threshold"], 0, WARNING_THRESHOLD_MAX)
        if data.get("overrun_threshold") is not None:
            out["OverrunThreshold"] = _whole(f"{where}.overrun_threshold", data["overrun_threshold"], 0, OVERRUN_THRESHOLD_MAX)
        if data.get(shows) is not None:
            out["ShowRemainingHoursInClientPortal"] = _flag(f"{where}.{shows}", data[shows])
        return out
    current_rate_type = None
    if current is not None:
        shown = current.get("RateType")
        current_rate_type = shown.get("Id") if isinstance(shown, Mapping) else None
    if key == "per_hour_details":
        _no_unknown(where, data, ("rate_type_id", "hourly_rate", "custom_rates"))
        return _rate_fields(where, data, fresh=fresh, current_rate_type=current_rate_type)
    _no_unknown(where, data, ("allocated_hours", shows, "rate_type_id", "hourly_rate", "custom_rates"))
    out = {}
    hours = data.get("allocated_hours")
    if hours is None and fresh:
        raise ValueError(f"{where}.allocated_hours: required on a new Limited Hours line")
    if hours is not None:
        out["AllocatedHours"] = _number(f"{where}.allocated_hours", hours, minimum=0)
    if data.get(shows) is not None:
        out["ShowRemainingHoursInClientPortal"] = _flag(f"{where}.{shows}", data[shows])
    out.update(_rate_fields(where, data, fresh=fresh, current_rate_type=current_rate_type))
    return out


def _line_wire(
    where: str,
    data: Mapping[str, Any],
    *,
    terms: int,
    fresh: bool,
    emit_terms: bool,
    current: Mapping[str, Any] | None = None,
    need_name: bool | None = None,
) -> dict[str, Any]:
    """The settings of one service line (everything but Id and LineItems) as a Gorelo object.

    `fresh` is a line Gorelo creates (POST, an added line of a PATCH, or a No Labor Terms line that takes labor terms):
    the labor terms and the one matching details object are required, and so is the Name unless `need_name` says the line
    has one already (the line that takes labor terms). `terms` is the labor terms id the line will have. `current` is the
    line as the read shows it (an existing line only)."""
    _no_unknown(where, data, ("id", "remove_line_item_ids", *LINE_KEYS))
    wire: dict[str, Any] = {}
    if data.get("name") is not None:
        wire["Name"] = _text(f"{where}.name", data["name"])
    elif fresh if need_name is None else need_name:
        raise ValueError(f"{where}.name: required on a new service line")
    if emit_terms:
        wire["LaborTermsId"] = terms
    label = LABOR_TERMS[terms]
    for key, field in (("work_type_ids", "WorkTypeIds"), ("work_role_ids", "WorkRoleIds")):
        if data.get(key) is not None:
            if terms == NO_LABOR_TERMS:
                raise ValueError(f"{where}.{key}: not accepted on a No Labor Terms line")
            wire[field] = positive_ids(f"{where}.{key}", data[key])
    for key, field in (("auto_approve_time", "AutoApproveTime"), ("auto_invoice_time", "AutoInvoiceTime")):
        if data.get(key) is not None:
            value = _flag(f"{where}.{key}", data[key])
            if value and terms == NO_LABOR_TERMS:
                raise ValueError(f"{where}.{key}: not accepted on a No Labor Terms line (every other labor term takes it)")
            wire[field] = value
    if data.get("included_time_goes_to_id") is not None:
        if terms not in INCLUDED_TIME_TERMS:
            raise ValueError(
                f"{where}.included_time_goes_to_id: only on Unlimited, Block and Limited Hours lines, got labor terms {terms} ({label})"
            )
        wire["IncludedTimeGoesToId"] = _scale(f"{where}.included_time_goes_to_id", data["included_time_goes_to_id"], INCLUDED_TIME)
    wanted = DETAILS.get(terms)
    for key in DETAILS_KEYS:
        if data.get(key) is not None and (wanted is None or key != wanted[0]):
            hint = f"give {wanted[0]} only" if wanted else "a No Labor Terms line takes none"
            raise ValueError(f"{where}.{key}: does not match labor_terms_id {terms} ({label}); {hint}")
    if wanted is not None:
        key, field = wanted
        given = data.get(key)
        if given is None:
            if fresh:
                extra = " (send {} for Unlimited Hours)" if terms == 1 else ""
                raise ValueError(f"{where}.{key}: required for labor_terms_id {terms} ({label}){extra}")
        else:
            current_details = current.get(field) if current is not None else None
            wire[field] = _details_wire(
                f"{where}.{key}",
                key,
                _mapping(f"{where}.{key}", given, "an object"),
                fresh=fresh,
                current=current_details if isinstance(current_details, Mapping) else None,
            )
    return wire


# --------------------------------------------------------------------------
# Line items
# --------------------------------------------------------------------------


def _item_wire(where: str, data: Mapping[str, Any], *, existing: bool) -> dict[str, Any]:
    """One entry of a service line's LineItems, WITHOUT its Id (the caller adds it for an existing item)."""
    allowed = ("id", *ITEM_FIELDS, "clear_fields")
    _no_unknown(where, data, allowed)
    if existing:
        if data.get("item_id") is not None:
            raise ValueError(f"{where}.item_id: not accepted on an existing line item; remove it and add a new one")
    else:
        if data.get("clear_fields") is not None:
            raise ValueError(f"{where}.clear_fields: only for an existing line item (one with id)")
        missing = [key for key in ("item_id", "quantity") if data.get(key) is None]
        if missing:
            raise ValueError(f"{where}: missing {' and '.join(missing)} (a new line item needs both)")
    wire: dict[str, Any] = {}
    if data.get("item_id") is not None:
        wire["ItemId"] = guid(f"{where}.item_id", data["item_id"])
    if data.get("quantity") is not None:
        wire["Quantity"] = _number(f"{where}.quantity", data["quantity"], above=0)
    if data.get("description") is not None:
        wire["Description"] = _text(f"{where}.description", data["description"])
    for key in ("unit_price", "unit_cost"):
        if data.get(key) is not None:
            wire[ITEM_FIELDS[key]] = _number(f"{where}.{key}", data[key], minimum=0)
    if data.get("discount_percent") is not None:
        wire["DiscountPercent"] = _number(f"{where}.discount_percent", data["discount_percent"], minimum=0, maximum=100)
    if data.get("tax_id") is not None:
        wire["TaxId"] = positive_id(f"{where}.tax_id", data["tax_id"])
    if data.get("billable_status_id") is not None:
        wire["BillableStatusId"] = _scale(f"{where}.billable_status_id", data["billable_status_id"], BILLABLE_STATUSES)
    for key in ("start_date", "end_date"):
        if data.get(key) is not None:
            wire[ITEM_FIELDS[key]] = _midnight(_calendar_date(f"{where}.{key}", data[key]))
    clear = data.get("clear_fields")
    if clear is not None:
        if isinstance(clear, (str, bytes, Mapping)) or not isinstance(clear, (list, tuple)):
            raise ValueError(f"{where}.clear_fields: expected a list such as ['end_date'], got {describe_value(clear)}")
        if not clear:
            raise ValueError(f"{where}.clear_fields: expected at least one name, got an empty list (omit it to clear none)")
        for position, name in enumerate(dict.fromkeys(clear)):
            if name not in ITEM_CLEARABLE:
                raise ValueError(f"{where}.clear_fields[{position}]: cannot clear {name!r}; allowed: {', '.join(ITEM_CLEARABLE)}")
            if name in wire or ITEM_FIELDS[name] in wire:
                raise ValueError(f"{where}.{name}: cannot be given a value and cleared in the same call")
            wire[ITEM_FIELDS[name]] = None
    return wire


def _line_items(where: str, value: Any, *, change: bool) -> list[tuple[int | None, dict[str, Any]]]:
    """The line_items of one service line as [(id or None, wire)], each entry validated. [] is refused."""
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{where}: expected a list of line items, got {describe_value(value)}")
    if not value:
        raise ValueError(f"{where}: expected at least one line item, got an empty list (omit line_items to keep them as they are)")
    entries: list[tuple[int | None, dict[str, Any]]] = []
    seen: set[int] = set()
    for position, raw in enumerate(value):
        at = f"{where}[{position}]"
        data = _mapping(at, raw, "an object with item_id and quantity" if not change else "a line item object")
        item_id = data.get("id")
        if item_id is not None:
            if not change:
                raise ValueError(f"{at}.id: not accepted when the contract is created")
            item_id = positive_id(f"{at}.id", item_id)
            if item_id in seen:
                raise ValueError(f"{at}.id: the line item appears more than once")
            seen.add(item_id)
        elif data.get("clear_fields") is not None and not change:
            raise ValueError(f"{at}.clear_fields: only for an existing line item (one with id)")
        entries.append((item_id, _item_wire(at, data, existing=item_id is not None)))
    return entries


def _new_service_lines(param: str, value: Any) -> tuple[list[dict[str, Any]], int]:
    """The ServiceLines array of a create from service_lines, and how many of its lines are Block Hours lines."""
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{param}: expected a list of service lines, each an object with name and labor_terms_id, got {describe_value(value)}")
    if not value:
        raise ValueError(f"{param}: at least one service line is required, each an object with name and labor_terms_id")
    lines: list[dict[str, Any]] = []
    blocks = 0
    for position, raw in enumerate(value):
        where = f"{param}[{position}]"
        data = _mapping(where, raw, "an object with name and labor_terms_id")
        for key in ("id", "remove_line_item_ids"):
            if data.get(key) is not None:
                raise ValueError(f"{where}.{key}: not accepted when the contract is created (it is for update_contract)")
        if data.get("labor_terms_id") is None:
            raise ValueError(f"{where}.labor_terms_id: required on a new service line ({LABOR_TERMS_SCALE})")
        terms = _scale(f"{where}.labor_terms_id", data["labor_terms_id"], LABOR_TERMS)
        wire = _line_wire(where, data, terms=terms, fresh=True, emit_terms=True)
        if data.get("line_items") is not None:
            wire["LineItems"] = [item for _, item in _line_items(f"{where}.line_items", data["line_items"], change=False)]
        blocks += terms == 3
        lines.append(wire)
    return lines, blocks


# --------------------------------------------------------------------------
# Contract level input (shared by create_contract and update_contract)
# --------------------------------------------------------------------------


def _contract_values(v: Mapping[str, Any], *, creating: bool) -> dict[str, Any]:
    """The checked contract level values (a CONTRACT_FIELDS param -> the value to send, None omitted).

    Pure: no HTTP, raises ValueError naming the snake_case param. The rules are the ones the spec states: dates are calendar
    dates and the end is after the start, RepeatPeriodId 1-4, ProrateTypeId 0-2, DaysBeforeInvoiceCreation 0-31,
    LocationIds only with IsForAllLocations false (a location list that Gorelo would silently ignore is refused here, because
    an all-locations contract is a different contract), AutoInvoiceTicketProducts only on an all-locations contract."""
    out: dict[str, Any] = {}
    if v.get("name") is not None:
        out["name"] = _text("name", v["name"])
    if v.get("reference") is not None:
        out["reference"] = _text("reference", v["reference"])
    for key in ("start_date", "end_date"):
        if v.get(key) is not None:
            out[key] = _calendar_date(key, v[key])
    if "start_date" in out and "end_date" in out and date.fromisoformat(out["end_date"]) <= date.fromisoformat(out["start_date"]):
        raise ValueError(f"end_date: {out['end_date']} is not after start_date {out['start_date']}; the end must come after the start")
    if v.get("prorate_type_id") is not None:
        out["prorate_type_id"] = _scale("prorate_type_id", v["prorate_type_id"], PRORATE_TYPES)
    if v.get("repeat_period_id") is not None:
        out["repeat_period_id"] = _scale("repeat_period_id", v["repeat_period_id"], REPEAT_PERIODS)
    if v.get("days_before_invoice_creation") is not None:
        out["days_before_invoice_creation"] = _whole("days_before_invoice_creation", v["days_before_invoice_creation"], 0, DAYS_BEFORE_MAX)
    if v.get("days_until_due") is not None:
        out["days_until_due"] = _whole("days_until_due", v["days_until_due"], 0, INT32_MAX)
    for key in ("auto_approve_and_send", "auto_invoice_ticket_products", "is_for_all_locations"):
        if v.get(key) is not None:
            out[key] = _flag(key, v[key])
    for key in ("invoice_template_id", "invoice_email_template_id"):
        if v.get(key) is not None:
            out[key] = positive_id(key, v[key])
    if v.get("branding_theme_id") is not None:
        out["branding_theme_id"] = guid("branding_theme_id", v["branding_theme_id"])
    if v.get("contact_ids") is not None:
        if isinstance(v["contact_ids"], (list, tuple)) and not v["contact_ids"]:
            how = "omit it for none" if creating else "to remove every auto-approve contact pass clear_fields=['contact_ids']"
            raise ValueError(f"contact_ids: expected at least one id, got an empty list ({how})")
        out["contact_ids"] = positive_ids("contact_ids", v["contact_ids"])
    if v.get("location_ids") is not None:
        out["location_ids"] = positive_ids("location_ids", v["location_ids"])
    everywhere = out.get("is_for_all_locations")
    if "location_ids" in out and everywhere is True:
        raise ValueError(
            "location_ids: only with is_for_all_locations=false; with true Gorelo ignores the list, so the contract would "
            "cover every location"
        )
    if creating:
        if everywhere is False and "location_ids" not in out:
            raise ValueError("location_ids: at least one location of the client is required when is_for_all_locations is false")
        if everywhere is not False and "location_ids" in out:
            raise ValueError(
                "location_ids: only with is_for_all_locations=false; left out or true Gorelo ignores the list, so the "
                "contract would cover every location"
            )
    if out.get("auto_invoice_ticket_products") is True and everywhere is False:
        raise ValueError("auto_invoice_ticket_products: only on a contract that covers all locations (is_for_all_locations true)")
    return out


def _contract_body(values: Mapping[str, Any], field_map: Mapping[str, str], *, clear: list[str] | None = None) -> dict[str, Any]:
    """The contract level fields of the request, from checked values (dates become <date>T00:00:00Z)."""
    wired = dict(values)
    for key in ("start_date", "end_date"):
        if key in wired:
            wired[key] = _midnight(wired[key])
    return build_body(wired, field_map, clear=clear, clear_values=CONTRACT_CLEAR_VALUES) if wired or clear else {}


# --------------------------------------------------------------------------
# list_contracts
# --------------------------------------------------------------------------


@gorelo_tool(toolset="billing", kind="read", ops=[LIST_OP], field_map=LIST_FIELDS)
async def list_contracts(
    ctx: Context,
    client_ids: Annotated[list[StrictId] | None, Field(description="Client ids (list_clients).")] = None,
    status_ids: Annotated[list[StrictId] | None, Field(description=CONTRACT_STATUSES)] = None,
    query: Annotated[str | None, Field(description="Name or reference text, max 200.")] = None,
    created_since: Annotated[str | None, Field(description="At or after.")] = None,
    created_before: Annotated[str | None, Field(description="At or before.")] = None,
    updated_since: Annotated[str | None, Field(description="At or after. Never changed: left out.")] = None,
    updated_before: Annotated[str | None, Field(description="At or before. Never changed: left out.")] = None,
    end_date_since: Annotated[str | None, Field(description="YYYY-MM-DD: ends on or after that day. No end date: left out.")] = None,
    end_date_before: Annotated[str | None, Field(description="YYYY-MM-DD: ends strictly before that day. No end date: left out.")] = None,
    sort_by: Annotated[
        Literal["createdOn", "updatedOn", "startDate", "endDate"] | None, Field(description="Default createdOn.")
    ] = None,
    sort_order: Annotated[Literal["asc", "desc"] | None, Field(description="Default desc.")] = None,
    page_size: Annotated[int, Field(description="Rows per page, 1-200.")] = 50,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous page.")] = None,
) -> dict:
    """List billing contracts with their service lines, one page at a time.

    Without status_ids, Active and Expired contracts are listed and Archived ones are left out: pass status_ids=[1] for live contracts only, or include 3 to see archived ones. A service line's Id is the service_line_id of create_time_entry and update_time_entry. get_contract has the line items and labor detail.
    Paging: pass next_cursor back as cursor with the SAME filters until has_more is false.
    """
    filters: dict[str, Any] = {
        "client_ids": positive_ids("client_ids", client_ids),
        "status_ids": _statuses("status_ids", status_ids),
        "query": _query(query),
        "created_since": utc_iso("created_since", created_since),
        "created_before": utc_iso("created_before", created_before),
        "updated_since": utc_iso("updated_since", updated_since),
        "updated_before": utc_iso("updated_before", updated_before),
        "end_date_since": None if end_date_since is None else _calendar_date("end_date_since", end_date_since),
        "end_date_before": None if end_date_before is None else _calendar_date("end_date_before", end_date_before),
        "sort_by": sort_by,
        "sort_order": sort_order,
    }
    non_empty("cursor", cursor)
    size = clamp_page_size(page_size)
    # the result echoes `filters` (what the next call can repeat); Gorelo gets the two end dates as midnight UTC
    wire = {**filters, **{param: _midnight(filters[param]) for param in DAY_FILTERS if filters[param] is not None}}
    params = {
        LIST_FIELDS[param]: csv_ids(param, value) if isinstance(value, list) else value for param, value in wire.items()
    }
    page = await client_of(ctx).get_page(LIST_OP, query=params, page_size=size, cursor=cursor, tool="list_contracts")
    return paged_result(page, filters)


def _query(value: Any) -> str | None:
    """The text filter: not blank, at most 200 characters (Gorelo's own limit)."""
    if value is None:
        return None
    text = _text("query", value)
    if len(text) > QUERY_MAX:
        raise ValueError(f"query: at most {QUERY_MAX} characters, got {len(text)}")
    return text


# --------------------------------------------------------------------------
# get_contract
# --------------------------------------------------------------------------


@gorelo_tool(toolset="billing", kind="read", ops=[GET_OP], field_map=ID_MAP)
async def get_contract(
    ctx: Context,
    contract_id: Annotated[StrictId, Field(description="Contract id (list_contracts).")],
) -> dict:
    """Get one contract in full: client, invoice schedule (days before creation, DaysUntilDue, auto approve and send, templates), contacts and every service line with labor detail, auto flags and line items.

    A service line's Id is the service_line_id time entries use; a line item's cost is UnitCost. An unknown id is a 404. Contracts cannot be deleted through this server.
    """
    number = positive_id("contract_id", contract_id)
    data = await client_of(ctx).get_one(GET_OP, path_params={"contractId": number}, tool="get_contract")
    return expect_object(data, GET_OP, tool="get_contract")


# --------------------------------------------------------------------------
# create_contract
# --------------------------------------------------------------------------


def _create_request(
    *,
    name: Any,
    client_id: Any,
    service_lines: Any,
    is_for_all_locations: Any = None,
    location_ids: Any = None,
    reference: Any = None,
    start_date: Any = None,
    end_date: Any = None,
    prorate_type_id: Any = None,
    auto_invoice_ticket_products: Any = None,
    repeat_period_id: Any = None,
    days_before_invoice_creation: Any = None,
    days_until_due: Any = None,
    auto_approve_and_send: Any = None,
    contact_ids: Any = None,
    invoice_template_id: Any = None,
    invoice_email_template_id: Any = None,
    branding_theme_id: Any = None,
) -> tuple[dict[str, Any], int]:
    """The body of POST /v1/contracts and the number of Block Hours lines in it. Pure: no HTTP."""
    if name is None:
        raise ValueError("name: required")
    values = _contract_values(
        {
            "name": name,
            "is_for_all_locations": is_for_all_locations,
            "location_ids": location_ids,
            "reference": reference,
            "start_date": start_date,
            "end_date": end_date,
            "prorate_type_id": prorate_type_id,
            "auto_invoice_ticket_products": auto_invoice_ticket_products,
            "repeat_period_id": repeat_period_id,
            "days_before_invoice_creation": days_before_invoice_creation,
            "days_until_due": days_until_due,
            "auto_approve_and_send": auto_approve_and_send,
            "contact_ids": contact_ids,
            "invoice_template_id": invoice_template_id,
            "invoice_email_template_id": invoice_email_template_id,
            "branding_theme_id": branding_theme_id,
        },
        creating=True,
    )
    cid = positive_id("client_id", client_id)
    lines, blocks = _new_service_lines("service_lines", service_lines)
    body = _contract_body(values, CONTRACT_FIELDS)
    body["ClientId"] = cid
    body["ServiceLines"] = lines
    return body, blocks


def _create_effect(body: Mapping[str, Any], blocks: int) -> str:
    """What a create_contract call without confirm=true says would happen (nothing is sent)."""
    lines = len(body["ServiceLines"])
    parts = [
        f"It creates a real billing contract for client {body['ClientId']} with {lines} service line{'' if lines == 1 else 's'}; "
        "contracts cannot be deleted through this server, only archived with update_contract."
    ]
    if blocks:
        parts.append(
            f"{blocks} Block Hours line{'' if blocks == 1 else 's'} raise{'s' if blocks == 1 else ''} a Draft invoice for the "
            "block at once."
        )
    if body.get("AutoApproveAndSend") is True:
        parts.append(AUTO_SEND_EFFECT)
    parts.append("Nothing has been sent to Gorelo.")
    return " ".join(parts)


def _same_text(shown: Any, wanted: str) -> bool:
    return isinstance(shown, str) and shown.strip() == wanted.strip()


def _code_id(record: Mapping[str, Any], key: str) -> Any:
    shown = record.get(key)
    return shown.get("Id") if isinstance(shown, Mapping) else None


def _create_problems(record: Mapping[str, Any], body: Mapping[str, Any]) -> list[str]:
    """What the read-back of a new contract does not show of what was sent (never quoting a value)."""
    problems = []
    if not _same_text(record.get("Name"), body["Name"]):
        problems.append("Name is not the name that was sent")
    if _code_id(record, "Client") != body["ClientId"]:
        problems.append("Client is not the client that was sent")
    shown = record.get("ServiceLines")
    sent = body["ServiceLines"]
    if not isinstance(shown, list) or len(shown) != len(sent):
        problems.append(f"ServiceLines does not hold the {len(sent)} line(s) that were sent")
        return problems
    rows = [row for row in shown if isinstance(row, Mapping)]
    if sorted(str(_code_id(row, "LaborTerms")) for row in rows) != sorted(str(line["LaborTermsId"]) for line in sent):
        problems.append("the labor terms of the service lines are not the ones that were sent")
    if sorted(str(row.get("Name")).strip() for row in rows) != sorted(line["Name"].strip() for line in sent):
        problems.append("the names of the service lines are not the ones that were sent")
    return problems


@gorelo_tool(toolset="billing", kind="destructive", ops=[CREATE_OP, GET_OP], field_map=CREATE_FIELDS)
async def create_contract(
    ctx: Context,
    name: Annotated[str, Field(description="Contract name.")],
    client_id: Annotated[StrictId, Field(description="Client to bill (list_clients). Cannot change later.")],
    service_lines: Annotated[
        list[NewServiceLine],
        Field(description="At least one line. Each: name, labor_terms_id and the ONE details object that matches it (none for 4; send {} for 1)."),
    ],
    is_for_all_locations: Annotated[
        StrictBool | None, Field(description="Cover every location of the client. Default true; false needs location_ids.")
    ] = None,
    location_ids: Annotated[
        list[StrictId] | None, Field(description="Locations covered (list_client_locations). Only with is_for_all_locations=false.")
    ] = None,
    reference: Annotated[str | None, Field(description="Free text printed on the invoices, e.g. a PO number.")] = None,
    start_date: Annotated[str | None, Field(description="YYYY-MM-DD. Default: today.")] = None,
    end_date: Annotated[str | None, Field(description="YYYY-MM-DD, after start_date. Default: open-ended.")] = None,
    prorate_type_id: Annotated[
        StrictId | None, Field(description="0 do not prorate (default), 1 prorate and add to next invoice, 2 prorate and invoice at once.")
    ] = None,
    auto_invoice_ticket_products: Annotated[
        StrictBool | None, Field(description="Sweep approved ticket products into the next recurring invoice. All locations only. Default false.")
    ] = None,
    repeat_period_id: Annotated[
        StrictId | None, Field(description="1 Monthly (default), 2 Quarterly, 3 Yearly, 4 Half Yearly.")
    ] = None,
    days_before_invoice_creation: Annotated[
        StrictId | None, Field(description="Days before the period ends that the invoice is created, 0-31. Default 0.")
    ] = None,
    days_until_due: Annotated[StrictId | None, Field(description="Days the client has to pay. Default: the provider's setting.")] = None,
    auto_approve_and_send: Annotated[
        StrictBool | None,
        Field(description="true: Gorelo approves every invoice of the contract itself, emails it to the client and pushes it to accounting. Default false."),
    ] = None,
    contact_ids: Annotated[
        list[StrictId] | None, Field(description="The client's contacts who can auto-approve the invoices (list_contacts).")
    ] = None,
    invoice_template_id: Annotated[StrictId | None, Field(description=f"Invoice template. Default: the provider's. {TEMPLATES_TEXT}")] = None,
    invoice_email_template_id: Annotated[
        StrictId | None, Field(description=f"Email template of the invoices. Default: the provider's. {TEMPLATES_TEXT}")
    ] = None,
    branding_theme_id: Annotated[str | None, Field(description="Accounting branding theme GUID. Ask the user: no tool lists them.")] = None,
    confirm: Annotated[StrictBool, Field(description=CONFIRM_TEXT)] = False,
) -> dict:
    """Create a billing contract with its service lines and return it as get_contract shows it.

    Needs name, client_id and at least one service line. Each line needs name, labor_terms_id (1 Unlimited, 2 Hourly, 3 Block, 4 No Labor Terms, 5 Limited) and the one matching details object; a setting the labor terms cannot have is refused.
    Resolve ids first: client_id -> list_clients, item_id -> list_items, tax_id -> list_taxes, work_type_ids -> list_work_types, work_role_ids -> list_billing_roles.
    Side effects: creates a real contract that cannot be deleted through this server (update_contract archives it). A Block Hours line raises a Draft invoice for the block at once. auto_approve_and_send=true makes Gorelo approve and send the recurring invoices on its own; it emails each to the client and approval pushes them to the accounting system. Tell the user.
    Ask the user first; needs confirm=true.
    """
    body, blocks = _create_request(
        name=name,
        client_id=client_id,
        service_lines=service_lines,
        is_for_all_locations=is_for_all_locations,
        location_ids=location_ids,
        reference=reference,
        start_date=start_date,
        end_date=end_date,
        prorate_type_id=prorate_type_id,
        auto_invoice_ticket_products=auto_invoice_ticket_products,
        repeat_period_id=repeat_period_id,
        days_before_invoice_creation=days_before_invoice_creation,
        days_until_due=days_until_due,
        auto_approve_and_send=auto_approve_and_send,
        contact_ids=contact_ids,
        invoice_template_id=invoice_template_id,
        invoice_email_template_id=invoice_email_template_id,
        branding_theme_id=branding_theme_id,
    )
    lines = len(body["ServiceLines"])
    require_confirm(
        confirm,
        action=f"create a contract for client {body['ClientId']} with {lines} service line{'' if lines == 1 else 's'}",
        effect=_create_effect(body, blocks),
    )
    written = await client_of(ctx).post(CREATE_OP, json_body=body, tool="create_contract")
    new_id = created_id(written, CREATE_OP, tool="create_contract")
    record = await reread_after_write(ctx, GET_OP, path_params={"contractId": new_id}, tool="create_contract", written_id=new_id)
    if set(record) == {"Id", "warning"}:  # the read-back failed: the contract exists, say so and do not repeat the write
        return record
    problems = _create_problems(record, body)
    if problems:
        raise _unconfirmed(CREATE_OP, f"the read-back does not match the request ({'; '.join(problems)})", f"get_contract, contract {new_id}")
    return record


# --------------------------------------------------------------------------
# update_contract
# --------------------------------------------------------------------------


class _LinePlan:
    """One entry of update_contract's service_lines after the checks that need no read."""

    def __init__(self, where: str, data: Mapping[str, Any], line_id: int | None) -> None:
        self.where = where
        self.position = int(where[where.index("[") + 1 : where.index("]")])  # the caller's index in service_lines
        self.data = data
        self.id = line_id
        self.items: list[tuple[int | None, dict[str, Any]]] | None = None
        self.item_removes: list[int] | None = None
        self.new_wire: dict[str, Any] = {}  # a NEW line: its checked settings
        self.terms: int | None = None  # a NEW line: its labor terms id


def _plan_lines(service_lines: Any, remove_ids: Any) -> tuple[list[_LinePlan], list[int]]:
    """Check service_lines and remove_service_line_ids without a read: shapes, repeated ids, an id both updated and
    removed, an entry that names nothing to change, and everything a NEW line needs (a new line is checked completely
    here; an existing line is checked against the contract after the read)."""
    plans: list[_LinePlan] = []
    removes = _distinct_ids("remove_service_line_ids", remove_ids) if remove_ids is not None else []
    if service_lines is not None:
        if isinstance(service_lines, (str, bytes, Mapping)) or not isinstance(service_lines, (list, tuple)):
            raise ValueError(f"service_lines: expected a list of service lines, got {describe_value(service_lines)}")
        if not service_lines:
            raise ValueError(
                "service_lines: expected at least one entry, got an empty list (omit it to keep the lines as they are; "
                "name lines to remove in remove_service_line_ids)"
            )
        seen: set[int] = set()
        for position, raw in enumerate(service_lines):
            where = f"service_lines[{position}]"
            data = _mapping(where, raw, "an object")
            _no_unknown(where, data, ("id", "remove_line_item_ids", *LINE_KEYS))
            line_id = data.get("id")
            plan = _LinePlan(where, data, None if line_id is None else positive_id(f"{where}.id", line_id))
            if plan.id is not None:
                if plan.id in seen:
                    raise ValueError(f"{where}.id: the service line appears more than once")
                seen.add(plan.id)
                if plan.id in removes:
                    raise ValueError(
                        f"{where}.id: the line is also in remove_service_line_ids; update it or remove it, not both"
                    )
                if not any(data.get(key) is not None for key in ("remove_line_item_ids", *LINE_KEYS)):
                    raise ValueError(
                        f"{where}: names only an id, which changes nothing (lines you do not name are kept); give a field "
                        "to change or leave the line out"
                    )
            else:
                if data.get("remove_line_item_ids") is not None:
                    raise ValueError(f"{where}.remove_line_item_ids: a new service line has no line items to remove")
                if data.get("labor_terms_id") is None:
                    raise ValueError(f"{where}.labor_terms_id: required on a new service line ({LABOR_TERMS_SCALE})")
                terms = _scale(f"{where}.labor_terms_id", data["labor_terms_id"], LABOR_TERMS)
                plan.new_wire = _line_wire(where, data, terms=terms, fresh=True, emit_terms=True)
                plan.terms = terms
            if data.get("line_items") is not None:
                plan.items = _line_items(f"{where}.line_items", data["line_items"], change=plan.id is not None)
            if data.get("remove_line_item_ids") is not None:
                plan.item_removes = _distinct_ids(f"{where}.remove_line_item_ids", data["remove_line_item_ids"])
                updated = {item_id for item_id, _ in plan.items or [] if item_id is not None}
                for number in plan.item_removes:
                    if number in updated:
                        raise ValueError(
                            f"{where}.remove_line_item_ids: line item {number} is also updated in line_items; update it or remove it, not both"
                        )
            plans.append(plan)
    return plans, removes


def _current_lines(record: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    """The service lines of the contract as the read shows them, by Id. Anything unreadable stops the update."""
    lines = record.get("ServiceLines")
    if not isinstance(lines, list):
        raise _unreadable(f"expected ServiceLines to be a list but got {describe_value(lines)}")
    found: dict[int, Mapping[str, Any]] = {}
    for row in lines:
        number = row.get("Id") if isinstance(row, Mapping) else None
        if not isinstance(row, Mapping) or isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise _unreadable("a service line of the contract has no usable Id")
        found[number] = row
    return found


def _current_items(line: Mapping[str, Any]) -> list[int]:
    """The ids of the line items of one service line, in the order the read shows them."""
    items = line.get("LineItems")
    if not isinstance(items, list):
        raise _unreadable(f"expected LineItems of service line {line.get('Id')} to be a list but got {describe_value(items)}")
    ids: list[int] = []
    for row in items:
        number = row.get("Id") if isinstance(row, Mapping) else None
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise _unreadable(f"a line item of service line {line.get('Id')} has no usable Id")
        ids.append(number)
    return ids


class _Expect:
    """What the read after an update must show (ids and counts of the lines and items, no field values)."""

    def __init__(self) -> None:
        self.kept: set[int] = set()
        self.removed: set[int] = set()
        self.added_names: list[str] = []
        self.lines: dict[int, dict[str, Any]] = {}  # line id -> checks: name, terms, flags
        self.items: dict[int, tuple[set[int], set[int], int]] = {}  # line id -> (kept, removed, added)
        # what the first read showed and what was sent, for the field by field comparison of the read-back
        self.before: dict[int, Mapping[str, Any]] = {}  # kept line id -> the line as the first read showed it
        self.sent: dict[int, Mapping[str, Any]] = {}  # updated line id -> the entry that was sent (without its Id)
        self.fresh: list[Mapping[str, Any]] = []  # the NEW lines that were sent, in order
        # where each entry of the ServiceLines that was sent came from, to name the caller's entry in a Gorelo error:
        # (kind, number, line id): ("entry", position in service_lines, the line's id or None for a new line) or ("kept",
        # line id, line id); item origins by line id: ("entry", index in line_items) or ("kept", line item id), in the
        # order of the LineItems that were sent
        self.origins: list[tuple[str, int, int | None]] = []
        self.item_origins: dict[int, list[tuple[str, int]]] = {}


def _build_lines(
    plans: list[_LinePlan], removes: list[int], record: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], _Expect]:
    """The complete ServiceLines to send: every current line, as {"Id": n} alone unless updated or removed, then the new
    lines. Raises ValueError for anything the read shows is wrong, before any PATCH."""
    current = _current_lines(record)
    for index, number in enumerate(removes):
        if number not in current:
            raise ValueError(f"remove_service_line_ids[{index}]: the contract has no service line with that id (get_contract lists them)")
    updates = {plan.id: plan for plan in plans if plan.id is not None}
    for plan in updates.values():
        if plan.id not in current:
            raise ValueError(f"{plan.where}.id: the contract has no service line with that id (get_contract lists them)")
    expect = _Expect()
    wire: list[dict[str, Any]] = []
    for number, line in current.items():
        if number in removes:
            expect.removed.add(number)
            continue
        expect.kept.add(number)
        expect.before[number] = line
        plan = updates.get(number)
        if plan is None:
            wire.append({"Id": number})
            expect.origins.append(("kept", number, number))
            continue
        entry: dict[str, Any] = {"Id": number}
        entry.update(_existing_line(plan, line, expect))
        expect.sent[number] = {key: value for key, value in entry.items() if key != "Id"}
        expect.origins.append(("entry", plan.position, number))
        wire.append(entry)
    for plan in plans:
        if plan.id is None:
            entry = dict(plan.new_wire)
            if plan.items is not None:
                entry["LineItems"] = [item for _, item in plan.items]
            expect.added_names.append(entry["Name"])
            expect.fresh.append(entry)
            expect.origins.append(("entry", plan.position, None))
            wire.append(entry)
    if not wire:
        raise ValueError(
            "remove_service_line_ids: this would remove every service line; a contract keeps at least one (add a line in the "
            "same call, or leave one in place)"
        )
    return wire, expect


def _existing_line(plan: _LinePlan, line: Mapping[str, Any], expect: _Expect) -> dict[str, Any]:
    """The change to an existing service line (without its Id), checked against the line as the read shows it."""
    where, data = plan.where, plan.data
    shown = _code_id(line, "LaborTerms")
    if isinstance(shown, bool) or shown not in LABOR_TERMS:
        raise _unreadable(f"service line {plan.id} shows no usable LaborTerms")
    terms, changing = shown, False
    given = data.get("labor_terms_id")
    if given is not None:
        given = _scale(f"{where}.labor_terms_id", given, LABOR_TERMS)
        if given != shown:
            if shown != NO_LABOR_TERMS:
                raise ValueError(
                    f"{where}.labor_terms_id: the line is {LABOR_TERMS[shown]}; only a No Labor Terms line can change its "
                    "labor terms (once)"
                )
            terms, changing = given, True
    entry = _line_wire(where, data, terms=terms, fresh=changing, emit_terms=changing, current=line, need_name=False)
    checks: dict[str, Any] = {}
    if "Name" in entry:
        checks["name"] = entry["Name"]
    if changing:
        checks["terms"] = terms
    for key, field in (("auto_approve_time", "AutoApproveTime"), ("auto_invoice_time", "AutoInvoiceTime")):
        if field in entry:
            checks[key] = entry[field]
    if "IncludedTimeGoesToId" in entry:
        checks["included"] = entry["IncludedTimeGoesToId"]
    expect.lines[plan.id or 0] = checks
    if plan.items is not None or plan.item_removes is not None:
        have = _current_items(line)
        removes = plan.item_removes or []
        for index, number in enumerate(removes):
            if number not in have:
                raise ValueError(f"{where}.remove_line_item_ids[{index}]: the service line has no line item with that id (get_contract lists them)")
        updated = {item_id: item for item_id, item in (plan.items or []) if item_id is not None}
        for number in updated:
            if number not in have:
                raise ValueError(f"{where}.line_items: the service line has no line item with id {number} (get_contract lists them)")
        items: list[dict[str, Any]] = []
        origins: list[tuple[str, int]] = []
        entries = plan.items or []
        for number in have:
            if number in removes:
                continue
            items.append({"Id": number, **updated[number]} if number in updated else {"Id": number})
            origins.append(
                ("entry", next(index for index, (item_id, _) in enumerate(entries) if item_id == number))
                if number in updated
                else ("kept", number)
            )
        added = [item for item_id, item in entries if item_id is None]
        items.extend(added)
        origins.extend(("entry", index) for index, (item_id, _) in enumerate(entries) if item_id is None)
        expect.item_origins[plan.id or 0] = origins
        entry["LineItems"] = items
        expect.items[plan.id or 0] = ({n for n in have if n not in removes}, set(removes), len(added))
    return entry


# --------------------------------------------------------------------------
# The read-back, field by field (what the first read showed, overlaid with what was sent)
# --------------------------------------------------------------------------

# Rounding margins: Gorelo rounds prices, costs and hours to two decimals, quantities to four and percents to two. A small
# margin so a rounding never raises a false "not confirmed".
_TOLERANCE = {
    "Quantity": 0.0001,
    "UnitPrice": 0.005,
    "UnitCost": 0.005,
    "DiscountPercent": 0.01,
    "Balance": 0.005,
    "WarningThreshold": 0.5,
    "OverrunThreshold": 0.5,
    "HourlyRate": 0.005,
    "AllocatedHours": 0.005,
}
_TEXT_KEYS = ("Description",)
_DATE_KEYS = ("StartDate", "EndDate")
_CODE_KEYS = ("LaborTerms", "IncludedTimeGoesTo", "RateType", "Tax", "BillableStatus")
# line item: (field of ContractLineItemModel, field of ContractLineItemInput that sends it)
_ITEM_FIELDS = (
    ("Quantity", "Quantity"),
    ("UnitPrice", "UnitPrice"),
    ("UnitCost", "UnitCost"),
    ("DiscountPercent", "DiscountPercent"),
    ("Description", "Description"),
    ("StartDate", "StartDate"),
    ("EndDate", "EndDate"),
    ("Tax", "TaxId"),
    ("BillableStatus", "BillableStatusId"),
)
# the details objects of a service line, with the fields of the Model that the Input sends under the same name
_DETAIL_OBJECTS = ("UnlimitedHoursDetails", "PerHourDetails", "BlockHoursDetails", "LimitedHoursDetails")
_DETAIL_SAME = ("Balance", "WarningThreshold", "OverrunThreshold", "ShowRemainingHoursInClientPortal", "TemplateId", "AllocatedHours", "HourlyRate")


def _quoted(value: Any) -> str:
    """A number, a flag or None as it is; anything else only by its type (never the value)."""
    return "none" if value is None else str(value) if _is_number(value) or isinstance(value, bool) else describe_value(value)


def _compare(label: str, expected: Mapping[str, Any], shown: Mapping[str, Any]) -> list[str]:
    """Each field of `expected` (in the shape of the read) that `shown` does not show: "<label>: <field> expected <x>, read <y>".
    Numbers are compared within a rounding margin, dates by their day, a code object by its Id; text is not quoted."""
    found: list[str] = []
    for key, want in expected.items():
        got = shown.get(key)
        if key in _CODE_KEYS:
            got = got.get("Id") if isinstance(got, Mapping) else None
        elif key in _DATE_KEYS:
            want = want[:10] if isinstance(want, str) else want
            got = got[:10] if isinstance(got, str) else got
        if key in _TEXT_KEYS:
            if (want.strip() if isinstance(want, str) else "") != (got.strip() if isinstance(got, str) else ""):
                found.append(f"{label}: {key} is not the text it should be (text not shown)")
            continue
        if key in _TOLERANCE and want is not None and got is not None:
            same = _is_number(want) and _is_number(got) and abs(got - want) <= _TOLERANCE[key]
        else:
            same = want == got and isinstance(want, bool) == isinstance(got, bool)
        if not same:
            if key in _DATE_KEYS:  # a date is not private
                found.append(f"{label}: {key} expected {'none' if want is None else want}, read {'none' if got is None else got}")
            else:
                found.append(f"{label}: {key} expected {_quoted(want)}, read {_quoted(got)}")
    return found


def _detail_expectation(name: str, before: Any, sent: Mapping[str, Any] | None) -> dict[str, Any]:
    """What one details object must show: the leaves the first read had, overlaid with the leaves that were sent. Price is not
    on the model; CustomRates are compared only when neither they nor the rate type were sent (Gorelo may clear them when the
    rate type moves away from Custom), and HourlyRate follows the rate type (the model sets it only for a fixed rate)."""
    expected: dict[str, Any] = {}
    if isinstance(before, Mapping):
        for key in _DETAIL_SAME:
            if key in before:
                expected[key] = before[key]
        if isinstance(before.get("RateType"), Mapping):
            expected["RateType"] = before["RateType"].get("Id")
    if sent:
        for key in _DETAIL_SAME:
            if key in sent:
                expected[key] = sent[key]
        if "RateTypeId" in sent:
            expected["RateType"] = sent["RateTypeId"]
            if sent["RateTypeId"] != RATE_TYPE_FIXED and "HourlyRate" not in sent:
                expected["HourlyRate"] = None
    return expected


def _detail_problems(label: str, before_row: Mapping[str, Any] | None, sent: Mapping[str, Any], shown: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    for name in _DETAIL_OBJECTS:
        first = before_row.get(name) if before_row is not None else None
        given = sent.get(name)
        expected = _detail_expectation(name, first, given if isinstance(given, Mapping) else None)
        if not expected:
            continue
        read = shown.get(name)
        if not isinstance(read, Mapping):
            found.append(f"{label}: {name} expected an object, read {describe_value(read)}")
            continue
        found.extend(_compare(f"{label}: {name}", expected, read))
        if (
            isinstance(first, Mapping)
            and "CustomRates" in first
            and "CustomRates" in read
            and not isinstance(given, Mapping)
            and first["CustomRates"] != read["CustomRates"]
        ):
            found.append(f"{label}: {name} CustomRates are not the ones the contract had")
    return found


def _ids_of(rows: Any) -> list[int] | None:
    if not isinstance(rows, list):
        return None
    return sorted(row["Id"] for row in rows if isinstance(row, Mapping) and isinstance(row.get("Id"), int) and not isinstance(row.get("Id"), bool))


def _item_expectation(before: Mapping[str, Any] | None, sent: Mapping[str, Any] | None) -> dict[str, Any]:
    """What one line item must show: the fields the first read had, overlaid with those that were sent. A TaxId or a
    BillableStatusId is compared by id."""
    expected: dict[str, Any] = {}
    for model, field in _ITEM_FIELDS:
        if before is not None and model in before:
            value = before[model]
            expected[model] = value.get("Id") if model in _CODE_KEYS and isinstance(value, Mapping) else value
        if sent is not None and field in sent:
            expected[model] = sent[field]
    return expected


def _item_problems(label: str, before_items: Any, sent_items: Any, shown_items: Any) -> list[str]:
    """The line items of one service line: the ones the first read had (kept or updated by id) and the new ones."""
    if not isinstance(shown_items, list):
        return []
    shown = {row["Id"]: row for row in shown_items if isinstance(row, Mapping) and isinstance(row.get("Id"), int)}
    first = {row["Id"]: row for row in before_items if isinstance(row, Mapping) and isinstance(row.get("Id"), int)} if isinstance(before_items, list) else {}
    found: list[str] = []
    if sent_items is None:  # no LineItems were sent: the line keeps its items as they are
        for number, row in first.items():
            if number not in shown:
                found.append(f"{label}: line item {number} is missing")
            else:
                found.extend(_compare(f"{label}: line item {number}", _item_expectation(row, None), shown[number]))
        return found
    fresh = [row for number, row in shown.items() if number not in first]
    new_sent = [item for item in sent_items if "Id" not in item]
    for item in sent_items:
        number = item.get("Id")
        if number is not None and number in shown:
            found.extend(_compare(f"{label}: line item {number}", _item_expectation(first.get(number), item), shown[number]))
    if len(fresh) == len(new_sent):
        used: set[int] = set()
        for position, item in enumerate(new_sent, start=1):
            wanted = str(item.get("ItemId") or "").lower()
            index = next(
                (i for i, row in enumerate(fresh) if i not in used and wanted and str(row.get("ItemId") or "").lower() == wanted),
                next((i for i in range(len(fresh)) if i not in used), None),
            )
            if index is not None:
                used.add(index)
                found.extend(_compare(f"{label}: new line item {position}", _item_expectation(None, item), fresh[index]))
    return found


def _field_problems(rows: Mapping[int, Mapping[str, Any]], expect: _Expect) -> list[str]:
    """Every kept line against the first read and every value that was sent: the labor terms and their details object, the auto
    flags, where included time goes, the work types and roles, and each line item (quantity, prices, discount, tax, billable
    status, description, dates). The Name and the other values that update_contract names itself are checked by the caller."""
    found: list[str] = []
    for number in sorted(expect.kept):
        row = rows.get(number)
        before = expect.before.get(number)
        if row is None or before is None:
            continue
        label = f"service line {number}"
        sent = expect.sent.get(number, {})
        # a No Labor Terms line that takes labor terms gets Gorelo's defaults for what the call does not send (where included
        # time goes, the work types and roles it covers), so those are compared only on a line whose labor terms stay
        converting = "LaborTermsId" in sent
        expected: dict[str, Any] = {}
        for model, field in (
            ("LaborTerms", "LaborTermsId"),
            ("IncludedTimeGoesTo", "IncludedTimeGoesToId"),
            ("AutoApproveTime", "AutoApproveTime"),
            ("AutoInvoiceTime", "AutoInvoiceTime"),
        ):
            if field in sent or (converting and model == "IncludedTimeGoesTo"):
                continue  # a value that was sent is checked by _line_problems, in its own words
            if model in before:
                expected[model] = _code_id(before, model) if model in _CODE_KEYS else before[model]
        found.extend(_compare(label, expected, row))
        found.extend(_detail_problems(label, before, sent, row))
        for model, field in (("WorkTypes", "WorkTypeIds"), ("WorkRoles", "WorkRoleIds")):
            if converting and field not in sent:
                continue
            want = sorted(sent[field]) if field in sent else _ids_of(before.get(model))
            got = _ids_of(row.get(model))
            if want is not None and got is not None and want != got:
                found.append(f"{label}: {model} expected {want}, read {got}")
        found.extend(_item_problems(label, before.get("LineItems"), sent.get("LineItems"), row.get("LineItems")))
    fresh = [row for number, row in rows.items() if number not in expect.kept]
    if len(fresh) == len(expect.fresh):
        taken: set[int] = set()
        for wire in expect.fresh:
            index = next(
                (i for i, row in enumerate(fresh) if i not in taken and _same_text(row.get("Name"), wire.get("Name", ""))), None
            )
            if index is None:
                continue
            taken.add(index)
            row = fresh[index]
            label = f"new service line {len(taken)}"
            found.extend(_detail_problems(label, None, wire, row))
            found.extend(_item_problems(label, None, wire.get("LineItems"), row.get("LineItems")) if "LineItems" in wire else [])
    return found


# --------------------------------------------------------------------------
# A Gorelo error that names a position of the list the tool built
# --------------------------------------------------------------------------

_WIRE_LINE = re.compile(r"ServiceLines\[(\d+)\](.*)", re.DOTALL)
_WIRE_ITEM = re.compile(r"\.LineItems\[(\d+)\](.*)", re.DOTALL)


def _dots(path: str) -> str:
    """".PerHourDetails.HourlyRate" as "PerHourDetails, HourlyRate"."""
    return path.lstrip(".").replace(".", ", ")


def _retarget(err: GoreloAPIError, expect: _Expect | None) -> None:
    """Rewrite the PropertyName of every Notification that points into the ServiceLines the tool SENT so that it names the
    caller's own entry. Gorelo counts the complete list the tool built (every current line first, then the new ones), which is
    not the order of the caller's service_lines; the same holds for the LineItems of a line. A line the caller did not name is
    called "kept service line Id N", a line item the caller did not name "kept line item Id N". Anything else keeps Gorelo's
    own name (and so does every error of a call that sent no ServiceLines)."""
    if expect is None:
        return
    for note in err.notifications:
        line = _WIRE_LINE.fullmatch(str(note.get("property") or ""))
        if line is None or int(line.group(1)) >= len(expect.origins):
            continue
        kind, which, line_id = expect.origins[int(line.group(1))]
        tail = line.group(2)
        if kind == "kept":
            note["property"] = f"kept service line Id {which}" + (f" ({_dots(tail)})" if tail else "")
            continue
        item = _WIRE_ITEM.fullmatch(tail)
        if item is None:
            note["property"] = f"ServiceLines[{which}]{tail}"
            continue
        origins = expect.item_origins.get(line_id) if line_id is not None else None
        index = int(item.group(1))
        if origins is None:  # a new line: its items are in the order the caller gave them
            note["property"] = f"ServiceLines[{which}].LineItems[{index}]{item.group(2)}"
        elif index < len(origins) and origins[index][0] == "entry":
            note["property"] = f"ServiceLines[{which}].LineItems[{origins[index][1]}]{item.group(2)}"
        elif index < len(origins):
            rest = _dots(item.group(2))
            note["property"] = f"service_lines (item {which + 1}, kept line item Id {origins[index][1]}{', ' + rest if rest else ''})"


def _update_problems(record: Mapping[str, Any], wire: Mapping[str, Any], expect: _Expect | None, cleared: list[str]) -> list[str]:
    """What the read after an update does not show of what was sent (never quoting a value)."""
    problems: list[str] = []

    def same(field: str, shown: Any) -> None:
        if field in wire and wire[field] != shown:
            problems.append(f"{field} is not the value that was sent")

    if "Name" in wire and not _same_text(record.get("Name"), wire["Name"]):
        problems.append("Name is not the name that was sent")
    for field in ("DaysBeforeInvoiceCreation", "DaysUntilDue", "AutoApproveAndSend", "AutoInvoiceTicketProducts", "IsForAllLocations"):
        same(field, record.get(field))
    for field, key in (("RepeatPeriodId", "RepeatPeriod"), ("ProrateTypeId", "ProrateType")):
        same(field, _code_id(record, key))
    for field in ("Reference", "InvoiceTemplateId", "InvoiceEmailTemplateId"):
        if field not in wire:
            continue
        if wire[field] is None:
            if record.get(field) is not None:
                problems.append(f"{field} is still set")
        else:
            same(field, record.get(field))
    if "BrandingThemeId" in wire:
        shown = record.get("BrandingThemeId")
        if wire["BrandingThemeId"] is None:
            if shown is not None:
                problems.append("BrandingThemeId is still set")
        elif not isinstance(shown, str) or shown.lower() != wire["BrandingThemeId"].lower():
            problems.append("BrandingThemeId is not the theme that was sent")
    for field in ("StartDate", "EndDate"):
        if field in wire:
            shown = record.get(field)
            if wire[field] is None:
                if shown is not None:
                    problems.append(f"{field} is still set")
            elif not isinstance(shown, str) or shown[:10] != wire[field][:10]:
                problems.append(f"{field} is not the date that was sent")
    if "StatusId" in wire:
        shown = _code_id(record, "Status")
        # the status is derived: a restored contract reads Active, or Expired when its end date is behind it
        if shown not in ((3,) if wire["StatusId"] == 3 else (1, 2)):
            problems.append("Status is not the one that was asked for")
    if "ContactIds" in wire:
        contacts = record.get("Contacts")
        shown_ids = sorted(row.get("Id") for row in contacts if isinstance(row, Mapping)) if isinstance(contacts, list) else None
        if shown_ids != sorted(wire["ContactIds"]):
            problems.append("Contacts are not the ones that were sent")
    if "LocationIds" in wire:
        shown_ids = record.get("LocationIds")
        if not isinstance(shown_ids, list) or sorted(shown_ids) != sorted(wire["LocationIds"]):
            problems.append("LocationIds are not the ones that were sent")
    if expect is not None:
        problems.extend(_line_problems(record, expect))
    return problems


def _line_problems(record: Mapping[str, Any], expect: _Expect) -> list[str]:
    shown = record.get("ServiceLines")
    if not isinstance(shown, list):
        return ["ServiceLines is missing from the answer"]
    rows = {row["Id"]: row for row in shown if isinstance(row, Mapping) and isinstance(row.get("Id"), int)}
    problems = []
    if not expect.kept <= set(rows):
        problems.append("a service line that should stay is missing")
    if expect.removed & set(rows):
        problems.append("a service line that was removed is still there")
    fresh = [row for number, row in rows.items() if number not in expect.kept]
    if len(fresh) != len(expect.added_names):
        problems.append(f"the contract shows {len(fresh)} new service line(s), {len(expect.added_names)} were added")
    elif sorted(str(row.get("Name")).strip() for row in fresh) != sorted(name.strip() for name in expect.added_names):
        problems.append("the names of the added service lines are not the ones that were sent")
    for number, checks in expect.lines.items():
        row = rows.get(number)
        if row is None:
            continue
        if "name" in checks and not _same_text(row.get("Name"), checks["name"]):
            problems.append(f"service line {number}: Name is not the name that was sent")
        if "terms" in checks and _code_id(row, "LaborTerms") != checks["terms"]:
            problems.append(f"service line {number}: LaborTerms is not the one that was sent")
        for key, field in (("auto_approve_time", "AutoApproveTime"), ("auto_invoice_time", "AutoInvoiceTime")):
            if key in checks and row.get(field) is not checks[key]:
                problems.append(f"service line {number}: {field} is not the value that was sent")
        if "included" in checks and _code_id(row, "IncludedTimeGoesTo") != checks["included"]:
            problems.append(f"service line {number}: IncludedTimeGoesTo is not the one that was sent")
    for number, (kept, removed, added) in expect.items.items():
        row = rows.get(number)
        items = row.get("LineItems") if row is not None else None
        if not isinstance(items, list):
            if row is not None:
                problems.append(f"service line {number}: LineItems is missing from the answer")
            continue
        after = {item["Id"] for item in items if isinstance(item, Mapping) and isinstance(item.get("Id"), int)}
        if not kept <= after:
            problems.append(f"service line {number}: a line item that should stay is missing")
        if removed & after:
            problems.append(f"service line {number}: a line item that was removed is still there")
        if len(after - kept) != added:
            problems.append(f"service line {number}: the line items added are not the number that was sent")
    problems.extend(_field_problems(rows, expect))
    return problems


def _block_lines(plans: list[_LinePlan]) -> list[_LinePlan]:
    """The lines of the call that are new Block Hours lines or are set to Block Hours (labor_terms_id 3). A line that is
    already Block Hours and names labor_terms_id 3 again is counted too: the read that tells them apart comes after the
    confirmation, so the text says "new or converted"."""
    return [
        plan
        for plan in plans
        if (plan.id is None and plan.terms == 3) or (plan.id is not None and plan.data.get("labor_terms_id") == 3)
    ]


def _block_price(plan: _LinePlan) -> str:
    """What the Draft invoice of one Block Hours line bills, as far as the call says: the custom price, else the template's."""
    details = _plain(plan.data.get("block_hours_details"))
    price = details.get("price") if isinstance(details, Mapping) else None
    template = details.get("template_id") if isinstance(details, Mapping) else None
    if template in (None, BLOCK_CUSTOM) and _is_number(price):
        return f"{plan.where}: price {price:g}"
    return f"{plan.where}: the price of Block Hours Template {template}" if template else f"{plan.where}: its price"


def _id_list(numbers: list[int]) -> str:
    return ", ".join(str(number) for number in numbers)


def _update_effect(
    *,
    status: str | None,
    plans: list[_LinePlan],
    removes: list[int],
    auto_approve_and_send: Any,
    clear: list[str],
    blocks: int,
) -> str:
    """What an update_contract call without confirm=true says would happen (nothing is sent)."""
    parts = []
    if status == "archived":
        parts.append("The contract is archived (StatusId 3); contracts cannot be deleted through this server.")
    elif status == "active":
        parts.append("The contract is restored (StatusId 1).")
    if plans or removes:
        changed = len([plan for plan in plans if plan.id is not None])
        added = len([plan for plan in plans if plan.id is None])
        said = []
        if changed:
            said.append(f"{changed} service line{'' if changed == 1 else 's'} updated")
        if added:
            said.append(f"{added} added")
        if removes:
            said.append(f"{len(removes)} removed ({_id_list(removes)}; Gorelo refuses a line with un-invoiced time)")
        parts.append(
            f"Gorelo replaces the contract's service lines: {', '.join(said)}; the tool sends every other current line unchanged."
        )
    items = []
    for plan in plans:
        if plan.id is None:
            continue
        if plan.item_removes:
            many = len(plan.item_removes) != 1
            items.append(f"service line {plan.id}: line item{'s' if many else ''} {_id_list(plan.item_removes)} removed")
        if plan.items:
            updated = len([1 for item_id, _ in plan.items if item_id is not None])
            fresh = len(plan.items) - updated
            said_items = [text for text in (f"{updated} updated" if updated else "", f"{fresh} added" if fresh else "") if text]
            items.append(f"service line {plan.id}: line items {', '.join(said_items)}")
    if items:
        parts.append(f"Line items replace each line's set: {'; '.join(items)}; the tool keeps every other current line item.")
    if blocks:
        prices = "; ".join(_block_price(plan) for plan in _block_lines(plans))
        parts.append(
            f"{blocks} Block Hours line{'' if blocks == 1 else 's'} (labor_terms_id 3, new or converted from No Labor "
            f"Terms): each new or converted block raises a Draft invoice for the block at once ({prices})."
        )
    if auto_approve_and_send is True:
        parts.append(AUTO_SEND_EFFECT)
    if clear:
        parts.append(f"Cleared: {', '.join(clear)}.")
    parts.append("Nothing has been sent to Gorelo.")
    return " ".join(parts)


@gorelo_tool(toolset="billing", kind="destructive", ops=[GET_OP, UPDATE_OP], field_map=UPDATE_FIELDS)
async def update_contract(
    ctx: Context,
    contract_id: Annotated[StrictId, Field(description="Contract id (list_contracts).")],
    name: Annotated[str | None, Field(description="New contract name.")] = None,
    status: Annotated[
        Literal["active", "archived"] | None, Field(description="archived archives the contract, active restores it.")
    ] = None,
    reference: Annotated[str | None, Field(description="Free text on the invoices. To clear it use clear_fields.")] = None,
    start_date: Annotated[str | None, Field(description="YYYY-MM-DD. Refused (409) once an invoice was generated.")] = None,
    end_date: Annotated[str | None, Field(description="YYYY-MM-DD. To make the contract open-ended use clear_fields.")] = None,
    repeat_period_id: Annotated[
        StrictId | None, Field(description="1 Monthly, 2 Quarterly, 3 Yearly, 4 Half Yearly. Refused (409) once an invoice was generated.")
    ] = None,
    days_before_invoice_creation: Annotated[
        StrictId | None, Field(description="Days before the period ends that the invoice is created, 0-31.")
    ] = None,
    days_until_due: Annotated[StrictId | None, Field(description="Days the client has to pay.")] = None,
    auto_approve_and_send: Annotated[
        StrictBool | None, Field(description="true: Gorelo approves every invoice of the contract itself, emails it to the client and pushes it to accounting.")
    ] = None,
    contact_ids: Annotated[
        list[StrictId] | None, Field(description="Auto-approve contacts, REPLACING the set. To remove them all use clear_fields.")
    ] = None,
    is_for_all_locations: Annotated[StrictBool | None, Field(description="false needs location_ids (sent or already stored).")] = None,
    location_ids: Annotated[
        list[StrictId] | None, Field(description="Locations covered, replacing the set (list_client_locations). Use with is_for_all_locations=false.")
    ] = None,
    prorate_type_id: Annotated[StrictId | None, Field(description="0 do not prorate, 1 prorate and add to next invoice, 2 prorate and invoice at once.")] = None,
    auto_invoice_ticket_products: Annotated[
        StrictBool | None, Field(description="Sweep approved ticket products into the next recurring invoice. All locations only.")
    ] = None,
    invoice_template_id: Annotated[StrictId | None, Field(description=f"Invoice template. To clear it use clear_fields. {TEMPLATES_TEXT}")] = None,
    invoice_email_template_id: Annotated[
        StrictId | None, Field(description=f"Email template of the invoices. To clear it use clear_fields. {TEMPLATES_TEXT}")
    ] = None,
    branding_theme_id: Annotated[str | None, Field(description="Accounting branding theme GUID. To clear it use clear_fields.")] = None,
    service_lines: Annotated[
        list[ServiceLineChange] | None,
        Field(description="Lines to update (with id) or add (without). Lines you leave out stay unless named in remove_service_line_ids."),
    ] = None,
    remove_service_line_ids: Annotated[
        list[StrictId] | None,
        Field(description="Ids of current service lines to remove (get_contract). Gorelo refuses a line with un-invoiced time."),
    ] = None,
    clear_fields: Annotated[
        list[ContractClearable] | None,
        Field(description="Fields to clear. contact_ids removes every auto-approve contact."),
    ] = None,
    confirm: Annotated[StrictBool, Field(description=CONFIRM_TEXT)] = False,
) -> dict:
    """Change a contract and return it as get_contract shows it. Send only what changes. Archive with status=archived (contracts cannot be deleted through this server), restore with status=active.
    The tool reads the contract first: Gorelo REPLACES service lines and line items, so it keeps every line and line item you do not name and removes only those in remove_service_line_ids and remove_line_item_ids.
    Resolve ids first: contract_id, line and line item ids -> list_contracts, get_contract.
    Side effects: overwrites the fields given and can remove service lines (refused with un-invoiced time). A new or converted Block Hours line raises a Draft invoice for the block at once. auto_approve_and_send=true makes Gorelo approve and send the recurring invoices on its own; it emails each to the client and approval pushes them to the accounting system. Tell the user.
    Ask the user first; needs confirm=true.
    """
    number = positive_id("contract_id", contract_id)
    clear = _clear_names(clear_fields)
    stated = status
    if status is not None and status not in STATUS_WRITES:
        raise ValueError(f"status: expected 'active' or 'archived', got {describe_value(status)}")
    values = _contract_values(
        {
            "name": name,
            "reference": reference,
            "start_date": start_date,
            "end_date": end_date,
            "repeat_period_id": repeat_period_id,
            "days_before_invoice_creation": days_before_invoice_creation,
            "days_until_due": days_until_due,
            "auto_approve_and_send": auto_approve_and_send,
            "contact_ids": contact_ids,
            "is_for_all_locations": is_for_all_locations,
            "location_ids": location_ids,
            "prorate_type_id": prorate_type_id,
            "auto_invoice_ticket_products": auto_invoice_ticket_products,
            "invoice_template_id": invoice_template_id,
            "invoice_email_template_id": invoice_email_template_id,
            "branding_theme_id": branding_theme_id,
        },
        creating=False,
    )
    for param in clear:
        if param in values:
            raise ValueError(f"{param}: cannot be given a value and cleared in the same call")
    plans, removes = _plan_lines(service_lines, remove_service_line_ids)
    wire = _contract_body(values, UPDATE_FIELDS, clear=clear)
    if stated is not None:
        wire["StatusId"] = STATUS_WRITES[stated]
    touches_lines = bool(plans or removes)
    if not wire and not touches_lines:
        raise ValueError(
            "nothing to change: give at least one of name, status, reference, start_date, end_date, repeat_period_id, "
            "days_before_invoice_creation, days_until_due, auto_approve_and_send, contact_ids, is_for_all_locations, "
            "location_ids, prorate_type_id, auto_invoice_ticket_products, invoice_template_id, invoice_email_template_id, "
            "branding_theme_id, service_lines, remove_service_line_ids or clear_fields"
        )
    blocks = len(_block_lines(plans))
    require_confirm(
        confirm,
        action=f"update contract {number}",
        effect=_update_effect(
            status=stated,
            plans=plans,
            removes=removes,
            auto_approve_and_send=auto_approve_and_send,
            clear=clear,
            blocks=blocks,
        ),
    )
    client = client_of(ctx)
    needs_read = touches_lines or (location_ids is not None and is_for_all_locations is None)
    expect: _Expect | None = None
    if needs_read:
        before = await client.get_one(GET_OP, path_params={"contractId": number}, tool="update_contract")
        record = expect_object(before, GET_OP, tool="update_contract")
        if location_ids is not None and is_for_all_locations is None and record.get("IsForAllLocations") is True:
            raise ValueError(
                "location_ids: the contract covers all locations, so Gorelo would ignore the list; pass "
                "is_for_all_locations=false with it to scope the contract. Nothing was changed."
            )
        if touches_lines:
            lines, expect = _build_lines(plans, removes, record)
            wire["ServiceLines"] = lines
    try:
        answer = await client.patch(UPDATE_OP, path_params={"contractId": number}, json_body=wire, tool="update_contract")
    except GoreloAPIError as err:
        _retarget(err, expect)  # an error index counts the list the tool built, not the caller's service_lines
        raise
    expect_object(answer, UPDATE_OP, tool="update_contract")
    shown = await reread_after_write(ctx, GET_OP, path_params={"contractId": number}, tool="update_contract", written_id=number)
    if set(shown) == {"Id", "warning"}:  # the read-back failed: the update was applied, say so and do not repeat it
        return shown
    problems = _update_problems(shown, wire, expect, clear)
    if problems:
        raise _unconfirmed(UPDATE_OP, f"the read-back does not show the change ({'; '.join(problems)})", f"get_contract, contract {number}")
    return shown


def _clear_names(clear_fields: Any) -> list[str]:
    """The distinct names in clear_fields, each one update_contract can clear. [] is an error."""
    if clear_fields is None:
        return []
    non_empty("clear_fields", clear_fields)
    if isinstance(clear_fields, (str, bytes, Mapping)) or not isinstance(clear_fields, (list, tuple)):
        raise ValueError(f"clear_fields: expected a list such as ['end_date'], got {describe_value(clear_fields)}")
    for index, name in enumerate(clear_fields):
        if not isinstance(name, str):
            raise ValueError(f"clear_fields[{index}]: expected a field name such as 'end_date', got {describe_value(name)}")
    names = list(dict.fromkeys(clear_fields))
    for name in names:
        if name not in CONTRACT_CLEAR_VALUES:
            raise ValueError(f"clear_fields: {name!r} cannot be cleared; allowed: {', '.join(CONTRACT_CLEAR_VALUES)}")
    return names
